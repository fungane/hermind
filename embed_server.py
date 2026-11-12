"""Embedding worker for hermind (runs in its OWN virtualenv with onnxruntime + numpy + tokenizers, so the
Hermes environment stays untouched). Speaks one JSON object per line on stdin/stdout, exits after --idle seconds
without a request (the ~450 MB model is then gone from RAM) and is started again on demand by vec.py.

ops: {"op":"embed","prefix":"passage: ","texts":[...]}            -> {"ok":true,"dim":384,"vecs":"<base64 float32 n*dim>"}
     {"op":"search","query":"...","k":30,"prefix":"query: "}     -> {"ok":true,"hits":[[note_id, cosine], ...]}
     {"op":"ping"}                                                -> {"ok":true}
The search op scores every vector in the database's `vectors` table (reloaded when its row count/max id changes).
"""
import argparse
import base64
import json
import os
import select
import sqlite3
import sys

# onnxruntime's official builds send usage telemetry to Microsoft by default (1DS, Linux and macOS included) and keep a
# device id for it. Hermind promises that nothing leaves the machine, so it is switched off before onnxruntime loads.
os.environ["ORT_DISABLE_TELEMETRY"] = "1"

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

import dbdriver     # same folder; opens the database with the key from HERMIND_DB_KEY_FILE / HERMIND_DB_KEY when set

ap = argparse.ArgumentParser()
ap.add_argument("--model", required=True)
ap.add_argument("--tokenizer", required=True)
ap.add_argument("--db", required=True)
ap.add_argument("--model-name", default="e5")
ap.add_argument("--idle", type=float, default=300.0)
ap.add_argument("--threads", type=int, default=2)
ap.add_argument("--pooling", choices=("mean", "cls"), default="mean")   # only used when the graph does not pool by itself
a = ap.parse_args()

so = ort.SessionOptions()
so.intra_op_num_threads = a.threads
so.enable_cpu_mem_arena = False
sess = ort.InferenceSession(a.model, so, providers=["CPUExecutionProvider"])
tok = Tokenizer.from_file(a.tokenizer)
tok.enable_truncation(256)
tok.enable_padding()
names = {i.name for i in sess.get_inputs()}
onames = [o.name for o in sess.get_outputs()]
cache = {"key": None, "ids": None, "mat": None}


def embed(texts, prefix, bs=16):
    out = []
    for i in range(0, len(texts), bs):
        enc = tok.encode_batch([prefix + t for t in texts[i:i + bs]])
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        am = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": am}
        if "token_type_ids" in names:
            feed["token_type_ids"] = np.zeros_like(ids)
        outs = sess.run(None, feed)
        if "sentence_embedding" in onames:       # the graph already pools (static models, some fine-tunes)
            m = outs[onames.index("sentence_embedding")]
        else:                                    # token vectors: pool them here
            h = outs[0]
            if a.pooling == "cls":               # the first token's vector (bge-m3)
                m = h[:, 0, :]
            else:                                # mean over the real (non-padding) tokens
                m = (h * am[..., None]).sum(1) / am.sum(1, keepdims=True)
        out.append(m / np.linalg.norm(m, axis=1, keepdims=True))
    return np.vstack(out).astype(np.float32)


def load_matrix():
    con = dbdriver.connect(f"file:{a.db}?mode=ro", uri=True, timeout=5)
    try:
        key = con.execute("SELECT COUNT(*), COALESCE(MAX(rowid),0) FROM vectors WHERE model=?", (a.model_name,)).fetchone()
        if key != cache["key"]:
            rows = con.execute("SELECT note_id, vec FROM vectors WHERE model=?", (a.model_name,)).fetchall()
            cache["ids"] = [r[0] for r in rows]
            cache["mat"] = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows]) if rows else None
            cache["key"] = key
    finally:
        con.close()


def handle(req):
    op = req.get("op")
    if op == "ping":
        return {"ok": True}
    if op == "embed":
        m = embed([str(t) for t in req.get("texts", [])], req.get("prefix", "passage: "))
        return {"ok": True, "dim": int(m.shape[1]), "vecs": base64.b64encode(m.tobytes()).decode()}
    if op == "search":
        load_matrix()
        if cache["mat"] is None:
            return {"ok": True, "hits": []}
        q = embed([str(req["query"])], req.get("prefix", "query: "))[0]
        sims = cache["mat"] @ q
        k = min(int(req.get("k", 30)), len(sims))
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        return {"ok": True, "hits": [[cache["ids"][int(i)], round(float(sims[i]), 4)] for i in top]}
    return {"ok": False, "error": "unknown op"}


while True:
    ready, _, _ = select.select([sys.stdin], [], [], a.idle)
    if not ready:
        break                                  # idle: exit and release the model's memory
    line = sys.stdin.readline()
    if not line:
        break
    try:
        resp = handle(json.loads(line))
    except Exception as exc:                   # never die on a bad request
        resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
