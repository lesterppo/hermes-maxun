# hermes-maxun — contributor notes

This repo ships a Hermes Agent tool (`maxun`) that wraps self-hosted/cloud Maxun.

## Layout
- `tools/maxun_tool.py` — the tool. Single `maxun` registry entry, `action` enum (9 ops).
- `tools/maxun_tool_audit.py` — run with `python3 tools/maxun_tool_audit.py`.
- `skills/maxun-tool/` — SKILL.md + references (auto-loaded by Hermes when the skill dir is in `~/.hermes/skills/`).
- `patches/maxun-abort-apikey.patch` — optional Maxun upstream patch (API-key abort route).
- `install.sh` — idempotent installer; copies tool+skill, verifies import. Does NOT edit core files.

## Rules
- NO secrets, API keys, or `/home/*` paths in any committed file.
- Schema `description` must stay <= 500 chars (skill standard).
- Keep output compact: row-capped previews + full data saved to `get_hermes_home()/maxun_output/`.
- Never truncate mid-JSON — agents need valid JSON.
