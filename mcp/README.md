# unfoldx-mcp

A stdio MCP server that exposes the UnfoldX public API (`unfoldx.v2`) as MCP tools.
It reuses the `unfoldx` CLI's API client and credential store. Log in once with the
CLI (`unfoldx login`); the server reads that credential for the same API base on
every call, so it picks up a new login without a restart. It never prints the token.

```bash
uv tool install unfoldx-mcp --with-executables-from unfoldx   # or: pip install unfoldx-mcp
unfoldx login                          # approve in the browser
claude mcp add unfoldx -- unfoldx-mcp  # any MCP host that launches stdio servers
codex mcp add unfoldx -- unfoldx-mcp   # Codex
qwen mcp add -s user unfoldx unfoldx-mcp
gemini mcp add -s user unfoldx unfoldx-mcp  # without -s user it writes the project's .gemini/settings.json
```

Kimi Code reads `~/.kimi-code/mcp.json` (`mcpServers`, the same JSON as Cursor). DeepSeek Harness (`dsh`)
mounts MCP servers as `@deepseek-ai/dsh-mcp-client` rows in `~/.dsh/cordis.patch.yml`
(`- insert: [{id: mcp-unfoldx, name: '@deepseek-ai/dsh-mcp-client', config: {serverName: unfoldx, transport: stdio, command: unfoldx-mcp}}]`).
To check a host, ask it to call `unfoldx_whoami`; it should name your credential.
Every run and workspace carries `web_url`; give it to the user so they can watch the run on the web.

Every tool is a short call and returns the same envelope as the HTTP API and
`unfoldx --json`, as both `structuredContent` and JSON text:
`{"schema":"unfoldx.v2","ok":true,"data":…}`. Business failures (not logged in,
missing scope, not found, …) come back with `isError: true` and
`{"ok":false,"error":{type,code,message,retryable,…}}`. A run waiting for an answer
is a normal state, not an error; its question carries `approval_url` for the user.
`unfoldx_run_submit` requires a `command_id` chosen and saved **before** the call
(8–128 letters, digits, `.`, `_`, `:`, `-`). Reuse it verbatim when retrying. On a lost
response, query `unfoldx_command_get` with that id; never generate a replacement id to retry.

Read each card’s `answer_schema` and `answer_examples` before answering. Each tool advertises
and validates its actual response schema from `unfoldx_cli/openapi.json`, packaged with the `unfoldx` dependency;
the tool adapter does not maintain a second response model. Workspace listing supports
`limit`, `cursor`, `query` and `state`; keep filters unchanged when following `next_cursor`.

| Tool | Does |
| --- | --- |
| `unfoldx_material_upload`, `unfoldx_material_get` | upload a local file as material; its state |
| `unfoldx_run_submit`, `unfoldx_command_get` | send the main agent a message (new or follow-up); find a run by `command_id` |
| `unfoldx_run_answer`, `unfoldx_run_cancel` | answer a card within the budget (else `approval_required` + `approval_url`); stop a run |
| `unfoldx_capabilities`, `unfoldx_whoami` | what this credential may do, and the skill catalog the web lists |
| `unfoldx_workspaces_list`, `unfoldx_workspace_get` | conversations and their runs |
| `unfoldx_run_status`, `unfoldx_run_events`, `unfoldx_run_questions`, `unfoldx_run_result`, `unfoldx_run_usage` | one run |
| `unfoldx_artifact_get`, `unfoldx_artifact_access` | finished films; `access` issues a one-hour download link |

JSON Schema validation uses jsonschema; the standard JSON loader reads the sole packaged OpenAPI contract.
The SDK is pinned (`mcp==2.3.0`) and is a dependency of this package only; the CLI stays
stdlib-only. Tests: `cd mcp && uv run pytest tests`.


## Resource tools (0.6.0)

Resource tool descriptions, scopes, input and output schemas come from `unfoldx_cli/openapi.json`, bundled once
with the CLI dependency. There is no duplicate YAML or generated return-model registry. `tools/list` includes
materials, works/exports, catalogs, audition/cover jobs, public shares, publishing accounts/receipts,
brands/assets, preferences/pronunciations, and separate superfast jobs. Each tool states its required scope.
Personal memory, deletes and publishing-platform logins are not exposed; manage them in the UnfoldX web app.
Publishing, sharing and other public actions without their scope answer `insufficient_scope` with
`error.details.approval_url`, where the user confirms in the web app.
Writes with side effects do not automatically retry unless their existing service provides idempotency. Destructive
operations require `confirm=true`. Missing scopes are server-enforced even if a host ignores tool annotations.

`unfoldx_work_export` writes local files and shares the CLI's atomic no-clobber behavior; `overwrite=true` is an
explicit replacement. Derived audio requires ffmpeg. Other media tools return signed access links; never log them.
`unfoldx_superfast_submit` requires a saved command_id and opt-in scope; billing is not_metered, not free. Poll
`unfoldx_superfast_status` with a bounded interval. No automatic fallback from ordinary runs to superfast.

For a smaller context surface, run `unfoldx-mcp --toolsets core,materials,works,catalogs`; add domains as needed.
`--read-only` both removes mutating tools from discovery and rejects direct calls to them. This is a host
configuration boundary in addition to server-side credential scopes, not a substitute for authorization.

Superfast status exposes `duration`: measured seconds, target seconds and delta. A warning is a quality fact,
not a failed job. Report any deviation clearly; only an explicit caller submission edits the film.
