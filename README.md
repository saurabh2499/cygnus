# Cygnus

A multi-agent system that finds vendors (restaurants and hotels), phones them on the
client's behalf, negotiates, and returns a ranked summary of the best-fit options. The
client approves one, and Cygnus books it.

It is built mainly for **corporate firms that host events or book travel regularly**, and
also for small startups with no dedicated admin team.

The system is built around three cooperating agents — a **Chatbot Agent**, an **Engine
Agent**, and a **Calling Agent** — coordinated by a cron job and backed by a shared
Firestore database and a backend API with a web dashboard.

## Overview

```
Client ──► Dashboard / Chatbot Agent ──► Backend API ──► DB (request + requirements)
                                                              │
                                                         Cron Job (Cloud Scheduler polls for jobs)
                                                              │  spawns 1 per job (retry ≤ 3, configurable)
                                                              ▼
                                                       Engine Agent ──► discovers nearby vendors (web search)
                                                              │          and shortlists the top 3
                                                              │  spawns in parallel, 1 per vendor
                                                              ▼
                                                       Calling Agent(s) ──► call the vendor (Twilio)
                                                              │
                                                              ▼  transcripts
                                                       Engine Agent ──► per-vendor summaries + final ranked summary ──► DB
                                                              │
                                                              ▼
                                                       Dashboard ──► client approves one vendor
                                                              │
                                                              ▼
                                                       Confirmation call to the vendor + email to the client
```

Two request types are supported: **restaurant / venue** (team dinners, offsites) and
**hotel** (stays, with check-in, check-out and rooms).

## Agents

### 1. Chatbot Agent

Handles interaction with the client when requirements are gathered by chat. The dashboard
form is the main way to create requests (restaurant or hotel); the chatbot covers
restaurant requests only.

- Interacts with the client and gathers requirements.
- Summarises the requirements, distinguishing **mandatory** from **optional** criteria.
- Gets the client's approval on the final set of requirements.
- Creates an entry in the DB with a unique **request ID** and the summarised
  requirement list.
- On a status query (by request ID), fetches the current status from the DB and shows
  it to the client.

### 2. Cron Job

- Fetches the list of pending jobs from the DB.
- Spawns one **Engine Agent** per job to process that request.
- Retries a failed entry up to **3 times** (configurable).
- In the cloud this is **Cloud Scheduler** calling `POST /internal/cron/sweep` every
  2 minutes; locally, run `python -m scheduler.cron`.

### 3. Engine Agent

Orchestrates the vendor outreach for a single request. Runs on Gemini; deployed on
Vertex AI Agent Engine (or in-process locally).

- Gets the status of past calls made to vendors for the request.
- **Discovers** real vendors near the location with live web search (Tavily), tagging
  restaurants or hotels separately, and caches them per location.
- Determines the **top 3** best-fit vendors by matching requirements (type, distance,
  amenities, budget) against vendor features.
- Spawns **Calling Agents** in parallel — one per vendor — passing the requirement
  context to each.
- Waits for each Calling Agent's response (the conversation transcript).
- Produces a per-vendor summary from each transcript and logs it to the DB.
- Produces a final cross-vendor summary, including a **ranking** of the vendors, and
  logs it to the DB. Vendors that did not answer are still listed, marked as not reached.

### 4. Calling Agent

- Places a real call to the vendor through **Twilio** and holds a multi-turn conversation:
  - confirms it has reached the right place ("Am I speaking with …?") and ends the call
    politely on a wrong number;
  - states the dates (hotels: check-in, check-out and nights) without being asked;
  - repeats a question when asked ("come again?") and waits on "hold on";
  - asks for the price and **never volunteers the budget**; it works out the total
    (hotels: nightly rate × rooms × nights) and only if that is over budget says so and
    asks for a discount.
- Returns the transcript to the Engine Agent.
- The dialogue is a guided script handled by the backend call webhooks; Gemini is not in
  the live loop. Without Twilio configured it falls back to a simulated transcript.

## Backend API

Implemented with FastAPI in [backend/api.py](backend/api.py). Interactive docs are at `/docs`.

| Method & path | Purpose |
| ------------- | ------- |
| `POST /auth/signup`, `/auth/login`, `/auth/logout`, `GET /auth/me` | Create an account (individual or corporate), sign in or out (session cookie). |
| `POST /requests` | Register a request and enqueue a job. Requires login; the request belongs to that user. Past dates are rejected; hotel requests need `check_out` after `date`. |
| `GET /requests` | List the logged-in user's requests. Other users' requests are never returned. |
| `GET /requests/{request_id}/summary` | Return the status, final summary, ranking, and per-vendor results with profile details (owner only, otherwise 404). |
| `POST /requests/{request_id}/status` | Approve (or unapprove) a vendor. Only one vendor can be approved per request. Approval triggers the confirmation call and email. |
| `GET /vendors` | The vendor catalog. |
| `POST /internal/cron/sweep` | Run one job sweep (called by Cloud Scheduler). |
| `/twiml/call`, `/twiml/turn` | Twilio webhooks that drive the live call. |

