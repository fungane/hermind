# How it works

What Hermind stores, how a note gets in and how a search finds it, what each optional layer adds, which module does what,
and how well it recalls in the maintainer's own tests.

- [What you get](#what-you-get)
- [Data model](#data-model)
- [Write path](#write-path)
- [Read path](#read-path)
- [Optional layers](#optional-layers)
- [Lifecycle](#lifecycle)
- [Module map](#module-map)
- [How well does it recall](#how-well-does-it-recall)

## What you get

| Area | What it does |
|---|---|
| **Retrieval** | BM25 over content and keywords, entity graph (one-hop expansion), time words ("last week", `2026-10-18`), recency, kind / trust / hotness priors. Fused with reciprocal-rank fusion and MMR-style diversity |
| **Semantic search** *(optional)* | A local embedding model in a **separate worker process** with its own virtualenv; the plugin itself stays stdlib-only. Used by `memory_search` and fused into automatic recall (3 s timeout, lexical fallback). The worker exits after 5 idle minutes and frees its RAM. See [Setup](setup.md#semantic-search) |
| **LLM rerank** *(optional)* | The lexical top 20 go to any OpenAI-compatible model, which picks the notes that answer the question. Circuit breaker after 3 failures |
| **Fact extraction** *(optional)* | After each turn an LLM extracts what the turn settled (usually 0 to 1 facts, at most 3) with alternative phrasings, in the user's language, instead of storing the raw chunk. A fact that restates an existing note refreshes that note. Runs in the background; the raw chunk is the fallback |
| **Consolidation** | Near-duplicate notes are merged. Newer facts (for example a swap or RAM size) mark older ones as outdated (`outdated_by`, ranked lower). Nothing is deleted |
| **Timeline and `as_of`** | `memory_related(mode="timeline")` lists the notes about an entity oldest first with superseded versions marked. `as_of=YYYY-MM-DD` on `memory_search` and `memory_related` returns what was valid then |
| **Reflect** | `memory_reflect` returns an evidence pack grouped by topic with conflicts flagged. With an LLM it returns a dated fact digest (oldest first, ids cited, changing values marked CURRENT) |
| **Lifecycle** | News expires in 14 days and chat in 90. Decisions, preferences and tech notes never expire. Idle chat, news and state notes lose rank slowly if nobody recalls them; progress snapshots ("user is working on...") fade faster |
| **Review queue** | Notes written by LLM extraction start as `[pending]`: searchable, ranked x0.6, marked in results. `memory_review` lists, confirms, rejects and restores them |
| **Core memory** | A deterministic block (no LLM) of pinned notes and the highest-trust preferences, capped at `core_summary_chars` (3500), added to the system prompt each session |
| **Recall log** | Each search or automatic recall records which note ids it listed (ids and rank only), and a later `memory_get` or helpful feedback records which listing led to it. With the question text already kept in `usage_log` (200 characters, 90 days, `<private>` stripped) this is a real question-to-right-note history; `tools/mine_eval.py` can turn it into a private eval set |
| **Open-rate prior** | Notes the agent opens after they were listed rank slightly higher, ignored ones slightly lower: smoothed rate against the corpus rate, only after 5 shows, 90-day decay, **never more than ±10 %**. `tools/usage_report.py` shows its effect |
| **Quiet recall** | One-word replies such as "OK", "可以", "做吧" do not trigger a lookup (they are still stored: they are often approvals). Filler (謝謝, 幫我, 一下 ...) is ignored and generic task words (問題, 排查 ...) count less; prompts with no topic word, image-only messages and background-process notices recall nothing. Empty results tell the model not to loop |
| **Hygiene** | Secret screen over the whole turn, masked-credential refusal, forget tombstones, injected-memory stripping, fence-tag defanging, future source dates kept as event dates, scopes and tags. See [Privacy](privacy.md) |
| **Import and export** | `hermind import` previews and adds notes from other tools, with one outcome per entry, a backup first and a way back; `hermind export` writes everything as JSON, entity lines or Markdown. See [Import, export and backup](import-export.md) |
| **Offline tools** | Sleep-time review, alias backfill, entity summaries, usage report, recall eval. See [Optional LLM tools](setup.md#optional-llm-tools) |

## Data model

One SQLite file.

| Table | Holds |
|---|---|
| `notes` | text, `kind` (decision, preference, tech, state, chat, news, summary), agent `keywords`, generated aliases, `status` (active, merged, superseded, rejected), `review` (active, pending), trust, proof count, scope, tags, timestamps, validity window (`valid_from`, `valid_to`), open-rate counts (`shown_count`, `opened_count`, `fb_at`) |
| `postings` | inverted index `(term, note_id, field, tf)` for content, keywords, generated aliases and learned terms |
| `entities`, `note_entities`, `entity_links` | entity graph: names with aliases, which notes mention them, co-mention weights |
| `note_links` | `merged_into`, `version_of` and similar relations between notes |
| `vectors` | optional: one float32 embedding per note (`note_id`, `model`, `vec`) |
| `tombstones` | digests of forgotten notes so auto-capture cannot recreate them |
| `usage_log` | searches, feedback, rerank and extraction calls (pruned after 90 days) |
| `meta` | schema and tokenizer versions, tool bookkeeping |

A database built with an older tokenizer is re-indexed automatically on open. Schema changes are additive (nullable or
defaulted columns added on open; the current schema version is 5), so an old database opens unchanged, after a backup.

## Write path

1. **Capture.** After a reply the provider either stores a raw `User:/Assistant:` chunk (default) or, with
   `llm.capture_extract`, asks the model for 0 to 3 durable facts with alternative phrasings. Extraction runs on a single
   background thread; if the model fails, the raw chunk is stored so nothing is lost.
2. **Screen.** `screen.py` refuses text that looks like a credential and strips previously injected memory blocks; the
   tombstone table refuses anything you asked to forget.
3. **Index.** `textnorm.py` normalises (NFKC, case, simplified to traditional Chinese, English stemming), tokenises (CJK
   unigrams and bigrams, Latin words and their parts) and writes postings; entities are detected and linked.
4. **Deduplicate.** Near-duplicates of the same kind collapse into one note and raise its proof count.
5. **Embed** (optional). New notes are queued for the embedding worker in the background.

**Review queue.** A fact written by LLM extraction is stored with `review='pending'` (`plugins.hermind.review_queue`). It is
searchable but scored x0.6 and marked `[pending]`. `memory_review` confirms it (`review='active'`, eligible for
consolidation) or rejects it: `status='rejected'` hides it from every read, its digest is tombstoned so capture cannot
recreate it, and `restore` lifts both. Pending notes never hide confirmed ones in consolidation and are skipped by the
entity-summary tool.

**Validity.** `valid_from` / `valid_to` say when a fact was true in the world (the creation time is the fallback start). A
supersession, by `consolidate()` or a versioned `update()`, sets the old note's `valid_to` and the new note's `valid_from`
when empty; `memory_remember` accepts both. Plain searches ignore the window; `as_of=YYYY-MM-DD` keeps notes with
`valid_from <= day < valid_to`, keeps superseded versions that were current then, and still folds merged duplicates into
their canonical note.

## Read path

```
query ─► tokenise + synonym expansion ─► BM25 (content, keywords) ─┐
      ─► entity match ─► one-hop graph expansion ───────────────────┤
      ─► temporal window ───────────────────────────────────────────┼─► RRF ─► recency decay ─► diversity ─► top N
      ─► recency / kind prior / trust / hotness ────────────────────┘                                          │
                                                                                                                ▼
 optional: embedding worker top-30 ──► RRF with the lexical list ──► optional LLM rerank of the top 20 ─► results
```

- **Concepts.** A synonym group found in the query forms one concept; a note scores the *max* within a concept and the *sum*
  across concepts, so many phrasings of one idea do not crowd out different ideas.
- **Filters** (kind, date window, `as_of`, tags, scope, trust, expiry) apply to lexical and vector candidates alike.
- **Multipliers after fusion:** trust, idle decay, pending (x0.6), stale, the open-rate prior and mild recency decay. The
  open-rate prior is `1 + clamp(0.5 x (smoothed open rate - corpus rate), +-10 %)`, applied only after 5 effective shows,
  with counts decayed by a 90-day half-life. A note counts as shown once per session when a search or the automatic recall
  lists it, and as opened when `memory_get` follows. Because RRF scores of neighbouring ranks differ by only a few percent,
  the prior can reorder near-ties and notes a few ranks apart, never a lead above 22 %.
- **Fail soft.** If the embedding worker or the LLM is missing, slow or wrong, the lexical ranking is returned unchanged.

## Optional layers

**Embedding worker** (`vec.py` and `embed_server.py`). A separate process in its own virtualenv (onnxruntime, numpy,
tokenizers), set up by `hermind setup-vectors`, loads the ONNX model of the chosen profile and speaks one JSON object per
line over stdin/stdout. `embed` turns texts into a base64 float32 matrix that the plugin stores in `vectors`; `search` turns a
query into the nearest note ids (the worker reads `vectors` read-only and caches the matrix). It exits after `idle` seconds
without a request; the client restarts it on demand and pauses it for 5 minutes after three consecutive failures.

**LLM layer** (`llm.py`). Any OpenAI-compatible `/chat/completions` endpoint, used for rerank, fact extraction, entity
extraction, reflect synthesis and pre-compression pickup. A circuit breaker skips the endpoint for `cooldown` seconds after
three failures; `extra_body` carries provider switches such as DeepSeek's `thinking: disabled`.

**Core memory** (`store_core.py`). No LLM. `core_summary()` collects confirmed, non-candidate, non-low-trust notes: pinned ones
first, then preferences by trust and proof count, one line each (220 characters, fence tags defanged, secret-like lines
dropped), within `core_summary_chars`. Pinned notes keep at most two thirds of the budget so preferences are never starved.
The provider appends the block, inside the `<hermind-context>` fence, to the system prompt. The text is cached per size and
scope and rebuilt after any write by any connection, or when the hour changes.

**Offline tools that write.** `tools/sleep_consolidate.py` (LLM) proposes merges, supersessions and aggregate facts; every
proposal is validated in code, aggregates are new pending notes, nothing is deleted. `tools/export_md.py` and
`tools/import_md.py` round-trip notes through Markdown files with YAML front matter: import compares the file's fields with
the note, uses the `rev` fingerprint to tell a file edit from a database change (conflicts are reported, not applied), and
sends text changes through `update()` so history is kept. Details in [Setup](setup.md#optional-llm-tools) and
[Import, export and backup](import-export.md).

## Lifecycle

- `maintenance()` (on startup): purge expired notes, consolidate new notes (merge near-duplicates, supersede outdated `state`
  notes). Nothing is deleted; merged and superseded notes stay restorable.
- `memory_forget` deletes the note, its merged duplicates and edit history, and leaves tombstones. Rejecting a pending note
  (`memory_review`) is the reversible alternative: the note stays, hidden.
- `memory_update` keeps the previous text as a hidden `version_of` row.
- Offline tools (`backfill_aliases`, `build_summaries`, `embed_all`) are never run from the search path.

## Module map

```
            ┌────────── Hermes ──────────┐
            │ prefetch / tools / hooks   │
            └─────────────┬──────────────┘
                          │ MemoryProvider API
   ┌──────────────────────▼────────────────────────────┐
   │ __init__.py   provider, tool schemas, capture      │
   │   ├─ store*.py  SQLite: notes, postings,           │   stdlib only
   │   │             entities, links, vectors           │
   │   ├─ textnorm.py / synonyms.yaml  tokenizer        │
   │   ├─ screen.py  secret + injected-text screen      │
   │   ├─ pii.py     personal data redacted before LLM  │
   │   ├─ llm.py     optional: rerank, extract          │──► OpenAI-compatible API
   │   ├─ dbdriver.py  opens the DB (plain or SQLCipher)│
   │   └─ vec.py     optional: embedding client         │──► embed_server.py (own venv, exits when idle)
   └────────────────────────────────────────────────────┘
```

| Module | Responsibility |
|---|---|
| `__init__.py` | the Hermes provider: tool schemas, automatic recall, capture, review queue |
| `hermind_cli.py` | the `hermind` command: doctor, selftest, install-hermes, config, import, export, setup-vectors and the rest |
| `hermind_mcp.py` | the stdio MCP server (`hermind-mcp`), on the same handlers as the provider |
| `hermind_hook.py` | `hermind-hook`: read-only automatic recall for Claude Code's `UserPromptSubmit` hook |
| `store.py` | thin facade over the store modules below |
| `store_common.py` | constants, tuning weights, pure helpers (no I/O) |
| `store_schema.py` | schema creation, migration, re-indexing |
| `store_aux.py` | usage log, entity summaries, vector storage, access tracking |
| `store_entities.py` | entity dictionary and graph |
| `store_writes.py` | add / update / forget, generated and learned keywords, dedup and write checks |
| `store_consolidate.py` | maintenance, near-duplicate merge, supersede, restore, history |
| `store_reads.py` | get / list / count / stats / related (including timeline) |
| `store_search.py` | BM25 and multi-signal retrieval, rank fusion, reflect |
| `store_review.py` | review queue: pending, confirm, reject (soft forget), restore |
| `store_core.py` | the cached core summary of pinned notes and trusted preferences |
| `store_feedback.py` | shown / opened counts behind the open-rate prior |
| `textnorm.py` | normalisation, tokenisation, date parsing and kind classification |
| `screen.py` | credential and injected-text screens on the write path |
| `pii.py` | personal-data redaction before LLM calls |
| `llm.py` | the optional OpenAI-compatible client: rerank, extraction, reflect synthesis |
| `dbdriver.py` | opening the database (plain SQLite or optional SQLCipher) |
| `db_backup.py` | the one backup routine (SQLite online backup, integrity check, owner-only, same key for an encrypted database), used before an import, a revert and a schema upgrade |
| `vec.py` | the embedding client: talks to the worker, stores and searches vectors |
| `embed_server.py` | the embedding worker, run in its own virtualenv |
| `vec_setup.py` | `hermind setup-vectors` and `hermind embed-all`: the worker's private venv, the pinned model download (checksums), embedding existing notes |
| `import_core.py` | the core behind `hermind import`: one decision function for preview and apply, batches, revert, readers registry |
| `import_readers.py` | the readers for Markdown / text folders, Hermes' built-in memory files and instruction files |
| `reader_claude_code.py` | the `claude-code` reader: Claude Code's configuration folder, one memory folder or a MEMORY.md index |
| `reader_exports.py` | the `chatgpt-export` and `claude-export` readers: a data export's ZIP (read in place) or unpacked folder |
| `reader_hermind_json.py` | the `hermind-json` reader: Hermind's own JSON export comes back (live notes, older versions, links, entity aliases) |
| `import_hindsight.py` | one-off migration of an exported memory bank into a new Hermind database (see [Setup](setup.md#hermes-plugin)) |
| `text_blocks.py` | the block splitter of `hermind import`: a text becomes blocks with their heading paths |
| `export_notes.py` | `hermind export`: the full JSON export and the entity and observation lines (`jsonl`); read-only |
| `md_export.py` | the writing code of the Markdown export, used by `hermind export --format markdown` and `tools/export_md.py` |
| `md_notes.py` | shared Markdown note-file helpers: front matter, folder walk, decoding, subjects |

## How well does it recall

These are one maintainer's measurements, on one user's real notes and on synthetic sets. They are indications, not
guarantees: your notes, your language and your questions will differ. The question sets are not published (the real ones
contain one user's notes; the synthetic ones are kept with the maintainer). Every number is a top-5 hit rate unless stated: a
question counts when the expected answer appears in the first five notes returned, judged by keyword match.

| Test | Lexical only | With the optional layers |
|---|---|---|
| 54 casual Cantonese and English questions on 2,183 real notes, written by the data's owner | 42 of 54 | 49 of 54 with semantic search |
| 300 questions an LLM wrote as paraphrases of 300 real notes (right note in the top 5) | 55 % | 66 % with the `balanced` semantic profile |
| 20 deliberately hard English paraphrases on invented notes | 10 of 20 | 17 of 20 with semantic search (all 20 in the top 10) |
| LongMemEval-S, 470 questions: session recall@5 | 96.4 % | not measured |
| LongMemEval-S: answer accuracy, dated-fact digest handed to an LLM | not applicable | 87.0 % (multi-session questions 75.2 %, the weakest type) |

What the numbers say:

- **The vocabulary gap is the main failure.** A question that shares no word with the note is where lexical search misses,
  and the semantic layer is what closes most of that gap (10 to 17 of 20 on the hard set). If you mostly ask in your own
  words, set it up: [Semantic search](setup.md#semantic-search).
- **Questions written by the data's owner are easier** for lexical search because they contain concrete names and numbers.
  LLM-written paraphrases favour the semantic layer; the truth for your notes is somewhere between.
- **On LongMemEval-S, how the notes are handed over matters.** Giving the model the raw top-10 sessions gave 67.2 % answer
  accuracy; a dated-fact digest of the same notes gave 87.0 % (what `memory_reflect` produces with an LLM). Answerer and
  judge were both DeepSeek Flash with a LongMemEval-style yes/no prompt, not GPT-4o, so this is not comparable with
  published leaderboards; the 30 abstention questions were skipped.
- **Optional LLM steps help a little.** Generated aliases (`tools/backfill_aliases.py`) moved the hard set from 10 to 13 of 20
  on their own, and on the 54-question set LLM rerank alone gave 49 of 54 (lexical: 42) and rerank plus semantic search 50 of 54.
- **Known weak spots:** questions that share no word with the note and are not covered by a synonym group or the semantic
  layer; "why" questions where the reason sits in a different note than the topic; very generic questions ("what do I do for
  work") unless the semantic layer or rerank is on. Nothing here measures long-term behaviour: 20,000+ notes, concurrent
  writers, months of use.

See [Limitations](limitations.md) for the rest.
