# Maxun self-host recipe (for E2E testing the `maxun` tool)

## docker-compose `.env` essentials

```
NODE_ENV=production
JWT_SECRET=<openssl rand -base64 48>
DB_NAME=maxun
DB_USER=postgres
DB_PASSWORD=<openssl rand -base64 24>
DB_HOST=postgres
DB_PORT=5432
ENCRYPTION_KEY=<openssl rand -base64 64>
SESSION_SECRET=<openssl rand -base64 48>
MINIO_ENDPOINT=minio
MINIO_PORT=9000
MINIO_ACCESS_KEY=minio
MINIO_SECRET_KEY=<openssl rand -base64 24>
BACKEND_PORT=8080
FRONTEND_PORT=5173
BACKEND_URL=http://localhost:8080
PUBLIC_URL=http://localhost:5173
VITE_BACKEND_URL=http://localhost:8080
VITE_PUBLIC_URL=http://localhost:5173
BROWSER_WS_HOST=browser        # REQUIRED for create_ai_robot browser builder
BROWSER_WS_PORT=3001
BROWSER_HEALTH_PORT=3002
MAXUN_TELEMETRY=false
```

`docker compose up -d`, then wait ~30s for postgres/minio/backend/browser to come up.

## API key (self-hosted needs a user first)

```
curl -s -X POST http://localhost:8080/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"hermes@local.test","password":"HermesTest123!","name":"Hermes"}'
curl -s -c cookies.txt -X POST http://localhost:8080/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"hermes@local.test","password":"HermesTest123!"}'
curl -s -b cookies.txt -X POST http://localhost:8080/auth/generate-api-key
# -> {"message":"API key generated successfully","api_key":"..."}
```

Set `MAXUN_API_KEY=<api_key>` and `MAXUN_API_URL=http://localhost:8080` in
`~/.hermes/.env` (or export them for a test run).

## Gotchas (debugged during the `maxun` tool build)

- Auth routes are under `/auth/*` (NOT `/api/auth/*`). `/api/*` is the
  key-authenticated robot/run surface.
- `create_ai_robot` drives a REAL browser via the `browser` service. Without
  `BROWSER_WS_HOST=browser` it fails: "Failed to initialize browser after 3
  attempts: Could not launch local browser."
- Maxun dedupes robots by name → `create_ai_robot` returns HTTP 409 if the same
  name+prompt+url already exists. Reuse the existing robot id from `list_robots`.
- Backend health: `curl -o /dev/null -w '%{http_code}' http://localhost:8080/api/robots`
  returns 401 (unauth) when no key, 403 with a bogus key — both confirm the
  auth middleware is live.
- Stop the stack: `docker compose down` (in the repo dir with the `.env`).

## Patching the abort route (so `abort_run` works over the API key)

Upstream Maxun gates `/storage/runs/abort/:id` behind `requireSignIn` (session
cookie), so the `x-api-key` header cannot reach it — `abort_run` returns
404/401. Fix: add an `requireAPIKey`-guarded variant of the route, registered
BEFORE the session one (Express uses first match).

File: `server/src/routes/storage.ts`.

1. Add the import (next to the existing `requireSignIn` import):
   ```ts
   import { requireAPIKey } from '../middlewares/api';
   ```
2. Insert this block BEFORE the existing `router.post('/runs/abort/:id', requireSignIn, ...)`
   route (so the API-key variant wins for key-auth clients):
   ```ts
   router.post('/runs/abort/:id', requireAPIKey, async (req: AuthenticatedRequest, res) => {
     try {
       if (!req.user) { return res.status(401).json({ error: 'Unauthorized' }); }
       const run = await Run.findOne({ where: { runId: req.params.id } });
       if (!run) { return res.status(404).json({ error: 'Run not found' }); }
       const robot = await Robot.findOne({ where: { 'recording_meta.id': run.robotMetaId, userId: req.user.id } });
       if (!robot) { return res.status(404).json({ error: 'Run not found' }); }
       if (!['running', 'queued'].includes(run.status)) {
         return res.status(400).json({ error: `Cannot abort run with status: ${run.status}` });
       }
       const isQueued = run.status === 'queued';
       await run.update({ status: 'aborting' });
       if (isQueued) {
         await run.update({ status: 'aborted', finishedAt: new Date().toLocaleString(), log: 'Run aborted while queued' });
         return res.json({ success: true, message: 'Queued run aborted', isQueued: true });
       }
       try {
         const browser = browserPool.getRemoteBrowser(run.browserId);
         if (browser && browser.interpreter) { await browser.interpreter.stopInterpretation(); }
       } catch (e: any) { logger.log('warn', `Failed to stop interpreter: ${e.message}`); }
       const jobId = await addJob(QUEUE_NAMES.ABORT_RUN, { userId: req.user.id, runId: req.params.id }, { maxAttempts: 3 });
       return res.json({ success: true, message: 'Run stopped immediately, cleanup queued', jobId, isQueued: false });
     } catch (e) { const { message } = e as Error; return res.status(500).json({ error: 'Failed to abort run' }); }
   });
   ```
3. Enable the local build in `docker-compose.yml` (the `build:` stanza is
   commented by default):
   ```yaml
   backend:
     build:
       context: .
       dockerfile: Dockerfile.backend
     # image: getmaxun/maxun-backend:latest
   ```
4. Rebuild + recreate (full build ~3-5 min; layer-cached on repeat):
   ```bash
   docker compose build backend
   docker compose up -d --force-recreate backend
   ```
5. Verify: `POST /storage/runs/abort/<valid-uuid>` with `x-api-key` header
   returns `400 {"error":"Cannot abort run with status: success"}` for a finished
   run (correct — proves the route is reachable over the key). A non-UUID id
   returns `500` (Postgres rejects before the not-found check) — that's expected
   input validation, not a route failure.

## Synchronous run behavior (why `wait=false` still returns a finished run)

Maxun's `POST /robots/{id}/runs` executes the run to completion **server-side
before the HTTP response returns**. So even with `wait=false`, the `run_id`
comes back alongside a `success` status — there is no client-observable
`running` window for fast robots. Consequences:

- `abort_run` can only cancel a run that is still `running`/`queued` when the
  request lands. On a lightly-loaded instance with small robots, the run may
  finish before an out-of-band abort arrives (you'll see `400 Cannot abort run
  with status: success`). This is correct behavior, not a bug.
- To demonstrate a successful cancellation, use a genuinely long scrape (many
  pages) on a busy instance, or abort a `queued` run (one waiting for a browser
  slot). The `abort-run` queue job (`QUEUE_NAMES.ABORT_RUN`) does the cleanup.
- For polling patterns, `wait=false` + `get_run`/`list_runs` is still useful for
  runs whose server-side execution outlives your agent's patience — just don't
  expect `run` to return "in progress".

