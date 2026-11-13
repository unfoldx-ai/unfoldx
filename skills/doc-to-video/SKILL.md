---
name: doc-to-video
description: Ask the UnfoldX main agent for an explainer video from a topic, files or links, follow the run to the finished film and download it, using the `unfoldx` CLI. Use when the user wants a video made on UnfoldX, a follow-up change to one ("make the opening shorter"), its progress, an answer to its route or storyboard card, or the film file. Also use for UnfoldX material search, work exports, covers, audition, publishing-account receipts, brand/preferences/memory management, or explicitly requested superfast generation.
---

# doc-to-video

The UnfoldX main agent turns a topic or material into an explainer video in a
conversation (a **workspace**); each message to it is a **run**. This skill follows
runs through the `unfoldx` CLI. It never generates video locally.

## Prerequisites

1. The CLI is installed (`pip install unfoldx`). Check with `unfoldx doctor --json`.
2. The user has logged in with `unfoldx login`. It shows a link and a code that the user
   approves in the browser. Ask the user to do this; never approve on their behalf.
3. If `doctor` reports `ok_all: false`, show the failing checks to the user and stop.

## Workflow

1. Start or find the run.
   - New video: `unfoldx runs submit --goal "<the user's request>" [--file <path>]… [--url <link>]… --json`.
     Keep `command_id` and `run.run_id`, and `run.workspace_id` for follow-ups.
   - Follow-up in the same video: add `--workspace <workspace_id>`.
   - Existing run: `unfoldx workspaces list --query "<title>" --json` (follow `next_cursor` with the same filters), then `unfoldx workspaces show <workspace_id> --json`.
   If a submit's response was lost, run the same command again: it returns the same run
   (`idempotent_replay: true`). Use `--new` only when the user wants a second video.
2. Follow it: `unfoldx runs wait <run_id> --json`. Progress goes to stderr.
   - exit 0: this message ended. Read `result.message` and `result.ready`; only `ready: true`
     means a new film is delivered. If no film was delivered, explain the reply and decide the
     next message with the user’s existing authorization; do not blindly resubmit.
   - exit 5 with `error.code = question_open`: the main agent is asking something (a
     production route with its credit quote, a storyboard, publishing). Show the user the
     question and quotes. For supported cards use their `answer_schema` and `answer_examples`;
     unsupported cards have `answerable: false` and require their approval URL. If the user chose a route, `unfoldx runs answer <run_id> --route <route> --json`;
     exit 5 `approval_required` means it is above the budget they granted: give them
     `error.details.approval_url` to confirm on the web. Never pick a route for them, never retry
     around the budget. Then `runs wait` again.
   - exit 5 with `error.code = no_open_question`: the run waits on something only its page shows
     (no card the CLI can answer). Give the user `error.details.web_url` to continue there; do not
     poll it again until they say they did.
   - exit 8: still running; call `runs wait` again with `--after-seq <error.details.next_seq>`.
   - exit 7: the run failed or was cancelled; tell the user, do not retry blindly.
3. Stop: `unfoldx runs cancel <run_id> --json` when the user asks to stop.
4. Download: `unfoldx artifacts download <run_id>.video --out ./unfoldx_out --json`
   (and `<run_id>.cover` for the cover). Each run has its own output subdirectory.
   Existing files require explicit `--overwrite`. Report the path and sha256.
5. Cost: `unfoldx runs usage <run_id> --json`. `usage_complete: false` means settlement is
   not final; do not present a partial number as the total.

## Errors (branch on exit code / `error.type` / `error.code`)

| exit | meaning | what to do |
|---|---|---|
| 3 | not logged in, revoked, expired, or missing scope | Ask the user to run `unfoldx login`; for `insufficient_scope`, to approve the needed scope |
| 4 | rate limited | If `retryable`, wait `retry_after_sec`, retry once |
| 5 | not found, or a question is open | Check the id; for questions see step 2 |
| 8 | timeout | Wait again with the cursor; never start a second run for the same request |
| 9 | policy blocked | Tell the user; never rephrase to get around it |

Full command and exit-code reference: <https://github.com/unfoldx-ai/unfoldx/blob/main/docs/cli-reference.md>.

## Notes

- Everything on stdout is one JSON object; tokens are redacted from all output.
- Download links last one hour; ask for a new one with `artifacts download` instead of saving links.


## Resource and fast-mode extensions

Before invoking a resource operation, call capabilities and inspect the relevant `--help` / MCP schema.
Find existing materials with `materials list`, finished versions with `works list`, and selectable style/voice IDs
with `catalogs`. Do not upload duplicates or guess IDs. Read question.answer_schema before every confirmation.

Use `speech preview` only on an existing review scene; carry its exact dictionary revision and explicitly select
completed audition IDs in the answer. For covers, read the catalog price, request one keyed generation within the
credential budget, then show actual candidates before selecting. Sharing/publishing/account linking requires the
user's explicit intent and the corresponding non-default grant. A failed/unknown publish receipt is not permission
to create another remote upload; follow next_action. `published_url` may be null even after the platform accepts a
publish request; acceptance does not prove moderation approval or public visibility.

For delivery, prefer `works export RUN --format zip --out DIR`; use separate per-run directories. Preserve old
versions unless the user requests overwrite. Exact subtitle files may be unavailable; show that fact and offer the
separately named transcript, not guessed SRT timing. Extracted audio is the mixed film track, not isolated narration.

Preferences and pronunciation writes are read-modify-write with the current revision and separate scopes.
Keep unrelated fields and respect consent. Brand image assets must reference the user's uploaded material.

Only choose superfast when the user explicitly requests it and capabilities says it is enabled. It uses
`superfast submit/status/wait/download`, not runs/route answer. Save command_id before submission; reuse it only
for the same request. Explain `not_metered` truthfully; do not imply credit caps apply. Verify request_kind,
terminal state, actual playable download and account/ledger facts, keeping streaming latency separate from playback.
For source_unreadable, obtain complete readable material instead of retrying unchanged. Editing needs the full
returned storyboard and its parent_job_id; an unavailable parent workspace is an explicit refusal.

Use `--instruction` with `--parent-job-id` to edit a film in one sentence; only the changed shots re-render. Relay
`reply` to the user, and when `video_available` is false treat it as the whole answer: no new film was made.

Treat the superfast duration as a soft goal. Inspect `duration` and its warning before delivery; disclose a
significant deviation with measured and target seconds. Use an explicit narration revision when the user’s goal
calls for it; never describe the requested duration as measured or assume the service silently shortened the film.
