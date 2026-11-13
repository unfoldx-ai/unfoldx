# unfoldx

Thin HTTPS client for the UnfoldX public API (`unfoldx.v2`, contract shipped in the
package as `unfoldx_cli/openapi.json`). Stdlib-only; install the `keychain`
extra to keep the token in the OS keychain instead of a 0600 file.

```bash
pip install unfoldx
unfoldx login                      # approve the code in the browser
unfoldx runs submit --goal "讲清楚量子纠缠" --file notes.pdf --json
unfoldx runs wait <run_id> --json  # until the film is ready or a question is open
unfoldx runs answer <run_id> --route template --json   # within the approved budget
unfoldx artifacts download <run_id>.video --out ./out --json
```

Designed to be driven by AI agents: non-interactive, one JSON object on stdout in
machine mode (`--json`, or whenever stdout is not a TTY), progress on stderr,
closed-set `error.type` with a precise `error.code`, and stable exit codes
(0 ok / 2 usage / 3 auth / 4 quota-or-rate / 5 input or open question /
6 environment / 7 failed / 8 timeout / 9 policy / 130 interrupt).
See [`../docs/cli-reference.md`](../docs/cli-reference.md).

Publishing receipts distinguish saved drafts from accepted publication. `published_url` is a validated link returned by the platform, or null when unavailable; acceptance does not prove public visibility or moderation approval. Follow `next_action` without uploading again.

`superfast download JOB --out DIR` creates the per-job directory on first use and refuses to overwrite a previous download unless `--overwrite` is explicit.
