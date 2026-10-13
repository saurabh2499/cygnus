"""Backend API for the vendor-management system.

Endpoints (per the project spec):
  1. POST /requests                     register a request (called by the chatbot)
  2. GET  /requests/{request_id}/summary return the summary for a request
  3. POST /requests/{request_id}/status  approve a vendor for a request

The API only reads and writes the shared store; the cron job picks up registered
requests and the engine agent produces the summaries.

Run: uvicorn backend.api:app --reload
"""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from engine_agent import db

from . import auth

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
            not payload.company or payload.team_budget is None or not payload.budget_expiry):
        raise HTTPException(status_code=400,
                            detail="Corporate accounts need company, team_budget and budget_expiry")
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
def register_request(payload: RegisterRequest) -> RegisterResponse:
    """Registers a request and enqueues it for the cron job to process."""
    if not payload.requirements:
        raise HTTPException(status_code=400, detail="requirements must not be empty")
    max_retries = payload.max_retries if payload.max_retries is not None else db.DEFAULT_MAX_RETRIES
    db.register_request(payload.request_id, payload.requirements, max_retries)
    return RegisterResponse(request_id=payload.request_id, status="queued")


@app.get("/requests/{request_id}/summary", response_model=SummaryResponse)
def get_summary(request_id: str) -> SummaryResponse:
    """Returns the processing status, final summary, ranking, and per-vendor results."""
    result = db.get_request_result(request_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Unknown request {request_id}")
    return SummaryResponse(**result)


@app.post("/requests/{request_id}/status")
def update_status(request_id: str, payload: StatusUpdate) -> dict:
    """Approves (or unapproves) a vendor for a request."""
    ok = db.set_vendor_approval(request_id, payload.vendor_id, payload.approved)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"No call on record for vendor {payload.vendor_id} on request {request_id}",
        )
    return {
        "status": "success",
        "request_id": request_id,
        "vendor_id": payload.vendor_id,
        "approved": payload.approved,
    }


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
    return Response(content=body, media_type="application/xml")


@app.api_route("/twiml/call", methods=["GET", "POST"], include_in_schema=False)
async def twiml_call(request: Request) -> Response:
    """Returns the TwiML for a vendor call: speak the inquiry, then gather a reply."""
    from xml.sax.saxutils import escape

    token = request.query_params.get("token", "")
    script = request.query_params.get("script", "Hello, this is an event planning assistant.")
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    voice = os.environ.get("TWILIO_VOICE", "alice")
    lang = os.environ.get("TWILIO_LANGUAGE", "en-US")
    action = f"{base}/twiml/collect?token={token}"
    return _xml(
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Say voice="{voice}" language="{lang}">{escape(script)}</Say>'
        f'<Gather input="speech" speechTimeout="auto" method="POST" action="{escape(action)}">'
        f'<Say voice="{voice}" language="{lang}">Please tell us about your availability '
        "and pricing after the tone.</Say>"
        "</Gather>"
        f'<Say voice="{voice}" language="{lang}">Thank you. We will follow up by email. Goodbye.</Say>'
        "</Response>"
    )


@app.api_route("/twiml/collect", methods=["GET", "POST"], include_in_schema=False)
async def twiml_collect(request: Request) -> Response:
    """Receives the vendor's recognised speech and stores it against the token."""
    from xml.sax.saxutils import escape

    token = request.query_params.get("token", "")
    speech = ""
    try:
        form = await request.form()
        speech = form.get("SpeechResult", "") or ""
    except Exception:
        pass
    if token and speech:
        db.set_call_reply(token, speech)
    voice = os.environ.get("TWILIO_VOICE", "alice")
    lang = os.environ.get("TWILIO_LANGUAGE", "en-US")
    return _xml(
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Say voice="{voice}" language="{lang}">'
        f'{escape("Thank you, that is all we need. Goodbye.")}</Say></Response>'
    )
