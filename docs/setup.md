# Setup

How to install Hermind, connect it to MCP clients or to Hermes, and switch on the optional layers (semantic search, LLM
tools).

- [Install](#install)
- [MCP clients](#mcp-clients)
- [Hermes plugin](#hermes-plugin)
- [Semantic search](#semantic-search)
- [Optional LLM tools](#optional-llm-tools)

## Install

Hermind is standard-library Python 3.10 or newer, with no runtime dependencies. The test suite is run on Python 3.10,
3.12 and 3.14. There are two routes, and each has its own database file unless you point them at the same one.

| Route | For | Install | Default database |
|---|---|---|---|
| MCP server (`hermind-mcp`) | Claude Code, Cursor, Codex and any other MCP client | `uvx` (needs [uv](https://docs.astral.sh/uv/)), `pipx`, `pip install .`, or run from a checkout | `~/.hermind/memory.db` (or `$HERMIND_DB`) |
| Hermes plugin | Hermes: automatic recall before every turn, automatic capture, review queue | `hermind install-hermes`, or `tools/deploy.sh` | `~/.hermes/hermind.db` (per profile) |

```bash
# MCP server: run straight from git (git URL form is pinned to the release tag), or from a checkout
uvx --from git+https://github.com/fungane/hermind@v1.0.0 hermind-mcp --version
uvx --from /path/to/hermind hermind-mcp --version
pipx install git+https://github.com/fungane/hermind@v1.0.0        # or: pip install .   (from a checkout)
python hermind_mcp.py                                              # from a checkout, no install

# Hermes plugin
git clone --branch v1.0.0 https://github.com/fungane/hermind.git ~/hermind
cd ~/hermind && python3 hermind_cli.py install-hermes --dry-run   # preview; add --profile NAME for a profile
python3 hermind_cli.py install-hermes                              # backs up first, verifies after, prints a rollback
hermes config set memory.provider hermind                          # first time only; then restart the Hermes gateway and app
```

The git URL is pinned to a release tag; change the version to upgrade.

**Check it works.** `hermind selftest` starts the server on a throwaway database, writes and searches a note, confirms the
read-only default refuses writes and checks the 2026-07-28 protocol. It does not touch your database. `hermind doctor`
shows what is installed where and changes nothing. Without an agent you can use the database directly:

```bash
hermind remember "Atlas uses PostgreSQL for orders" --keywords "orders database 訂單 資料庫"
hermind search "which database for orders"
```

`hermind` is the command of an installed package; from a checkout use `python3 hermind_cli.py`. With `uvx` write
`uvx --from <source> hermind ...`.

**Let your AI install it.** Tell the agent: "Read AGENTS.md in the Hermind repository and set Hermind up for me." It asks
where to install, shows a dry run, makes a backup and gives you a rollback command. Nothing restarts or switches on an
optional layer by itself ([AGENTS.md](../AGENTS.md)).

**Upgrades.** A release that changes the database layout upgrades the file automatically, after copying it to
`<db>.bak-v<N>-<time>-<n>` (SQLite online backup, integrity-checked, owner-only, still encrypted if the database is; the
three newest are kept). If the copy cannot be made, the upgrade stops and the database stays as it was.

**Rename.** New Hermes-route databases are `hermind.db`. An existing `decision_memory.db` keeps being used; nothing is
renamed on its own. To rename, stop the agent, then `hermind rename-db` (dry run) and `hermind rename-db --apply`.

## MCP clients

`hermind-mcp` serves the notes over the Model Context Protocol (stdio), so any MCP client can search, and if you allow
it write, one shared database. It needs no Hermes and no network, and lexical search works out of the box. It speaks the
stateless 2026-07-28 revision (`server/discover`, per-request `_meta`, no `initialize`) and the older
`initialize`-based ones (2025-06-18, 2025-03-26, 2024-11-05).

**Read-only by default.** Only `memory_search`, `memory_get`, `memory_list` and `memory_related` are listed (about 1.9 KB
of tool definitions). `--write` (or `HERMIND_MCP_WRITE=1`) also lists `memory_remember` and `memory_update` (about 2.8 KB):
notes are saved at once, and only risky ones (instruction-like text, personal data, pin requests, edits of pinned notes)
wait for `hermind review`. `memory_forget`, `memory_feedback`, `memory_review` and `memory_reflect` appear only when you
name them, and the ones that change notes need `--write` as well: `--write --tools search,get,remember,update,forget`.
Write tools are refused if a client calls them without `--write`. `--write` and `HERMIND_MCP_WRITE=1` are the same
switch; `hermind config` prints the environment form. Notes
are screened as in the plugin (credentials are refused).

| Option | Env | Default |
|---|---|---|
| `--db FILE` | `HERMIND_DB` | `~/.hermind/memory.db` (created on first run) |
| `--agent NAME` | `HERMIND_AGENT` | the client's own name; stored as `source_session` `mcp:<name>` (provenance only, never a filter) |
| `--write` | `HERMIND_MCP_WRITE=1` | off |
| `--trust-writes` | `HERMIND_MCP_TRUST_WRITES=1` | off: risky agent notes wait for your review |
| `--review-all` | `HERMIND_MCP_REVIEW_ALL=1` | off: holds every agent note for review |
| `--core-chars N` | `HERMIND_CORE_CHARS` | `1200`; characters of core notes put into the server instructions, `0` = off |
| `--tools a,b` | `HERMIND_MCP_TOOLS` | `search,get,list,related` (+ `remember,update` with `--write`) |
| `--scope NAME` | `HERMIND_SCOPE` | `default`; reads also see `shared`; `*` reads every scope |
| `--config FILE.json` | `HERMIND_CONFIG` | none; plugin-style options (`llm`, `vectors`, `search_weights`...), all off unless set |

Print a ready-made entry for a client with `hermind config CLIENT [--write] [--db FILE] [--db-from-profile NAME]`
(`hermind config --list` names the clients; `--source git|checkout|pipx` picks how the server is started). Desktop apps
(Cursor, Claude Desktop) often do not see your shell `PATH`: give the full path to `uvx` or `hermind-mcp` (`which uvx`) if a
server fails to start.

### Claude Code

```bash
claude mcp add hermind -- uvx --from git+https://github.com/fungane/hermind@v1.0.0 hermind-mcp
claude mcp add hermind-rw -e HERMIND_MCP_WRITE=1 -- uvx --from git+https://github.com/fungane/hermind@v1.0.0 hermind-mcp
```

In `claude -p` (non-interactive) the tools must be allowed first, for example
`--allowedTools "mcp__hermind__memory_search,mcp__hermind__memory_get"`. The desktop app's Code tab uses the same engine and
settings.

### DeepSeek Harness (dsh)

`@deepseek-ai/dsh` is a release candidate and its maker marks it as subject to incompatible changes. Its MCP client starts a
stdio server from a Cordis patch. Put this in a patch file and start with `dsh web --patch /path/to/hermind.cordis.yml` (or
merge it into `$DSH_HOME/cordis.patch.yml`; restart `dsh web` after editing):

```yaml
- insert:
    - id: mcp-hermind
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: hermind
        transport: stdio
        command: uvx
        args: ['--from', 'git+https://github.com/fungane/hermind@v1.0.0', 'hermind-mcp']
        env:
          HERMIND_DB: /home/you/.hermind/memory.db
        toolCallTimeoutMs: 30000
```

Tools appear as `mcp__hermind__memory_search` and so on. Server `instructions` are added to the system prompt, so the core
block reaches the model. DSH removes credential-looking and `DSH_*` variables before starting the child (`HERMIND_*` and
`PATH` pass through) and starts a probe process before the serving one; both are harmless for a read-only server.

### Other clients

Most clients use this `mcpServers` shape (add `"--write"` to `args` to allow writing):

```json
{
  "mcpServers": {
    "hermind": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/fungane/hermind@v1.0.0", "hermind-mcp"],
      "env": { "HERMIND_DB": "/home/you/.hermind/memory.db" }
    }
  }
}
```

| Client | Where it goes |
|---|---|
| **Cursor** | `~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project) |
| **Cline** | `cline_mcp_settings.json`: in the IDE extension use the MCP Servers icon, then *Configure MCP Servers*; the CLI keeps it at `~/.cline/data/settings/cline_mcp_settings.json` |
| **Antigravity CLI** (`agy`, Google's replacement for Gemini CLI) | `~/.gemini/config/mcp_config.json` (all projects) or `.agents/mcp_config.json` (one project), under `mcpServers`; separate from `settings.json`. Its App and IDE use the same format, but their global path was not checked |
| **Gemini CLI** (legacy; no longer served for Google AI Pro/Ultra and free accounts since 2026-06-18; API-key and Code Assist Standard/Enterprise users can keep it) | `~/.gemini/settings.json`, under `mcpServers` |
| **Claude Desktop** (chat app, not Claude Code) | `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows); restart the app. The app's own route for packaged servers is Settings → Extensions (`.mcpb` files); the config-file route was not re-checked on the newest build |
| **Codex / ChatGPT desktop app** (CLI, IDE extension and the desktop app share one file) | `~/.codex/config.toml` (TOML, below) or `codex mcp add hermind -- uvx --from git+https://github.com/fungane/hermind@v1.0.0 hermind-mcp` |
| **Windsurf** (now Devin Desktop) | `~/.config/devin/mcp_config.json` (older versions: `~/.codeium/windsurf/mcp_config.json`) |
| **Qwen Code** | `mcpServers` in `settings.json`, or `qwen mcp add` |
| **Amp** | `amp.mcpServers` in `~/.config/amp/settings.json` (same `command` / `args` / `env` fields) |
| **Goose** | `goose configure` → *Add Extension* → *Command-line Extension*, with `uvx` as the command and the arguments above |
| **WorkBuddy** (Tencent) | `~/.workbuddy/mcp.json` (user) or `<project>/.workbuddy/mcp.json`, `mcpServers` shape; or Plugins → MCP Servers → *Configure MCP* in the app |
| **CodeBuddy** (Tencent) | `~/.codebuddy/mcp.json` (global) or `.mcp.json` in the project, `mcpServers` shape |
| **Kiro** (IDE and Kiro CLI) | `~/.kiro/settings/mcp.json` (user) or `.kiro/settings/mcp.json` (workspace), `mcpServers` shape; the CLI also has `kiro-cli mcp add` |
| **Trae** | Settings → MCP → Add → manual (raw JSON), paste the `mcpServers` JSON; for one project, `.trae/mcp.json` |
| **Warp** | Settings → Agents → MCP servers → *Add*, paste the `mcpServers` JSON |
| **Cherry Studio** (version 2) | Settings → MCP → MCP Servers → Add → *Import from JSON*, paste the `mcpServers` JSON |
| **LobeChat** (desktop app; the self-hosted web version only reaches remote servers) | Settings → MCP plugins, a stdio server with `uvx` as the command |
| **OpenClaw** | `mcp.servers.hermind` in `openclaw.json` (`command`, `args`), or `openclaw mcp set` |

Orca runs Claude Code, Codex and other agents side by side in git worktrees. It is not an MCP client itself: set Hermind
up in the agent you launch inside it. Any app with an "add MCP server" screen that asks for command, args and env wants the
shape above.

These clients use a different shape:

```jsonc
// VS Code / GitHub Copilot: .vscode/mcp.json (workspace) or your user profile
{ "servers": { "hermind": { "type": "stdio", "command": "uvx",
    "args": ["--from", "git+https://github.com/fungane/hermind@v1.0.0", "hermind-mcp"] } } }

// Zed: settings.json
{ "context_servers": { "hermind": { "command": "uvx",
    "args": ["--from", "git+https://github.com/fungane/hermind@v1.0.0", "hermind-mcp"], "env": {} } } }

// OpenCode: opencode.json or opencode.jsonc (command is one array, env is called "environment");
// `opencode mcp add` writes the same block
{ "mcp": { "hermind": { "type": "local",
    "command": ["uvx", "--from", "git+https://github.com/fungane/hermind@v1.0.0", "hermind-mcp"],
    "environment": { "HERMIND_DB": "/home/you/.hermind/memory.db" } } } }
```

```json5
// OpenClaw: openclaw.json
{ mcp: { servers: { hermind: { command: "uvx",
    args: ["--from", "git+https://github.com/fungane/hermind@v1.0.0", "hermind-mcp"] } } } }
```

```yaml
# Continue: .continue/mcpServers/hermind.yaml (agent mode only). Continue is no longer maintained
# (final release 2.0.0); the snippet is for people who still run it.
name: Hermind
version: 0.0.1
schema: v1
mcpServers:
  - name: hermind
    command: uvx
    args: ["--from", "git+https://github.com/fungane/hermind@v1.0.0", "hermind-mcp"]
```

```toml
# Codex / ChatGPT desktop app: ~/.codex/config.toml
[mcp_servers.hermind]
command = "uvx"
args = ["--from", "git+https://github.com/fungane/hermind@v1.0.0", "hermind-mcp"]

[mcp_servers.hermind.env]
HERMIND_DB = "/home/you/.hermind/memory.db"
```

### What was tested

| Tested | How |
|---|---|
| Claude Code | The server definition was run with `claude -p --mcp-config ... --strict-mcp-config` and a real search; the `claude mcp add` command itself was not run |
| The protocol | The official MCP TypeScript SDK (`@modelcontextprotocol/client` 2.2.0, the SDK DeepSeek Harness's MCP client depends on) connected over stdio in all three negotiation modes: legacy (`initialize`, 2025-06-18), auto (probes `server/discover`, negotiated 2026-07-28) and pinned to 2026-07-28. Each time the instructions with the core block arrived, four tools were listed, a search returned notes and a write call was refused |
| ssh | A real `sshd` on localhost (see below); not across the internet and not from Windows |

Every other client snippet follows that client's documentation and has not been run. The DSH YAML comes from its docs and a
user-reported working configuration; it was not run through `dsh`. If one of these works or fails for you, the client-report
issue form is how it moves from "untested" to "tested".

### One memory for all agents

Every process opens the same SQLite file (WAL mode, 5 s busy timeout), so point them all at one `HERMIND_DB`. To share notes
with the Hermes plugin, use the plugin's database (default `~/.hermes/hermind.db`) or `--db-from-profile NAME`. The plugin
reads and writes the `scope` it is configured with (`default` unless you set `plugins.hermind.scope`, which may contain
`{profile}`) and also sees `shared`. Run the MCP server with the same `--scope`: a note in another scope is invisible to
search, list, related and get-by-id.

### Limits of the MCP server

- Tools only: the agent has to call `memory_search` itself; there is no automatic recall and no hook (but see
  [Automatic recall in Claude Code](#automatic-recall-in-claude-code)).
- Every session gets a short core block (your pinned notes and most trusted preferences, fenced as data) in the server
  `instructions`. Clients that put server instructions into the system prompt start knowing it; Claude Code does (checked),
  others are untested. The default is 1,200 characters because Claude Code caps server instructions at about 2 KB.
- Auto-capture, consolidation at session start and the nightly tools are not run by the server. Expired notes are purged
  and new notes consolidated once, before the first write call.
- Recalled notes are returned as plain text for the agent to read, never executed; results are capped at about 20,000
  characters.

### Notes on another machine (over ssh)

An MCP client reads a database file, so it must run where the file is. When Hermes and its notes live on a server and you
use an agent on your own computer, let the client start the server over ssh instead of opening a port:

```bash
hermind config chatgpt-desktop --remote me@203.0.113.7 --remote-hermes-profile default
```

The printed entry is `ssh -T -o BatchMode=yes me@203.0.113.7 'env HERMIND_DB=... python3 ~/hermind/hermind_mcp.py'`. It
needs a key that logs in without a password and a Hermind checkout on the server (`--remote-checkout`, default
`~/hermind`). Nothing listens on the network and nothing new runs on the server between sessions.

**A key that can only run Hermind (recommended).** Do not hand an agent your login key. On the machine that holds the
notes:

```bash
hermind authorize-key --generate ~/.ssh/hermind_mcp --profile default
```

It prints one line for `~/.ssh/authorized_keys`, `restrict,command="python3 .../hermind_mcp.py --db ..." ssh-ed25519 ...`.
With it the key cannot open a shell, forward ports or run any other command, and it is read-only unless you add `--write`.
Copy the private key to the other machine and use `hermind config CLIENT --remote USER@HOST --restricted --ssh-key FILE`.
Checked against a real `sshd` on localhost: reading works, the client's own command is ignored, writes are refused, remote
port forwarding is refused. Hermind prints the line and never edits `authorized_keys` itself.

**Not supported:** two-way merging of two databases, and an HTTP MCP server (it would need its own authentication). Never
sync the database file itself with Syncthing or Dropbox: SQLite's `-wal` file makes that unsafe. Use
[`tools/backup.py`](import-export.md#backup).

### Automatic recall in Claude Code

An MCP server only answers when the agent decides to ask. Claude Code can also run a command before every prompt, and
Hermind uses that to look the prompt up for you and hand the model the matching notes.

```bash
hermind install-hook            # preview: shows the change and runs the hook once as Claude Code would
hermind install-hook --write    # applies it (saves a backup of settings.json first)
```

Without `--db` the hook reads the MCP server's database (`~/.hermind/memory.db`); `--db FILE` or `--db-from-profile NAME`
(a Hermes profile's database) picks another. `--source` sets how Claude Code starts the hook: `checkout` (this clone),
`pipx` (an installed `hermind-hook` command, from pip, pipx or `uv tool install`) or `git` (uvx from the release tag). The
default follows how you ran `install-hook`; a one-off `uvx --from` run gets `git`, because its command vanishes when it ends.
Restart Claude Code afterwards. Undo with `install-hook --remove --write`; only the entry that runs `hermind-hook` is
touched.

- Runs `hermind_hook.py` on every `UserPromptSubmit`, usually in 0.1 to 0.3 s, read-only: it writes nothing, not even usage
  counts.
- Same rules as the Hermes plugin: filler prompts (`ok`, `thanks`), slash commands and prompts with no topic get nothing;
  the result is capped at about 1,200 characters; notes are fenced as data and each gets up to 200 characters.
- Text inside `<private>...</private>` is never searched.
- Never blocks and never fails a prompt: with no database, bad input or any error it prints nothing and exits 0.
  `HERMIND_HOOK=off` turns it off; `HERMIND_HOOK_DEBUG=1` prints the reason on stderr.
- Lexical search only: starting the semantic worker on every prompt would add about a second.

Checked with the real Claude Code CLI (tools off): without the hook the model said it did not know; with the hook it
answered from the recalled note. Other agents with a similar hook (Codex has a `UserPromptSubmit` hook that can return
`additionalContext`; Antigravity CLI has `PreInvocation`; Cline) could call the same command, but that is untested.

## Hermes plugin

Needs Hermes with the memory-provider plugin API. Install as in [Install](#install); that is the whole zero-dependency
setup (lexical search, rule-based capture, no network). Restart the Hermes gateway and the desktop app afterwards.
`hermind install-hermes --rollback` undoes it.

### Coming from Hindsight

Hindsight is a memory service for Hermes. Two scripts move a bank's facts and observations into a new Hermind database;
both are read-only on the Hindsight side and never delete anything there:

```bash
python3 tools/export_hindsight.py BANK bank.json [--url URL]   # default: the local API on port 9177; GET requests only
python3 import_hindsight.py bank.json NEW.db      # NEW.db is overwritten: never point it at a database you use
```

Entities and the graph are rebuilt, near-duplicates are merged (nothing is deleted), and kinds are guessed from the text.
Check the result with `hermind search --db NEW.db ...`, then set `db_path` to it, or merge notes in with
[`hermind export` and `hermind import`](import-export.md). To go back, set `memory.provider` to the previous provider.

### Tools the agent gets

`memory_remember` · `memory_search` (optional `queries` rewrites, filters, `detail=brief|more`, `as_of`) · `memory_get` ·
`memory_reflect` · `memory_related` (`mode="timeline"`, `as_of`) · `memory_update` · `memory_feedback` · `memory_forget` ·
`memory_list` · `memory_review` (list, confirm, reject, restore LLM-extracted notes). `memory_remember` takes `valid_from`
and `valid_to`. The system-prompt block tells the model to store 5 to 10 alternative phrasings with each note, to pass one or
two rewrites when a question is casual, and to open results with `memory_get`.

### Settings

Set under `plugins.hermind` in the profile's `config.yaml`, with `hermes config set plugins.hermind.KEY VALUE` (do not
hand-edit `config.yaml`).

| Key | Default | Meaning |
|---|---|---|
| `db_path` | `$HERMES_HOME/hermind.db` | database file |
| `auto_capture` | `true` | store turns after each reply |
| `skip_sources` | `cron` | session sources that are never captured |
| `prefetch_limit` / `prefetch_chars` | `4` / `1200` | notes / characters injected per turn |
| `prefetch_vectors` / `prefetch_vector_timeout` | `true` / `3.0` | fuse embedding hits into automatic recall (lexical only on failure or timeout) |
| `prefetch_min_weight` | `1.0` | skip automatic recall for prompts with no topic word (filler only); `0` disables |
| `extract_merge_similarity` | `0.5` | an extracted fact this close to an existing note refreshes that note instead of adding a copy; `0` disables |
| `review_queue` | `true` | LLM-extracted notes start as `[pending]` until confirmed with `memory_review` |
| `core_summary_chars` | `3500` | size cap of the pinned-notes / preferences block in the system prompt; `0` turns it off |
| `open_rate` | `true` | count shown / opened notes and apply the ±10 % open-rate prior; `false` disables both |
| `scope` | `default` | `{profile}` / `{platform}` placeholders allowed |
| `dedup_threshold` | `0.85` | near-duplicate similarity |
| `precompress_capture`, `mirror_builtin` | `true` | pick up facts before compression; mirror built-in MEMORY.md / USER.md |
| `phrasing_languages` | *(user's languages)* | languages the model should write alias phrasings in |
| `llm.*` | off | `enabled`, `rerank`, `capture_extract`, `base_url`, `model`, `api_key_env`, `timeout`, `extra_body`, `rerank_candidates`, `cooldown`, `pii` (`redact` default / `block` / `off`), `redact_patterns` (your own regexes) |
| `vectors.*` | off | `enabled`, `profile` (`default`/`light`/`balanced`/`quality`), `python`, `model`, `tokenizer`, `idle`, `candidates`; advanced: `model_name`, `query_prefix`, `passage_prefix`, `pooling`, `sec_per_note` |

### LLM rerank and fact extraction

```bash
hermes config set plugins.hermind.llm.enabled true
hermes config set plugins.hermind.llm.rerank true
hermes config set plugins.hermind.llm.capture_extract true
hermes config set plugins.hermind.llm.base_url https://api.deepseek.com/v1
hermes config set plugins.hermind.llm.model deepseek-flash
hermes config set plugins.hermind.llm.api_key_env DEEPSEEK_API_KEY   # the variable's NAME, never the key
hermes config set plugins.hermind.llm.extra_body '{"thinking":{"type":"disabled"}}'
```

Any OpenAI-compatible `/chat/completions` endpoint works. On every rerank the search text and about 20 short candidate
snippets are sent to that provider; leave `llm.enabled` off if that is not acceptable (see
[what is sent](privacy.md#where-your-data-goes)). A circuit breaker skips the endpoint for `cooldown` seconds after three
failures.

### Several users (profiles)

Every Hermes profile has its own database: its own notes, entities, tombstones and trust scores. Nothing is shared and
there is no cross-profile query. That is the boundary; `scope` and `tags` only separate things inside one profile. Files are
mode 600, but anyone who can log in to the same OS account can read every profile's file.

## Semantic search

Optional. Without it, search is lexical with synonym expansion and finds notes that share words with the question; the
semantic layer adds recall when they do not (see [how well it recalls](how-it-works.md#how-well-does-it-recall)).

```bash
hermind setup-vectors balanced                                   # private venv + model in ~/.hermes/hermind-vec
hermind embed-all --db ~/.hermes/hermind.db --profile balanced   # embed existing notes
hermes config set plugins.hermind.vectors.enabled true
hermes config set plugins.hermind.vectors.profile balanced
```

For the MCP server, pass a config file: `hermind-mcp --config vectors.json` with
`{"vectors": {"enabled": true, "profile": "balanced"}}` in it. From a checkout, `tools/setup_vectors.sh balanced` and
`tools/embed_all.py` do the same as the two commands.

`setup-vectors` needs the network once (PyPI for the worker's packages, Hugging Face for the model); every model is
pinned to a revision and a checksum. `--dry-run` shows what would be downloaded and where. Everything goes into one
folder (`$HERMIND_VEC_HOME` to choose another), with no cache elsewhere, so deleting it removes the semantic layer
completely. onnxruntime's official builds send usage telemetry to Microsoft by default; the worker runs with
`ORT_DISABLE_TELEMETRY=1`, so nothing is sent.

Pick one model profile. All run locally on the CPU. Each note keeps one vector, so switching replaces the old ones: run
`setup-vectors` and `embed-all` again, and going back means embedding again (back up first with
[`tools/backup.py`](import-export.md#backup)). Measure on your own notes before switching.

| Profile | Model | File | RAM while awake | Embedding 5,000 notes* |
|---|---|---|---|---|
| `default` | multilingual-e5-small (int8) | 118 MB | ~480 MB | ~8 min |
| `light` | bekko-static-embedding-v1 | 52 MB | ~160 MB | seconds |
| `balanced` | hs-embed-1 (Hindsight, MIT) | 152 MB | ~600 MB | ~3 min |
| `quality` | bge-m3 (int8) | 568 MB | ~910 MB | ~30 min |

\* On a 4-core server. The worker stops after 5 idle minutes and frees its memory. `default` stays the default so existing
installs keep their vectors. `quality` needs a longer wait per batch, which the profile already sets (`sec_per_note`).
If the worker runs against an encrypted database, install `sqlcipher3` in its venv too (see
[Encryption](privacy.md#encryption)). Semantic search is not supported on Windows yet ([limitations](limitations.md)).

## Optional LLM tools

Offline scripts in `tools/` (from a checkout or the installed plugin folder). None runs by itself, nothing in the plugin
calls them and Hermind installs no cron job. The LLM ones send note text to the endpoint you name and cost tokens; they take
`--base-url`, `--model` and `--api-key-env` (the variable's name, never the key). For DeepSeek, add
`--extra-body '{"thinking": {"type": "disabled"}}'` or most batches time out. Back the database up first.

| Tool | Needs an LLM | What it does |
|---|---|---|
| `tools/backfill_aliases.py --db DB` | yes | Writes alternative phrasings for old notes (one call per batch of notes; the model never sees questions). `--dry-run`, `--kinds`, `--limit`, `--since YYYY-MM-DD\|last`, `--rebuild`. Never on the search path |
| `tools/build_summaries.py --db DB` | yes | Per-entity consolidated summaries. `--stale-only` for incremental runs, `--entities`, `--dry-run`, `--price-in/--price-out` for a cost estimate |
| `tools/sleep_consolidate.py --db DB` | yes | Nightly review of the last day's notes (below) |
| `tools/usage_report.py DB [--days N]` | no | How recall is doing in real use, including the effect of the open-rate prior |
| `tools/recall_eval.py ROOT DB_COPY --set YOUR_SET.json [--show] [-k K] [--no-vectors]` | no | Recall quality of the automatic recall path on a database copy: hit, recall and precision at 4 on your hand-labelled prompts, and how often topicless prompts recall nothing. Format: `tools/recall_eval_set.example.json`. Labels refer to your own note ids; keep the set out of the repository |
| `tools/mine_eval.py DB [--out FILE]` | no | Turns your recall history (`recall_shown`, the 200-character queries kept in `usage_log` for 90 days) into an eval set and shows how often the opened note was ranked first. Read-only; the output file is mode 600 and refused inside a git checkout |

### Sleep-time consolidation

For the notes created in the last `--since-hours` (default 24, at most `--limit` 30) the model sees the recent notes plus a
few older, similar ones and may propose:

| Proposal | Effect when applied |
|---|---|
| **merge** | the redundant note is hidden behind the kept one (`status='merged'`), as in the built-in consolidation |
| **supersession** | the outdated note gets `valid_to` and leaves the default search; the newer one gets `valid_from`; `memory_search as_of=...` still finds it. The model is told to propose it only when old and new cannot both be true now, but it can get this wrong: read the dry-run reports before `--apply` |
| **aggregate fact** | a new note with `review='pending'` citing its source ids, e.g. `(綜合 #12 #15)`; it ranks lower until confirmed with `memory_review` |

Nothing is deleted. The code enforces these guards; the model is not trusted to follow them: every id must be a note the
model was shown; a merge may not lose a number; a figure in an aggregate must occur in a cited source; an aggregate needs
two real sources; pinned, locked and summary notes are never hidden; a pending note never hides a confirmed one; secrets
and forgotten text are refused; at most `--max-apply` (20) changes are made per run.

```bash
# 1. dry run (the default): prints the report, changes nothing
python3 tools/sleep_consolidate.py --db ~/.hermes/hermind.db --llm-config ~/.hermes/hermind-llm.json
# 2. apply what the guards accepted
python3 tools/sleep_consolidate.py --db ~/.hermes/hermind.db --llm-config ~/.hermes/hermind-llm.json --apply
# 3. undo a merge or supersession (ids are in the report); reject an unwanted aggregate with memory_review
python3 tools/sleep_consolidate.py --db ~/.hermes/hermind.db --restore 45 52
```

LLM settings use the same fields as `plugins.hermind.llm`, but the tool does not read `config.yaml`: put them in a small
JSON file (`{"base_url": "...", "model": "...", "api_key_env": "NAME_OF_ENV_VAR", "extra_body": {...}}`), or use
`--llm-base-url/--llm-model/--llm-api-key-env`, or `HERMIND_LLM_BASE_URL/HERMIND_LLM_MODEL/HERMIND_LLM_API_KEY_ENV`. With no
endpoint and model it prints a message and exits 0. The key is read from the environment variable and never printed or
stored. Exit code 0 also means the model was unreachable or nothing qualified, so a failing endpoint never makes cron noisy;
2 means the database file was not found.

Example cron entry (not installed by Hermind): back up, then review the last 24 hours, and keep a log. Run a week of dry
runs first (drop `--apply`) and read the reports.

```cron
30 3 * * *  cd ~/.hermes/plugins/hermind && python3 tools/backup.py ~/.hermes/hermind.db && \
            python3 tools/sleep_consolidate.py --db ~/.hermes/hermind.db \
            --llm-config ~/.hermes/hermind-llm.json --since-hours 24 --limit 30 --apply \
            >> ~/.hermes/logs/sleep_consolidate.log 2>&1
```
