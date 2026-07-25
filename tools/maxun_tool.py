"""Maxun web-data extraction tool (AI-agent-native, token-efficient).

Maxun is an open-source no-code platform that turns websites into structured,
reliable data and exposes it through a REST API. This tool is the Hermes-native
wrapper around that API.

Output uses compact 1-2 char keys to minimize token cost; full data offloaded
to disk. Requires MAXUN_API_KEY + MAXUN_API_URL. Gated by check_fn — zero
footprint when unconfigured.

Actions (all 9): list_robots, get_robot, create_ai_robot, create_search,
  run, list_runs, get_run, abort_run, duplicate
"""

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from tools.registry import registry

DEFAULT_API_URL = "http://localhost:8080"
MAX_INLINE_CHARS = 9000
MAX_INLINE_ROWS = 10
MAX_INLINE_SCREENSHOTS = 5


# ── Config ────────────────────────────────────────────────────────────────────

def _maxun_base_url() -> str:
    url = os.getenv("MAXUN_API_URL", "").strip()
    return url.rstrip("/") if url else DEFAULT_API_URL


def _maxun_api_key() -> str:
    return os.getenv("MAXUN_API_KEY", "").strip()


def _maxun_available() -> bool:
    if not _maxun_api_key():
        return False
    p = urlparse(_maxun_base_url())
    return p.scheme in ("http", "https") and bool(p.netloc)


def _out_dir() -> Path:
    from hermes_constants import get_hermes_home
    d = get_hermes_home() / "maxun_output"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── HTTP plumbing ─────────────────────────────────────────────────────────────

def _req(method: str, path: str, timeout: int = 300, json_body=None):
    url = f"{_maxun_base_url()}{path}"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": _maxun_api_key(),
        "x-run-source": "hermes-agent",
    }
    try:
        resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
    except requests.exceptions.Timeout:
        return {"e": f"timeout {timeout}s ({method} {path})"}
    except requests.exceptions.ConnectionError as e:
        return {"e": "connection refused", "d": str(e)[:200]}
    except Exception as e:
        return {"e": "request failed", "d": str(e)[:200]}

    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:500]}

    if resp.status_code >= 400:
        msg = (body.get("message") or body.get("error") or body.get("messageCode") or "").strip()
        return {"e": f"HTTP {resp.status_code}", "d": msg[:300], "h": _hint(msg)}

    return body


def _hint(msg: str) -> str:
    m = (msg or "").lower()
    if "failed to generate workflow" in m or "initialize browser" in m:
        return "LLM/browser unreachable — set llm_provider or check BROWSER_WS_HOST"
    if "api key" in m or "unauthorized" in m or "401" in m:
        return "Check MAXUN_API_KEY"
    if "not found" in m or "404" in m:
        return "Not found — use list_robots/list_runs"
    return ""


# ── Output compaction ─────────────────────────────────────────────────────────

def _trunc(s: str, n: int) -> str:
    if not isinstance(s, str):
        return s
    if len(s) <= n:
        return s
    return s[:n] + f"...[{len(s) - n} more]"


def _compact_rows(obj: dict) -> dict:
    """Compact column-keyed data: keep first MAX_INLINE_ROWS, report count."""
    if not isinstance(obj, dict) or not obj:
        return obj
    cols = list(obj.keys())
    n_rows = max((len(v) for v in obj.values() if isinstance(v, list)), default=0)
    preview = {}
    for c in cols:
        v = obj[c]
        preview[c] = v[:MAX_INLINE_ROWS] if isinstance(v, list) else v
    return {"n": n_rows, "c": cols, "v": preview}