Requests, summaries and approvals require a logged-in user, and requests are private to the
user who created them. Requests created before this was added are matched to their
`client_email`, and requests created through the chatbot are not tied to any user, so
create requests from the dashboard or the API.

A request body looks like this (`vendor_type` is `restaurant` by default):

```json
{"request_id": "hotel-1", "requirements": {
  "vendor_type": "hotel", "location": "downtown Chicago",
  "date": "2026-12-01", "check_out": "2026-12-04", "rooms": 3, "guest_count": 6,
  "budget": "3000 USD", "must_haves": ["breakfast", "wifi"], "nice_to_haves": ["pool"]}}
```

## Request Lifecycle

1. **Gather** — the client submits a request in the dashboard (or the Chatbot Agent
   collects it); it is written to the DB with a unique request ID, owned by the user.
2. **Dispatch** — Cron job picks up the pending request and spawns an Engine Agent
   (retrying up to 3 times on failure).
3. **Discover & match** — Engine Agent finds real vendors nearby and shortlists the top 3.
4. **Call** — Engine Agent spawns Calling Agents in parallel; each calls its vendor and
   returns a transcript. The dashboard shows each vendor as *Calling now…* until its call ends.
5. **Summarise & rank** — Engine Agent writes per-vendor summaries and a final ranked
   summary to the DB.
6. **Approve** — the client approves one vendor in the dashboard; the vendor gets a
   confirmation call and the client gets an email.

## Project Layout

```
cygnus/          Chatbot Agent (gathers & approves requirements)   [ADK]
engine_agent/    Engine Agent + Calling Agent + shared DB layer     [ADK]
  agent.py          engine root_agent and its tool wiring
  tools.py          discover/match vendors, parallel vendor calls, summaries
  discovery.py      live vendor search (restaurants and hotels)
  calling_agent.py  the per-vendor calling agent and call scripts
  telephony.py      Twilio calls
  secret_store.py   call override numbers, read from Secret Manager
  db.py             shared Firestore store (requests, vendors, calls, users)
scheduler/       Cron job that spawns one engine agent per pending job
  cron.py           poll loop + retry handling (CLI)
  runner.py         runs the engine agent for a single request
backend/         FastAPI backend and dashboard
  api.py            endpoints and Twilio webhooks
  auth.py           accounts and session cookies
  call_helpers.py   live-call helpers (wrong-number check, quote vs budget)
  notify.py         confirmation email
  static/index.html the web dashboard
scripts/         validate.py and test_*.py
requirements.txt Python dependencies
```

All components share one Firestore database. Locally, run the Firestore emulator
(`FIRESTORE_EMULATOR_HOST=localhost:8080`) or use a real project via
`gcloud auth application-default login` + `GOOGLE_CLOUD_PROJECT`.

## Getting Started

> For detailed run **and validation** steps (including an offline check script and
> an end-to-end test), see [RUNBOOK.md](RUNBOOK.md). To deploy on Google Cloud, see
> [DEPLOY.md](DEPLOY.md).

### Prerequisites

- Python 3.11+ (developed on 3.14).
- A Google Cloud project with Firestore (Native mode) and Vertex AI — the agents run on
  Gemini. (Set `MODEL_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` to run on Claude locally.)
- Optional: a Tavily key (live vendor discovery), a Twilio account and number (real
  calls), SMTP credentials (confirmation email).

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
export MODEL_PROVIDER="gemini"
export GOOGLE_GENAI_USE_VERTEXAI="TRUE"
export GOOGLE_CLOUD_PROJECT="your-project"      # Firestore + Vertex AI project
export GOOGLE_CLOUD_LOCATION="us-central1"
gcloud auth application-default login
# export FIRESTORE_EMULATOR_HOST=localhost:8080  # or use the local emulator
# export TAVILY_API_KEY=...                      # live vendor discovery
```

Firestore collections are created implicitly on first write — there is no migration step.

### 3. Run the components

The project has three runnable parts. For the full flow you typically run all three
(each in its own terminal, with the virtualenv activated).

**a) Chatbot Agent** (optional) — talk to it to gather restaurant requirements:

```bash
adk web                 # browser UI; pick "event_requirements_agent" (cygnus)
# or run it in the terminal:
adk run cygnus
```

**b) Cron Job** — picks up queued requests and runs the engine agent for each:

```bash
python -m scheduler.cron --once          # process everything due once, then exit
python -m scheduler.cron --interval 30   # keep polling every 30 seconds
```

**c) Backend API and dashboard** — create requests / view rankings / approve vendors:

```bash
uvicorn backend.api:app --reload         # serves on http://127.0.0.1:8000
```

Open `http://127.0.0.1:8000` for the dashboard (sign up, then **New request** with a
**Restaurant** or **Hotel** tab). Interactive API docs are at `http://127.0.0.1:8000/docs`.
Real calls also need the Twilio settings below and a public `PUBLIC_BASE_URL` (for example an
ngrok tunnel) so Twilio can reach the webhooks; without them calls are simulated.

