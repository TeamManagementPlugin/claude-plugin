# Contributing to team.management

Thanks for your interest in improving team.management. Bug reports, ideas, and
pull requests are all welcome.

By taking part, you agree to our [Code of Conduct](CODE_OF_CONDUCT.md).

## Ways to contribute

- **Report a bug** — open an issue with steps to reproduce, what you expected, and
  what happened. Include your OS, Python version, and Claude Code version.
- **Suggest a feature** — open an issue describing the problem you want solved. The
  motivation matters more than the solution.
- **Send a pull request** — for small fixes, go ahead. For anything larger, open an
  issue first so we can agree on the approach before you spend time on it.

## Development setup

team.management is a Claude Code plugin. To run your working copy inside Claude Code,
load the checkout directly:

```
claude --plugin-dir /path/to/claude-plugin/plugin
```

Or register the checkout as a local marketplace:

```
/plugin marketplace add /path/to/claude-plugin
```

The plugin builds its own isolated Python venv on first run, so there is nothing to
install by hand. See **[docs/INSTALL.md](docs/INSTALL.md)** for details.

## Running the tests

The test suite is Python and lives in `test/`. You need Python 3.10 or newer:

```
python3 -m pytest test/
```

Please add or update tests for any behavior change, and make sure the suite passes
before you open a pull request.

## Pull requests

- Keep each pull request focused on one change.
- Write a clear description: what changed, and why.
- Link the issue it addresses.
- Make sure `python3 -m pytest test/` passes.

## Code style

A few principles the codebase follows — please match them:

- **Locality of behavior** — keep related code close together rather than spread
  across layers of abstraction.
- **Minimal abstraction** — a plain function call beats a clever hierarchy.
- **Readability over cleverness** — code should be obvious to the next reader.

## Where things live

- `plugin/hooks/` — the hooks and the protocol engine
- `plugin/mcp/` — the MCP server and its tools
- `docs/` — install, usage, and MCP documentation
- `CLAUDE.md` — architecture overview and patterns

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
