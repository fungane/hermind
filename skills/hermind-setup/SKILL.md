---
name: hermind-setup
description: Use when asked to install Hermind. Asks where first.
---

# Hermind setup

The user said something like "install / deploy / set up / upgrade Hermind (the memory tool)". Do not install anything yet:
Hermind can go to a Hermes profile (CLI, desktop and gateway share one install, which belongs on the machine where the agent runs), to a command-line agent or a desktop app/IDE through MCP, or several, and the right answer depends on the user.

1. Find the checkout (`hermind_cli.py` next to `AGENTS.md`). If there is none, `git clone --branch v1.0.2 https://github.com/fungane/hermind.git`.
2. Run `python3 hermind_cli.py questions --json`. It lists the questions to ask, with choices taken from this machine
   (Hermes profiles and their versions, detected agents). Skip a question whose `when` does not match earlier answers.
3. Ask them in ONE question form (your clarify/ask tool), with the choices as options. Do not write the questions as prose.
   A profile belongs to a person: offer only profiles that exist, preselect nothing the user did not name.
4. Build the plan from the answers and show it without applying:
   - Hermes: `python3 hermind_cli.py install-hermes --profile NAME --dry-run` (omit `--profile` for default)
   - MCP: `python3 hermind_cli.py config CLIENT --source git [--db-from-profile NAME] [--write] --json` (`--source checkout`
     instead if the user keeps this clone and wants the client to run it); if the notes are on
     another machine than the agent (answer `shared-remote`): `config CLIENT --remote USER@HOST --remote-hermes-profile NAME`, after checking `ssh -o BatchMode=yes USER@HOST true`. Offer the locked key first (`authorize-key`, then `config ... --restricted`: see "Notes on another machine" in AGENTS.md); the user pastes the printed line into authorized_keys, you never edit it;
     `apply` in the output says whether to run a command, edit a file at `where`, or paste into the app's MCP screen.
5. Ask once: apply this plan? (yes / change something / cancel). Only on yes, run the real commands.
6. Finish as `AGENTS.md` step 4 says: `selftest`, tell the user to restart the gateway/app (you do not restart it), show the rollback.

Never: touch a profile the user did not name, enable encryption or the LLM layers unasked, or mark an untested
client as working.
