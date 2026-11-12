# Changelog

## Unreleased

- Windows: the hook command from `install-hook` and the `claude mcp add` line from `config claude-code` are quoted for Windows shells (no change on Linux and macOS); the tests that assumed POSIX paths now compare native ones, what cannot apply on Windows is skipped there, and three known Windows defects are listed in docs/limitations.md.

## 1.0.0

First public release. Long-term memory for AI agents in one SQLite file, standard library only, for the Hermes agent
and for any agent that speaks MCP. Works with no LLM and no network.

**Memory and search**
- Notes with a kind (decision, preference, tech, state, news, chat, summary), keywords, entities, pins and trust.
  Decisions, preferences and technical notes never expire, and neither does a note you save yourself without choosing
  chat or news (notes saved before this version keep the expiry they were given); automatically captured chat and news expire (90 and 14 days); idle chat, news and state notes lose rank slowly.
- Search for English, Chinese and Cantonese: BM25 with synonym groups and Traditional/Simplified folding, an entity
  graph, time words and recency, fused by reciprocal rank. About 20 ms on 5,000 notes.
- Consolidation that never deletes: near-duplicates are merged and outdated facts superseded, and every change can be
  restored. Validity dates (`valid_from`, `valid_to`) and `as_of` questions ("what did we use in September?").
  The automatic run at session start stops after 10 seconds and continues next time.
- A pinned core block, a review queue, feedback that adjusts trust, and tombstones that stop a forgotten note from being
  captured again.

**Agents**
- Hermes plugin: recall before every turn, capture after every reply, ten memory tools.
- `hermind-mcp`: an MCP server over stdio, read-only by default, `--write` for saving. Speaks the 2026-07-28 stateless
  protocol and the older `initialize` handshake. Entries for 20+ clients (`hermind config CLIENT`), notes on another
  machine over ssh with a locked key (`hermind authorize-key`), and automatic recall in Claude Code through a hook
  (`hermind install-hook`).
- `hermind` CLI for people and agents, every command with `--json`: `doctor`, `selftest`, `questions`,
  `install-hermes` (backup, verification, rollback), `config`, `remember`, `search`, `review`, `audit-db`, `export`,
  `import`, `setup-vectors`, `embed-all`, `rename-db`, `install-skill`. `AGENTS.md` is the runbook for an agent that
  installs Hermind.

**Your data**
- `hermind export`: every note in every status with all fields, entities and links, as JSON, entity/relation lines
  (`--format jsonl`) or a Markdown folder (`--format markdown`).
- `hermind import`: Markdown folders, Claude Code memory and instruction files, ChatGPT and Claude data exports,
  `AGENTS.md`-style instruction files, a pasted list, Hermes' built-in memory files and Hermind's own export. A preview
  first; `--apply` backs up, `--revert` takes a batch back. The ChatGPT and Claude export formats are not published: the
  readers follow third-party descriptions and were tested on synthetic exports only.
- Upgrades copy the database first (SQLite online backup, integrity-checked, owner-only, newest three kept) and refuse to
  run if the copy fails.

**Optional layers (off by default, fail soft)**
- Semantic search with a local model in a separate worker that exits when idle: four profiles from 52 to 568 MB, each
  pinned to a revision and checked against a SHA-256. `hermind setup-vectors` sets it up from an installed package; the
  worker runs with onnxruntime's telemetry switched off and keeps its caches in its own folder.
- LLM rerank, LLM fact extraction, a dated fact digest (`memory_reflect`), generated search aliases, entity summaries and
  a nightly review (`tools/sleep_consolidate.py`, dry run unless `--apply`), with any OpenAI-compatible endpoint you choose.
- SQLCipher encryption of the database (`tools/encrypt_db.py`).

**Privacy and safety**
- Credentials are refused; `<private>...</private>` text is never stored, logged or sent; e-mail addresses, phone,
  ID-card and payment-card numbers are redacted before any LLM call. Database files are owner-only.
- Notes an agent writes through MCP are saved at once, except risky ones (instruction-like text, personal data, pin
  requests, edits of pinned notes), which wait for the user's review.
- `hermind doctor --share` gives a report that is safe to paste into an issue.

Known limits are listed in [docs/limitations.md](docs/limitations.md). Windows is not supported yet.
