# Manual Test Guide — Vendor Management

Follow top to bottom to exercise every component once, with a **validation** check
after each step. Each step says what to run and what you should see (✅).

> **Venv:** all commands assume your virtualenv is active:
> `source ~/.venv/bin/activate` (that's where the deps are installed).
> Run everything from the project root: `cd ~/test-agent`.

---

## 0. Setup check

```bash
cd ~/test-agent
source ~/.venv/bin/activate
python -c "import google.adk, fastapi, twilio, google.cloud.firestore; print('deps OK')"
test -f .env && echo ".env present" || echo "MISSING .env (copy from .env.example)"
```
✅ Prints `deps OK` and `.env present`. `.env` should have `ANTHROPIC_API_KEY` (and optionally `TAVILY_API_KEY`, Twilio keys).

---

## 1. Offline validation (fast, no cost, no network)

Exercises imports, DB, matching, job queue + retry, cron, API, chatbot wiring on an in-memory mock Firestore.

```bash
python scripts/validate.py
```
✅ 13 `[PASS]` lines, ends with `RESULT: all checks passed` (exit 0).

---

## 2. Component tests (isolated)

### 2a. Chatbot (needs ANTHROPIC_API_KEY)
```bash
python scripts/test_chatbot.py
```
✅ Shows the conversation + tool calls (`update_requirements`, `get_requirements_summary`, `record_approval`), ends with a `request_id` and `RESULT: chatbot PASS ✅`.

### 2b. Cron (no LLM, mocked engine)
```bash
python scripts/test_cron.py
```
✅ 6 `[PASS]` lines: one engine spawned per job, bounded concurrency, success completes, **failing job retries then fails at the cap**, atomic claim, idle sweep does nothing. `RESULT: cron PASS ✅`.

### 2c. Engine + calling agents (needs ANTHROPIC_API_KEY; ~40–60s)
```bash
python scripts/test_engine.py
```
✅ Shows `get_past_calls → discover_vendors → call_vendors (spawns N calling agents IN PARALLEL) → save_vendor_summary ×N → save_final_summary`, then 5 `[PASS]` checks and `RESULT: engine PASS ✅`.
(If `TAVILY_API_KEY` is set it discovers live venues; otherwise it falls back to the seeded catalog.)

### 2d. Backend API (no LLM, TestClient)
```bash
python scripts/test_api.py
```
✅ 9 `[PASS]` lines (register/list/summary/approve/404s/catalog/dashboard). `RESULT: backend API PASS ✅`.

---

## 3. Bring the app up (two terminals)

**Terminal A — backend + dashboard:**
```bash
source ~/.venv/bin/activate && cd ~/test-agent
uvicorn backend.api:app --host 127.0.0.1 --port 8000
```
✅ Logs `Application startup complete` and `Uvicorn running on http://127.0.0.1:8000`.

**Terminal B — cron worker:**
```bash
source ~/.venv/bin/activate && cd ~/test-agent
python -m scheduler.cron --interval 20
```
✅ Logs `cron started: interval=20s concurrency=5 max_retries=3`.

**Validate both (Terminal C):**
```bash
curl -s -o /dev/null -w "backend %{http_code}\n" http://127.0.0.1:8000/     # expect 200
```

> **Tip — test with SIMULATED calls** (recommended unless you've got the ngrok tunnel up):
> start the cron in Terminal B with telephony disabled so the chain completes cleanly:
> `PUBLIC_BASE_URL="" TWILIO_FROM_NUMBER="" python -m scheduler.cron --interval 20`

---

## 4. Dashboard (manual, visual)

Open **http://127.0.0.1:8000/**

1. Click **+ New request**, fill in: event type, guests, **date**, location (use a real one like `downtown Chicago`), budget, must-haves (`catering, av`), nice-to-haves (`vegan`). Submit.
   ✅ Toast "Request queued"; it appears in the list as **Pending**.
2. Watch the badge.
   ✅ It moves **Pending → Processing → Completed** on its own within ~30–60s (the cron picks it up).
3. Click the request.
   ✅ You see the requirement chips (incl. **Date**), a **final summary**, and a **ranked vendor list** with reasons.
4. Click **Approve this vendor** on #1.
   ✅ Shows **✓ approved** (writes a `user_confirmation`).

---

## 5. Backend API (manual, curl)

```bash
# Register
curl -s -X POST http://127.0.0.1:8000/requests -H 'Content-Type: application/json' \
  -d '{"request_id":"man-1","requirements":{"event_type":"team dinner","guest_count":40,"date":"2026-12-12","location":"downtown Chicago","budget":"$5000","must_haves":["catering","av"],"nice_to_haves":["vegan"]}}'
# expect: {"request_id":"man-1","status":"queued"}

# Poll until completed (repeat)
curl -s http://127.0.0.1:8000/requests/man-1/summary | python -m json.tool
# expect: job_status/result_status go pending -> completed; non-empty final_summary + ranking

# Approve top vendor (use a vendor_id from the ranking above)
curl -s -X POST http://127.0.0.1:8000/requests/man-1/status -H 'Content-Type: application/json' \
  -d '{"vendor_id":"<id-from-ranking>","approved":true}'
# expect: {"status":"success",...,"approved":true}

# Error cases
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/requests/nope/summary   # expect 404
```
✅ All responses as noted; the dashboard reflects the same request.

---

## 6. Chatbot (manual, interactive)

```bash
adk web           # open the printed URL, choose "event_requirements_agent"
# or in terminal:
adk run cygnus
```
Have a conversation ("plan a dinner for 60 in downtown Chicago, Dec 12, $8000, must have catering and AV, vegan nice-to-have"), approve when asked.
✅ It asks sensible follow-ups, refuses to approve while required info is missing, and on approval returns a **request ID**. Then ask "what's the status of request `<id>`?" → it reports status (and ranking once processed). That request also shows on the dashboard.

---

## 7. Discovery cache (only searches new locations)

With `TAVILY_API_KEY` set and the app up:

1. Register a request with a **brand-new** location (e.g. `SoHo New York`). Watch it complete.
   ✅ First time for that location → the engine does a live web search (engine log/processing takes a bit longer).
2. Register a **second** request with the **same** location.
   ✅ Second time → it reuses cached venues (no new search; faster). Verify:
```bash
python - <<'PY'
from engine_agent import db
print("cached venues for 'soho new york':",
      [v['name'] for v in db.get_web_vendors_for_location('soho new york')])
PY
```
✅ Prints the venues discovered the first time.

---

## 8. Calling agent

### 8a. Simulated (default when Twilio/ngrok not reachable)
If you started the cron with telephony disabled (step 3 tip), the calling agents generate transcripts in-process.
```bash
# after a request completes, inspect the per-vendor call records:
python - <<'PY'
from engine_agent import db
rid="man-1"   # a completed request id
for c in db.get_calls(rid):
    print(c['vendor_id'], c['status'], '| transcript:', (c['transcript'] or '')[:60])
PY
```
✅ One record per vendor with a transcript and `status=summarized` → proves **engine spawned calling agents**.

### 8b. Real Twilio calls (optional — needs a public URL)
Real calls need Twilio to fetch TwiML from a public URL. Locally that means an **ngrok tunnel to port 8000**; on Cloud Run the public URL is automatic.
```bash
# in another terminal, with ngrok installed:
ngrok http --domain=<your-domain>.ngrok-free.dev 8000
# verify Twilio can reach the TwiML (must return XML, not an HTML error page):
curl -s "<PUBLIC_BASE_URL>/twiml/call?token=t&script=hi" | head -c 40
```
✅ Returns `<?xml ... <Response><Say ...`. Then a processed request dials **CALL_OVERRIDE_NUMBER** — the phone rings, you hear the inquiry, speak a reply after the tone, and it's captured into the transcript.
⚠️ If the curl returns an HTML/ngrok error page, the tunnel isn't reaching the backend — fix that first (real calls will fail until it does).

---

## 9. Database (Firestore)

Local runs use Firestore too — start the emulator (`gcloud emulators firestore start --host-port=localhost:8080`) or use a real project.
```bash
export FIRESTORE_EMULATOR_HOST=localhost:8080
python -c "from engine_agent import db; print('backend:', db.backend())"   # expect: firestore
# then run the app (steps 3–5) with these env vars set; data lands in Firestore collections
```
✅ Requests/restaurants/summaries/confirmations appear in the emulator (or real Firestore on GCP).

---

## 10. Teardown

```bash
# Ctrl+C in Terminals A and B, or:
pkill -f "uvicorn backend.api"
pkill -f "scheduler.cron"
```
✅ `curl http://127.0.0.1:8000/` fails (connection refused) → everything stopped.

---

## Quick reference — what each piece proves

| Step | Proves |
| ---- | ------ |
| 1 | Whole system wired correctly (offline) |
| 2a–2d | Each component works in isolation |
| 3–5 | Live app: cron → engine → calling agents → ranked result → approval |
| 6 | Chatbot gather/approve/status |
| 7 | Discovery searches new locations, caches known ones |
| 8 | Calling agent (simulated and, with a tunnel, real Twilio calls) |
| 9 | Firestore (emulator / real project) connection |
