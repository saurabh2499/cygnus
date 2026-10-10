"""Backend API for the vendor-management system.

Endpoints (per the project spec):
  1. POST /requests                     register a request (called by the chatbot)
  2. GET  /requests/{request_id}/summary return the summary for a request
  3. POST /requests/{request_id}/status  approve a vendor for a request

The API only reads and writes the shared store; the cron job picks up registered
requests and the engine agent produces the summaries.

Run: uvicorn backend.api:app --reload
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from engine_agent import db

app = FastAPI(title="Vendor Management API", version="0.1.0")


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
    job_status: str
    result_status: str
    final_summary: str | None = None
    ranking: list[dict] = []
    vendors: list[VendorResult] = []


class StatusUpdate(BaseModel):
    vendor_id: str = Field(..., description="The vendor being approved/unapproved.")
    approved: bool = Field(True, description="True to approve the vendor for this request.")


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
