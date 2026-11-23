---
name: publish-with-extension
description: Hand a finished UnfoldX film to the user's browser extension so it can be published to 抖音 / 小红书 / B站 / 视频号. Use when the user asks to publish, post or upload a finished film to one of those platforms, or asks why the publish card cannot be answered from the CLI or MCP.
---

# publish-with-extension

The UnfoldX CLI and MCP can make and download a film, but they **cannot publish it**. Publishing to an outside
platform happens in the user's own browser: the UnfoldX extension fills that platform's upload page, and the user
presses 发布. This skill is the handover, not a publish command.

## What the agent cannot do (say it plainly, do not try)

- There is no publish operation in the public API. `unfoldx publishing …` does not exist; do not invent it or call
  `/v2/publishing/*`.
- A publish card for an outside platform is never answered by an API key. `publish:confirm` only lets a key confirm
  the **in-site gallery** publish card; outside platforms answer on the user's browser ("its card never answers
  publish").
- Do not drive the browser to click 发布 on the user's behalf, and never ask for the account's password or cookies.

## Workflow

1. Confirm the film is finished and downloaded: `unfoldx works download <run_id> --out ./out --json` (or
   `unfoldx runs result <run_id> --json` for the artifact). Give the user the file path.
2. Check the extension is available. If the user has not installed it, point them to the web app's 连接 →
   浏览器插件 tab (`/connections?tab=extension`) and stop; nothing else works without it.
3. Tell the user exactly what to do, in their words: open the film's 发布卡片 on the web (or the extension's side
   panel), pick the target platform, let the extension fill the title/description/cover, then **press 发布
   themselves**. Offer the title and description text if they want to paste it.
4. After they publish, ask for the work link and record it if the product asks (作品数据 / 发布后数据 lives in the
   web app). Do not claim the film is live without that link.

## Notes

- The extension fills a form; it does not have the account's permission to publish on its own.
- If the user asks for the in-site gallery instead (作品广场), that card can be answered with a key that holds
  `publish:confirm` via `unfoldx runs answer`; it is a different target and needs no extension.
- Publishing is irreversible once the user presses 发布 on the platform; never rush them or click for them.
