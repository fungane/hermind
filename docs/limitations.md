# Limitations

What does not work well yet, and what to keep in mind.

- [Status](#status)
- [Recall](#recall)
- [Privacy and security](#privacy-and-security)
- [Resources](#resources)
- [Behaviour to know](#behaviour-to-know)

## Status

- **Young and tested by one person.** The recall numbers come from one person's notes plus synthetic sets
  ([how well it recalls](how-it-works.md#how-well-does-it-recall)). The test suite is run on Linux (Python 3.10, 3.12, 3.14)
  and macOS (3.10, 3.14) on the maintainer's machines before each release; a person has used it only on Linux.
- **Windows is not supported yet.** GitHub Actions runs the suite there (Python 3.10 and 3.14) as information only, and no
  person has used Hermind on Windows. What the Windows run leaves out, and what it knows to be wrong:
  - *Cannot apply there.* The owner-only modes (600 for files, 700 for folders) are Unix permission bits; Windows ignores
    them, so backups, exports and key files are as readable as the folder that holds them (see [privacy](privacy.md#encryption)).
    Semantic search is POSIX-only: `setup-vectors` refuses, the embedding worker is read with `select()` on a pipe (Windows
    allows that only for sockets) and its Python is looked for in `venv/bin`. Without it, search is lexical, as without any vectors.
  - *Known defects (the tests that show them are marked as expected failures on Windows).*
    `authorize-key` writes a POSIX command line (`python3`, single quotes around the paths), which a Windows OpenSSH
    server cannot run. `encrypt_db.py --generate-key-file` says "already exists" instead of "cannot write" when it cannot
    make the folder of the key file. A command ended by a closed pipe (`hermind export | more`) prints
    `Exception ignored ... OSError: [Errno 22]` at exit, because Windows reports a closed pipe as EINVAL rather than as a broken pipe.
  - *Changed for Windows, not yet tried by a person there.* The hook command that `install-hook` writes and the
    `claude mcp add` line of `config claude-code` quote with double quotes instead of POSIX single quotes (on Linux and macOS
    they are unchanged). A path with a cmd.exe special character (`&`, `|`, `<`, `>`, `^`, `%`) is still not escaped.
  - Generated config snippets carry native paths (`C:\...`); a path you give as `/data/m.db` becomes `\data\m.db`.
  The stdio MCP server itself uses UTF-8 explicitly. Reports from Windows users are welcome.

## Recall

- Without the optional layers, recall depends on shared words; the vocabulary gap is the main failure mode. On hard English
  paraphrases, lexical search finds about half in the top 5 and semantic search about 85%. The English synonym table is
  small and general.
- Short follow-ups that only make sense with the previous turn ("怎麼操作") may recall loosely related notes.
- Search is by whole words: a single emoji is not a searchable word, and a prefix such as `Postgre%` does not match
  `PostgreSQL`.
- The open-rate prior is not corrected for position (top results get opened more), so it is capped at ±10 %: it can reorder
  near-ties and notes a few ranks apart, never overturn a clear lead. `open_rate: false` turns it off.
- `valid_to` set by hand only filters when `as_of` is used; the default search does not hide a note whose end date has passed.
- A recall eval set is labelled against one database; on another database its note ids mean nothing.

## Privacy and security

- Credential screening is pattern-based (API-key shapes, "password is ...") and will miss unusual formats. Hermes masks known
  secret formats before they reach the plugin, so chat-based fake-key tests only show the masked-form refusal. Do not paste
  secrets into chat.
- There is no access control inside a database file; separation is by profile, not by permission.
- Encryption is optional, needs the `sqlcipher3` package and keeps the key on the same machine as the program.
- The optional LLM layers send text to the configured provider and cost tokens.

## Resources

- Vector search needs 160 to 910 MB of memory while the worker is awake (depends on the profile) and a 52 to 568 MB model
  download.
- **Large databases.** Measured on synthetic notes (random vocabulary, about 5 % near-duplicates) on one machine:
  consolidating 50,000 new notes took about 66 s and 100,000 about 170 s (peak memory about 250 MB, database about 123 MB,
  `integrity_check` ok). Cost grows faster than the note count (8,000 notes: about 3 s), so nightly runs on an already
  consolidated database are cheap, but a first run over a very large import is not. `consolidate()` takes a `time_budget`
  and continues on the next call; the automatic run at session start (`maintenance()`) uses 10 seconds, so a large import is
  consolidated over several sessions instead of holding one up. A search using only very common words (every word in more than
  40 % of the notes) scores just the rarest of them over its 2,000 newest notes, so it answers in about 25 ms at 50,000 notes but
  finds only recent notes; the duplicate check before a write skips this fallback. Searches with distinctive words took 10 to 25 ms. Real notes will differ: treat these as orders of
  magnitude.
- Consolidation ignores words that appear in more than 300 notes when looking for near-duplicates, so a note made only of very
  common words may not be folded into its twin (about 0.5 % of planted duplicates in the test above). Nothing is lost; both
  notes stay visible.

## Behaviour to know

- The core block repeats pinned notes that mirror Hermes' built-in `MEMORY.md` / `USER.md` (`mirror_builtin`); lower
  `core_summary_chars` or turn the mirror off if you do not want both. Pending notes are never confirmed automatically.
- An install that already has a `decision_memory.db` keeps using it; see [Setup](setup.md#install) to rename it.
