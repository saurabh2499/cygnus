# Vendor Management

A multi-agent system that gathers client requirements, reaches out to vendors on the
client's behalf, and returns a ranked summary of the best-fit options.

The system is built around three cooperating agents — a **Chatbot Agent**, an **Engine
Agent**, and a **Calling Agent** — coordinated by a cron job and backed by a shared
database and a backend API.

## Overview

```
Client ──► Chatbot Agent ──► DB (request + requirements)
                                   │
                              Cron Job (polls for jobs)
                                   │  spawns 1 per job (retry ≤ 3, configurable)
                                   ▼
                            Engine Agent ──► matches vendors to requirements
                                   │  spawns in parallel, 1 per vendor
                                   ▼
                            Calling Agent(s) ──► call vendor, transcribe audio
                                   │
                                   ▼  transcripts
                            Engine Agent ──► per-vendor summaries + final ranked summary ──► DB
                                   │
                                   ▼
                            Chatbot Agent ──► returns final summary to client
```

## Agents

### 1. Chatbot Agent

Handles all direct interaction with the client.

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

### 3. Engine Agent

Orchestrates the vendor outreach for a single request.

- Gets the status of past calls made to vendors for the request.
- Determines the set of best-fit vendors by matching requirements against vendor
  features.
- Spawns **Calling Agents** in parallel — one per vendor — passing the requirement
  context to each.
- Waits for each Calling Agent's response (the conversation transcript).
- Produces a per-vendor summary from each transcript and logs it to the DB.
- Produces a final cross-vendor summary, including a **ranking** of the vendors, and
  logs it to the DB. The Chatbot Agent uses this to return the final summary for the
  request.

### 4. Calling Agent

- Places the call to the vendor provided by the Engine Agent.
- Transcribes the audio conversation and returns the transcript to the Engine Agent.

## Backend API

Implemented with FastAPI in [backend/api.py](backend/api.py).

| Method & path | Purpose |
| ------------- | ------- |
| `POST /requests` | Register a request and enqueue a job (called by the Chatbot Agent). |
| `GET /requests/{request_id}/summary` | Return the status, final summary, ranking, and per-vendor results. |
| `POST /requests/{request_id}/status` | Approve (or unapprove) a vendor for a request. |

## Request Lifecycle

1. **Gather** — Chatbot Agent collects and summarises requirements, gets approval, and
   writes a request (with a unique request ID) to the DB.
2. **Dispatch** — Cron job picks up the pending request and spawns an Engine Agent
   (retrying up to 3 times on failure).
3. **Match** — Engine Agent selects best-fit vendors from the requirements.
4. **Call** — Engine Agent spawns Calling Agents in parallel; each calls its vendor and
   returns a transcript.
5. **Summarise & rank** — Engine Agent writes per-vendor summaries and a final ranked
   summary to the DB.
6. **Return** — Chatbot Agent surfaces the final summary and ranking to the client; on
   approval, the status is updated via the API.

## Project Layout

```
cygnus/          Chatbot Agent (gathers & approves requirements)   [ADK]
engine_agent/    Engine Agent + Calling Agent + shared DB layer     [ADK]
  agent.py          engine root_agent and its tool wiring
  tools.py          match vendors, parallel vendor calls, summaries
  calling_agent.py  the per-vendor calling agent (one spawned per call)
  db.py             shared SQLite store (requirements, calls, jobs, summaries)
scheduler/       Cron job that spawns one engine agent per pending job
  cron.py           poll loop + retry handling (CLI)
  runner.py         runs the engine agent for a single request
backend/         FastAPI backend (register / summary / status endpoints)
requirements.txt Python dependencies
```

All components share one SQLite database. By default it is `cygnus/requirements.db`;
override with the `VENDOR_DB_PATH` environment variable.

## Getting Started

### Prerequisites

- Python 3.11+ (developed on 3.14).
- An Anthropic API key — the chatbot, engine, and calling agents run on Claude.

### 1. Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
export ANTHROPIC_API_KEY="sk-ant-..."   # required to run the agents
# Optional — all components default to cygnus/requirements.db:
export VENDOR_DB_PATH="/path/to/vendor_management.db"
```

The shared SQLite database and its sample vendor catalog are created automatically on
first use — there is no separate migration step.

### 3. Run the components

The project has three runnable parts. For the full flow you typically run all three
(each in its own terminal, with the virtualenv activated).

**a) Chatbot Agent** — talk to it to gather requirements and get a request ID:

```bash
adk web                 # browser UI; pick "event_requirements_agent" (cygnus)
# or run it in the terminal:
adk run cygnus
```

On approval the chatbot saves the requirements and enqueues a job automatically.

**b) Cron Job** — picks up queued requests and runs the engine agent for each:

```bash
python -m scheduler.cron --once          # process everything due once, then exit
python -m scheduler.cron --interval 30   # keep polling every 30 seconds
```

**c) Backend API** — register requests / fetch summaries / approve vendors:

```bash
uvicorn backend.api:app --reload         # serves on http://127.0.0.1:8000
```

Interactive API docs are at `http://127.0.0.1:8000/docs`.

### End-to-end quickstart (API only)

You can drive the whole pipeline without the chatbot UI:

```bash
# 1. Register a request
curl -X POST http://127.0.0.1:8000/requests \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"demo-1","requirements":{
        "event_type":"company offsite dinner","guest_count":60,
        "location":"downtown","budget":"$8000 USD",
        "must_haves":["catering","av"],"nice_to_haves":["vegan"]}}'

# 2. Let the cron job process it (spawns the engine + calling agents)
python -m scheduler.cron --once

# 3. Fetch the ranked summary
curl http://127.0.0.1:8000/requests/demo-1/summary

# 4. Approve the chosen vendor
curl -X POST http://127.0.0.1:8000/requests/demo-1/status \
  -H 'Content-Type: application/json' \
  -d '{"vendor_id":"v_olive","approved":true}'
```

### Configuration

| Variable | Applies to | Default | Purpose |
| -------- | ---------- | ------- | ------- |
| `ANTHROPIC_API_KEY` | all agents | — | Auth for the Claude model backend. |
| `VENDOR_DB_PATH` | all components | `cygnus/requirements.db` | Shared SQLite database location. |
| `CRON_INTERVAL` | cron | `30` | Seconds between poll sweeps. |
| `CRON_MAX_CONCURRENCY` | cron | `5` | Max engine agents run at once. |
| `CRON_BATCH` | cron | `10` | Jobs claimed per sweep. |
| `CRON_MAX_RETRIES` | cron | `3` | Attempts per job before it is marked failed. |
| `CRON_RETRY_BACKOFF` | cron | `60` | Seconds to wait before a retry. |

## Status

Early-stage project. The chatbot, engine, calling agents, cron job, and backend API
are implemented; telephony in the calling agent is simulated (it generates a realistic
transcript) and is the intended integration point for a real voice provider.
