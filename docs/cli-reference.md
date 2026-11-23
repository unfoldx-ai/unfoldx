# unfoldx CLI reference (`unfoldx.v2`)

The CLI is a thin client of [`../cli/src/unfoldx_cli/openapi.json`](../cli/src/unfoldx_cli/openapi.json). A **workspace**
is one conversation with the UnfoldX main agent; a **run** is one message to it,
followed to its film.

## Commands

| Command | Does |
| --- | --- |
| `login [--client-name NAME]` | Device login: prints a link and code, waits for approval in the browser, stores the token for this API base |
| `logout` | Revokes this machine's credential on the server and forgets it |
| `whoami` | The credential in use: scopes, credit budget, expiry |
| `capabilities` | Operations, routes and minimum client version for this credential |
| `credits` | What this key can still approve today: wallet, caps, the rolling-24h remainder and `auto_approve_limit` |
| `doctor` | Python, credentials, API reachability, auth and version checks |
| `workspaces list [--limit N] [--cursor CURSOR] [--query TEXT] [--state STATE]` / `workspaces show ID` | Workspaces ordered by creation time; title search and run-state filtering; follow `next_cursor` with the same query/state until null / one workspace with its runs |
| `runs submit --goal TEXT [--url URL]… [--file PATH]… [--material ID]… [--workspace ID]` | Sends the main agent a message: a new workspace, or a follow-up in `--workspace`. Files are uploaded first. Prints `command_id` and the run. `--input-json FILE\|-` takes the same fields (`goal`, `urls`, `files`, `material_ids`, `workspace_id`) instead of flags; mixing both is a usage error |
| `runs answer RUN --route ROUTE [--mode autonomous\|interactive] [--style ID] [--note TEXT]` | Chooses a quoted production route on the open route card (the `quote_ref` is read from the card). Within the credential's budget it starts production; above it, exit 5 `needs_input/approval_required` with `details.approval_url` for the user |
| `runs answer RUN --question ID (--answer TEXT \| --answer-json JSON)` | Answers another card (storyboard, publish) with the web's answer format |
| `runs cancel RUN` | Stops the run; repeating it, or cancelling a finished run, prints its current state |
| `runs status RUN` | State and the open question, if any |
| `runs events RUN [--after-seq N] [--limit N]` | Events after a cursor; pass `next_seq` back as `--after-seq` |
| `runs questions RUN` | Open confirmation cards: route choice with quotes, storyboard, publish; each with `approval_url` |
| `runs result RUN` | The assistant reply, workspace and film artifacts (`ready` says whether a new film was delivered) |
| `runs usage RUN` | Credits held and charged, and whether settlement is complete |
| `runs wait RUN [--after-seq N] [--interval S] [--wait-timeout S]` | Polls until the message completes (exit 0, with the result; inspect `ready` and `message` before claiming a film), asks a question (exit 5, `needs_input/question_open`, question in `error.details`), waits on its page with no question the API can answer (exit 5, `needs_input/no_open_question`, `error.details.web_url` to open), fails or is cancelled (exit 7), or the wait times out (exit 8, resume with `--after-seq`) |
| `materials upload PATH` / `materials show ID` | Uploads a local file (≤100 MB) straight to storage and confirms it; prints its `material_id` / its state |
| `commands show COMMAND_ID` | The run a submit started, for when its response was lost |
| `artifacts show ID` | Role and file name |
| `artifacts download ID [--out DIR] [--overwrite]` | Downloads into `DIR/<run_id>/<filename>`; existing files are refused unless `--overwrite` is explicit. Only a complete download is published; prints path, bytes and sha256, never the link |

Global options go anywhere on the line: `--json`, `--api-base URL`, `--timeout SECONDS`.

## Client identity

Every request, the storage upload and the download included, carries `X-Unfoldx-Client: cli`
(`mcp` from `unfoldx-mcp`) and `User-Agent: unfoldx-cli/<version>`; the platform shows which
client a credential is connected through.

