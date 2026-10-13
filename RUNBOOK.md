# Runbook — Running & Validating Vendor Management

Step-by-step guide to run the system and confirm every component works. For the
architecture, see [README.md](README.md).

The system has three runnable parts that share one SQLite database:

| Part | Command | Needs API key? |
| ---- | ------- | -------------- |
| Backend API | `uvicorn backend.api:app` | No |
| Cron worker | `python -m scheduler.cron` | Yes (to run the engine) |
| Chatbot agent | `adk web` / `adk run cygnus` | Yes |

---

## 1. One-time setup

```bash
# from the project root
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create your `.env` from the template and add your key:

```bash
cp .env.example .env
# edit .env and set:
#   ANTHROPIC_API_KEY=sk-ant-...
```

Get a key at https://console.anthropic.com/settings/keys (and add billing credits).
`.env` is git-ignored. The database and its sample vendor catalog are created
automatically on first use — no migration step.

---

## 2. Quick validation (no API key needed)

Run the offline validator. It exercises imports, the database, vendor matching,
the job queue + retry cap, a cron sweep, the API endpoints, and the chatbot→queue
wiring against a throwaway database:

```bash
python scripts/validate.py
```

Expected: 13 `[PASS]` lines ending in `RESULT: all checks passed` (exit code 0).
If anything prints `[FAIL]`, the detail on that line says what was wrong — see
[Troubleshooting](#5-troubleshooting).

This is the fastest "is everything wired correctly?" check and is safe to run any
time; it touches no real data and makes no network calls.

---

## 3. Run the system

Open three terminals (virtualenv activated in each), or background the first two.

### a) Backend API

```bash
uvicorn backend.api:app --host 127.0.0.1 --port 8000
```

Validate it's up:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/docs    # expect 200
```

Interactive docs: http://127.0.0.1:8000/docs

### b) Cron worker

```bash
python -m scheduler.cron --interval 30     # poll every 30s
# or process once and exit:
python -m scheduler.cron --once
```

Expect a startup log line like `cron started: interval=30s concurrency=5 max_retries=3`.

### c) Chatbot (optional, interactive)

```bash
adk web                 # browser UI; choose "event_requirements_agent"
# or:
adk run cygnus
```

---

## 4. End-to-end validation (with API key + both services running)

This confirms the full pipeline: register → cron → engine → parallel vendor calls
→ ranked summary.

```bash
# 1) Register a request
curl -s -X POST http://127.0.0.1:8000/requests \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"check-1","requirements":{
        "event_type":"company offsite dinner","guest_count":60,
        "location":"downtown","budget":"$8000 USD",
        "must_haves":["catering","av"],"nice_to_haves":["vegan"]}}'
# expect: {"request_id":"check-1","status":"queued"}

# 2) Watch it get processed (the cron worker picks it up within one interval)
curl -s http://127.0.0.1:8000/requests/check-1/summary
# job_status goes: pending -> in_progress -> completed   (takes ~30s)

# 3) Inspect the ranked result once result_status == "completed"
curl -s http://127.0.0.1:8000/requests/check-1/summary | python -m json.tool
# expect: a non-empty "final_summary", a "ranking" list (best-first with
#         rank/vendor_id/reason), and per-vendor "summary" entries.

# 4) Approve the top vendor
curl -s -X POST http://127.0.0.1:8000/requests/check-1/status \
  -H 'Content-Type: application/json' \
  -d '{"vendor_id":"v_olive","approved":true}'
# expect: {"status":"success",...,"approved":true}
#   (use a vendor_id from the ranking in step 3)
```

**What "working fine" looks like:**
- Step 1 returns `"status":"queued"`.
- Step 2's `job_status` advances to `completed` on its own (no manual trigger).
- Step 3 has a real `final_summary` and a `ranking` with one entry per vendor that
  was called, ordered best-first.
- Step 4 flips that vendor's `approved` to `true` (re-fetch step 3 to confirm).

If you ran `--once` instead of the interval worker, run it again after step 1 to
process the job, then continue to step 3.

### Chatbot path (optional)

In `adk web`, tell the agent about an event, approve the summary — it returns a
**request ID**. Then ask it, "what's the status of request `<id>`?" It should
report the processing status and, once the cron has run, the final ranking. The
same request also appears via `GET /requests/<id>/summary`.

---

## 5. Troubleshooting

| Symptom | Cause / fix |
| ------- | ----------- |
| `ModuleNotFoundError` running any command | Virtualenv not active, or deps not installed. `source .venv/bin/activate && pip install -r requirements.txt`. |
| Job stuck, cron log shows "No Anthropic credential" and `-> retry_scheduled` | `ANTHROPIC_API_KEY` not set/valid. Fix `.env`, then re-run the cron; queued jobs are retried, not lost. |
| Job reaches `failed` | The engine failed `max_retries` times. Check the cron log for the error; `last_error` is stored on the job. |
| `GET /summary` returns 404 | Unknown `request_id` — check the ID. |
| `POST /status` returns 404 | That vendor wasn't called for this request — use a `vendor_id` from the ranking. |
| Port 8000 already in use | Another process is bound; stop it or run uvicorn with `--port 8001`. |
| Want a clean slate | Delete the database file (default `cygnus/requirements.db`, or `$VENDOR_DB_PATH`). It is recreated and reseeded on next use. |

---

## 6. Stopping

- Terminal: `Ctrl+C` in each window.
- Backgrounded uvicorn/cron: find and stop them, e.g.
  `pkill -f "uvicorn backend.api"` and `pkill -f "scheduler.cron"`.
