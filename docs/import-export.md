# Import, export, backup and uninstall

How to bring notes into Hermind (`hermind import`), take them out (`hermind export`), copy the database safely
(`hermind/tools/backup.py`) and remove everything.

- [Import](#import)
- [Export](#export)
- [Backup](#backup)
- [Uninstall](#uninstall)

## Import

`hermind import` brings notes you already have somewhere else into Hermind: a folder of Markdown or text files, Hermes'
built-in memory files, Claude Code's memory, the data export of ChatGPT or Claude, an instruction file such as `AGENTS.md`, a
list your assistant wrote when you asked what it remembers about you, or the JSON export of another Hermind. It shows what it
would do before it does anything, backs the database up first, and one command takes an import back.

### How it works

| You run | What happens |
|---|---|
| `hermind import SOURCE` | **Preview.** Lists every entry found and what would happen to it. Changes nothing, not even by creating the database |
| `hermind import SOURCE --apply` | **Does it.** Saves a backup copy of the database and prints where it is, then adds the new notes as one batch |
| `hermind import --list` | Shows the batches imported so far and whether each one finished |
| `hermind import --revert BATCH` | Preview of taking a batch back; add `--apply` to do it |
| `hermind import --readers` | Lists the kinds of source that can be imported |

Three promises hold for every source:

- **The preview is the truth.** It shows, entry by entry, what `--apply` will do (the same function decides both).
- **Notes you already have are never touched.** An import adds notes. It never overwrites a note, merges keywords into one or
  raises a proof count.
- **Everything is accounted for.** Each entry gets exactly one outcome, and what a reader sees but does not import is named
  in a note under the preview, with a count.

Imported notes are ordinary live notes. They never expire, whatever their kind, and keep the date the source gives (when it
gives one) as their creation date. Their idle clock starts at the import, so a note dated two years ago does not rank as long
forgotten before anyone could have recalled it. Vectors are not computed during the import: run
[`hermind embed-all`](setup.md#semantic-search) or let the background indexing pick the new notes up.

**The core block** is the short standing summary an agent receives in every session (pinned notes and the most trusted
preferences). A preference imported from another tool stays out of it **until it has been used**: while it is not pinned,
has never been recalled and its trust is not above the default, it is left out, so a fresh import cannot push your own
preferences out. Once it has been recalled, pinned or trusted through feedback it counts like any other. Search is not
affected. Notes from Hermind's own export (Markdown files with `id` and `rev` in the front matter, the JSON export) count as
your own and are not held back.

### Outcomes

| Outcome | Meaning |
|---|---|
| `new` | Will be (or was) added as a note. If some details could not be used (an unreadable date, an unknown kind, a trust outside 0 to 1) they are left out, the text is still imported, and the line says which |
| `version` | An older version of a note this import adds (from Hermind's JSON export). Kept as an older version of that new note: found by a search that includes history, never shown as a live note |
| `duplicate` | The same text is already in the database (the line names the note, and says when it is not live) or came earlier in this import. Nothing is added |
| `edited` | The file is Hermind's own Markdown export of a note you have, and it differs from that note. Nothing is applied; `hermind/tools/import_md.py` brings edits back into the note |
| `history` | Not imported as a live note, and the line says why: an older version or merged duplicate in an export; an older version whose note is not new; a note rejected in review; something the source's owner removed later (ChatGPT) |
| `refused` | The store's own rules say no: credential-like text, text you asked Hermind to forget, text over 100,000 characters, or nothing left outside `<private>...</private>`. The reason is the store's wording |
| `unreadable` | The file's text could not be decoded; the reason says how to fix it (see `--encoding`) |
| `empty` | No text |
| `excluded` | The entry's origin matched an `--exclude` pattern |

A count line always lists all nine outcomes in this order: `new`, `version`, `duplicate`, `edited`, `history`, `refused`,
`unreadable`, `empty`, `excluded`.

### Options

| Option | What it does |
|---|---|
| `SOURCE` | The folder, file or ZIP to import |
| `--from READER` | Use this reader. Without it the reader is chosen from the shape of the source and the preview says which; if two fit equally, Hermind asks you to choose |
| `--apply` | Really do it. Without it an import (or a revert) only shows what it would do |
| `--exclude PATTERN` | Leave out entries whose origin matches this glob (`*`, `?`, `[...]`); repeatable. An excluded entry is treated as if the source did not have it. The preview shows every entry's origin |
| `--no-backup` | With `--apply`: do not copy the database first |
| `--encoding NAME` | Markdown, text and instruction files that are not valid UTF-8 (for example `big5`, `gb18030`, `cp1252`, `shift_jis`). UTF-8, and UTF-16 or UTF-32 with a byte-order mark, are always read; other files are `unreadable` without the option |
| `--ignore-file-names` | Markdown and text folders: do not put the file name in front of a note that does not say it (a title or name in the front matter is still used) |
| `--include-removed` | ChatGPT export only: also import what ChatGPT's data shows you removed or changed later |
| `--list` | Show the batches |
| `--revert BATCH` | Take a batch back (preview unless `--apply`) |
| `--readers` | List the readers, each with a line on what it accepts |
| `--db FILE` | The database (default `$HERMIND_DB`, else `~/.hermind/memory.db`) |
| `--json` | Machine-readable output with stable keys |

When a source has several parts (every project of a Claude Code folder, the lists and writes of a ChatGPT export, the notes
and older versions of a Hermind export), the preview ends with a "By source" section: one line per part with its counts and
the `--exclude 'PATTERN'` that leaves it out. Leaving one part out changes nothing else.

### Backups, batches and reverting

`--apply` first copies the database (SQLite's online backup, so it also holds what an agent saved a moment ago) to
`<database>.bak-import-<time>-<n>`, checks the copy, makes it readable by you only and prints its path. The newest five
import backups are kept. An encrypted database stays encrypted. If the copy cannot be made, nothing is written.

The new notes are written as one **batch** (a sortable id such as `20261111-153012-482a1`) in chunks of 300, so an agent
using the same database is never locked out for long. `hermind import --list` shows each batch, how many of its notes are
still there and whether it finished; a run that stopped partway shows as `incomplete`.

`hermind import --revert BATCH` removes what the batch created, without leaving a tombstone, so the same text can be imported
again. It also takes back what the notes taught the entity index: entities the batch created and no note uses, and the
aliases it added. A note that was edited, merged, superseded or pinned since the import is **kept**, and the preview says
why; so is every older version that belongs to a kept note. It previews first; `--apply` does it, with a backup first.

### The sources

Every source is only read: no reader writes, renames or extracts anything, and a ZIP is read in place. The reader is chosen
by the source's shape; `--from NAME` picks one yourself.

#### Markdown and text folders (`markdown`)

**Where to get it:** any folder of notes: an Obsidian vault, a folder of `.md` files, or a folder written by
`hermind export --format markdown`. A single `.md` or `.txt` file works too.

- **Taken:** every `.md`, `.markdown`, `.mdx` and `.txt` file, one note each. Front matter is optional; `kind`, `keywords`
  (and `aliases`), `tags`, `valid_from`, `valid_to`, `created` (or `date`), `pinned`, `trust` and `scope` are honoured, and
  `status: pending` keeps the note in the review queue. The title, name or file name, and the description, are put in front of
  a body that does not already say them (`--ignore-file-names` turns file names off). Empty, nothing-outside-`<private>` and
  forgotten are judged on the body alone; duplicates on the full text.
- **Not taken:** other files are counted in one note under the preview, per extension; files inside hidden folders (`.git`,
  `.obsidian`) are counted per folder; the `index.md` files Hermind's exporter wrote are passed by (an `index.md` you wrote is
  an ordinary note). Unused front matter keys are named with the number of files that had them.
- **Symbolic links:** a link to a file or to a folder inside the folder you give is never followed, in every reader that
  walks a folder (Markdown, Claude Code, Hermes built-in memory, unpacked export folders): it is listed as `unreadable`
  with the reason "symbolic link", and what it points to is not read. A single file you name yourself is read even when it is
  a link. If a dotfile manager (a stow-style setup) made the files inside the folder links, those files are not read: name the
  real folder (or the file) as SOURCE instead. ZIP members that are links, or whose names are absolute or contain `..`, are refused and named in the preview.
- **Hermind's own Markdown export:** a file that is the export of a note of this database is `duplicate` (`same as #id`) or
  `edited`; a file whose `status` is `superseded` or `merged` is `history`. Into a different database the folder imports as
  new notes, with kind, tags, trust, pin and validity.
- **Defaults:** files that are not UTF-8 are `unreadable` until you give `--encoding`; file names are added to notes.

#### Hermes' built-in memory (`hermes-builtin`)

**Where to get it:** Hermes keeps `MEMORY.md` (what the agent noted) and `USER.md` (your profile) in its memories folder
(usually `~/.hermes/memories/`). Give the folder or one of the files.

- **Taken:** each entry (entries are separated by a line holding only `§`) becomes one note, with an origin such as
  `USER.md#3`. Each note is written exactly as the live mirror in the Hermes plugin writes it (same source, kind and pin), so
  a later edit or removal through Hermes' memory tool finds the imported note. An entry the mirror already stored is a
  `duplicate`.
- **Not taken:** other files in the folder.

#### Claude Code (`claude-code`)

**Where to get it:** Claude Code's configuration folder (usually `~/.claude`). **You give the path; Hermind never looks for it
on its own.** You can also give one project's memory folder (`.../projects/<project>/memory`) or a `MEMORY.md` index.

- **Taken** (each part is a group in "By source"): every project's memory folder; subagent memory (`agent-memory/`, and a
  project's `.claude/agent-memory/`); your user instructions (`CLAUDE.md` and `rules/`); each known project's instruction
  files (`CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md`, `.claude/rules/`); a custom memory folder named by
  `autoMemoryDirectory` in `settings.json`. A memory topic file becomes one note (its `type` sets the kind, `modified` the
  date); an index line in `MEMORY.md` that links to a topic file becomes keywords of that file; an index line to a missing
  file, or any other line, becomes a note. Instruction files are split into one note per block under a heading, and each
  note says its project, file and heading.
- **Defaults:** `AGENTS.md` beside `CLAUDE.md` is **not** read (Claude Code reads `AGENTS.md` only when the project has no
  `CLAUDE.md`, `.claude/CLAUDE.md` or `CLAUDE.local.md`); `@path` imports are listed, not followed; `CLAUDE.md` files in a
  project's subfolders are not searched. A project's folder is found by decoding the name Claude Code gave its memory; when
  it is not found its memory is still read and the project is counted in a note.
- **Not taken, and never opened:** session transcripts (`*.jsonl`) and credentials (`.credentials.json`). Other items
  (`agents/`, `skills/`, `commands/`, `plans/`...) are named in a note.

#### ChatGPT export (`chatgpt-export`)

**Where to get it:** in ChatGPT, profile menu, **Settings → Data controls → Export data → Export**. ChatGPT emails you a link
to a ZIP. Give Hermind the ZIP (or the folder you unpacked it into). Hermind reads these exports as third-party tools describe them; the formats are not published, so read the preview before you `--apply`.

- **Taken:** the newest memory list (`memory#N`), memory writes made after it, and your custom instructions ("about you" and
  "how to respond", from the newest conversation that has them). All are preferences. Groups: `memory`, `memory writes`,
  `custom instructions` and `removed or older versions`.
- **Defaults:** what you removed stays removed. ChatGPT keeps old memory lists inside old conversations; an item not in the
  newest list, a memory write not in it, and older versions of the custom instructions are `history` and **not imported**.
  `--include-removed` imports them as ordinary notes.
- **Not taken:** the conversations themselves (only their number is reported), `user.json` (never opened), `chat.html`,
  feedback files and everything else, each named in a note.

#### Claude export (`claude-export`)

**Where to get it:** in Claude, **Settings → Privacy → Export data**. Claude emails you a link to a ZIP. Give Hermind the ZIP
(or its unpacked folder). Hermind reads these exports as third-party tools describe them; the formats are not published, so read the preview before you `--apply`.

- **Taken:** `memories.json`: what Claude remembers about you (preferences) and about each of your projects (kind chosen from
  the text, with the project's name in the note); each project's instructions (`prompt_template`) and description. Texts with
  sections are split into one note per section; a heading never becomes a note of its own. Groups: `memory`, `project
  memory`, `project instructions`, `project descriptions`.
- **Defaults:** a project's **documents** are not imported (they are files, not memories: a note gives their number, total
  size and up to five names); Claude's starter project is skipped.
- **Not taken, never opened:** `conversations.json` is never read (only its size is reported) and `users.json` is never
  opened.

#### Instruction files (`instructions`)

**Where to get it:** one file of a coding agent: `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`, `CLAUDE.local.md`,
`GEMINI.md`, `.cursorrules`, `.windsurfrules`, `.clinerules`, `copilot-instructions.md`, `*.instructions.md` or `*.mdc`. A
file with one of these names is recognised by its name; a folder stays the Markdown reader's.

- **Taken:** one note per block under a heading, each saying its heading path and the folder (project) the file is in. HTML
  comments are removed (Claude Code strips them too); front matter is not text (its `description` and `paths`/`globs` join
  the subject).
- **Not taken:** `@path` imports are listed in a note and never followed, even when the file exists. `--encoding` works as for
  Markdown.

#### A pasted list (`paste`)

**Where to get it:** ask an assistant to list everything it remembers about you, and save its answer as a text file. This
reader is used **only with `--from paste`**.

- **Taken:** one preference note per list item (bullets and numbered items, with the lines that continue them), each saying
  its category, and one note for a paragraph before the first item (an introduction, if the assistant wrote one: check it in
  the preview and `--exclude` it by its origin, `<file name>#1`).
- **Not taken:** nothing is left out silently; headings and category lines only give the notes their category. `--encoding`
  works as for Markdown.

#### Hermind's JSON export (`hermind-json`)

**Where to get it:** `hermind export --out notes.json` on the other Hermind (see [Export](#export)). A `.json` file that
is an array of objects with `id`, `content`, `kind` and `status` is recognised. The first export format (five keys, no
`status`, live notes only) is read too, with `--from hermind-json`.

- **Taken:**
  - *Live notes* (`status: active`): text, kind, keywords, tags, pin, trust, scope, both validity dates, the creation date and
    the review state (a pending note stays pending), plus event date, proof count, generated aliases and learned terms. The
    counters of use are not carried: the idle clock starts at the import. The origin is `note/<id>` (the file's id).
  - *Older versions* (`superseded`, `merged`): when the note they belong to is imported as new, each is kept as an older
    version of it (outcome `version`, origin `version/<id>`), found by searches that include history. When that note is a
    duplicate, refused or excluded, the older version is `history` and the line says which.
  - *Links* between two notes this import writes are recreated, and the file's entity aliases are added to the entities of the
    new notes; a revert takes both back. Other links are counted in one note.
- **Not taken:** rejected notes (`history`, "rejected in review"). Groups: `notes`, `older versions`, `rejected`.

## Export

`hermind export` hands over everything in the database. It only reads: the database is opened the way `hermind search`
opens it (an encrypted one needs its key, as everywhere) and nothing in it changes. Run it as often as you like.

```
hermind export [--format json|jsonl|markdown] [--out FILE_OR_FOLDER] [--force]
               [--live-only] [--include-history] [--by kind|entity] [--scope NAME] [--db FILE] [--json]
```

Without `--out` the output goes to the screen. With `--out FILE` it goes to a file created with mode 600, and an existing file
is never overwritten unless you add `--force`. The summary line says how many notes went out per status, for example
`exported 4 notes to notes.json (active 3, superseded 1)`. `--db` picks the database (default `$HERMIND_DB` or
`~/.hermind/memory.db`); `--json` prints the summary as JSON. `--scope NAME` keeps only notes written in that scope (a link
can still point at a note of another scope).

### The three formats

| Format | Who it is for | What you get |
|---|---|---|
| `json` (default) | You, a readable backup, a script, another Hermind (`hermind import` reads it back), tools that read a JSON array of objects with a text field | One object per note, **every note in every status**, with every field and the note's entities and links |
| `jsonl` | Tools that keep a graph of entities with observations, one JSON object per line | One entity line per current note, and one relation line per link |
| `markdown` | People, and tools that read a folder of Markdown files | A folder with one `.md` file per note (front matter, then the text) and index files; `--out DIR` is required |

`json` is the only format that hands over everything; the other two carry what their readers can express.

#### `--format json`

A JSON array ordered by note id. The first five keys of every object (`id`, `kind`, `content`, `keywords`, `created_at`)
are the keys of the first version of this command, so an old script still works. `--live-only` selects only notes whose
status is `active` (this includes notes waiting for review); every field is still written.

Each column of the `notes` table is written under its own name, with the value as stored (numbers stay numbers, empty values
are `null`, times are seconds since 1970-01-01 UTC):

| Field | What it is |
|---|---|
| `id` | The note's number in this database |
| `kind` | `decision`, `preference`, `tech`, `news`, `chat`, `state` or `summary` |
| `content` | The text of the note |
| `keywords` | Alternative phrasings the agent gave, separated by spaces |
| `created_at` | When the note was written (or the date of the source it came from) |
| `expires_at` | When the note stops being listed; `null` for notes that never expire |
| `source_session` | Where the note came from (an agent session, or `import:<reader>` for an import) |
| `pinned` | `1` for a pinned note (it goes into the core block), else `0` |
| `content_hash` | A digest of the normalised text (older versions have an empty one) |
| `doc_len_content` | The number of search terms in `content` |
| `doc_len_keywords` | The number of search terms in `keywords` |
| `status` | `active`, `merged` (a duplicate hidden behind another note), `superseded` (an older version hidden behind a newer one), `rejected` (refused in review); any other value is written as found |
| `canonical_id` | For a merged or superseded note: the id of the note it belongs to; else `null` |
| `proof_count` | How many times the same fact was seen |
| `event_at` | The date the note talks about, when its text names one |
| `consolidated_at` | When consolidation last looked at the note |
| `locked` | `1` for a note that consolidation must never merge or supersede (older versions, summaries) |
| `trust` | 0 to 1; 0.5 is the default |
| `valid_to` | When the fact stopped being true; `null` if it still is |
| `candidate` | `1` for a note captured automatically from a conversation; the core block leaves it out |
| `scope` | The scope (`default` unless the note was written in another) |
| `tags` | Tags, separated by spaces |
| `access_count` | How often the note was recalled |
| `last_access` | When it was last recalled |
| `gen_keywords` | Alternative phrasings generated by an LLM (only if you ran that optional step) |
| `gen_at` | When they were generated |
| `gen_model` | Which model generated them |
| `learned_terms` | Query words the note learned from use |
| `review` | `active`, or `pending` for a note that waits for your review |
| `valid_from` | When the fact became true; `null` means since it was created |
| `shown_count` | How often the note was listed to an agent (a faded count, so not a whole number) |
| `opened_count` | How often the agent then opened it |
| `fb_at` | When the two counts above were last updated (they fade over time) |
| `entities` | The note's entities: `[{"name": ..., "aliases": [...]}]` |
| `links` | The note's outgoing links: `[{"to": <note id>, "rel": ...}]`, where `rel` is `version_of`, `superseded_by`, `merged_into` or `stale_by` |

An older version is never a live note: its `status` says so, and a merged or superseded record points at its note with
`canonical_id` and a link. A tool that only wants current notes should keep records whose `status` and `review` are both
`active`.

#### `--format jsonl`

One JSON object per line, for tools that keep entities with observations.

- **One entity line per current note** (status `active`, notes waiting for review included). With `--include-history` the
  older versions (superseded and merged notes) are written too; rejected notes never are. The line is
  `{"type": "entity", "name": "hermind-note-<id>", "entityType": <kind>, "observations": [<text>], "hermind": {...}}`. The
  name is the same for the same note every time; `hermind` holds the full record of the JSON export.
- **The text says what the note is**, because many tools keep only the text. Each statement is put in brackets in front of
  the text, in this order, only when true: `(waiting for review)`, `(outdated: hermind-note-<id> is newer)`,
  `(no longer valid after YYYY-MM-DD)`, `(an older version of hermind-note-<id>, kept for history)`.
- **One relation line per link** whose two notes are both written:
  `{"type": "relation", "from": ..., "to": ..., "relationType": <rel>}`.

Exporting twice gives byte-identical files.

#### `--format markdown --out DIR`

Files are written with mode 600 and folders with mode 700. Options: `--by kind` (default; `DIR/<kind>/00012-first-words.md`)
or `--by entity` (`DIR/<scope>/<entity>/...`), `--scope NAME`, and `--include-history` for older versions. Each file starts
with front matter (`id`, `kind`, `status`, `tags`, `trust`, `created`, `valid_from`, `valid_to`, `supersedes`, `outdated_by`,
`pinned`, `scope`, `keywords`, `rev`) followed by the text; every folder gets an `index.md`. A folder that is not empty is
refused unless you add `--force`, which overwrites files of the same name and deletes nothing. The folder reads back with
`hermind import` (older versions come back as history, not live notes). To edit the files and bring the edits back into the notes, use
`hermind/tools/import_md.py` from a checkout (a dry run
unless `--apply`; text changes go through versioned updates, notes without an id become new notes, conflicts are reported).

### What each format carries

| | `json` | `jsonl` | `markdown` |
|---|---|---|---|
| Live notes | all | all | all |
| Notes waiting for review | yes (`review: pending`) | yes, marked `(waiting for review)` | yes (`status: pending`) |
| Older versions (merged, superseded) | always | only with `--include-history`, marked in the text | only with `--include-history`, `status:` in the front matter |
| Rejected notes | yes (`status: rejected`) | no | no |
| Every field of a note | yes | in `hermind` | no: kind, status, tags, trust, dates, pinned, scope, keywords |
| Entities and aliases | yes | in `hermind` | no |
| Links between notes | yes | as relation lines | partly: `supersedes` and `outdated_by` |
| Counters of use (`access_count`, `shown_count`, ...) | yes | in `hermind` | no |
| Status marked in the text itself | no (`status` says it) | yes | no (the front matter says it) |

A tool that drops front matter or ignores `status` cannot tell an older version from a current note: only use
`--include-history` (or keep the `json` records whose `status` is not `active`) when the receiving side keeps that
information.

### What is never exported

- **Vectors** (the semantic index) and **the search index** (`postings`): both are rebuilt from the text.
- **Tombstones**: digests of notes you forgot, which keep automatic capture from bringing them back.
- **The usage log** (what was searched and listed) and the bookkeeping of imports (batches).

To move everything, tombstones included, copy the database file (see [Backup](#backup)).

### Examples

Four invented notes: one pinned, one edited so that its first text became an older version, one waiting for review.

**`hermind export`**: one record of the array (note 2, the edited one); the others look the same.

```json
{
  "id": 2,
  "kind": "tech",
  "content": "The Juniper dashboard runs on SQLite since March.",
  "keywords": "",
  "created_at": 1767312000.0,
  "expires_at": null,
  "source_session": "",
  "pinned": 0,
  "content_hash": "fe40c396858abded6d0a84f44aea1442b086f2b3",
  "doc_len_content": 6,
  "doc_len_keywords": 0,
  "status": "active",
  "canonical_id": null,
  "proof_count": 1,
  "event_at": null,
  "consolidated_at": null,
  "locked": 0,
  "trust": 0.5,
  "valid_to": null,
  "candidate": 0,
  "scope": "default",
  "tags": "dashboard",
  "access_count": 0,
  "last_access": null,
  "gen_keywords": "",
  "gen_at": null,
  "gen_model": "",
  "learned_terms": "",
  "review": "active",
  "valid_from": 1772409600.0,
  "shown_count": 0.0,
  "opened_count": 0.0,
  "fb_at": null,
  "entities": [
    {"name": "Juniper", "aliases": ["juniper board"]},
    {"name": "PostgreSQL", "aliases": []},
    {"name": "SQLite", "aliases": []}
  ],
  "links": []
}
```

The older text is its own record: id 3, `"status": "superseded"`, `"canonical_id": 2`, and
`"links": [{"to": 2, "rel": "version_of"}]`.

**`hermind export --format jsonl --include-history`** (the `hermind` part is shortened; in the file it is the full record):

```json
{"type": "entity", "name": "hermind-note-1", "entityType": "preference", "observations": ["Maya prefers tea to coffee in the morning."], "hermind": {"id": 1, "status": "active", "pinned": 1, "...": "..."}}
{"type": "entity", "name": "hermind-note-2", "entityType": "tech", "observations": ["The Juniper dashboard runs on SQLite since March."], "hermind": {"id": 2, "status": "active", "...": "..."}}
{"type": "entity", "name": "hermind-note-3", "entityType": "tech", "observations": ["(no longer valid after 2026-03-02) (an older version of hermind-note-2, kept for history) The Juniper dashboard runs on PostgreSQL."], "hermind": {"id": 3, "status": "superseded", "canonical_id": 2, "...": "..."}}
{"type": "entity", "name": "hermind-note-4", "entityType": "chat", "observations": ["(waiting for review) Ravi may move the Juniper dashboard to a new server."], "hermind": {"id": 4, "review": "pending", "...": "..."}}
{"type": "relation", "from": "hermind-note-3", "to": "hermind-note-2", "relationType": "version_of"}
```

**`hermind export --format markdown --out notes --include-history`** writes:

```
notes/index.md
notes/.hermind-export.json
notes/chat/00004-Ravi-may-move-the-Juniper-dashboard-to-a.md
notes/chat/index.md
notes/preference/00001-Maya-prefers-tea-to-coffee-in-the-mornin.md
notes/preference/index.md
notes/tech/00002-The-Juniper-dashboard-runs-on-SQLite-sin.md
notes/tech/00003-The-Juniper-dashboard-runs-on-PostgreSQL.md
notes/tech/index.md
```

and `notes/tech/00002-The-Juniper-dashboard-runs-on-SQLite-sin.md` reads:

```markdown
---
id: 2
kind: tech
status: active
tags: ["dashboard"]
trust: 0.5
created: 2026-01-02
valid_from: 2026-03-02
valid_to:
supersedes: []
outdated_by:
pinned: false
scope: default
keywords:
rev: "55e7cd49b1a7"
---
The Juniper dashboard runs on SQLite since March.
```

## Backup

The database is one SQLite file plus, while something has it open, a `-wal` file with the newest writes. Copying the file
by hand while an agent is running can miss notes, and syncing it with Dropbox or Syncthing is unsafe. Use SQLite's online
backup instead:

```bash
python3 hermind/tools/backup.py ~/.hermes/hermind.db ~/.hermind/memory.db   # one or more databases
python3 hermind/tools/backup.py --keep 14 --dest /home/you/backups DB
```

`hermind/tools/backup.py` (`tools/backup.py` inside the installed plugin folder) copies each database while it is in use, runs
`PRAGMA integrity_check` on the copy, makes it mode 600 and keeps the newest N copies (`--keep`, default 7) in `--dest`
(default `~/backups/hermind`). A copy that fails the check is discarded, older backups stay and the exit code is 1. An
encrypted database stays encrypted with the same key, so copy the key file separately
([Encryption](privacy.md#encryption)).

Hermind makes the same kind of copy by itself before an import, a revert and a schema upgrade
(`<db>.bak-import-...`, `<db>.bak-v<N>-...`). To restore, stop everything that uses the database and put a copy in place of
it. A backup is the only way to move everything, tombstones and vectors included; [`hermind export`](#export) is for moving
the notes.

Run it nightly from cron (example, not installed by Hermind):

```cron
15 3 * * *  cd ~/hermind && python3 hermind/tools/backup.py ~/.hermes/hermind.db >> ~/.hermes/logs/backup.log 2>&1
```

## Uninstall

Hermind has no uninstall command; nothing of it runs once its entries are gone. Everything it can create, by route:

| What | Where | Remove |
|---|---|---|
| MCP client entry | the client's config (`claude mcp remove hermind`, or the file `hermind config CLIENT` named) | delete the entry, restart the client |
| MCP database | `~/.hermind/memory.db` with `-wal`, `-shm`, upgrade copies `.bak-v*` and import copies `.bak-import-*` next to it; `<db>.vec.log` if semantic search ran | `rm ~/.hermind/memory.db*` (or the `--db` / `$HERMIND_DB` file) |
| Claude Code hook | one `hermind-hook` entry in `~/.claude/settings.json`, backups `settings.json.bak-hermind-*` | `hermind install-hook --remove --write` |
| Setup skill | `~/.claude/skills/hermind-setup`, `~/.hermes/skills/hermind-setup` (and `.bak`) | delete the folders |
| Hermes plugin | `plugins/hermind` in each profile, plus `plugins/hermind.replaced-*` after a rollback | set `memory.provider` back, restart, delete the folders |
| Hermes database | `hermind.db*` (or `decision_memory.db*`) in each profile, with the same `.bak-*` copies | delete them |
| Install backups | `backups/hermind-plugin-*` and `backups/db/` in each profile | delete them |
| Semantic search | `~/.hermes/hermind-vec` (or `$HERMIND_VEC_HOME`, or the `--home` you chose): venv, models, caches | delete the folder |
| Encryption key | wherever you put it (`HERMIND_DB_KEY_FILE`) | delete it only after the database is gone |
| Backups you made | `~/backups/hermind` (the default of `hermind/tools/backup.py`) or your `--dest` | delete them |

