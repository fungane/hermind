"""Exercise the batch CLIs against a loopback HTTP stub, not a model."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "hermind" / "tools"


@pytest.mark.parametrize("tool", ["build_summaries", "backfill_aliases"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["normal", "dry-run"])
def test_batch_tool_redacts_at_http_boundary(tool, dry_run, store, tmp_path):
    summary = tool == "build_summaries"
    public = "JuniperAtlas"
    source_email = "input-canary@example.test"
    reply_email = "reply-canary@example.test"
    content = f"{public} is ready; contact {source_email}"
    nid, _ = store.add(content, kind="decision", entities=[public], keywords="draft")
    result = "catalog index"
    generated = f"{result} [email]"
    private = "secret swatch"
    marked = f"card <private>sample <private>glaze</private> {private}</private>"
    if summary:
        store.add("The star charts are sorted by season", kind="decision", entities=[public])
        answer = {"summaries": [{"text": f"{result} {reply_email} {marked}", "sources": [nid]}]}
        event, field = ("preview" if dry_run else "done"), "summaries"
    else:
        answer = {"items": [{"id": nid, "aliases": [f"{result} {reply_email}", marked]}]}
        event, field = ("note_preview" if dry_run else "note_done"), "aliases"

    # Compare logical state, including notes, generated fields, index and metadata.
    before = list(store.conn.iterdump()) if dry_run else None
    received = []
    response = json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            received.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    log = tmp_path / "events.jsonl"
    # Don't inherit endpoint, model, proxy or database settings from the developer.
    env = {key: val for key, val in os.environ.items() if key.upper() in {"PATH", "SYSTEMROOT"}}
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), NO_PROXY="*")
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/v1"
            args = [sys.executable, str(TOOLS / f"{tool}.py"), "--db", store.db_path,
                    "--base-url", url, "--model", "stub", "--log", str(log),
                    "--retries", "0", "--timeout", "5"]
            if summary:
                args += ["--entities", public]
            if dry_run:
                args += ["--dry-run"]
            proc = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
        finally:
            server.shutdown()
            thread.join()

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert len(received) == 1
    path, body = received[0]
    assert path == "/v1/chat/completions"
    assert source_email.encode() not in body
    prompt = json.loads(body)["messages"][0]["content"]
    notes = {row["id"]: row["note"] for row in
             (json.loads(line) for line in prompt.splitlines() if line.startswith('{"id"'))}
    assert notes[nid] == content.replace(source_email, "[email]")

    log_text = log.read_text(encoding="utf-8")
    for stream in (proc.stdout, proc.stderr, log_text):
        assert source_email not in stream and reply_email not in stream and private not in stream
    events = [json.loads(line) for line in log_text.splitlines()]
    assert [e[field] for e in events if e["event"] == event and field in e] == (
        [[generated + " card"]] if summary else [[generated, "card"]])

    assert tuple(store.conn.execute("SELECT content, keywords FROM notes WHERE id=?", (nid,)).fetchone()) == (
        content, "draft")
    if dry_run:
        assert list(store.conn.iterdump()) == before
    elif summary:
        rows = store.conn.execute("SELECT content FROM notes WHERE kind='summary' AND status='active'")
        assert [row[0] for row in rows] == [f"{generated} card (綜合 #{nid})"]
    else:
        row = store.conn.execute("SELECT gen_keywords FROM notes WHERE id=?", (nid,)).fetchone()
        assert generated in row[0] and "card" in row[0] and private not in row[0]
