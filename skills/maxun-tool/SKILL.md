---
name: maxun-tool
description: Turn websites into structured data via self-hosted or cloud Maxun from Hermes.
version: 1.1.0
author: Peter/lesterppo
license: MIT
tags: [web, scraping, data-extraction, automation]
category: research
metadata:
  hermes:
    tags: [web, scraping, data-extraction]
    category: research
    related_skills: [crawl-tool, web-research]
---

# Maxun Web-Data Extraction Skill

Wrap the `maxun` Hermes tool. Maxun (getmaxun/maxun) is an open-source no-code
platform that turns any website into structured, reliable data and exposes it
through a REST API. The `maxun` tool is the AI-agent-native wrapper: one tool,
nine actions, compact JSON, full run data saved to a profile-aware file so the
agent's context stays small.

## When to Use

- "Scrape the top 25 products with name, price, rating from <site>"
- "Turn <site> into a structured API I can re-run"
- "Extract all job postings / papers / listings matching <criteria>"
- "Search the web for <topic> and pull the result pages"
- Re-running a recurring extraction (robot) on demand or via `cronjob`

Do NOT use it for one-off page reads — `web_extract` is cheaper and faster for a
single page. Maxun shines when the target needs clicking, pagination, login, or
repeatable structured capture.

## Prerequisites

- A Maxun instance: `docker compose up -d` (self-hosted, default
  http://localhost:8080) or a Maxun cloud account.
- `MAXUN_API_KEY` set in `~/.hermes/.env` (Hermes setup auto-discovers it via
  `OPTIONAL_ENV_VARS`). For self-hosted: register a user in the dashboard, then
  `POST /auth/generate-api-key` (or the dashboard "API Key" tab).
- `MAXUN_API_URL` (default `http://localhost:8080`).
- The tool is gated by `check_fn` — it only appears in the model schema when
  both env vars are set. Set them with `/secret` or `hermes setup`.

## How to Run

The tool is `maxun` with an `action` parameter. Reference actions by name:

- `list_robots` — list existing robots (id + name).
- `get_robot` (robot_id) — inspect a robot.
- `create_ai_robot` (prompt, url?, robot_name?, llm_provider?, llm_model?, llm_api_key?) —
  the headline action. Describe what you want in plain English; Maxun builds the
  workflow and returns a robot_id you can `run`. URL is optional — Maxun can
  search for the site. If the self-hosted instance has no server-side LLM,
  pass `llm_provider`/`llm_model`/`llm_api_key`.
- `create_search` (query, mode?, limit?) — spin up a web-search robot. `mode`
  defaults to `discover` (find URLs); use `scrape` to also scrape them.
- `run` (robot_id, wait?, formats?, save_to?) — execute a robot. `wait=true`
  (default) blocks until completion and returns data; `wait=false` fires the run
  and returns `run_id` immediately for polling/abort. Full JSON is written to
  `~/.hermes/maxun_output/`.
- `list_runs` (robot_id) — run history for a robot.
- `get_run` (robot_id, run_id) — one run's full data (saved to file).
- `abort_run` (run_id) — cancel a running or queued run. Works over the API
  key on the patched self-hosted Maxun (the upstream `/storage/runs/abort/:id`
  route is session-gated; a local API-key variant was added). Returns 400 if
  the run already finished.
- `duplicate` (robot_id, target_url) — clone a robot onto a new URL (handy for
  gathering different slices via category/listing URLs).

Example agent flow:
1. `maxun(action="create_ai_robot", url="https://news.ycombinator.com", prompt="Extract the top 30 stories with title, points, author and number of comments")`
2. Take the returned `robot_id`.
3. `maxun(action="run", robot_id="...")`
4. Read the structured data from the inline `list_data` / `text_data`, or open
   the `saved_to` file for the full markdown/links/screenshots.

## Quick Reference

Formats for `run`: `markdown`, `html`, `text`, `links`, `summary`,
`screenshot-visible`, `screenshot-fullpage`.

Output is always JSON. On success: `{"ok": true, "action": ..., ...}`. On
failure: `{"error": "...", "detail": "...", "hint": "..."}` — never raises into
the agent loop.

## Procedure

1. Confirm availability: the tool won't be offered unless `MAXUN_API_KEY` and
   `MAXUN_API_URL` are set. If missing, tell the user to set them.
2. For a brand-new target, use `create_ai_robot` (one call, returns robot_id).
   For recurring targets, build once then `run` repeatedly (or wire a
   `cronjob` with `skills=["maxun-tool"]`).
3. After `run`, surface the compact preview; point the user at `saved_to` for
   the full dataset. For long jobs use `wait=false` + poll `list_runs`/`get_run`.

## Pitfalls (learned from live testing)

- **Put navigation/pagination in the `create_ai_robot` prompt, not in `run`.**
  Maxun's `run` replays the workflow built at robot creation — runtime
  `prompt_instructions` do NOT re-plan navigation or inject scroll/load-more.
  A vague "scroll past the banner and find the grid" prompt on a JS-heavy SPA
  will snap to the first visible sections. Give it a **deep listing URL**
  (e.g. `/skills`, `/explore`) instead of the homepage + navigation words.
- **Prompt-only robots cap at the initial page's cards** on lazy-load/infinite
  scroll sites (observed: 24 cards on skillhub.cn regardless of instructions).
  To gather more, `duplicate` the robot onto category-filtered URLs, or build
  the robot in Maxun's UI (record mode) and just `run` it from Hermes.
- **`robot_name` must be unique** — Maxun 409s on duplicates with the same
  prompt. Use a distinct name per target.
- **`abort_run` works over the API key** on the patched self-hosted Maxun. The
  upstream route was session-gated; a local API-key variant was added in
  `server/src/routes/storage.ts` (see `references/maxun-selfhost-recipe.md` →
  "Patching the abort route" for the exact edit + rebuild steps). Note: Maxun's
  `POST /runs` executes the run to completion server-side before returning, so
  `wait=false` still gets a finished run — abort only catches runs still
  `running`/`queued` when called (long scrapes on a busy instance). See
  "Synchronous run behavior" in that recipe file.
- **Self-hosted first run**: Maxun needs a registered user + an API key before
  any `/api/*` call works, and the backend must reach the `browser` service
  (`BROWSER_WS_HOST=browser` in the backend env).

## Verification

Run the bundled audit: `python3 tools/maxun_tool_audit.py`. Target: 100% pass
(load, validation, token cost, accuracy, edge cases, integration). Set
`MAXUN_API_KEY`/`MAXUN_API_URL` to also exercise the live-call paths.
