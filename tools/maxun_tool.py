"""Maxun web-data extraction tool (AI-agent-native).

Maxun is an open-source no-code platform that turns websites into structured,
reliable data and exposes it through a REST API. This tool is the Hermes-native
wrapper around that API. It lets an agent:

  * list / inspect robots (pre-built extraction "robots")
  * create a robot from a natural-language prompt (``create_ai_robot``) -- the
    headline agent-native action: "scrape the 20 latest GitHub trending repos
    with their stars and languages" -> Maxun builds the workflow and returns a
    robot id that the agent can then run.
  * create a search robot from a query (``create_search``)
  * run a robot and capture its structured output (saved to a file so the
    inline response stays tiny)
  * abort a runaway run, inspect run history

All requests go to ``MAXUN_API_URL`` (default ``http://localhost:8080`` for a
self-hosted instance) authenticated with ``MAXUN_API_KEY`` (header
``x-api-key``). The tool is gated by ``_maxun_available`` so it only appears in
the model's schema when both are configured -- zero footprint otherwise.

Contract is read directly from Maxun's source (server/src/api/record.ts,
server/src/api/sdk.ts, server/src/mcp-worker.ts). Endpoints:

  GET    /api/robots
  GET    /api/robots/{id}
  POST   /api/robots/{id}/runs            (server waits for completion, returns run)
  POST   /storage/runs/abort/{runId}      (API-key variant added locally; upstream is session-gated)
  GET    /api/robots/{id}/runs
  GET    /api/robots/{id}/runs/{runId}
  POST   /api/robots/{id}/duplicate
  POST   /api/sdk/extract/llm             (prompt -> robot)
  POST   /api/sdk/search                  (query -> robot)

Run output shape (formatRunResponse):
  { id, status, name, robotId, startedAt, finishedAt, runId,
    data:{textData, listData, crawlData, searchData, markdown, html,
          links, summary, promptResult}, screenshots:[...] }

Implementation notes / design decisions (see tool-reliability-audit):
  * ``prompt_instructions`` on ``run`` ONLY affects run-scoped tweaks
    (formats/limits). Maxun's ``run`` replays the workflow built at robot
    creation -- runtime instructions do NOT re-plan navigation or inject
    scroll/load-more steps. Navigation requirements belong in the
    ``create_ai_robot`` prompt.
  * ``run`` defaults to waiting server-side for completion. For long jobs use
    ``wait=false``: it fires the run and returns the run_id immediately, then
    the agent polls with ``list_runs`` / ``get_run`` (or calls ``abort_run``).
  * ``create_ai_robot`` accepts optional ``llm_provider`` / ``llm_model`` /
    ``llm_api_key`` so a self-hosted instance without a server-side LLM can
    still build robots using a key the agent supplies.
"""

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from tools.registry import registry

# Default base URL for a locally self-hosted Maxun instance.
DEFAULT_API_URL = "http://localhost:8080"

# Output format cap -- keep inline responses small; full data is saved to a file.
MAX_INLINE_CHARS = 9000
MAX_INLINE_ROWS = 10
MAX_INLINE_SCREENSHOTS = 5

# How long run() will poll a run when wait=false before returning "still running".
RUN_POLL_TIMEOUT = 240
RUN_POLL_INTERVAL = 5


# ---------------------------------------------------------------------------
# Config / availability
# ---------------------------------------------------------------------------
def _maxun_base_url() -> str:
    url = os.getenv("MAXUN_API_URL", "").strip()
    if not url:
        return DEFAULT_API_URL
    return url.rstrip("/")


def _maxun_api_key() -> str:
    return os.getenv("MAXUN_API_KEY", "").strip()


def _maxun_available() -> bool:
    """Gate the tool on real availability, not mere existence of an env var.

    Both an API key and a usable http(s) base URL must be present. Returns
    False fast (no network call) so it is cheap to call on every schema build.
    """
    if not _maxun_api_key():
        return False
    parsed = urlparse(_maxun_base_url())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    return True


def _maxun_output_dir() -> Path:
    # Imported lazily to avoid a circular import at tool-import time.
    from hermes_constants import get_hermes_home
    d = get_hermes_home() / "maxun_output"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------
def _headers() -> dict:
    return {
        "Content-Type": "application/json",
        "x-api-key": _maxun_api_key(),
        "x-run-source": "hermes-agent",
    }