## Command ids

Every `runs submit` carries a command id as its `Idempotency-Key`. The CLI records it in
`~/.config/unfoldx/commands/` before sending, keyed by what was asked (goal, sources, the
files' bytes), so running the same submit again after a crash or a lost response returns the
run already started (`idempotent_replay: true`) instead of a second one. `--new` starts a new
command on purpose; `--command-id ID` uses your own. Dropped connections and server errors marked `retryable` are retried within the operation timeout.
`Retry-After` seconds and HTTP dates are honored without shortening them. If the remaining time
cannot cover the delay, the error returns immediately with the required wait. Submission errors
include `details.command_id` and `details.recovery_command`; find the accepted command before
starting another one. A corrupt local command receipt fails explicitly rather than creating a new ID.

## Settings

Precedence: flags > environment > `./.unfoldx.toml` > `~/.config/unfoldx/config.toml` > defaults.

| Setting | Env | Default |
| --- | --- | --- |
| `api_base` | `UNFOLDX_API_BASE` | `https://api.unfoldx.ai/api` (production; the development site is `https://dev.unfoldx.ai/api`) |
| `timeout_sec` | `--timeout` flag | 30 (total HTTP operation, including retries) |
| `wait_timeout_sec` | — | 3600 |
| `poll_interval_sec` | — | 5 |

`UNFOLDX_CONFIG_DIR` moves `~/.config/unfoldx`.

## Credentials

`login` stores the `ufx_` token per API base: in the OS keychain with the `keychain`
extra, otherwise in `~/.config/unfoldx/credentials.json` (0600). `UNFOLDX_TOKEN`
overrides both; it is meant for CI secrets, not for typing into a command or an MCP host's
config, where the key would sit in shell history or a plain config file. A token is only
ever sent to the API base that issued it. The user
chooses scopes and a credit budget on the approval page; `publish:confirm` is off
by default. Credentials last 90 days and can be revoked on the web or with `logout`.

## Output

Machine mode (`--json`, or stdout is not a TTY) prints exactly one JSON object:

```json
{"schema": "unfoldx.v2", "ok": true, "data": {}}
{"schema": "unfoldx.v2", "ok": false, "error": {"type": "auth_failed", "code": "insufficient_scope", "message": "…", "retryable": false, "retry_after_sec": null, "outcome": null, "details": null}}
```

Progress and hints go to stderr. Tokens are redacted everywhere. Argument parsing, configuration and
local file errors use the same JSON envelope; `--help` and `--version` remain text. In a terminal,
status/usage/questions show concise Chinese summaries, plain narration, quotes and shell-safe next commands.
Each supported card carries `answer_schema` and `answer_examples`; unsupported cards return `false`/`[]`.

## Exit codes

| Exit | `error.type` | Typical `error.code` | What to do |
| --- | --- | --- | --- |
| 0 | — | — | — |
| 2 | usage | — | Fix the command line |
| 3 | auth_failed | `not_logged_in`, `credential_revoked`, `credential_expired`, `insufficient_scope` | Ask the user to run `unfoldx login` (again, with the needed scope) |
| 4 | rate_limited / quota_exceeded | `slow_down` | If `retryable`, wait `retry_after_sec` and retry once |
| 5 | invalid_request / not_found / needs_input | `question_open`, `no_open_question`, `approval_required`, `idempotency_conflict`, `material_not_ready` | For an open question, answer it within budget or give the user `approval_url`; for `no_open_question`, give the user `web_url`; for a conflict, use `--new` only if a second run is really wanted |
| 6 | environment | `unreachable`, `insecure_url` | Check network and `--api-base` |
| 7 | generation_failed / server_error | `run_failed`, `run_cancelled`, `internal_error` | Report; do not start a duplicate run |
| 8 | timeout | `wait_timeout`, `request_timeout` | The run may still be going: wait again with `--after-seq` |
| 9 | policy_blocked | — | Tell the user; never rephrase to get around it |

## Recovery and review guarantees

Explicit `--command-id` retries reuse the uploaded material IDs recorded for that
request. A changed payload with the same ID is rejected, never silently resubmitted.
Authenticated API requests reject redirects; configure the final API base explicitly.
Route answers use `execution_mode: autonomous|interactive` (no `review` alias).
Storyboard questions include the editable slides and their stable UIDs; other
unsupported cards remain visible with `answerable: false` and an approval URL.
Run usage exposes active ledger holds. A failed run includes a safe error code,
message and next action; `runs wait` preserves them in its error details.

Downloaded versions never share a local path across runs. Repeating a download requires explicit
`--overwrite`; failed or concurrent downloads do not destroy an existing file.


## Resource workflows (0.6.0)

Run `unfoldx <group> <command> --help` for typed flags. Complex replacement documents use
`--input-json file.json` or `--input-json -`; first read the current document and preserve its
revision and fields. `--json` is always one envelope. Scope changes require explicit reauthorization;
existing tokens do not silently gain new permissions.

- `materials list --query TEXT --type document --page 1 --page-size 20`; continue through `next_page`.
- `works list --workspace-id WORKSPACE`; `works show RUN` and `workspaces show WORKSPACE` identify each version.
  `works exports RUN` lists saved files; `works export RUN --format zip --out ./out` downloads them atomically.
  Formats: video, cover, subtitles, storyboard, transcript, audio, zip. Audio extraction needs local `ffmpeg` and
  includes background music. An absent original SRT/VTT is `export_unavailable`; transcript text is separately named,
  never presented as timed subtitles. ZIP lists unavailable originals in its manifest.
- `catalogs styles`, `catalogs voices --language Chinese`, `catalogs covers` return selectable IDs and cover price.
- While a run waits at its storyboard, `speech preview RUN --command-id ID --question-id QUESTION --scene-id UID
  --text TEXT --dictionary-revision REV`; `speech status RUN JOB` returns audio and timings. An audition does not
  approve production. To use auditions, include `speech_preview_jobs` in the storyboard answer.
- `covers generate RUN --command-id ID --platform douyin --headline TEXT --labels '["A","C"]' --yes` makes one cover
  per layout in `labels` (1–3 of A 大字标题, B 主体特写, C 图解对比) at the catalog `credits_each`, within credential
  limits. `covers status RUN` returns actual candidates; `covers select RUN --cover-id ID` chooses one. Cover cards
  can also be answered using their `answer_schema`, with both `runs:answer` and `covers:write` scopes.
- `shares create RUN --yes` explicitly publishes a public link; `shares revoke RUN --yes` withdraws the workspace's
  owner shares so an earlier version cannot become public accidentally. `shares show RUN` is read-only.
- `brands list/create/update`, `brand-assets create/update` manage existing brand entitlement.
  Logo/background inputs use an owned image `--material-id`; slogan/palette use structured data. No voice-cloning
  operation or arbitrary server URL fetch is implied.
- `preferences show/update`, `pronunciations show/update` use separate scopes and revision checks. Never
  read-edit-write a guessed default; read first. A conflict means reread and reconcile the user's intent. Do not
  overwrite a concurrent edit. Personal memory and deletes are managed only in the UnfoldX web app.

## Topics and auto tasks via CLI

A topic is what the account wants covered; an auto task is a recurring job that picks from it. Both are opt-in
scopes (`topics:read`/`topics:write`, `schedules:read`/`schedules:write`) and both MCP toolsets default off: add
`--toolsets core,topics,schedules` when starting `unfoldx-mcp`.

```bash
unfoldx topics list --json
unfoldx topics subscribe --topic '认知科学' --description '记忆与注意' --json
unfoldx topics unsubscribe KEY --json

unfoldx schedules list --json
unfoldx schedules add --input-json schedule.json --json
unfoldx schedules edit 12 --state paused --json
unfoldx schedules runs 12 --json
unfoldx schedules remove 12 --json
```

Adding a task records when it next runs; it starts nothing and spends nothing. Every run it later does stays inside
the account's per-run and per-day caps, and a run that would exceed them comes back on the web like any route card.
`scope.topic_ids` and `scope.event_ids` must name items the account already follows (`topics list`), or the request
is refused. Pausing (`--state paused`) is the way to stop it without losing its history; deleting keeps the runs it
already did in their sessions.

`--input-json` also accepts the same fields as flags; do not mix both in one call.

Schedule scope takes `topic_ids`, `event_ids`, and `text`; use empty arrays or an empty string for unused fields.
A brief deliverable includes `kind`, `angles`, `plan`, `language`, and `reply_language`. Video and publish
deliverables use `route` in place of `plan`, and a publish deliverable also includes `target`.

## Superfast via CLI

Superfast is a separate job, not a route name in `runs answer`. It must be enabled on the target deployment and
requires the explicit, default-off `superfast:create` grant. Its production hold counts against the same key budget
as route cards: `billing.status` is `held` until the film settles it (`settled`) or a failure refunds it
(`refunded`). Above the key's budget the submit answers `approval_required`; raise the key's caps in the web app's
key settings or ask the user to submit there.

```bash
unfoldx superfast submit --chat '讲清月相' --command-id sf-moon-20261104-01 --json
unfoldx superfast wait JOB --wait-timeout 600 --json
unfoldx superfast download JOB --out ./out --json
```

For a file, upload it first and pass `--material-id`; a web source uses `--web-url`. To edit, submit an input JSON
with `parent_job_id` and the complete edited `slides` (`id`, `title`, `narration`) and a new command ID. Wait for the
parent to complete first. Lost submit responses: replay the exact body and ID; changed content with that ID is 409.
Its idempotency lasts for the underlying Redis job's retention, unlike durable main-agent command receipts.
A wait timeout leaves the job running. Use the returned timeline for first-part/completion latency; a first-part
milestone is not proof that a player actually played it.

Superfast editing: `unfoldx superfast submit --parent-job-id JOB --instruction '缩短第一段' --command-id ID`
re-renders only the changed shots and returns a new film plus `reply`, a one-line account of what changed. A question
or a request superfast cannot carry out completes with `reply` and `video_available: false`, and no film.

Terminal output shows `reply`, and suggests a download only when the job produced a film.
A failed/cancelled superfast job has no wait/download suggestion; inspect its error before submitting anything new.

Superfast passes explicit numeric lengths such as “约20秒” or “1.5 minutes” to the planner’s duration budget
(1–3600 seconds), rather than leaving them only in the prompt. Verify the downloaded film’s actual duration;
narration timing can differ slightly from the requested target.

Superfast duration is a soft goal. Completed status includes `duration` with measured `duration_seconds`,
`target_duration_seconds`, `delta_ratio` and any warning. Human output shows these facts too. The caller can
review and explicitly submit edited narration; the service does not silently rewrite or fail a film merely
because it differs from the target. A missing measurement stays null and is never replaced with the target.

## Publishing outside the web card

Preparing a publish does not publish it. `publish prepare` records the film, platform and copy and returns a
one-time `handoff_url`: the browser extension claims that link, fills the creator page, reads every field back and
reports what it saw. The last click stays with the person (YouTube and TikTok can be published privately by the
extension where the platform allows it). Read the result with `publish status`; `unverified` means the extension
could not read a field back — look before assuming it worked.

```bash
unfoldx publish prepare RUN --platform douyin --title '…' --desc '…' --tags '["英语单词","词根词缀"]' --json
unfoldx publish status INTENT --json
unfoldx publish cancel INTENT --json
unfoldx works publications RUN --json
```

`works publications` is the read projection of every platform record for a film: platform, state, link and where the
record came from (extension, public data, official API or the user pasting a link). It needs `publish:read`.