def _compact_run(run: dict) -> dict:
    """Compact a run object — short keys, row-capped data."""
    data = run.get("data", {}) or {}
    out = {
        "rid": run.get("runId"),
        "s": run.get("status"),
        "bid": run.get("robotId"),
        "st": run.get("startedAt"),
        "fn": run.get("finishedAt"),
    }
    if data.get("listData"):
        out["ld"] = _compact_rows(data["listData"])
    if data.get("textData"):
        out["td"] = _compact_rows(data["textData"])
    if data.get("crawlData"):
        out["cd"] = _compact_rows(data["crawlData"])
    if data.get("searchData"):
        out["sd"] = _compact_rows(data["searchData"])
    if data.get("promptResult"):
        out["pr"] = _trunc(data["promptResult"], 1500)
    if data.get("summary"):
        out["sum"] = _trunc(data["summary"], 1500)
    if data.get("markdown"):
        out["md"] = _trunc(data["markdown"], 2000)
    if data.get("links"):
        lk = data["links"]
        out["lk"] = lk[:MAX_INLINE_ROWS]
        out["nl"] = len(lk)
    if run.get("screenshots"):
        sh = run["screenshots"]
        out["sh"] = sh[:MAX_INLINE_SCREENSHOTS]
        out["ns"] = len(sh)
    return out


def _save(robot_name: str, run: dict) -> str:
    try:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(robot_name))[:40]
        path = _out_dir() / f"{safe}_{int(time.time())}.json"
        path.write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")
        return str(path)
    except Exception:
        return ""


# ── Action handlers ───────────────────────────────────────────────────────────

def _list_robots():
    body = _req("GET", "/api/robots")
    if "e" in body:
        return body
    items = (body.get("robots", {}) or {}).get("items", []) or []
    return {
        "ok": True,
        "n": len(items),
        "rs": [{"id": r.get("id"), "nm": r.get("name"), "cr": r.get("createdAt")} for r in items],
    }


def _get_robot(robot_id: str):
    if not robot_id:
        return {"e": "robot_id required"}
    body = _req("GET", f"/api/robots/{robot_id}")
    if "e" in body:
        return body
    r = body.get("robot", {}) or {}
    return {"ok": True, "id": r.get("id"), "nm": r.get("name"), "cr": r.get("createdAt")}


def _create_ai_robot(url: str, prompt: str, robot_name: str,
                     llm_provider: str, llm_model: str, llm_api_key: str):
    if not prompt or not prompt.strip():
        return {"e": "prompt required"}
    if url:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.netloc:
            return {"e": "invalid URL"}
    req = {"prompt": prompt}
    if url:
        req["url"] = url
    if robot_name:
        req["robotName"] = robot_name
    if llm_provider:
        req["llmProvider"] = llm_provider
    if llm_model:
        req["llmModel"] = llm_model
    if llm_api_key:
        req["llmApiKey"] = llm_api_key
    body = _req("POST", "/api/sdk/extract/llm", json_body=req)
    if "e" in body:
        return body
    d = body.get("data", {}) or {}
    return {
        "ok": True,
        "rid": d.get("robotId"),
        "nm": d.get("name"),
        "url": d.get("url"),
        "ex": body.get("existing", False),
    }


def _create_search(query: str, mode: str, limit: int):
    if not query or not query.strip():
        return {"e": "query required"}
    mode = mode if mode in ("discover", "scrape") else "discover"
    sc = {"query": query, "provider": "duckduckgo", "mode": mode}
    if isinstance(limit, int) and limit > 0:
        sc["limit"] = min(limit, 50)
    body = _req("POST", "/api/sdk/search", json_body={"searchConfig": sc})
    if "e" in body:
        return body
    robot = body.get("data", {}) or {}
    rm = robot.get("recording_meta", {}) or {}
    return {"ok": True, "rid": rm.get("id") or robot.get("id"), "nm": rm.get("name") or robot.get("name"), "md": mode}