def _req(method: str, path: str, timeout: int = 300, json_body=None):
    url = f"{_maxun_base_url()}{path}"
    try:
        resp = requests.request(
            method, url, headers=_headers(), json=json_body, timeout=timeout
        )
    except requests.exceptions.Timeout:
        return {"error": f"Maxun request timed out after {timeout}s ({method} {path})"}
    except requests.exceptions.ConnectionError as e:
        return {"error": f"Cannot reach Maxun at {_maxun_base_url()}: {e}. "
                         f"Is the backend running and MAXUN_API_URL correct?"}
    except Exception as e:  # noqa: BLE001 -- surface any transport error
        return {"error": f"Maxun request failed: {e}"}

    # Maxun returns JSON on 4xx/5xx too; parse defensively.
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:500]}

    if resp.status_code >= 400:
        msg = (body.get("message") or body.get("error") or body.get("messageCode")
               or resp.text[:300])
        return {
            "error": f"Maxun returned HTTP {resp.status_code}",
            "detail": str(msg)[:500],
            "path": path,
            "hint": _error_hint(str(msg), path),
        }
    return body


def _error_hint(msg: str, path: str) -> str:
    """Agent-actionable hint for common Maxun failures."""
    m = (msg or "").lower()
    if "failed to generate workflow" in m or "initialize browser" in m:
        return ("Maxun's LLM workflow builder failed -- the self-hosted instance "
                "likely has no LLM configured, or its browser service is unreachable. "
                "Set llm_provider/llm_model/llm_api_key on create_ai_robot, or check "
                "BROWSER_WS_HOST on the backend.")
    if "api key" in m or "unauthorized" in m or "401" in m:
        return "Check MAXUN_API_KEY (register a user + generate it in the dashboard)."
    if "not found" in m or "404" in m:
        return "Robot/run id not found -- list with list_robots/list_runs."
    return ""


# ---------------------------------------------------------------------------
# Output compaction
# ---------------------------------------------------------------------------
def _truncate_text(s: str, n: int) -> str:
    if not isinstance(s, str):
        return s
    if len(s) <= n:
        return s
    return s[:n] + f"...[{len(s) - n} chars truncated; full data saved to file]"


def _compact_rows(obj: dict) -> dict:
    """Compact a column-keyed data object (listData/textData).

    Each value is an array of cell values for that column. We keep only the
    first ``MAX_INLINE_ROWS`` rows inline and report the full row count so the
    agent knows how many records exist without pulling them all into context.
    """
    if not isinstance(obj, dict) or not obj:
        return obj
    cols = list(obj.keys())
    n_rows = 0
    for v in obj.values():
        if isinstance(v, list):
            n_rows = max(n_rows, len(v))
    preview = {}
    for c in cols:
        v = obj[c]
        preview[c] = v[:MAX_INLINE_ROWS] if isinstance(v, list) else v
    return {"_rows": n_rows, "_cols": cols, "preview": preview}


def _compact_run(run: dict) -> dict:
    """Compact a single run object for inline output."""
    data = run.get("data", {}) or {}
    out = {
        "run_id": run.get("runId"),
        "status": run.get("status"),
        "robot_id": run.get("robotId"),
        "started": run.get("startedAt"),
        "finished": run.get("finishedAt"),
    }
    if data.get("listData"):
        out["list_data"] = _compact_rows(data["listData"])
    if data.get("textData"):
        out["text_data"] = _compact_rows(data["textData"])
    if data.get("crawlData"):
        out["crawl_data"] = _compact_rows(data["crawlData"])
    if data.get("searchData"):
        out["search_data"] = _compact_rows(data["searchData"])
    if data.get("promptResult"):
        out["prompt_result"] = _truncate_text(data["promptResult"], 1500)
    if data.get("summary"):
        out["summary"] = _truncate_text(data["summary"], 1500)
    if data.get("markdown"):
        out["markdown"] = _truncate_text(data["markdown"], 2000)
    if data.get("links"):
        links = data["links"]
        out["links"] = links[:MAX_INLINE_ROWS]
        out["link_count"] = len(links)
    if run.get("screenshots"):
        shots = run["screenshots"]
        out["screenshots"] = shots[:MAX_INLINE_SCREENSHOTS]
        out["screenshot_count"] = len(shots)
    return out


