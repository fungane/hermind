"""Recall-quality eval for the automatic prefetch path (no LLM, no network; the local embedding worker is optional).

    python tools/recall_eval.py ROOT DB_COPY [--no-vectors] --set YOUR_SET.json (format: tools/recall_eval_set.example.json) [--show]

Loads the plugin checkout at ROOT the way Hermes does, points it at a scratch copy of DB_COPY (the original is never
opened), calls provider.prefetch() for every prompt and scores the injected note ids:
  topical:  hit@k (any relevant note recalled), recall@k (relevant recalled / min(#relevant, k)), precision@k
  negative: share of prompts with no recall at all (topicless prompts should inject nothing), avg notes injected
Compare two checkouts on the same DB copy; labels refer to note ids of that database.
"""
import argparse, importlib.util, json, os, re, shutil, sys, tempfile, types
from abc import ABC, abstractmethod
from pathlib import Path


def _stubs():
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider(ABC):
        @property
        @abstractmethod
        def name(self): ...

        @abstractmethod
        def is_available(self): ...

        @abstractmethod
        def initialize(self, session_id, **kwargs): ...

        @abstractmethod
        def get_tool_schemas(self): ...

    class RecallStatus:
        def __init__(self, provider_label, count, glyph=""):
            self.provider_label, self.count = provider_label, count

    mp.MemoryProvider, mp.RecallStatus = MemoryProvider, RecallStatus
    mp.is_trivial_prompt = lambda t: not (t or "").strip()
    sys.modules.setdefault("agent", types.ModuleType("agent"))
    sys.modules["agent.memory_provider"] = mp
    reg = types.ModuleType("tools.registry")
    reg.tool_error = lambda m: json.dumps({"error": m})
    sys.modules.setdefault("tools", types.ModuleType("tools"))
    sys.modules["tools.registry"] = reg


def load(root: str):
    _stubs()
    spec = importlib.util.spec_from_file_location("hermind_eval", str(Path(root) / "__init__.py"),
                                                  submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermind_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root"); ap.add_argument("db")
    ap.add_argument("--set", required=True, help="your labelled prompts (see recall_eval_set.example.json); keep it out of the repository")
    ap.add_argument("--no-vectors", action="store_true"); ap.add_argument("--show", action="store_true")
    ap.add_argument("-k", type=int, default=4)
    a = ap.parse_args()
    data = json.load(open(a.set, encoding="utf-8"))
    work = tempfile.mkdtemp(prefix="hm-eval-")
    db = os.path.join(work, "eval.db")
    shutil.copy(a.db, db)
    os.chmod(db, 0o600)
    mod = load(a.root)
    cls = getattr(mod, "HermindProvider", None) or getattr(mod, "DecisionMemoryProvider")
    cfg = {"db_path": db, "prefetch_limit": a.k, "auto_capture": False, "llm": {"enabled": False},
           "vectors": {"enabled": not a.no_vectors}}
    p = cls(config=cfg)
    p.initialize("20261104_000000_eval", hermes_home=work, platform="cli")
    ids_of = lambda txt: [int(x) for x in re.findall(r"^#(\d+)", txt or "", re.M)]
    hits = rec = prec = 0.0
    for q, rel in data["topical"]:
        got = ids_of(p.prefetch(q))[:a.k]
        good = [i for i in got if i in rel]
        hits += bool(good); rec += len(good) / min(len(rel), a.k); prec += len(good) / a.k
        if a.show:
            print(f"{'OK ' if good else 'MISS'} {len(good)}/{min(len(rel), a.k)} {q[:50]!r} -> {got}")
    n = len(data["topical"])
    empty = injected = 0
    for q in data["negative"]:
        got = ids_of(p.prefetch(q))
        empty += not got; injected += len(got)
        if a.show:
            print(f"{'EMPTY' if not got else 'NOISE'} {q[:50]!r} -> {got}")
    m = len(data["negative"])
    p.shutdown()
    shutil.rmtree(work, ignore_errors=True)
    res = {"topical_n": n, f"hit@{a.k}": round(hits / n, 3), f"recall@{a.k}": round(rec / n, 3),
           f"precision@{a.k}": round(prec / n, 3), "negative_n": m, "negative_empty": round(empty / m, 3),
           "negative_avg_notes": round(injected / m, 2), "vectors": not a.no_vectors}
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