def _run(robot_id: str, formats: list, save_to: str, wait: bool):
    if not robot_id:
        return {"e": "robot_id required"}
    req = {}
    if isinstance(formats, list) and formats:
        req["formats"] = [f for f in formats if isinstance(f, str)]
    body = _req("POST", f"/api/robots/{robot_id}/runs", json_body=req or None)
    if "e" in body:
        return body
    run = body.get("run", {}) or {}
    run_id = run.get("runId")

    if not wait and run_id:
        return {"ok": True, "rid": run_id, "bid": robot_id, "s": "started",
                "h": "Started (wait=false). Poll list_runs/get_run or abort_run."}

    compact = _compact_run(run)
    compact["ok"] = True
    if save_to and save_to.strip():
        try:
            Path(save_to.strip()).parent.mkdir(parents=True, exist_ok=True)
            Path(save_to.strip()).write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")
            compact["@"] = save_to.strip()
        except Exception as e:
            compact["@"] = f"save failed: {e}"
    elif run:
        saved = _save(run.get("name") or robot_id, run)
        if saved:
            compact["@"] = saved
    return compact


def _list_runs(robot_id: str):
    if not robot_id:
        return {"e": "robot_id required"}
    body = _req("GET", f"/api/robots/{robot_id}/runs")
    if "e" in body:
        return body
    runs = (body.get("runs", {}) or {}).get("items", []) or []
    return {
        "ok": True, "n": len(runs), "bid": robot_id,
        "rs": [{"rid": r.get("runId"), "s": r.get("status"), "nm": r.get("name"),
                "st": r.get("startedAt"), "fn": r.get("finishedAt")} for r in runs],
    }


def _get_run(robot_id: str, run_id: str):
    if not robot_id:
        return {"e": "robot_id required"}
    if not run_id:
        return {"e": "run_id required"}
    body = _req("GET", f"/api/robots/{robot_id}/runs/{run_id}")
    if "e" in body:
        return body
    run = body.get("run", {}) or {}
    compact = _compact_run(run)
    compact["ok"] = True
    saved = _save(run.get("name") or f"{robot_id}_{run_id}", run)
    if saved:
        compact["@"] = saved
    return compact


def _abort_run(run_id: str):
    if not run_id:
        return {"e": "run_id required"}
    body = _req("POST", f"/storage/runs/abort/{run_id}", timeout=60)
    if "e" in body:
        d = str(body.get("d", ""))
        if "401" in d or "404" in d or "Cannot POST" in d:
            body["h"] = "Abort endpoint session-gated. Prebuilt Maxun image lacks API-key abort route."
        return body
    return {"ok": True, "rid": run_id, "result": body}


