# unfoldx-mcp

A stdio MCP server that exposes the UnfoldX public API (`unfoldx.v2`) as MCP tools.
It reuses the `unfoldx` CLI's API client and credential store. Log in once with the
CLI (`unfoldx login`); the server reads that credential for the same API base on
every call, so it picks up a new login without a restart. It never prints the token.

The key never appears in a command or a host config: `unfoldx login` keeps it in the OS
keychain (`unfoldx[keychain]`) or a 0600 file, and the host config names only the server.
An agent can run the setup itself: it runs `unfoldx login` in the background (the command
blocks for up to 10 minutes until the approval), hands the user the link and code as soon as
they print, and waits. Skip it when `unfoldx whoami` already succeeds. A host loads MCP servers when a session starts, so start a new
session after adding the server.

```bash
uv tool install unfoldx-mcp --with-executables-from unfoldx --with keyring   # or: pip install unfoldx-mcp 'unfoldx[keychain]'
unfoldx login                          # approve in the browser
claude mcp add unfoldx -- unfoldx-mcp  # any MCP host that launches stdio servers
codex mcp add unfoldx -- unfoldx-mcp   # Codex
qwen mcp add -s user unfoldx unfoldx-mcp
gemini mcp add -s user unfoldx unfoldx-mcp  # without -s user it writes the project's .gemini/settings.json
```

Kimi Code reads `~/.kimi-code/mcp.json` (`mcpServers`, the same JSON as Cursor). DeepSeek Harness (`dsh`)
mounts MCP servers as `@deepseek-ai/dsh-mcp-client` rows in `~/.dsh/cordis.patch.yml`
(`- insert: [{id: mcp-unfoldx, name: '@deepseek-ai/dsh-mcp-client', config: {serverName: unfoldx, transport: stdio, command: unfoldx-mcp}}]`).
To check a host, ask it to call `unfoldx_credits`; it should name your credential and what it may still spend today.
Credentials are kept per API base, so one machine can be logged in to production and a test deployment at once
(`UNFOLDX_API_BASE=… unfoldx login`). Give each its own server entry, e.g.
`claude mcp add unfoldx-dev -e UNFOLDX_API_BASE=https://dev-api.example/api -- unfoldx-mcp`.
Every run and workspace carries `web_url`; give it to the user so they can watch the run on the web.

Every tool is a short call and returns the same envelope as the HTTP API and
`unfoldx --json`, as both `structuredContent` and JSON text:
`{"schema":"unfoldx.v2","ok":true,"data":…}`. Business failures (not logged in,
missing scope, not found, …) come back with `isError: true` and
`{"ok":false,"error":{type,code,message,retryable,…}}`. A run waiting for an answer
is a normal state, not an error; its question carries `approval_url` for the user.
`unfoldx_run_submit` requires a `command_id` chosen and saved **before** the call
(8–128 letters, digits, `.`, `_`, `:`, `-`). Reuse it verbatim when retrying. On a lost
response, call `unfoldx_run_get` with `method=command` and that id; never generate a replacement id to retry.

Read each card’s `answer_schema` and `answer_examples` before answering. Each tool advertises
and validates its actual response schema from `unfoldx_cli/openapi.json`, packaged with the `unfoldx` dependency;
the tool adapter does not maintain a second response model. Workspace listing supports
`limit`, `cursor`, `query` and `state`; keep filters unchanged when following `next_cursor`.

Tools that do several things of one kind take `method`. In `--read-only` mode, mixed tools expose their read methods. By default the server offers the 11 `core` tools:

