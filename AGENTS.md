# Installing Hermind (runbook for AI agents)

You are an AI agent asked to set up Hermind for the user. Hermind is a long-term memory kept in one SQLite file on the
user's machine. Follow the steps in order. Every step ends with a check. If a check fails, stop and report the output
to the user word for word; do not improvise a workaround.

## Rules

1. **Ask before you change anything.** First a dry run, then the user's yes, then the real run.
2. **Never restart** Hermes, a gateway, a desktop app or an MCP client. Tell the user what to restart.
3. **One profile is one person.** Install only into the Hermes profiles the user names. Never read, copy or merge
   another profile's database.
4. **Optional layers stay off** unless the user asks for them: semantic search, LLM tools, encryption, writes from MCP
   clients, automatic recall hooks. Encryption is always its own task, never part of an install.
5. **Be honest about testing.** Only the `claude-code` client entry is marked `"tested": true`. For every other client,
   say the entry follows that client's documentation and has not been run.
6. Use `--json` on every command and read the fields named below. Exit codes: 0 success, 1 a check failed, 2 usage or
   environment error.

## 0. Get the code

```bash
git clone --branch v1.0.2 https://github.com/fungane/hermind.git ~/hermind && cd ~/hermind
```

The commands below are written as `python3 hermind_cli.py ...` from that folder. If Hermind was installed as a command
(pip, pipx, `uv tool install`), write `hermind ...`; with uvx only, `uvx --from git+https://github.com/fungane/hermind@v1.0.2 hermind ...`. The checkout needs only Python 3.10 or newer; MCP clients that use `--source git`
also need [uv](https://docs.astral.sh/uv/).

## 1. Look first

```bash
python3 hermind_cli.py doctor --json
```

This changes nothing. Before you go on, be able to say: the Hermind version here, which Hermes profiles exist and the
plugin version each one runs, which MCP clients were found, and what is in `problems` (an empty list is green).

## 2. Ask the user, in one form

If the user only said "install Hermind", you do not know where it should go. Run:

```bash
python3 hermind_cli.py questions --json
```

You get the questions, with choices built from this machine. Skip any question whose `when` does not match the answers
so far, and ask the rest together, in one form if your interface has one. They cover:

- **Target:** the Hermes agent (automatic recall and capture), an agent through MCP (it calls the memory tools itself),
  or several.
- **Where Hermes runs:** the plugin goes on the machine where the agent runs. If a desktop app connects to a remote
  server, install on that server over ssh; the user's own computer then needs nothing for Hermind.
- **Which profiles** (asked only when Hermes profiles exist), and, for MCP, whether to share a Hermes profile's database (`--db-from-profile NAME`) or use a
  separate one.
- **Write access** for MCP agents (read-only unless the user wants the agent to save notes).

The form does not ask about extras (semantic search, LLM tools, encryption, the Claude Code hook): they stay off unless the
user asks for one.

To have an agent follow this runbook by itself whenever it hears "install Hermind", run
`python3 hermind_cli.py install-skill --dry-run`, then run it again without `--dry-run`.

## 3a. Hermes agent

```bash
python3 hermind_cli.py install-hermes --profile NAME --dry-run --json    # leave out --profile for the default profile
python3 hermind_cli.py install-hermes --profile NAME --json              # backs up first and checks the result
```

If the dry run's output has `"skipped": true`, Hermes is not installed for this profile: stop here (do not run the
command again without `--dry-run`, it would just fail) and use 3b below instead, unless `--hermes-home` finds it
somewhere else.

This step is done when the output has `"ok": true` and `verify.files_identical` is true. When the profile already had a
database, `verify.opens_database_copy` must be true as well (the field is absent on a first install). If `ok` is false,
the old plugin was left alone or restored; report `error` word for word.

Then:

- If the profile did not use Hermind before, the user (or you, if they ask) runs
  `hermes config set memory.provider hermind` (for a named profile: `hermes -p NAME config set memory.provider hermind`).
  Note the provider it had before, so the user can switch back.
- Tell the user: "The new code loads after you restart the Hermes gateway and the desktop app. I did not restart
  anything." When the output has `rollback` (an upgrade: there was a plugin before), give them that command too.

Upgrading is the same command run from a checkout of the newer tag.

## 3b. MCP client

```bash
python3 hermind_cli.py config --list                                          # client names
python3 hermind_cli.py config CLIENT --source git --json                      # or --source checkout (no uv or network)
```

