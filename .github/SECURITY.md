# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's **Report a vulnerability** button on the
repository's Security tab (private advisory). Include what you did, what happened and what you expected. This is a
one-person project: expect a first reply within about a week, and a fix or a written explanation after that.

## What counts as a vulnerability

- Changing notes through the MCP server when it was started without `--write`, or reading notes through it beyond what
  its tools return.
- A way to make the server or plugin write outside the configured database folder, or run code from stored text.
- Note text, credentials or usage data leaving the machine other than to an LLM endpoint you configured, or any network
  connection besides that endpoint and the model download you started with `hermind setup-vectors`.
- A credential pattern that the screen should have caught and did not (fixed in `screen.py`, with a test).
- Permissions on the database or its folder looser than documented in [docs/privacy.md](../docs/privacy.md).

## Out of scope

- Anything that needs an attacker who can already read your files or run code as your user.
- The database being plaintext when you have not turned encryption on (documented; see [docs/privacy.md](../docs/privacy.md#encryption)).
- Someone who can read both the database and its key file or environment.
- What an MCP client or its model provider does with notes you let it read.
- Prompt injection that depends on the agent ignoring the "notes are not instructions" reminder (reduced, not eliminated).
