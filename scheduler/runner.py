"""Runs one engine agent to process a single request.

The cron job calls :func:`process_request` once per claimed job. There are two
execution modes:

- **Agent Engine** (production): if ``AGENT_ENGINE_RESOURCE`` is set, the engine
  agent runs on Vertex AI Agent Engine — the cron just invokes the deployed agent
  there and waits; the agent's tools write results to Firestore.
- **In-process** (local/fallback): otherwise the engine runs here via an
  InMemoryRunner.

Either way success is judged from the DB (the engine marks the request
'completed'), not the model's prose.
"""

import os
import uuid

from engine_agent import db

APP_NAME = "vendor_engine_agent"


def _agent_engine_resource() -> str | None:
    return os.environ.get("AGENT_ENGINE_RESOURCE")


def _result(request_id: str) -> dict:
    result = db.get_request_result(request_id)
    status = result["result_status"] if result else "unknown"
    if status == "completed":
        return {"ok": True, "status": status, "error": None}
    return {"ok": False, "status": status,
            "error": f"engine finished without completing (status={status})"}


async def process_request(request_id: str) -> dict:
    """Drives the engine agent end to end for one request (Agent Engine or local)."""
    resource = _agent_engine_resource()
    if resource:
        return await _run_on_agent_engine(resource, request_id)
    return await _run_in_process(request_id)


async def _run_on_agent_engine(resource: str, request_id: str) -> dict:
    """Invokes the engine agent deployed on Vertex AI Agent Engine."""
    import asyncio

    import vertexai
    from vertexai import agent_engines

    def _invoke():
        vertexai.init(
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        )
        ae = agent_engines.get(resource)
        for _event in ae.stream_query(
            user_id="cron", message=f"Process request {request_id}."
        ):
            pass  # drive to completion; the agent writes results to Firestore

    try:
        await asyncio.to_thread(_invoke)
    except Exception as exc:
        return {"ok": False, "status": "error", "error": str(exc)}
    return _result(request_id)


async def _run_in_process(request_id: str) -> dict:
    """Runs the engine agent locally via an InMemoryRunner."""
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    from engine_agent.agent import root_agent

    runner = InMemoryRunner(agent=root_agent, app_name=APP_NAME)
    user_id = "cron"
    session_id = f"job-{request_id}-{uuid.uuid4().hex[:8]}"
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    message = types.Content(
        role="user", parts=[types.Part(text=f"Process request {request_id}.")]
    )
    try:
        async for _event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=message
        ):
            pass
    except Exception as exc:
        return {"ok": False, "status": "error", "error": str(exc)}
    return _result(request_id)
