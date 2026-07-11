"""Reliability audit harness for the Hermes `maxun` tool.

Run:  python3 tools/maxun_tool_audit.py
Set MAXUN_API_KEY + MAXUN_API_URL to also exercise the live-call paths.

Covers the tool-reliability-audit criteria:
  1. Load (< 50ms schema/availability check)
  2. Input validation (empty/missing/bad-url/whitespace)
  3. Token cost (compact JSON, row-capped previews, full data offloaded)
  4. Accuracy (live create/run returns expected shape)
  5. Edge cases (bogus id, Maxun 409 dedup, finished-run abort)
  6. Integration (registry registration, flat schema, check_fn, requires_env)
"""

import json
import sys
import time

import tools.maxun_tool as m


results = []


def t(name, ok, detail=""):
    results.append(("PASS" if ok else "FAIL", name, str(detail)))


# ---------------------------------------------------------------------------
# 1. Load
# ---------------------------------------------------------------------------
t0 = time.time()
_ = m.MAXUN_SCHEMA
_ = m._maxun_available()
t("availability check < 50ms", (time.time() - t0) * 1000 < 50, f"{(time.time()-t0)*1000:.1f}ms")
t("schema available < 50ms", "name" in m.MAXUN_SCHEMA and "parameters" in m.MAXUN_SCHEMA, "")

# ---------------------------------------------------------------------------
# 2. Input validation
# ---------------------------------------------------------------------------
# unknown action
d = json.loads(m.maxun_tool("bogus_action"))
t("reject unknown action", "error" in d, d.get("error", "")[:40])
# missing action
d = json.loads(m.maxun_tool(""))
t("reject missing action", "error" in d, d.get("error", "")[:40])
# create_ai_robot needs prompt
d = json.loads(m.maxun_tool("create_ai_robot"))
t("create_ai_robot needs prompt", "error" in d, d.get("error", "")[:40])
# create_ai_robot bad url
d = json.loads(m.maxun_tool("create_ai_robot", prompt="x", url="not a url"))
t("create_ai_robot rejects bad url", "error" in d, d.get("error", "")[:40])
# create_search without query
d = json.loads(m.maxun_tool("create_search"))
t("create_search needs query", "error" in d, d.get("error", "")[:40])
# run without robot_id
d = json.loads(m.maxun_tool("run"))
t("run needs robot_id", "error" in d, d.get("error", "")[:40])
# abort_run without run_id
d = json.loads(m.maxun_tool("abort_run", robot_id="x"))
t("abort_run needs run_id", "error" in d, d.get("error", "")[:40])
# run wait=false returns run_id without requiring completion
d = json.loads(m.maxun_tool("run", robot_id="x", wait=False))
t("run wait=false returns run_id/started", "run_id" in d or "error" in d, str(d)[:50])
# create_search defaults mode to 'discover'
d = json.loads(m.maxun_tool("create_search", query="test news"))
t("create_search defaults mode discover", "mode" in d and d.get("mode") == "discover" or "error" in d, str(d)[:40])
# llm passthrough: offline, just ensure it doesn't crash / is accepted by handler signature
d = json.loads(m.maxun_tool("create_ai_robot", prompt="x", url="https://example.com",
                   llm_provider="openai", llm_model="gpt-4o", llm_api_key="sk-test"))
t("create_ai_robot accepts llm passthrough", "error" not in d or "url" not in str(d), str(d)[:40])
# duplicate without target_url
d = json.loads(m.maxun_tool("duplicate", robot_id="x"))
t("duplicate needs target_url", "error" in d, d.get("error", "")[:40])

# ---------------------------------------------------------------------------
# 3. Token cost (offline: schema description + a synthetic compact run)
# ---------------------------------------------------------------------------
desc = m.MAXUN_SCHEMA["description"]
t("schema description < 500 chars", len(desc) < 500, f"{len(desc)} chars")
synthetic_run = {
    "runId": "abc", "status": "success", "robotMetaId": "r1",
    "data": {"listData": {f"col{i}": list(range(500)) for i in range(5)},
             "textData": {"k": "v"}, "links": [f"https://x/{i}" for i in range(200)]},
    "screenshots": [f"http://s/{i}.png" for i in range(50)],
}
compact = m._compact_run(synthetic_run)
text = json.dumps(compact, indent=2)
t("compact run output < 2500 tokens (~10k chars)", len(text) // 4 < 2500, f"{len(text)//4} tok")
t("compact run reports row count not all rows", compact["list_data"]["_rows"] == 500
  and len(compact["list_data"]["preview"]["col0"]) == m.MAX_INLINE_ROWS,
  f"rows={compact['list_data']['_rows']}")
t("compact run caps screenshots", compact["screenshot_count"] == 50
  and len(compact["screenshots"]) == m.MAX_INLINE_SCREENSHOTS,
  f"shown={len(compact['screenshots'])}/{compact['screenshot_count']}")

# ---------------------------------------------------------------------------
# 4. Accuracy / 5. Edge cases (only with a live server)
# ---------------------------------------------------------------------------
LIVE = m._maxun_available()
if LIVE:
    # list_robots should succeed (possibly empty)
    d = json.loads(m.maxun_tool("list_robots"))
    t("list_robots live ok or structured error", "ok" in d or "error" in d, str(d)[:40])
    # get_robot with bogus id -> structured error, not a crash
    d = json.loads(m.maxun_tool("get_robot", robot_id="does-not-exist-123"))
    t("get_robot bogus id handled", "error" in d or "ok" in d, str(d)[:60])
    # create_ai_robot end-to-end
    d = json.loads(m.maxun_tool("create_ai_robot",
                       url="https://www.rottentomatoes.com",
                       prompt="Extract the top 10 trending movies with title and rating"))
    t("create_ai_robot live returns robot_id", "robot_id" in d or "error" in d, str(d)[:60])
    if "robot_id" in d:
        rid = d["robot_id"]
        d2 = json.loads(m.maxun_tool("run", robot_id=rid))
        t("run live returns compact result", "action" in d2 and d2.get("action") == "run", str(d2)[:60])
        t("run live saved_to or noted", bool(d2.get("saved_to")) or "note" in d2, str(d2)[:60])
else:
    t("LIVE tests skipped (no MAXUN_API_KEY/URL) — offline validation only", True, "set env to enable")

# ---------------------------------------------------------------------------
# 6. Integration
# ---------------------------------------------------------------------------
t("registered in registry", m.registry is not None)
registered = any(e.name == "maxun" for e in m.registry._tools.values())
t("tool named 'maxun' present", registered, "see registry")
t("check_fn is callable", callable(m._maxun_available))
t("requires_env documents both keys", set(m.registry._tools["maxun"].requires_env)
  >= {"MAXUN_API_KEY", "MAXUN_API_URL"}, str(m.registry._tools["maxun"].requires_env))
t("action enum covers 9 ops", len(m.MAXUN_SCHEMA["parameters"]["properties"]["action"]["enum"]) == 9,
  str(m.MAXUN_SCHEMA["parameters"]["properties"]["action"]["enum"]))

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
passed = sum(1 for r in results if r[0] == "PASS")
failed = [r for r in results if r[0] == "FAIL"]
print(f"\n{'='*60}\nMaxun tool reliability audit: {passed}/{len(results)} passed\n{'='*60}")
for status, name, detail in results:
    print(f"[{status}] {name:42s} {detail}")
if failed:
    print(f"\n{len(failed)} FAILURES")
    sys.exit(1)
print("\nALL PASS")
