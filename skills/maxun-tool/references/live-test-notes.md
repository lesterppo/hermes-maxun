# Live test notes (condensed, reusable)

Findings from driving the `maxun` tool against a live self-hosted Maxun
(docker) on real sites. Not upstream docs — just what worked and what didn't.

## skillhub.cn (JS-heavy SPA, Chinese)

- Homepage prompt (`create_ai_robot` on `https://skillhub.cn/`) → returned only
  the 3 top banner zones. The AI builder anchored on the initial viewport.
- Same prompt on `https://skillhub.cn/skills` → **24 real skill cards** with
  name / category / install count / view count / description. ✅ This is the
  pattern: point at the deep listing route, not the homepage.
- `run` with `prompt_instructions="scroll to 50 cards"` → identical 24 cards.
  Confirmed `run` is deterministic replay; `prompt_instructions` does not
  re-plan navigation.
- Rebuilding the robot with pagination in the *build* prompt ("scroll until 50")
  → still 24 cards. Maxun did not trigger the lazy-load. 24 is the observed
  cap for prompt-only robots on this site.
- Decoded sample (first 3 of 24):
  1. web-tools-guide — 知识管理 — 117 installs — 17.9万 views
  2. 腾讯文档 TENCENT DOCS — 办公效率 — 188 — 13.9万
  3. ima-skills — 知识管理 — 359 — 9.8万

## Hacker News (news.ycombinator.com)

- `create_ai_robot` + `run` → status success, ~12s, but captured the nav bar
  rather than story rows (sparse page, weak signal for the builder). A more
  specific prompt ("click first story, extract title/points") extracts cleaner.

## Timings (self-hosted, local browser)
- create_ai_robot: ~15-21s (drives real Chromium via the `browser` service)
- run: ~14-24s server-side wait
- list_robots / get_run / list_runs: <1s

## Tool behavior confirmed
- `check_fn` gates the tool off the schema unless MAXUN_API_KEY + MAXUN_API_URL
  are set (zero footprint otherwise).
- All transport errors and Maxun 4xx/409 return `{"error":...,"detail":...}`
  JSON — never raises into the agent loop.
- 409 (robot name/prompt collision) is surfaced cleanly; reuse the existing id
  from `list_robots`.
- Inline output is row-capped (first 10 rows preview + `_rows` count) and never
  truncated mid-JSON; full run JSON is written to `~/.hermes/maxun_output/`.
