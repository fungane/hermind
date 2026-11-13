# Contributing

**Please open an issue, not a pull request.** Hermind reads and stores a person's notes for years, so code changes are
written only by the maintainer (with an AI assistant) and reviewed line by line. Pull requests from other people are not accepted
(the repository allows them only from collaborators); this is about
safety, not about your idea.

What helps most:

- **Bug reports** through the [issue form](https://github.com/fungane/hermind/issues/new/choose). Use `hermind doctor --share` for the environment, and
  reproduce the problem with made-up notes. Never paste your notes, a database, a key file or a screenshot of recalled notes.
- **Client reports**: an MCP client changed where it keeps its configuration, or an entry from `hermind config CLIENT`
  ([docs/setup.md](../docs/setup.md#mcp-clients)) does not work.
- **Ideas**, with the problem you had rather than the code you would write.
- **Security problems**: use the private advisory described in [SECURITY.md](SECURITY.md), not a public issue.

Replies can take a few days: issues are first looked at by an AI assistant that may draft a fix in a sandbox, and the
maintainer reads and sends every reply. Linux and macOS are tested before every release; Windows is not supported yet.

## Running the tests

From a checkout: `uvx pytest -q` (needs [uv](https://docs.astral.sh/uv/)), or without it,
`python -m pip install pytest pyyaml && python -m pytest -q`. Setting `HERMIND_INSTALL_SMOKE=1` also runs an install
smoke test that builds and installs the package the way a newcomer does and walks the README's MCP-only route against
it. With `uv` on `PATH` it needs the network the first time (uv fetches the build backend); without `uv` it falls back
to `pip install --target`, which needs setuptools 77 or newer in the Python you run it with.
