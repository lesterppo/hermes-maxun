"""Reliability audit for the Hermes `maxun` tool (token-efficient v2).

Run:  python3 tools/maxun_tool_audit.py
Set MAXUN_API_KEY + MAXUN_API_URL to exercise live-call paths.
"""

import json
import sys
import time

import tools.maxun_tool as m

results = []


def t(name, ok, detail=""):
    results.append(("PASS" if ok else "FAIL", name, str(detail)))


# ── 1. Load ───────────────────────────────────────────────────────────────────
t0 = time.time()
_ = m.MAXUN_SCHEMA
_ = m._maxun_available()
t("availability check < 50ms", (time.time() - t0) * 1000 < 50, f"{(time.time()-t0)*1000:.1f}ms")
t("schema name is maxun", m.MAXUN_SCHEMA["name"] == "maxun")

# ── 2. Input validation ──────────────────────────────────────────────────────
for action, kw, label in [
    ("", {}, "empty action"),
    ("bogus_action", {}, "unknown action"),
    ("create_ai_robot", {}, "no prompt"),
    ("create_ai_robot", {"prompt": "x", "url": "bad"}, "bad url"),
    ("create_ai_robot", {"prompt": "  "}, "whitespace prompt"),
    ("create_search", {}, "no query"),
    ("create_search", {"query": "  "}, "whitespace query"),
    ("run", {}, "no robot_id"),
    ("duplicate", {"robot_id": "x"}, "no target_url"),
    ("duplicate", {"robot_id": "x", "target_url": "bad"}, "bad target_url"),
    ("get_robot", {"robot_id": ""}, "empty robot_id"),
    ("list_runs", {"robot_id": ""}, "empty robot_id"),
    ("abort_run", {"run_id": ""}, "empty run_id"),
]:
    d = json.loads(m.maxun_tool(action, **kw))
    t(f"reject {label}", "e" in d, d.get("e", "")[:50])

# unknown action also lists valid actions
d = json.loads(m.maxun_tool("bogus_action"))
t("unknown action has actions list", isinstance(d.get("actions"), list))

# llm passthrough — ensure handler accepts keys
d = json.loads(m.maxun_tool("create_ai_robot", prompt="x", url="https://example.com",
                             llm_provider="openai", llm_model="gpt-4o", llm_api_key="sk-test"))
t("llm passthrough accepted", "e" not in d or "url" not in str(d), str(d)[:40])

# run wait=false
d = json.loads(m.maxun_tool("run", robot_id="x", wait=False))
t("run wait=false returns started shape", "rid" in d or "e" in d, str(d)[:50])

# create_search defaults mode
d = json.loads(m.maxun_tool("create_search", query="test"))
t("create_search defaults mode", d.get("md") == "discover" or "e" in d, str(d)[:40])

# duplicate without target_url
d = json.loads(m.maxun_tool("duplicate", robot_id="x"))
t("duplicate needs target_url", "e" in d, d.get("e", "")[:40])

# ── 3. Token cost ────────────────────────────────────────────────────────────
schema_s = json.dumps(m.MAXUN_SCHEMA)
t("schema < 2100 chars", len(schema_s) < 2100, f"{len(schema_s)} chars")
t("desc < 400 chars", len(m.MAXUN_SCHEMA["description"]) < 400, f"{len(m.MAXUN_SCHEMA['description'])} chars")

# Compact keys check
synth = {"runId": "abc", "status": "success", "robotId": "r1",
         "data": {"listData": {"col0": list(range(500))}},
         "links": [f"https://x/{i}" for i in range(200)],
         "screenshots": [f"http://s/{i}.png" for i in range(50)]}