### 4. Set the call override numbers (testing)

While testing, send every vendor call to your own phone instead of the vendors' real
numbers. The numbers are kept in a Google Secret Manager secret (`CALL_OVERRIDE_NUMBERS`),
never in the API, the database or an environment variable:

```bash
# comma-separated, E.164 format; repeat a number to route several calls to the same phone
printf '%s' '+919876543210,+919876543211' | gcloud secrets versions add CALL_OVERRIDE_NUMBERS --data-file=-
```

The Nth vendor call in a batch dials the Nth number; the last number is reused when there
are more calls than numbers. The calling code reads the latest secret version each time it
dials, so there is nothing to redeploy. With no numbers set, calls go to the vendors' real
phone numbers. One-time secret setup is in [DEPLOY.md](DEPLOY.md#call-override-numbers-secret-manager).

### End-to-end quickstart (API only)

You can drive the whole pipeline without the dashboard:

```bash
# 0. Create an account and keep the session cookie (use /auth/login next time)
curl -c cookies.txt -X POST http://127.0.0.1:8000/auth/signup \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"choose-a-password","account_type":"individual"}'

# 1. Register a request
curl -b cookies.txt -X POST http://127.0.0.1:8000/requests \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"demo-1","requirements":{
        "event_type":"company offsite dinner","guest_count":60,
        "location":"downtown","budget":"$8000 USD",
        "must_haves":["catering","av"],"nice_to_haves":["vegan"]}}'

# 2. Let the cron job process it (spawns the engine + calling agents)
python -m scheduler.cron --once

# 3. Fetch the ranked summary
curl -b cookies.txt http://127.0.0.1:8000/requests/demo-1/summary

# 4. Approve the chosen vendor (use a vendor_id from the ranking)
curl -b cookies.txt -X POST http://127.0.0.1:8000/requests/demo-1/status \
  -H 'Content-Type: application/json' \
  -d '{"vendor_id":"<vendor_id>","approved":true}'
```

### Configuration

| Variable | Applies to | Default | Purpose |
| -------- | ---------- | ------- | ------- |
| `MODEL_PROVIDER` | all agents | `anthropic` | `gemini` (Vertex AI) or `anthropic`. |
| `GEMINI_MODEL` | all agents | `gemini-3.6-flash` | Gemini model id (the deployed app uses `gemini-2.5-flash`). |
| `GOOGLE_GENAI_USE_VERTEXAI` | all agents | — | `TRUE` to use Gemini on Vertex AI. |
| `GOOGLE_CLOUD_PROJECT` | all components | — | Firestore and Vertex AI project (or use `FIRESTORE_EMULATOR_HOST`). |
| `GOOGLE_CLOUD_LOCATION` | all agents | — | Vertex AI region, e.g. `us-central1`. |
| `ANTHROPIC_API_KEY` | all agents | — | Only when `MODEL_PROVIDER=anthropic`. |
| `TAVILY_API_KEY` | engine | — | Live vendor discovery; without it the seeded catalog is used. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | engine, backend | — | Real calls (with `PUBLIC_BASE_URL`); if any is missing, calls are simulated. |
| `PUBLIC_BASE_URL` | engine, backend | — | Public URL Twilio uses for the call webhooks. |
| `TWILIO_VOICE`, `TWILIO_LANGUAGE` | backend | `alice`, `en-US` | Voice and speech language for calls. |
| `AGENT_ENGINE_RESOURCE` | cron, backend | — | Vertex AI Agent Engine resource; if unset the engine runs in-process. |
| `SESSION_SECRET` | backend | dev value | Signs the login cookie; set a long random value in production. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM` | backend | — | Booking confirmation email (skipped if not set). |
| `CALL_OVERRIDE_NUMBER` | engine, backend | — | Local-dev fallback only (comma-separated). In the cloud, use the `CALL_OVERRIDE_NUMBERS` Secret Manager secret. |
| `CRON_INTERVAL` | cron | `30` | Seconds between poll sweeps. |
| `CRON_MAX_CONCURRENCY` | cron | `5` | Max engine agents run at once. |
| `CRON_BATCH` | cron | `10` | Jobs claimed per sweep. |
| `CRON_MAX_RETRIES` | cron | `3` | Attempts per job before it is marked failed. |
| `CRON_RETRY_BACKOFF` | cron | `60` | Seconds to wait before a retry. |

## Status

Working end to end: the dashboard (restaurant and hotel requests), engine and calling
agents with real Twilio calls, ranking, one-vendor approval, confirmation call and email,
per-user requests, and deployment on Google Cloud (Cloud Run, Vertex AI Agent Engine,
Cloud Scheduler, Firestore, Secret Manager).

Known limits: the live call dialogue follows a guided script rather than a live Gemini
voice; vendor phone numbers come from web search and are not always available (test calls
use the override numbers); and the chatbot handles restaurant requests only.