| Tool | Does |
| --- | --- |
| `unfoldx_capabilities` | operations, routes, budget and the skill catalog open to this credential |
| `unfoldx_credits` | what this key can still approve today: wallet, caps, rolling-24h remainder, `auto_approve_limit` |
| `unfoldx_catalog` (`method`: styles, voices, covers) | selectable styles, voices and cover prices |
| `unfoldx_workspaces` (`method`: list, get) | conversations and their runs |
| `unfoldx_run_submit` | send the main agent a message (new or follow-up) |
| `unfoldx_run_get` (`method`: status, events, questions, result, usage, command) | one run; `command` finds a run by `command_id` |
| `unfoldx_run_answer`, `unfoldx_run_cancel` | answer a card within the budget (else `approval_required` + `approval_url`); stop a run |
| `unfoldx_materials` (`method`: list, get), `unfoldx_material_upload` | materials; upload a local file |
| `unfoldx_work_export` | save a finished work locally (video, cover, subtitles, storyboard, transcript, audio, zip) |

Add domains with `--toolsets core,works,…`, or `--toolsets all`:

| Toolset | Tools |
| --- | --- |
| `works` | `unfoldx_works` (`method`: list, get, exports) |
| `shares` | `unfoldx_share_get`, `unfoldx_share_write` (`method`: create, revoke) |
| `covers` | `unfoldx_covers_get`, `unfoldx_covers_write` (`method`: generate, select) |
| `speech` | `unfoldx_speech_get`, `unfoldx_speech_preview` |
| `settings` | `unfoldx_settings_get`, `unfoldx_settings_update` (`method`: preferences, pronunciations) |
| `brands` | `unfoldx_brands_get`, `unfoldx_brand_write` (`method`: create, update, asset_create, asset_update) |
| `superfast` | `unfoldx_superfast_submit`, `unfoldx_superfast_get`, `unfoldx_superfast_download` |

0.8.0 renamed the 0.7.0 tools with no aliases: `unfoldx_whoami` → `unfoldx_capabilities` (scopes and budget) or
`unfoldx_credits`; `unfoldx_run_status/events/questions/result/usage` and `unfoldx_command_get` →
`unfoldx_run_get` with that `method`; `unfoldx_workspaces_list`/`unfoldx_workspace_get` → `unfoldx_workspaces`;
`unfoldx_material_get`/`unfoldx_materials_list` → `unfoldx_materials`; `unfoldx_artifact_get`/`unfoldx_artifact_access`
→ `unfoldx_work_export`; the per-resource tools → the toolset tools above. The publishing tools are gone: the
platform no longer publishes on your behalf.

JSON Schema validation uses jsonschema; the standard JSON loader reads the sole packaged OpenAPI contract.
The SDK is pinned (`mcp==2.3.0`) and is a dependency of this package only; the CLI stays
stdlib-only. Tests: `cd mcp && uv run pytest tests`.


## Resource tools

Descriptions, scopes, input and output schemas come from `unfoldx_cli/openapi.json` (`x-mcp-tools` lists the tools),
bundled once with the CLI dependency. Response schemas accept fields this client does not know yet, so a newer
server does not break an older client. Each tool states its required scope. Personal memory is not exposed, and
nothing deletes your data; manage that in the UnfoldX web app. Sharing and other public actions without their scope answer
`insufficient_scope` with `error.details.approval_url`, where the user confirms in the web app.
Writes with side effects do not automatically retry unless their existing service provides idempotency. Making or
withdrawing a public share and generating paid covers require `confirm=true`; `unfoldx_run_cancel` needs none (repeating it is harmless). Missing scopes are server-enforced even if a host ignores tool annotations.

`unfoldx_work_export` and `unfoldx_superfast_download` write local files with the CLI's atomic no-clobber behavior;
`overwrite=true` is an explicit replacement. Derived audio requires ffmpeg.
`unfoldx_superfast_submit` requires a saved command_id and opt-in scope. Its production hold counts against the
same key budget as route cards (`billing`: held, then settled or refunded); above the budget it answers
`approval_required` with the key settings page. Poll `unfoldx_superfast_get` with a bounded interval. No automatic
fallback from ordinary runs to superfast.

`--read-only` removes write-only tools and mutating methods from discovery and rejects direct calls to them. This is a host
configuration boundary in addition to server-side credential scopes, not a substitute for authorization.

Superfast status exposes `duration`: measured seconds, target seconds and delta. A warning is a quality fact,
not a failed job. Report any deviation clearly; only an explicit caller submission edits the film.
