# Live test notes (condensed, reusable)

Findings from driving the `maxun` tool against a live self-hosted Maxun
(docker) on real sites. Not upstream docs — just what worked and what didn't.

## Setup (native Linux, Docker)

- Docker CE 29.6.2 on Ubuntu 24.04
- Maxun docker compose: 5 services (postgres, minio, backend, frontend, browser).
  ~30s to start after `docker compose up -d`.
- User registration + API key generation via the Maxun dashboard or
  `POST /auth/register` → `POST /auth/login` → `POST /auth/generate-api-key`.

## Book scraping (books.toscrape.com) — Jul 2026

- `create_ai_robot` on books.toscrape.com: 6.5s, extracted 40 rows (generic
  labels: Label 1 = detail URL, Label 2 = image URL — Maxun AI builder detected
  book image links but didn't name columns).
- `run`: 28.1s, full data 16390 bytes, inline 3589 chars. ✅

## Quote scraping (quotes.toscrape.com) — Jul 2026

- `create_ai_robot` on quotes.toscrape.com: 6–10s, 3–10 rows.
- `run`: 8.0s, full data 7.6K, inline 2144–6025 chars.
- Labels: "Label 1"=quote text, "Label 2"=author, "Label 3"=tags link,
  "Label 4"=author link. ✅ Correct data, generic column names.
- `duplicate` → page/2/ works: new robot ID returned. ✅

## HN (news.ycombinator.com) — no-URL mode

- `create_ai_robot` without URL (let Maxun search): returned robot_id.
- `run` with formats=["markdown","links"]: status=success, but markdown empty
  (HN listing page may not produce structured markdown). Site-specific.

## Timings (self-hosted, local browser)

- create_ai_robot: 6–21s (varies by site complexity)
- run: 8–28s server-side wait
- list_robots / get_run / list_runs: <1s
- create_search: ~2s

## Abort route (known gap — Jul 2026)

- Upstream Maxun gates `/storage/runs/abort/:id` behind `requireSignIn`
  (session cookie). The API-key guarded route is NOT available in the
  prebuilt Docker image (`getmaxun/maxun-backend:latest`).
- Attempted local build + patch (`maxun-abort-apikey.patch`) but `npm install`
  fails on `canvas` native deps (gyp error). Prebuilt image works fine;
  building from source needs libcairo2-dev etc installed in Docker image.
- Fallback: `abort_run` returns 401; tool handles it gracefully (doesn't crash).

## Tool behavior confirmed

- `check_fn` gates the tool off the schema unless MAXUN_API_KEY + MAXUN_API_URL
  are set (zero footprint otherwise).
- All transport errors and Maxun 4xx/500 return `{"e":...,"d":...,"h":...}`
  JSON — never raises into the agent loop.
- `create_search` with `mode=scrape` works (creates scraping search robot).
- `duplicate` onto a sibling URL works (same site, different page).
- Inline output is row-capped (first 10 rows preview + `n` count) and never
  truncated mid-JSON; full run JSON written to `@` path on disk.
- Schema: 2040 chars (12 params, 9 actions).
- `_HERMES_CORE_TOOLS`: wired, verified. 45/45 audit pass.
