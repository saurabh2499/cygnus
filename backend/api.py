"""Backend API for the vendor-management system.

Endpoints (per the project spec):
  1. POST /requests                     register a request (called by the chatbot)
  2. GET  /requests/{request_id}/summary return the summary for a request
  3. POST /requests/{request_id}/status  approve a vendor for a request

The API only reads and writes the shared store; the cron job picks up registered
requests and the engine agent produces the summaries.

Run: uvicorn backend.api:app --reload
"""

import logging
import os
from pathlib import Path

# Emit our INFO logs (cron progress, engine steps, errors) to stdout so they show
# up in Cloud Run logs. uvicorn doesn't configure app loggers by default.
logging.basicConfig(level=logging.INFO, force=True)
for _noisy in ("google_adk", "google_genai", "httpx", "google.api_core", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from engine_agent import db

from . import auth, notify

app = FastAPI(title="Vendor Management API", version="0.1.0")

_STATIC_DIR = Path(__file__).parent / "static"


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class RegisterRequest(BaseModel):
    request_id: str = Field(..., description="Unique id for the request (from the chatbot).")
    requirements: dict = Field(..., description="Approved requirements for the request.")
    max_retries: int | None = Field(
        None, description="Override the job's max processing attempts."
    )


class RegisterResponse(BaseModel):
    request_id: str
    status: str


class VendorResult(BaseModel):
    vendor_id: str
    status: str
    rank: int | None = None
    summary: str | None = None
    approved: bool = False


class SummaryResponse(BaseModel):
    request_id: str
    requirements: dict = {}
    job_status: str
    result_status: str
    final_summary: str | None = None
    ranking: list[dict] = []
    vendors: list[VendorResult] = []


class StatusUpdate(BaseModel):
    vendor_id: str = Field(..., description="The vendor being approved/unapproved.")
    approved: bool = Field(True, description="True to approve the vendor for this request.")


class SignupRequest(BaseModel):
    email: str
    password: str
    account_type: str = Field(..., description="'individual' or 'corporate'")
    name: str | None = None
    company: str | None = None
    team_budget: float | None = None
    budget_currency: str | None = "USD"
    budget_expiry: str | None = None  # ISO date, corporate only


class LoginRequest(BaseModel):
    email: str
    password: str


# --------------------------------------------------------------------------- #
# Auth (lightweight: hashed password + signed session cookie)
# --------------------------------------------------------------------------- #

def _current_user(request: Request) -> dict | None:
    token = request.cookies.get(auth.COOKIE_NAME)
    email = auth.read_token(token) if token else None
    if not email:
        return None
    user = db.get_user(email)
    return auth.public_user(user) if user else None


def _set_session(response: Response, email: str) -> None:
    response.set_cookie(auth.COOKIE_NAME, auth.make_token(email), httponly=True,
                        samesite="lax", max_age=60 * 60 * 24 * 7, path="/")


@app.post("/auth/signup")
def signup(payload: SignupRequest, response: Response) -> dict:
    email = (payload.email or "").strip().lower()
    if not email or not payload.password:
        raise HTTPException(status_code=400, detail="Email and password are required")
    if payload.account_type not in ("individual", "corporate"):
        raise HTTPException(status_code=400, detail="account_type must be 'individual' or 'corporate'")
    if payload.account_type == "corporate" and (
            not payload.company or not payload.budget_expiry):
        raise HTTPException(status_code=400,
                            detail="Corporate accounts need a company and access expiry")
    if db.get_user(email):
        raise HTTPException(status_code=409, detail="Email already registered")
    user = db.create_user(
        email=email, password_hash=auth.hash_password(payload.password),
        account_type=payload.account_type, name=payload.name, company=payload.company,
        team_budget=payload.team_budget, budget_currency=payload.budget_currency,
        budget_expiry=payload.budget_expiry,
    )
    if not user:
        raise HTTPException(status_code=409, detail="Email already registered")
    _set_session(response, email)
    return auth.public_user(user)


@app.post("/auth/login")
def login(payload: LoginRequest, response: Response) -> dict:
    email = (payload.email or "").strip().lower()
    user = db.get_user(email)
    if not user or not auth.verify_password(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    _set_session(response, email)
    return auth.public_user(user)


@app.post("/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"status": "ok"}


@app.get("/auth/me")
def me(request: Request) -> dict:
    user = _current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.post("/requests", response_model=RegisterResponse, status_code=201)
def register_request(payload: RegisterRequest, request: Request) -> RegisterResponse:
    """Registers a request and enqueues it for the cron job to process."""
    if not payload.requirements:
        raise HTTPException(status_code=400, detail="requirements must not be empty")
    requirements = dict(payload.requirements)
    # Remember who to email on booking confirmation (the logged-in client).
    user = _current_user(request)
    if user and not requirements.get("client_email"):
        requirements["client_email"] = user.get("email")
    max_retries = payload.max_retries if payload.max_retries is not None else db.DEFAULT_MAX_RETRIES
    db.register_request(payload.request_id, requirements, max_retries)
    return RegisterResponse(request_id=payload.request_id, status="queued")


@app.get("/requests/{request_id}/summary", response_model=SummaryResponse)
def get_summary(request_id: str) -> SummaryResponse:
    """Returns the processing status, final summary, ranking, and per-vendor results."""
    result = db.get_request_result(request_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown request {request_id}")
    return SummaryResponse(**result)


@app.post("/requests/{request_id}/status")
async def update_status(request_id: str, payload: StatusUpdate) -> dict:
    """Approves (or unapproves) a vendor. On approval, calls the restaurant to
    confirm the booking and emails the client a confirmation."""
    ok = db.set_vendor_approval(request_id, payload.vendor_id, payload.approved)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"No call on record for vendor {payload.vendor_id} on request {request_id}",
        )

    confirmation = {"call": None, "email": None}
    if payload.approved:
        confirmation = await _confirm_booking(request_id, payload.vendor_id)

    return {
        "status": "success",
        "request_id": request_id,
        "vendor_id": payload.vendor_id,
        "approved": payload.approved,
        "confirmation": confirmation,
    }


async def _confirm_booking(request_id: str, vendor_id: str) -> dict:
    """Places a confirmation call to the restaurant and emails the client."""
    from engine_agent import telephony

    result = db.get_request_result(request_id) or {}
    req = result.get("requirements", {})
    vendor = next((v for v in db.list_vendors() if v["id"] == vendor_id), {})
    vendor_name = vendor.get("name", vendor_id)
    db.set_request_stage(request_id, "booking")

    event = req.get("event_type", "event")
    guests = req.get("guest_count", "the")
    when = req.get("date", "the planned date")
    msg = (f"Hello from the event planning assistant. Great news — the client has "
           f"approved {vendor_name} for their {event} on {when} for about {guests} guests. "
           f"This call confirms the booking. We'll follow up by email with the details. Thank you!")

    out = {"call": "skipped", "email": "skipped"}
    # Confirmation call to the restaurant (override number in test mode).
    if telephony.is_configured():
        to = telephony.override_number() or vendor.get("phone")
        if to:
            res = await telephony.place_announcement(to, msg)
            out["call"] = res.get("status", "error")

    # Email the client.
    client_email = req.get("client_email")
    if client_email:
        subject = f"Booking confirmed: {vendor_name}"
        body = (f"Hi,\n\nYour {event} on {when} for about {guests} guests is confirmed with "
                f"{vendor_name}.\n\nSummary:\n{result.get('final_summary', '')}\n\n"
                f"We've also called the restaurant to confirm.\n\n— Vendor Management")
        out["email"] = "sent" if notify.send_email(client_email, subject, body) else "failed"
    db.set_request_stage(request_id, "completed")
    return out


@app.get("/requests")
def list_requests() -> list[dict]:
    """Lists every request with its job and result status (newest first)."""
    return db.list_requests()


@app.get("/vendors")
def list_vendors() -> list[dict]:
    """Returns the vendor catalog (used by the dashboard)."""
    return db.list_vendors()


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    """Serves the web dashboard."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.post("/internal/cron/sweep", include_in_schema=False)
async def cron_sweep() -> dict:
    """Runs one cron sweep. Triggered by Cloud Scheduler on a schedule.

    Lets the cron run on Cloud Run with NO always-on worker and NO min-instances
    (so the service scales to zero and stays free): Cloud Scheduler POSTs here,
    the instance wakes, runs the sweep to completion, then scales back down. The
    sweep is awaited (not backgrounded) so it finishes while the request is in
    flight — a backgrounded task could be frozen when the instance scales down.
    Give the Scheduler job a generous attempt deadline, and secure this endpoint
    with an OIDC token in production.
    """
    from scheduler.cron import CronConfig, run_sweep

    processed = await run_sweep(CronConfig())
    return {"status": "ok", "processed": processed}


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Twilio voice webhooks (fetched by Twilio over the public PUBLIC_BASE_URL)
# --------------------------------------------------------------------------- #

def _xml(body: str) -> Response:
    return Response(content='<?xml version="1.0" encoding="UTF-8"?>' + body,
                    media_type="application/xml")


def _say(text: str) -> str:
    from xml.sax.saxutils import escape
    voice = os.environ.get("TWILIO_VOICE", "alice")
    lang = os.environ.get("TWILIO_LANGUAGE", "en-US")
    return f'<Say voice="{voice}" language="{lang}">{escape(text)}</Say>'


def _ask(token: str, question: str) -> str:
    """A <Gather> that asks one question and waits for the vendor's answer.

    `timeout` is how long to wait for them to START speaking (default 5s is too
    short when someone pauses to think); `speechTimeout=auto` ends on their pause.
    On silence the <Redirect> re-enters /twiml/turn, which re-asks once before
    moving on."""
    from xml.sax.saxutils import escape
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    turn = escape(f"{base}/twiml/turn?token={token}")
    wait = os.environ.get("TWILIO_GATHER_TIMEOUT", "10")
    return (
        f'<Gather input="speech" speechTimeout="auto" timeout="{wait}" '
        f'method="POST" action="{turn}">'
        f"{_say(question)}</Gather>"
        f'<Redirect method="POST">{turn}</Redirect>'
    )


@app.api_route("/twiml/call", methods=["GET", "POST"], include_in_schema=False)
async def twiml_call(request: Request) -> Response:
    """Starts the call: greet, then ask the first requirement question."""
    token = request.query_params.get("token", "")
    st = db.get_call_state(token)
    if not st:
        return _xml(f"<Response>{_say('Sorry, there was a problem. Goodbye.')}<Hangup/></Response>")
    if not st.get("questions"):
        # Announcement (e.g. booking confirmation): just speak and hang up.
        return _xml(f"<Response>{_say(st.get('greeting', ''))}<Hangup/></Response>")
    return _xml(f"<Response>{_say(st['greeting'])}{_ask(token, st['questions'][0])}</Response>")


@app.api_route("/twiml/turn", methods=["GET", "POST"], include_in_schema=False)
async def twiml_turn(request: Request) -> Response:
    """Records the answer to the current question and asks the next one, until done."""
    token = request.query_params.get("token", "")
    st = db.get_call_state(token)
    if not st:
        return _xml(f"<Response>{_say('Thank you. Goodbye.')}<Hangup/></Response>")

    speech = ""
    try:
        form = await request.form()
        speech = (form.get("SpeechResult", "") or "").strip()
    except Exception:
        pass

    questions = st.get("questions", [])
    idx = st.get("idx", 0)

    # Silence on the current question: re-ask once before giving up on it, so a
    # pause doesn't skip the question or end the call prematurely.
    if not speech and idx < len(questions):
        if not st.get("retried"):
            st["retried"] = True
            db.set_call_state(token, st)
            return _xml(
                f"<Response>{_say('Sorry, I did not catch that.')}"
                f"{_ask(token, questions[idx])}</Response>")

    # Record the answer (or '(no response)' after a retry) and advance.
    if idx < len(questions):
        st.setdefault("answers", []).append(
            {"q": questions[idx], "a": speech or "(no response)"})
        idx += 1
        st["idx"] = idx
        st["retried"] = False

    if idx < len(questions):
        db.set_call_state(token, st)
        # Brief, varied, human acknowledgement — never "next question".
        acks = ["Got it.", "Okay.", "Sure.", "Thanks.", "Makes sense.", "Perfect."]
        ack = (acks[idx % len(acks)] + " ") if speech else ""
        return _xml(f"<Response>{_say(ack)}{_ask(token, questions[idx])}</Response>")

    # All requirements covered — finish and store the transcript.
    lines = [f"Agent: {st.get('greeting','')}".strip()]
    for qa in st.get("answers", []):
        lines += [f"Agent: {qa['q']}", f"Vendor: {qa['a']}"]
    lines.append("Agent: Thank you, that's everything. We'll follow up by email. Goodbye.")
    st["transcript"] = "\n".join(l for l in lines if l)
    st["status"] = "done"
    db.set_call_state(token, st)
    return _xml(f"<Response>{_say('Thank you, that is everything. Goodbye.')}<Hangup/></Response>")