def _save_run(robot_name: str, run: dict) -> str:
    """Save the full raw run JSON to a profile-aware file; return its path."""
    try:
        d = _maxun_output_dir()
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(robot_name))[:40]
        ts = int(time.time())
        path = d / f"{safe}_{ts}.json"
        path.write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001 -- best-effort; never fail the tool on save
        return ""


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------
def _list_robots():
    body = _req("GET", "/api/robots")
    if "error" in body:
        return body
    robots = (body.get("robots", {}) or {}).get("items", []) or []
    return {
        "ok": True,
        "action": "list_robots",
        "total": len(robots),
        "robots": [{"id": r.get("id"), "name": r.get("name"), "created": r.get("createdAt")} for r in robots],
    }


def _get_robot(robot_id: str):
    if not robot_id:
        return {"error": "robot_id is required"}
    body = _req("GET", f"/api/robots/{robot_id}")
    if "error" in body:
        return body
    r = body.get("robot", {}) or {}
    return {
        "ok": True,
        "action": "get_robot",
        "id": r.get("id"),
        "name": r.get("name"),
        "created": r.get("createdAt"),
    }


def _create_ai_robot(url: str, prompt: str, robot_name: str,
                     llm_provider: str, llm_model: str, llm_api_key: str):
    if not prompt or not prompt.strip():
        return {"error": "prompt is required for create_ai_robot"}
    if url:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return {"error": "url must be a valid http(s) URL"}
    body_req = {"prompt": prompt}
    if url:
        body_req["url"] = url
    if robot_name:
        body_req["robotName"] = robot_name
    # Pass-through an LLM config so a self-hosted instance without a server-side
    # LLM can still build robots using a key the agent supplies.
    if llm_provider:
        body_req["llmProvider"] = llm_provider
    if llm_model:
        body_req["llmModel"] = llm_model
    if llm_api_key:
        body_req["llmApiKey"] = llm_api_key
    body = _req("POST", "/api/sdk/extract/llm", json_body=body_req)
    if "error" in body:
        return body
    d = body.get("data", {}) or {}
    return {
        "ok": True,
        "action": "create_ai_robot",
        "robot_id": d.get("robotId"),
        "name": d.get("name"),
        "url": d.get("url"),
        "existing": body.get("existing", False),
        "next": "Run it with action=run and robot_id above.",
    }


def _create_search(query: str, mode: str, limit: int):
    if not query or not query.strip():
        return {"error": "query is required for create_search"}
    # Explicit default: 'discover' finds URLs, 'scrape' also scrapes them.
    mode = mode if mode in ("discover", "scrape") else "discover"
    search_config = {"query": query, "provider": "duckduckgo", "mode": mode}
    if isinstance(limit, int) and limit > 0:
        search_config["limit"] = min(limit, 50)
    body = _req("POST", "/api/sdk/search", json_body={"searchConfig": search_config})
    if "error" in body:
        return body
    robot = body.get("data", {}) or {}
    return {
        "ok": True,
        "action": "create_search",
        "robot_id": robot.get("recording_meta", {}).get("id") or robot.get("id"),
        "name": robot.get("recording_meta", {}).get("name") or robot.get("name"),
        "mode": mode,
        "next": "Run it with action=run and robot_id above.",
    }


