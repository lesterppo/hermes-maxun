# AGENTS.md — AI agent instructions for hermes-maxun

This document tells AI coding agents (Hermes, Claude Code, Codex, etc.)
everything they need to install, configure, and use the `maxun` tool in a
Hermes Agent installation.

## What this repo provides

A Hermes Agent **native tool** that wraps the
[Maxun](https://github.com/getmaxun/maxun) open-source web-extraction
platform. One tool, nine actions, compact token-efficient JSON output.

The tool is AI-agent-native: an agent can discover a website, build an
extraction robot from a plain-English prompt, run it, and read structured
data — all without leaving the conversation.

## Quick start (for an AI agent integrating this)

```bash
git clone https://github.com/lesterppo/hermes-maxun.git
cd hermes-maxun
./install.sh ~/.hermes/hermes-agent   # copies tool + skill
```

Then wire the tool into the agent's toolset by adding `"maxun"` to
`_HERMES_CORE_TOOLS` in `toolsets.py`:

```python
_HERMES_CORE_TOOLS = [
    "web_search", "web_extract", "crawl", "maxun",   # Web
    ...
]
```

Restart Hermes. The tool is gated by `check_fn` — it only appears when both
`MAXUN_API_KEY` and `MAXUN_API_URL` are set. Without them, the tool is
invisible (zero footprint in the schema).

## Configuration

Set in `~/.hermes/.env` or via `/secret`:

```
MAXUN_API_KEY=<api-key>
MAXUN_API_URL=http://localhost:8080   # or cloud URL
```

### Self-hosted Maxun (for E2E testing)

See `skills/maxun-tool/references/maxun-selfhost-recipe.md` for the full
docker compose setup, user registration, and API key generation.

Key gotcha: **NordVPN blocks Docker bridge networking.** If `nordvpn status`
shows Firewall enabled, run:
```bash
nordvpn set lan-discovery enabled
nordvpn whitelist add port 8080
```

## How the tool works

The tool exposes 9 actions through a single `maxun` registry entry:

| Action | Purpose |
|--------|---------|
| `list_robots` | List existing extraction robots |
| `get_robot` | Inspect a robot by ID |
| `create_ai_robot` | Build a robot from a plain-English prompt |
| `create_search` | Build a web-search robot from a query |
| `run` | Execute a robot, capture structured data |
| `list_runs` | Run history for a robot |
| `get_run` | One run's full data (offloaded to disk) |
| `abort_run` | Cancel a running/queued run |
| `duplicate` | Clone a robot onto a new URL |

### Typical agent flow

1. `maxun(action="create_ai_robot", url="https://example.com/list", prompt="Extract top 20 items with name, price, rating")`
2. Read `rid` from the response.
3. `maxun(action="run", robot_id=rid)`
4. Read inline preview (row-capped, keys `ld`/`td`/`md`), or full data from the `@` path.
5. For multi-page: `maxun(action="duplicate", robot_id=rid, target_url="https://example.com/list?page=2")`

## Output format (compact, token-efficient)

All responses use 1-2 char keys:

**Success:** `{"ok": true, "rid": "...", ...}`
**Error:** `{"e": "msg", "d": "detail", "h": "hint"}`

### Key reference

| Key | Meaning | Key | Meaning |
|-----|---------|-----|---------|
| `ok` | success | `s` | status |
| `e` | error | `d` | detail |
| `h` | hint | `n` | count |
| `rs` | items array | `rid` | robot/run id |
| `bid` | robot id (in run) | `nm` | name |
| `cr` | created | `st` | started |
| `fn` | finished | `ld` | list data |
| `td` | text data | `cd` | crawl data |
| `sd` | search data | `md` | markdown/mode |
| `pr` | prompt result | `sum` | summary |
| `lk` | links | `nl` | link count |
| `sh` | screenshots | `ns` | screenshot count |
| `@` | saved file path | `v` | preview values |
| `c` | column names | `ex` | existing flag |
| `src` | source robot | `new` | new robot |

Full skill with pitfalls and procedures: `skills/maxun-tool/SKILL.md`

## Testing

```bash
cd ~/.hermes/hermes-agent
MAXUN_API_KEY=... MAXUN_API_URL=http://localhost:8080 \
  python3 tools/maxun_tool_audit.py
```

Target: 45/45 pass (load, validation, token cost, accuracy, edge cases,
integration). Live-call paths exercise when env vars are set.

## Files

```
tools/maxun_tool.py            # The tool (registry.register + dispatch)
tools/maxun_tool_audit.py      # Reliability audit harness
skills/maxun-tool/SKILL.md     # Skill (auto-loaded by Hermes)
skills/maxun-tool/references/  # Self-host recipe + live-test notes
patches/maxun-abort-apikey.patch  # Optional Maxun upstream patch
install.sh                     # Idempotent installer
```

## Privacy rules

- NO secrets, API keys, or personal paths in any committed file.
- All paths use `get_hermes_home()`, never `/home/*`.
- The Maxun API key is user-generated in their own instance.