- **Where to put it:** add `snippet` to the file at `where`, merging with the entries that are already there, or follow
  `apply` when it is a command or a settings screen. Then the user restarts the client.
- **Same file, three clients:** the ChatGPT desktop app, the Codex CLI and the Codex IDE extension all read
  `~/.codex/config.toml`, so they share one entry.
- **Writes:** the server is read-only by default. Add `--write` only if the user wants the agent to save notes. With
  `--write`, notes are saved at once, except risky ones (text that looks like instructions, personal data, pin requests,
  edits of pinned notes). Those wait until the user runs `python3 hermind_cli.py review --confirm IDS`; the agent
  cannot confirm them itself.
- **Review options:** `--trust-writes` holds nothing back. `--review-all` is a server argument: add it to the snippet's
  `args` to hold every note.

### Automatic recall in Claude Code (optional, ask first)

On its own, the MCP server answers only when the agent asks it. A hook can look up every prompt. Preview it with
`python3 hermind_cli.py install-hook --db FILE --json` (or `--db-from-profile NAME`), which changes nothing. Show the
command to the user, and only then run it with `--write`. That keeps a backup of `~/.claude/settings.json`, leaves other
hooks alone, and `--remove --write` undoes it. The user restarts Claude Code afterwards.

### Notes on another machine (for example Hermes on a VPS, the agent on a laptop)

The client starts the server over ssh and talks to it through stdin and stdout. No port is opened and nothing new keeps
running on the server.

1. On the machine with the notes, make a locked key:
   `python3 hermind_cli.py authorize-key --generate ~/.ssh/hermind_mcp [--profile NAME] [--write]`. It prints one
   `authorized_keys` line that can start only the Hermind server: no shell, no forwarding, read-only unless `--write`.
   Show the line to the user and let them add it to `~/.ssh/authorized_keys` themselves; the command never edits that
   file.
2. The user copies the private key to the other machine.
3. There, run `python3 hermind_cli.py config CLIENT --remote USER@HOST --restricted --ssh-key FILE [--ssh-port N] --json`.
   The key fixes the database and the write mode, so `--db` and `--write` are refused with `--restricted`.
4. Check the login yourself with `ssh -o BatchMode=yes -i FILE USER@HOST true` before you hand over the snippet. The
   remote machine needs a Hermind checkout (`--remote-checkout`, default `~/hermind`).

Without a locked key, `--remote USER@HOST --remote-hermes-profile NAME` uses the user's normal login key. Prefer the
locked key.

## 4. Prove it works

```bash
python3 hermind_cli.py selftest --json
```

This step is done when the output has `"ok": true`. The self-test starts the server on a throwaway database, writes and
searches a note, and checks that the read-only default refuses writes. It never touches the user's database.

After a Hermes install, once the user has restarted, check the gateway log: it should contain
`Memory provider 'hermind' activated`. Any later line that mentions `hermind` together with an error, an
`EncryptionError`, or a `Traceback`, means something is wrong: report those lines.

## 5. Optional: semantic search (only if the user asked)

```bash
python3 hermind_cli.py setup-vectors balanced --dry-run --json   # light | default | balanced | quality
python3 hermind_cli.py setup-vectors balanced --json
python3 hermind_cli.py embed-all --db FILE --json                # gives the existing notes a vector
```

`--dry-run` shows the download size (`download_mb`), the RAM the model uses while running (`ram_while_awake`) and the
folder (`home`). Show that to the user before the real run. To switch it on, merge `config` from the output into the
Hermes plugin settings or the MCP server's `--config` file (the plain-text output says where), then the user restarts.
Not available on Windows.

## 6. Report to the user

Say what you installed and where, which database each route uses, what is still off, what they need to restart, and
the rollback command. If anything was red, report it word for word.

## Reference

| | |
|---|---|
| Uninstall, MCP | remove the client entry (`claude mcp remove hermind`); the notes stay in `~/.hermind/memory.db` until the user deletes it |
| Uninstall, Hermes | set `memory.provider` back to what the profile used before; the `rollback` command from `install-hermes` restores the previous plugin |
| Problem report | `python3 hermind_cli.py doctor --share` hides paths, user and profile names, so it is safe to paste into an issue |
| More detail | [docs/setup.md](docs/setup.md) (clients, settings), [docs/privacy.md](docs/privacy.md) (data, encryption), [docs/limitations.md](docs/limitations.md) |