def _run(robot_id: str, formats: list, save_to: str, wait: bool):
    if not robot_id:
        return {"error": "robot_id is required for run"}
    body_req = {}
    if isinstance(formats, list) and formats:
        body_req["formats"] = [f for f in formats if isinstance(f, str)]
    # Fire the run. Maxun's POST waits server-side and returns the completed run.
    body = _req("POST", f"/api/robots/{robot_id}/runs", json_body=body_req or None)
    if "error" in body:
        return body
    run = body.get("run", {}) or {}
    run_id = run.get("runId")

    if not wait and run_id:
        # Non-blocking: return the run_id so the agent can poll / abort.
        return {
            "ok": True,
            "action": "run",
            "status": "started",
            "robot_id": robot_id,
            "run_id": run_id,
            "note": "Run started (wait=false). Poll with list_runs/get_run or abort with abort_run.",
        }

    compact = _compact_run(run)
    saved = ""
    if save_to and save_to.strip():
        saved = save_to.strip()
        try:
            Path(saved).parent.mkdir(parents=True, exist_ok=True)
            Path(saved).write_text(json.dumps(run, indent=2, default=str), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            saved = f"save failed: {e}"
    elif run:
        saved = _save_run(run.get("name") or robot_id, run)
    compact["ok"] = True
    compact["action"] = "run"
    if saved:
        compact["saved_to"] = saved
        compact["note"] = "Full run JSON (markdown/html/links/screenshots) saved to saved_to."
    else:
        compact["note"] = "Run data not captured (empty output)."
    return compact


def _list_runs(robot_id: str):
    if not robot_id:
        return {"error": "robot_id is required for list_runs"}
    body = _req("GET", f"/api/robots/{robot_id}/runs")
    if "error" in body:
        return body
    runs = (body.get("runs", {}) or {}).get("items", []) or []
    return {
        "ok": True,
        "action": "list_runs",
        "robot_id": robot_id,
        "total": len(runs),
        "runs": [
            {"run_id": r.get("runId"), "status": r.get("status"), "name": r.get("name"),
             "started": r.get("startedAt"), "finished": r.get("finishedAt")}
            for r in runs
        ],
    }


def _get_run(robot_id: str, run_id: str):
    if not robot_id:
        return {"error": "robot_id is required for get_run"}
    if not run_id:
        return {"error": "run_id is required for get_run"}
    body = _req("GET", f"/api/robots/{robot_id}/runs/{run_id}")
    if "error" in body:
        return body
    run = body.get("run", {}) or {}
    compact = _compact_run(run)
    compact["ok"] = True
    compact["action"] = "get_run"
    compact["saved_to"] = _save_run(run.get("name") or f"{robot_id}_{run_id}", run)
    return compact


def _abort_run(run_id: str):
    if not run_id:
        return {"error": "run_id is required for abort_run"}
    # Maxun's abort route (server/src/routes/storage.ts) is mounted at
    # /storage/runs/abort/:id (NOT /api/storage/...). It now has an
    # API-key-guarded variant (requireAPIKey) registered before the
    # session-guarded one, so the x-api-key works.
    body = _req("POST", f"/storage/runs/abort/{run_id}", timeout=60)
    if "error" in body:
        detail = str(body.get("detail", ""))
        if "404" in detail or "401" in detail or "Cannot POST" in detail:
            body["hint"] = ("Abort endpoint unreachable. This Maxun build should "
                            "expose /storage/runs/abort/:id over x-api-key; if you see "
                            "404/401, your Maxun version gates abort behind a session.")
        return body
    return {
        "ok": True,
        "action": "abort_run",
        "run_id": run_id,
        "result": body,
    }


def _duplicate(robot_id: str, target_url: str):
    if not robot_id:
        return {"error": "robot_id is required for duplicate"}
    if not target_url or not target_url.strip():
        return {"error": "target_url is required for duplicate"}
    parsed = urlparse(target_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"error": "target_url must be a valid http(s) URL"}
    body = _req("POST", f"/api/robots/{robot_id}/duplicate", json_body={"targetUrl": target_url})
    if "error" in body:
        return body
    robot = body.get("robot", body.get("data", {})) or {}
    return {
        "ok": True,
        "action": "duplicate",
        "source_robot_id": robot_id,
        "new_robot_id": robot.get("recording_meta", {}).get("id") or robot.get("id"),
        "name": robot.get("recording_meta", {}).get("name") or robot.get("name"),
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def maxun_tool(action: str, task_id: str = None, **kwargs) -> str:
    action = (action or "").strip().lower()
    if not _maxun_available():
        return json.dumps({
            "error": "Maxun is not configured",
            "detail": "Set MAXUN_API_KEY and MAXUN_API_URL (hermes setup / /secret) to enable the maxun tool.",
        })
    handlers = {
        "list_robots": lambda: _list_robots(),
        "get_robot": lambda: _get_robot(str(kwargs.get("robot_id", ""))),
        "create_ai_robot": lambda: _create_ai_robot(
            str(kwargs.get("url", "")), str(kwargs.get("prompt", "")),
            str(kwargs.get("robot_name", "")),
            str(kwargs.get("llm_provider", "")), str(kwargs.get("llm_model", "")),
            str(kwargs.get("llm_api_key", ""))),
        "create_search": lambda: _create_search(
            str(kwargs.get("query", "")), str(kwargs.get("mode", "")),
            int(kwargs.get("limit", 0) or 0)),
        "run": lambda: _run(
            str(kwargs.get("robot_id", "")),
            list(kwargs.get("formats", []) or []), str(kwargs.get("save_to", "")),
            bool(kwargs.get("wait", True))),
        "list_runs": lambda: _list_runs(str(kwargs.get("robot_id", ""))),
        "get_run": lambda: _get_run(str(kwargs.get("robot_id", "")), str(kwargs.get("run_id", ""))),
        "abort_run": lambda: _abort_run(str(kwargs.get("run_id", ""))),
        "duplicate": lambda: _duplicate(str(kwargs.get("robot_id", "")), str(kwargs.get("target_url", ""))),
    }
    if not action:
        return json.dumps({"error": "action is required", "actions": sorted(handlers.keys())})
    fn = handlers.get(action)
    if fn is None:
        return json.dumps({"error": f"unknown action '{action}'", "actions": sorted(handlers.keys())})
    result = fn()
    # Output is already compacted by the action handlers (row-capped previews,
    # saved full data to a file). Never truncate mid-JSON -- an agent/parser
    # needs valid JSON back. MAX_INLINE_CHARS is enforced structurally via
    # _compact_run, so the result is always small and parseable.
    return json.dumps(result, indent=2, default=str)


MAXUN_SCHEMA = {
    "name": "maxun",
    "description": (
        "Turn websites into structured data via a self-hosted or cloud Maxun "
        "instance (REST API). Create an extraction robot from a plain-English "
        "prompt (create_ai_robot), a web-search robot (create_search), run robots "
        "to capture tables/lists/markdown/screenshots, abort runaway runs, and "
        "inspect history. Requires MAXUN_API_KEY + MAXUN_API_URL (default "
        "http://localhost:8080). Full run data is saved to a file. Put "
        "navigation/pagination in the create_ai_robot prompt -- run cannot re-plan it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Operation to perform.",
                "enum": [
                    "list_robots",
                    "get_robot",
                    "create_ai_robot",
                    "create_search",
                    "run",
                    "list_runs",
                    "get_run",
                    "abort_run",
                    "duplicate",
                ],
            },
            "robot_id": {"type": "string", "description": "Maxun robot id (from list_robots / create_*)."},
            "run_id": {"type": "string", "description": "A specific run id (get_run / abort_run)."},
            "prompt": {
                "type": "string",
                "description": "Natural-language extraction goal for create_ai_robot, e.g. 'scrape the top 25 products with name, price and rating'. Put navigation/pagination here (scroll to the grid, click first card) -- run cannot re-plan it.",
            },
            "url": {"type": "string", "description": "Target page URL for create_ai_robot (optional; Maxun can search for the site from the prompt). Prefer a deep listing URL over a homepage."},
            "robot_name": {"type": "string", "description": "Optional robot name for create_ai_robot (must be unique; Maxun 409s on duplicates)."},
            "llm_provider": {"type": "string", "description": "Optional LLM provider for create_ai_robot (e.g. openai, anthropic) -- supply when the self-hosted Maxun has no server-side LLM configured."},
            "llm_model": {"type": "string", "description": "Optional LLM model for create_ai_robot (paired with llm_provider/llm_api_key)."},
            "llm_api_key": {"type": "string", "description": "Optional LLM API key for create_ai_robot (paired with llm_provider/llm_model)."},
            "query": {"type": "string", "description": "Search query for create_search."},
            "mode": {"type": "string", "description": "create_search mode: 'discover' (find URLs, default) or 'scrape' (also scrape them)."},
            "limit": {"type": "integer", "description": "create_search result limit (1-50)."},
            "wait": {"type": "boolean", "description": "run only: true (default) blocks until the run completes and returns data; false fires the run and returns run_id immediately for polling/abort."},
            "formats": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional output overrides for run: any of markdown,html,text,links,summary,screenshot-visible,screenshot-fullpage.",
            },
            "target_url": {"type": "string", "description": "New target URL for duplicate."},
            "save_to": {"type": "string", "description": "Optional absolute path to save the full run JSON (else a profile-aware default is used)."},
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
