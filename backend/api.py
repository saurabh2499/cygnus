"""Backend API for the Cygnus system.

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
import random
import re
from datetime import date, datetime
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

from . import auth, call_helpers, notify

app = FastAPI(title="Cygnus API", version="0.1.0")

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
    profile: dict = {}


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

def _parse_event_date(value) -> date | None:
    """Best-effort parse of a requirements date; None for rough timeframes."""
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@app.post("/requests", response_model=RegisterResponse, status_code=201)
def register_request(payload: RegisterRequest, request: Request) -> RegisterResponse:
    """Registers a request and enqueues it for the cron job to process."""
    if not payload.requirements:
        raise HTTPException(status_code=400, detail="requirements must not be empty")
    requirements = dict(payload.requirements)
    vtype = requirements.setdefault("vendor_type", "restaurant")
    if vtype not in ("restaurant", "hotel"):
        raise HTTPException(status_code=400, detail="vendor_type must be 'restaurant' or 'hotel'")
    event_date = _parse_event_date(requirements.get("date"))
    if event_date and event_date < date.today():
        raise HTTPException(
            status_code=400,
            detail="Check-in date cannot be in the past" if vtype == "hotel"
            else "Event date cannot be in the past")
    if vtype == "hotel":
        out = _parse_event_date(requirements.get("check_out"))
        if out and (out < date.today() or (event_date and out <= event_date)):
            raise HTTPException(status_code=400,
                                detail="Check-out must be after the check-in date")
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
    if payload.approved:
        current = db.get_request_result(request_id) or {}
        other = next((v for v in current.get("vendors", [])
                      if v.get("approved") and v.get("vendor_id") != payload.vendor_id), None)
        if other:
            raise HTTPException(
                status_code=409,
                detail="Another vendor is already approved for this request. Unapprove it first.")
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
    hotel = req.get("vendor_type") == "hotel"
    if hotel:
        stay = f"from {when} to {req['check_out']}" if req.get("check_out") else f"on {when}"
        rooms = f"{req['rooms']} rooms" if req.get("rooms") else "a room"
        msg = (f"Hello from the booking assistant. Great news — the client has approved "
               f"{vendor_name} for {rooms} {stay}. This call confirms the booking. "
               f"We'll follow up by email with the details. Thank you!")
    else:
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
        what = (f"Your hotel stay from {when} to {req.get('check_out', '?')}" if hotel
                else f"Your {event} on {when} for about {guests} guests")
        entry = next((v for v in result.get("vendors", []) if v.get("vendor_id") == vendor_id), {})
        pts = [str(f).replace("_", " ").capitalize() for f in (vendor.get("features") or [])]
        pts += [str(c).capitalize() for c in (vendor.get("cuisine_types") or [])]
        if vendor.get("area"):
            pts.append(f"Located in {vendor['area']}")
        # What the vendor told us on the call (rates, inclusions, policies), one point each.
        pts += [x.strip().rstrip(".") for x in re.split(r"(?<=[.!?])\s+", entry.get("summary") or "")
                if len(x.strip()) > 3]
        points = "\n".join(f"  • {p}" for p in dict.fromkeys(pts)) or "  • Details will follow from the vendor"
        body = (f"Hi,\n\n{what} is confirmed with {vendor_name}.\n\n"
                f"Facilities & details:\n{points}\n\n"
                f"We've also called the {'hotel' if hotel else 'restaurant'} to confirm.\n\n"
                f"Thanks & Regards,\nCygnus")
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


_REPEAT_RE = re.compile(
    r"\b(repeat|say (that|it|this) again|come again|pardon|didn'?t (quite )?(catch|hear|get)|"
    r"can'?t (quite )?hear|what was (that|the question)|one more time|once more|"
    r"could you (please )?(rephrase|clarify)|what do you mean|i don'?t (understand|follow)|"
    r"say that slowly|too fast|you('re| are) breaking up)\b", re.I)
_SHORT_HUH_RE = re.compile(r"^\W*(what|huh|sorry|excuse me|hello|hm+|come again)\W*$", re.I)
_HOLD_RE = re.compile(
    r"\b(hold on|hang on|one (moment|second|sec|minute)|just a (moment|second|sec|minute)|"
    r"give me a (moment|second|minute)|let me (check|see|ask|look|find)|wait)\b", re.I)
_WHO_RE = re.compile(r"\b(who (is|are) (this|you)|who'?s (this|calling)|what('s| is) this (about|regarding)|"
                     r"why are you calling)\b", re.I)

_REPEAT_LEADS = ["Sure, no problem — ", "Of course, sorry about that. ", "Yeah, let me say that again. ",
                 "Sorry, I'll put it another way. ", "No worries. "]
_HOLD_LINES = ["Sure, take your time.", "No rush, I'll wait.", "Of course, go ahead."]


from xml.sax.saxutils import escape as escape_xml  # noqa: E402


def _wants_repeat(speech: str) -> bool:
    return bool(_REPEAT_RE.search(speech) or _SHORT_HUH_RE.match(speech))


def _simplify(question: str) -> str:
    """A shorter, plainer rewording for a second ask: drop filler up to the first dash/comma."""
    for sep in (" — ", ", "):
        if sep in question and len(question.split(sep, 1)[1]) > 15:
            return question.split(sep, 1)[1].strip().capitalize()
    return question


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

    # Human-style handling of what the vendor said, before treating it as an answer.
    # (They ask us to repeat / hold on / say who we are: respond and stay on the
    # same question rather than moving forward.)
    if speech and idx < len(questions):
        q = questions[idx]
        if _HOLD_RE.search(speech) and st.get("holds", 0) < 2 and len(speech.split()) <= 8:
            st["holds"] = st.get("holds", 0) + 1
            db.set_call_state(token, st)
            wait = os.environ.get("TWILIO_GATHER_TIMEOUT", "10")
            turn = escape_xml(f"{os.environ.get('PUBLIC_BASE_URL', '').rstrip('/')}/twiml/turn?token={token}")
            return _xml(
                f"<Response>{_say(random.choice(_HOLD_LINES))}"
                f'<Gather input="speech" speechTimeout="auto" timeout="{max(int(wait), 20)}" '
                f'method="POST" action="{turn}"></Gather>'
                f'<Redirect method="POST">{turn}</Redirect></Response>')
        if _WHO_RE.search(speech) and st.get("intros", 0) < 1:
            st["intros"] = 1
            db.set_call_state(token, st)
            return _xml(
                f"<Response>{_say(st.get('greeting', ''))}{_ask(token, q)}</Response>")
        if _wants_repeat(speech) and st.get("repeats", 0) < 3:
            st["repeats"] = st.get("repeats", 0) + 1
            db.set_call_state(token, st)
            text = q if st["repeats"] == 1 else _simplify(q)
            return _xml(
                f"<Response>{_say(random.choice(_REPEAT_LEADS))}"
                f"{_ask(token, text)}</Response>")

    # Silence on the current question: re-ask once before giving up on it, so a
    # pause doesn't skip the question or end the call prematurely.
    if not speech and idx < len(questions):
        if not st.get("retried"):
            st["retried"] = True
            db.set_call_state(token, st)
            lead = random.choice(["Sorry, I didn't catch that. ", "Hello? Sorry, are you still there? ",
                                  "Sorry, I think the line cut out for a second. "])
            return _xml(
                f"<Response>{_say(lead)}"
                f"{_ask(token, questions[idx])}</Response>")

    # Record the answer (or '(no response)' after a retry) and advance.
    if idx < len(questions):
        st.setdefault("answers", []).append(
            {"q": questions[idx], "a": speech or "(no response)"})
        # Identity check: wrong place -> apologise and end the call.
        if questions[idx].startswith(call_helpers.IDENTITY_PREFIX) and call_helpers.denies_identity(speech):
            lines = [f"Agent: {st.get('greeting', '')}".strip()]
            for qa in st["answers"]:
                lines += [f"Agent: {qa['q']}", f"Vendor: {qa['a']}"]
            lines.append("Agent: Sorry about that, I must have the wrong number. Goodbye.")
            lines.append("[Wrong number: the vendor did not confirm it is the expected place.]")
            st["transcript"] = "\n".join(l for l in lines if l)
            st["status"] = "done"
            db.set_call_state(token, st)
            return _xml(f"<Response>{_say('Sorry about that, I must have the wrong number. Goodbye.')}<Hangup/></Response>")
        # Quote vs. budget: only raise the budget if the quote goes over it.
        if idx == st.get("price_idx") and st.get("budget") and speech:
            total = call_helpers.quote_total(st, speech)
            if total and total > st["budget"]:
                questions.insert(idx + 1, call_helpers.discount_question(st, total))
                st["questions"] = questions
        idx += 1
        st["idx"] = idx
        st["retried"] = False
        st["repeats"] = 0
        st["holds"] = 0

    if idx < len(questions):
        db.set_call_state(token, st)
        # Brief, varied, human acknowledgement — never "next question".
        acks = ["Got it.", "Okay, great.", "Sure, thanks.", "Alright.", "Makes sense.", "Perfect, thank you.",
                "Okay, good to know.", "Mm-hm, thanks."]
        ack = (random.choice(acks) + " ") if speech else ""
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
