# Changelog

## 1.1.2

- Migration and import backup rotation now removes only regular files matching Hermind’s automatic backup naming format.

- Schema 7 stores new forget and rejection markers in the owning scope. Earlier markers have no reliable scope and continue to block recapture across the database. Stop all clients and back up the database before upgrading shared installations. Upgrade every writer before resuming writes. A rollback requires the matching pre-upgrade backup and binaries. Preserve a separate copy of any later writes before reverting.

- Historical links, consolidation, review lists and destructive operations respect scope boundaries. A named scope can read shared notes. Changes to shared notes require the shared write scope. Unknown and inaccessible note IDs give the same tool error. Scoped clients use aliases attached to visible notes and built-in synonyms. Legacy aliases without an owner remain available to all-scope readers.
- Read-only clients need an existing database prepared by a compatible writer. They read a temporary, owner-private snapshot that includes committed WAL data. Allow temporary disk space for the database and WAL. Read-only search no longer builds missing vectors.
- Optional model requests require HTTPS for remote endpoints. HTTP remains available on loopback. Requests stop on redirects. Nested private spans and credential-like input are screened before model requests.
- Search statistics omit query text. Learning vocabulary from searches now requires the explicit plugin setting `learn_query_terms: true`. Preview old query and vocabulary records locally with `hermind privacy-cleanup --db FILE`. The separate `--apply` option clears these fields and their learned index entries in one transaction. Existing backups retain their historical contents.
- Exports use owner-private files and enforce directory boundaries. Updates requiring review commit their text, history and review state together. JSON-RPC batches are limited to 64 messages. Oversized hook payloads are skipped.

## 1.1.1

- Hermind JSON import removes marked private text from scopes and link relations before using them. A private-only scope does not fall back to the default scope. Public scopes and relations still work. Import also cleans entity names, tag strings, generated keywords, learned terms and model labels. Preview and apply explain invalid `kind`, `created_at`, `trust` and `status` without quoting the input. Existing records are not rewritten.
- Clean marked private text on write and before compaction. The alias and summary batch tools mask personal data sent to the model and clean their preview and execution output.
- Optional model requests use the installed version in `User-Agent`. The session header is sent only over HTTPS to the exact hostname `opencode.ai`. Authenticated model requests do not forward keys on redirects.

## 1.1.0

Hermes plugin: automatic recall now knows which project a note came from. A user with no project (or who never
changes a setting) sees the same ranking as before for notes without a project.

- **Notes remember their project.** Each new note stores the working directory of the session that wrote it. Your home
  directory, a filesystem root and a folder above two or more projects count as no project. The value stays inside the
  database: it is never in a tool result, the MCP server's output or an export.
- **Automatic recall ranks notes from another project lower, never hides them.** `chat` and `summary` notes written in a
  different project get a factor of 0.5 (with semantic search on, a note that the embedding results also surface can be
  scaled a second time, 0.25 overall). Pinned notes, notes you opened or confirmed, and technical facts, decisions and
  preferences are not demoted; explicit searches are unchanged. Settings `project_penalty` (default `0.5`) and
  `project_penalty_kinds` (default `chat,summary`); to turn it off set `project_penalty: 1.0`.
- **Schema 6.** One new nullable column, `notes.project`. A schema 5 database is copied first
  (`<database>.bak-v5-...`, the three newest are kept) and the upgrade stops if the copy fails.
- **`hermind import-projects FILE.json`** gives notes written before the upgrade the project of their session, from a JSON
  object of session ids and working directories that you produce. It sets only notes without a project, saves a
  backup copy first, and `--dry-run` writes nothing (not even an upgrade of an older file). See
  docs/import-export.md.

- **LLM helper behind a relay.** The optional extraction/rerank client sends a `User-Agent` and, for
  `opencode.ai`, one stable session id per client, so a relay in front of an OpenAI-compatible endpoint accepts the
  call. Direct DeepSeek users see no change.

## 1.0.2

Fixes from a newcomer's install test: MCP-only installs (no Hermes) now work the way the docs say. Nothing changes
for an existing Hermes user who passes nothing new.

- `review` and `audit-db` now find the MCP database when there is no Hermes database (`$HERMIND_DB`, then
  `~/.hermind/memory.db`); with Hermes installed they use its database as before (pass `--db` for the MCP one). The
  error, when none exists, names every path it tried.
- `install-hermes --dry-run` says SKIPPED, not FAILED, when Hermes is not installed at the default place; a
  `--profile` or `--hermes-home` that does not exist is still an error.
- The semantic-search worker no longer stays a defunct process between its own idle exit and the next search.
- An install smoke test runs on every push: it builds and installs the package the way a newcomer does and walks the
  README's MCP-only route against the installed command, not the checkout.
- Corrected the "How light is it?" numbers in the README and the semantic-search profile table in docs/setup.md from
  a fresh measurement.
- Docs: an "MCP only (no Hermes)" semantic-search example, a "Running the tests" section in CONTRIBUTING.md, and the
  `claude mcp add` line now matches what `hermind config claude-code --write` prints.

## 1.0.1

Windows only; nothing changes on Linux and macOS. Windows is still not supported, but the test suite now passes there
(GitHub Actions, Python 3.10 and 3.14).

- `install-hook` writes the hook command, and `config claude-code` prints the `claude mcp add` line, with double quotes
  on Windows. Windows shells do not read POSIX single quotes, so the Claude Code hook never ran there.
- Tests that compared paths with POSIX strings now compare native paths. What cannot apply on Windows (owner-only file
  modes, semantic search) is skipped there, each with its reason.
- Three known Windows defects are listed in docs/limitations.md: `authorize-key` writes a POSIX command line,
  `encrypt_db.py --generate-key-file` gives the wrong message when it cannot make the key's folder, and output cut off
  by a closed pipe prints an `OSError` line at exit.

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