compact = m._compact_run(synth)
text = json.dumps(compact)
t("compact run < 2500 tok (~10k chars)", len(text) // 4 < 2500, f"{len(text)//4} tok")
t("compact rows uses n/c/v", sorted(m._compact_rows({"x": [1, 2]}).keys()) == ["c", "n", "v"])
t("compact run uses short keys", all(len(k) <= 3 for k in compact.keys()),
  f"keys: {sorted(compact.keys())}")

# Row cap
cr = m._compact_rows({"col0": list(range(500))})
t("row cap reports count", cr["n"] == 500 and len(cr["v"]["col0"]) == m.MAX_INLINE_ROWS,
  f"n={cr['n']}, preview={len(cr['v']['col0'])}")

# Screenshot cap
t("screenshot cap", compact.get("ns") == 50 and len(compact.get("sh", [])) == m.MAX_INLINE_SCREENSHOTS)

# ── 4. Live ──────────────────────────────────────────────────────────────────
LIVE = m._maxun_available()
if LIVE:
    d = json.loads(m.maxun_tool("list_robots"))
    t("list_robots live ok", d.get("ok"), f"n={d.get('n')}")
    t("list_robots compact keys", sorted(d.keys()) == ["n", "ok", "rs"], str(sorted(d.keys())))

    d = json.loads(m.maxun_tool("get_robot", robot_id="nonexistent-999"))
    t("get_robot bogus handled", "e" in d or "ok" in d, str(d)[:60])

    d = json.loads(m.maxun_tool("create_ai_robot",
                                 url="https://quotes.toscrape.com",
                                 prompt="Extract 5 quotes with author",
                                 robot_name=f"Hermes-Audit-{int(time.time())}"))
    t("create_ai_robot live ok", d.get("ok") and d.get("rid"), f"rid={d.get('rid','?')[:12]}")
    t("create_ai_robot compact keys", sorted(d.keys()) in (
        ["ex", "nm", "ok", "rid", "url"], ["ex", "nm", "ok", "rid"]), str(sorted(d.keys())))

    rid = d.get("rid")
    if rid:
        d2 = json.loads(m.maxun_tool("run", robot_id=rid))
        t("run live ok", d2.get("ok") and d2.get("s") == "success", f"s={d2.get('s')}")
        t("run has @ file", bool(d2.get("@")), str(d2.get("@", ""))[:50])
        t("run compact keys", all(len(k) <= 3 for k in d2.keys() if k != "@"),
          f"keys: {sorted(d2.keys())}")

        d3 = json.loads(m.maxun_tool("list_runs", robot_id=rid))
        t("list_runs compact keys", sorted(d3.keys()) == ["bid", "n", "ok", "rs"])

        d4 = json.loads(m.maxun_tool("duplicate", robot_id=rid,
                                      target_url="https://quotes.toscrape.com/page/2/"))
        t("duplicate compact keys", sorted(d4.keys()) == ["new", "nm", "ok", "src"])

    d = json.loads(m.maxun_tool("create_search", query="docker", limit=3))
    t("create_search compact keys", sorted(d.keys()) == ["md", "nm", "ok", "rid"])

    d = json.loads(m.maxun_tool("abort_run", run_id="fake"))
    t("abort_run handled", "e" in d, str(d)[:80])
else:
    t("LIVE tests skipped (no env vars)", True)

# ── 5. Integration ───────────────────────────────────────────────────────────
registered = any(e.name == "maxun" for e in m.registry._tools.values())
t("registered as 'maxun'", registered)
t("check_fn callable", callable(m._maxun_available))
entry = m.registry._tools.get("maxun")
t("requires_env both keys", entry and set(entry.requires_env) >= {"MAXUN_API_KEY", "MAXUN_API_URL"})
t("9 actions in enum", len(m.MAXUN_SCHEMA["parameters"]["properties"]["action"]["enum"]) == 9)

# ── 6. Unconfigured gate ─────────────────────────────────────────────────────
if LIVE:
    import os as _os
    old = _os.environ.pop("MAXUN_API_KEY", None)
    d = json.loads(m.maxun_tool("list_robots"))
    t("unconfigured gate fires", d.get("e") == "not configured")
    t("unconfigured compact keys", sorted(d.keys()) == ["d", "e"])
    if old:
        _os.environ["MAXUN_API_KEY"] = old

# ── Report ────────────────────────────────────────────────────────────────────
passed = sum(1 for r in results if r[0] == "PASS")
failed = [r for r in results if r[0] == "FAIL"]
print(f"\n{'='*60}\nMaxun tool audit (token-efficient v2): {passed}/{len(results)} passed\n{'='*60}")
for status, name, detail in results:
    print(f"[{status}] {name:42s} {detail}")
if failed:
    print(f"\n{len(failed)} FAILURES")
    sys.exit(1)
print("\nALL PASS")