def _duplicate(robot_id: str, target_url: str):
    if not robot_id:
        return {"e": "robot_id required"}
    if not target_url or not target_url.strip():
        return {"e": "target_url required"}
    p = urlparse(target_url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return {"e": "invalid target_url"}
    body = _req("POST", f"/api/robots/{robot_id}/duplicate", json_body={"targetUrl": target_url})
    if "e" in body:
        return body
    robot = body.get("robot", body.get("data", {})) or {}
    rm = robot.get("recording_meta", {}) or {}
    return {"ok": True, "src": robot_id, "new": rm.get("id") or robot.get("id"),
            "nm": rm.get("name") or robot.get("name")}


# ── Dispatch ──────────────────────────────────────────────────────────────────

_HANDLERS = {
    "list_robots": lambda kw: _list_robots(),
    "get_robot": lambda kw: _get_robot(str(kw.get("robot_id", ""))),
    "create_ai_robot": lambda kw: _create_ai_robot(
        str(kw.get("url", "")), str(kw.get("prompt", "")), str(kw.get("robot_name", "")),
        str(kw.get("llm_provider", "")), str(kw.get("llm_model", "")), str(kw.get("llm_api_key", ""))),
    "create_search": lambda kw: _create_search(
        str(kw.get("query", "")), str(kw.get("mode", "")), int(kw.get("limit", 0) or 0)),
    "run": lambda kw: _run(
        str(kw.get("robot_id", "")), list(kw.get("formats", []) or []),
        str(kw.get("save_to", "")), bool(kw.get("wait", True))),
    "list_runs": lambda kw: _list_runs(str(kw.get("robot_id", ""))),
    "get_run": lambda kw: _get_run(str(kw.get("robot_id", "")), str(kw.get("run_id", ""))),
    "abort_run": lambda kw: _abort_run(str(kw.get("run_id", ""))),
    "duplicate": lambda kw: _duplicate(str(kw.get("robot_id", "")), str(kw.get("target_url", ""))),
}


def maxun_tool(action: str, task_id: str = None, **kwargs) -> str:
    action = (action or "").strip().lower()
    if not _maxun_available():
        return json.dumps({"e": "not configured",
                           "d": "Set MAXUN_API_KEY + MAXUN_API_URL (hermes setup / /secret)."})

    if not action:
        return json.dumps({"e": "action required", "actions": sorted(_HANDLERS.keys())})

    fn = _HANDLERS.get(action)
    if fn is None:
        return json.dumps({"e": f"unknown action '{action}'", "actions": sorted(_HANDLERS.keys())})

    result = fn(kwargs)
    return json.dumps(result, indent=2, default=str)


# ── Schema ────────────────────────────────────────────────────────────────────

MAXUN_SCHEMA = {
    "name": "maxun",
    "description": (
        "Maxun web scraper: create extraction robots from English prompts "
        "(create_ai_robot), search robots (create_search), run to capture "
        "lists/tables/markdown/screenshots, abort, view history. "
        "Needs MAXUN_API_KEY + MAXUN_API_URL. Full output saved to file (@ key); "
        "inline preview row-capped. Nav/pagination goes in create_ai_robot prompt."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Operation.",
                "enum": ["list_robots", "get_robot", "create_ai_robot", "create_search",
                         "run", "list_runs", "get_run", "abort_run", "duplicate"],
            },
            "robot_id": {"type": "string", "description": "Robot id."},
            "run_id": {"type": "string", "description": "Run id (get_run/abort_run)."},
            "prompt": {"type": "string", "description": "Extraction goal, e.g. 'top 25 products with name, price'. Nav here."},
            "url": {"type": "string", "description": "Target URL (optional). Prefer listing URL."},
            "robot_name": {"type": "string", "description": "Unique robot name (409 on dup)."},
            "llm_provider": {"type": "string", "description": "LLM provider (e.g. openai) for self-hosted w/o server LLM."},
            "llm_model": {"type": "string", "description": "LLM model (paired with llm_provider)."},
            "llm_api_key": {"type": "string", "description": "LLM API key (paired with llm_provider)."},
            "query": {"type": "string", "description": "Search query for create_search."},
            "mode": {"type": "string", "description": "create_search mode: discover (default) or scrape."},
            "limit": {"type": "integer", "description": "Search result limit (1-50)."},
            "wait": {"type": "boolean", "description": "true=block till done; false=return run_id for polling."},
            "formats": {"type": "array", "items": {"type": "string"},
                        "description": "Output: markdown,html,text,links,summary,screenshot-visible,screenshot-fullpage."},
            "target_url": {"type": "string", "description": "New URL for duplicate."},
            "save_to": {"type": "string", "description": "Path for full JSON (default: ~/.hermes/maxun_output/)."},
        },
        "required": ["action"],
    },
}

registry.register(
    name="maxun",
    toolset="web",
    schema=MAXUN_SCHEMA,
    handler=lambda args, **kw: maxun_tool(
        args.get("action", ""),
        task_id=kw.get("task_id"),
        robot_id=args.get("robot_id", ""),
        run_id=args.get("run_id", ""),
        prompt=args.get("prompt", ""),
        url=args.get("url", ""),
        robot_name=args.get("robot_name", ""),
        llm_provider=args.get("llm_provider", ""),
        llm_model=args.get("llm_model", ""),
        llm_api_key=args.get("llm_api_key", ""),
        query=args.get("query", ""),
        mode=args.get("mode", ""),
        limit=args.get("limit", 0),
        wait=args.get("wait", True),
        formats=args.get("formats", []),
        target_url=args.get("target_url", ""),
        save_to=args.get("save_to", ""),
    ),
    check_fn=_maxun_available,
    requires_env=["MAXUN_API_KEY", "MAXUN_API_URL"],
    emoji="🕸️",
    max_result_size_chars=MAX_INLINE_CHARS,
)
