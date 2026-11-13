# unfoldx-mcp (npm installer)

One command installs the [UnfoldX](https://unfoldx.ai) MCP server and adds it to your AI tools:

```bash
npx -y unfoldx-mcp install
```

It installs the `unfoldx` CLI and the `unfoldx-mcp` server from PyPI with
[uv](https://docs.astral.sh/uv/), logs you in once in the browser (skipped when you are
already logged in or pass `--key`), and registers the server as `unfoldx` in every
supported tool it finds:

| Tool | How it is added |
| --- | --- |
| Claude Code | `claude mcp add -s user` |
| Codex | `codex mcp add` |
| Gemini CLI, Qwen Code | `<tool> mcp add -s user` |
| Cursor | `~/.cursor/mcp.json` |
| Kimi Code | `~/.kimi-code/mcp.json` |

An existing `unfoldx` entry is replaced; other servers are left alone.

```bash
npx -y unfoldx-mcp install --client claude,cursor            # only these tools
npx -y unfoldx-mcp install --key "<API key>"                  # key from the Connections page, no browser login
npx -y unfoldx-mcp install --api-base https://dev.unfoldx.ai/api
npx -y unfoldx-mcp install --client none                      # install and log in only
```

Needs Node 18+ and uv (the installer prints the uv install command when it is missing).
The key is written only into the tools' MCP config and is never printed. Then restart
the tool and ask it to call `unfoldx_whoami`.

Tests: `cd npm && npm test`.
