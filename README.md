# UnfoldX

> Delegate explainer videos to the UnfoldX main agent and follow them to the finished film.

Thin clients and the open API contract for [UnfoldX](https://unfoldx.ai): a CLI, an MCP
server and agent skills. No engine internals.

## Layout

```
├── cli/              # `unfoldx` CLI — stdlib-only HTTPS client (PyPI: unfoldx)
├── mcp/              # `unfoldx-mcp` — stdio MCP server over the same client (PyPI: unfoldx-mcp)
├── npm/              # `npx unfoldx-mcp install` — adds the server to local AI tools (npm: unfoldx-mcp)
├── skills/           # Agent skills (Claude Code etc.) wrapping the CLI
└── docs/             # CLI reference
```

## Quick start

```bash
pip install unfoldx
unfoldx doctor                     # credentials, connectivity, version
unfoldx login                      # device code, approved in the browser
unfoldx runs submit --goal "讲清楚量子纠缠" --file notes.pdf --json
unfoldx runs wait <run_id> --json
unfoldx runs answer <run_id> --route template --json
unfoldx runs wait <run_id> --json
unfoldx artifacts download <run_id>.video --out ./out --json
```

The public contract lives in `cli/src/unfoldx_cli/openapi.json` and is shipped with the stdlib-only CLI package and shared by MCP.

For MCP hosts, one command installs the server, logs in and adds it to Claude Code, Codex,
Cursor, Gemini CLI, Qwen Code and Kimi Code: `npx -y unfoldx-mcp install` (see `npm/README.md`).
By hand: install `unfoldx-mcp` (`uv tool install unfoldx-mcp --with-executables-from unfoldx`, which also installs the `unfoldx` CLI used to log in) and register `unfoldx-mcp` as a stdio
server; it uses the CLI's login. See `mcp/README.md`.

## Design principles

- **One contract.** The CLI and the MCP server are adapters over the same HTTP API
  and answer with the same `unfoldx.v2` envelope.
- **Agent-first, user-approved.** Agents follow runs with resumable cursors and
  closed-set errors; spending above the credential's budget and publishing are
  confirmed by the user on the web.
- **Fail loud, leak nothing.** Errors are actionable but never expose server
  internals; tokens are redacted from every output path and bound to the API
  that issued them.
