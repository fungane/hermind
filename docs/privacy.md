# Privacy and encryption

Where your data goes, what protects it, what you control, and how to encrypt the database.

- [Where your data goes](#where-your-data-goes)
- [What you control](#what-you-control)
- [What stays as you wrote it](#what-stays-as-you-wrote-it)
- [Other platforms](#other-platforms)
- [Sharing output safely](#sharing-output-safely)
- [Encryption](#encryption)

## Where your data goes

Hermind is a local program. Nobody, including the author, receives your notes.

| Data | Where it lives | Leaves the machine when |
|---|---|---|
| Notes, entities, trust and open counts | One SQLite file (`hermind.db` or `memory.db`). The file and its WAL/SHM files are mode 600; a folder Hermind creates for it is 700 (a folder you made is left alone). Plain by default, or [encrypted](#encryption) | Never by Hermind itself. Backups and file syncs you set up copy it |
| Search queries and results | In process memory | You connect an MCP client or Hermes: the notes it asks for go into that agent's prompt, so the agent's model provider sees them |
| Notes sent to an LLM | Nowhere stored | Only if you turn on `llm.enabled` (default **off**): rerank sends the question and about 20 candidate notes, `capture_extract` sends the conversation turn, the nightly `tools/sleep_consolidate.py` sends recent notes, all to the endpoint **you** configured. Credential-like notes are skipped and personal data is replaced by tags first (see below) |
| Vector index | The same database file | Never. The embedding model runs locally; `hermind setup-vectors` downloads the worker's packages from PyPI and the model you choose (52 to 568 MB, checked against a pinned checksum) from Hugging Face once, with Hugging Face's telemetry off. onnxruntime sends usage telemetry to Microsoft by default; Hermind starts the worker with `ORT_DISABLE_TELEMETRY=1`, so it sends nothing |
| Telemetry, update checks, analytics | None | The package opens no network connection except `llm.py` when enabled and the one-time `hermind setup-vectors` download |

What you should do:

- Keep `llm.enabled` off unless you accept sending those notes to that provider.
- Connect only agents whose model provider you are willing to show your notes to.
- Use one database per person (Hermes profiles): there is no access control inside a file.
- Turn on [encryption](#encryption) if the file could leave the machine (backups, sync, shared host), and keep the key file
  apart from the database.

`memory_forget` deletes a note for good; `hermind export` hands over everything
([Import, export and backup](import-export.md#export)). To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## What you control

| Control | What it does |
|---|---|
| `<private>...</private>` | Text between the tags is replaced by `[REDACTED]` before it is stored, indexed, logged or sent to an LLM, on every path (Hermes capture, MCP writes, imports, edits). An unclosed tag hides everything after it. A note with nothing left is refused. Tag and full-width variants are recognised |
| Credential screen | Notes that look like passwords, API keys or private keys are refused on every write path, and the whole turn is screened before any LLM call. Pattern-based: it misses unusual formats |
| Personal data before LLM calls | `llm.pii` (default `redact`) replaces email addresses, phone numbers (Hong Kong, mainland China, international `+` numbers, and bare 8-digit numbers next to a word like 電話 or tel), Hong Kong and mainland ID numbers and payment-card numbers with `[email]`, `[phone]`... ID and card numbers are checksum-verified, so ordinary numbers are left alone. The notes in the database are not changed. `block` skips the call instead; `off` sends as is; `redact_patterns` adds your own regexes. Names, street addresses and medical details are not found |
| Review of what agents write | With `--write`, an agent's notes are saved at once. Only risky ones wait for you: text that reads like an instruction to a future agent ("ignore previous instructions..."), notes with personal data, pin requests and edits to pinned notes (the old text stays in history). Held notes are searchable but never reach the core block, and the agent cannot confirm them. `--review-all` holds everything; `--trust-writes` holds nothing |
| `hermind review` | List held notes, confirm (`--confirm 12,15` or `all`) or reject (`--reject 12`) |
| `hermind audit-db` | Read-only scan of a database for credential-like values and personal data (patterns only; excerpts have personal data masked, and none is shown for credentials). Existing notes are not re-checked on their own; run it when you want to know |
| Notes are data | Text returned to an agent is wrapped with a reminder that notes are not instructions, and instruction-like lines in stored text are defanged. This reduces prompt-injection risk; it cannot remove it |
| Read-only MCP | Write tools are not advertised unless you pass `--write`, and a call to one fails as an unknown tool |
| Recall log | `recall_shown` holds note ids and ranks only. The question text it points to is the 200-character query kept in `usage_log` for 90 days (`<private>` text stripped). Nothing leaves the machine |

These controls are pattern-based, and a `<private>` tag only protects what you wrap. A memory agent that is told something
false can write it down: the review step holds the few notes that look like an attack so that a person, not the agent,
decides what becomes trusted memory.

## What stays as you wrote it

Redaction happens only **before text is sent to an LLM**. A note saved locally keeps its phone number, address or IP as you
typed it; only credentials are refused and `<private>` text is removed. The database is as sensitive as the notes you put in
it: back it up like a password manager, and encrypt it if the file can leave the machine.

## Other platforms

"Owner-only" means file mode 600 and folder 700 on Linux and macOS. **Windows ignores these modes**, so there the file is
protected only by your account's default folder permissions: keep the database inside your user profile and consider
encryption. Windows is not supported yet ([limitations](limitations.md)).

## Sharing output safely

`hermind doctor` prints home paths, your user name, executable paths and Hermes profile names. When you file an issue, use
`hermind doctor --share`, which removes them. Never paste notes, a database, a key file or screenshots of recalled notes;
reproduce a problem with made-up text.

## Encryption

Optional and off by default. With a key set, the database file, its WAL file and the search index are encrypted with
SQLCipher (AES-256). Without one nothing changes and nothing extra is installed.

```bash
pip install ".[encrypted]"     # in a checkout: the sqlcipher3 driver (wheels for Linux x86_64 / aarch64, macOS, Windows)
# for uvx, start the server with --from "hermind[encrypted] @ git+https://github.com/fungane/hermind@v1.0.0"
python3 tools/encrypt_db.py --generate-key-file ~/.hermind/db.key   # random key, mode 600, never overwrites
export HERMIND_DB_KEY_FILE=~/.hermind/db.key
# stop Hermes, hermind-mcp and the vector worker, then:
python3 tools/encrypt_db.py ~/.hermes/hermind.db            # writes a verified .encrypted copy, original untouched
python3 tools/encrypt_db.py ~/.hermes/hermind.db --replace  # swaps it in; the plaintext original is kept as
                                                           # .plaintext-backup: delete it yourself afterwards
```

The copy is verified (same tables and row counts, same `user_version`, `integrity_check`) before `--replace` swaps it in.
Then put `HERMIND_DB_KEY_FILE` into the environment of **every** process that opens the database: Hermes
(`~/.hermes/.env`), the nightly cron script, and each MCP client's `env` block. Several clients start servers with a reduced
environment, so an exported variable alone may not reach `hermind-mcp`. Prefer `HERMIND_DB_KEY_FILE` to `HERMIND_DB_KEY`
(the passphrase itself): it shows up in process environments, and some clients strip variables whose names look like
credentials (not checked for each one). Go back with `python3 tools/encrypt_db.py DB --decrypt --replace`.
[`tools/backup.py`](import-export.md#backup) keeps backups encrypted.

What it does and does not do:

- It protects the file when it leaves the machine: stolen disk, backup, cloud sync. It does **not** protect against someone
  who can already run code as your user, because that process can read the key too.
- **Lose the key and the notes are gone.** Keep a copy of the key file somewhere other than the database's disk.
- Cost, measured on a copy of a 5,400-note, 44 MB database (real notes are longer than the short synthetic ones behind
  the README's "5 MB per 5,000 notes", and the search index and vectors grow with them): opening takes about 160 ms instead of 3 ms (key derivation),
  search speed is unchanged (about 15 ms per query).
- A wrong or missing key stops the program with a message that says what to set; it never creates a second, empty database.
- The vector worker has its own Python environment: install `sqlcipher3` there too (`<its venv>/bin/pip install sqlcipher3`),
  or semantic search stays off; the worker's error is written to `<db>.vec.log`.
- Not tested: Android / Termux (no prebuilt wheel there; it would have to be built from source).
