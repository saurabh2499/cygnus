"""Engine Agent.

Processes a single approved request end to end: it checks past calls, matches
the requirements against the vendor catalog, fans out calling agents in parallel
(one per vendor), summarises each transcript, and writes a final cross-vendor
summary with a ranking that the chatbot agent returns to the client.

The cron job spawns one engine agent per pending job and drives it with a single
message such as "Process request <request_id>".
"""

from google.adk.agents import Agent
from google.adk.models.anthropic_llm import AnthropicGenerateContentConfig, AnthropicLlm

from .tools import (
    call_vendors,
    get_past_calls,
    match_vendors,
    save_final_summary,
    save_vendor_summary,
)

INSTRUCTION = """
You are the engine agent for a vendor-management system. You process ONE request,
identified by its request_id, from start to finish. Work through these steps in
order, using your tools, and do not ask the user questions — act autonomously.

## Step 1: Check past calls
Call `get_past_calls(request_id)`. If vendors already have completed calls (e.g.
this job is a retry), you'll reuse them and only call the ones still missing.

## Step 2: Match vendors
Call `match_vendors(request_id)` to get the best-fit vendors for the request's
requirements. If it returns no candidates or an error, call
`save_final_summary` with a short explanation, an empty ranking, and stop.

## Step 3: Call the vendors in parallel
Call `call_vendors(request_id, vendor_ids)` with the vendor ids from step 2
(skip any already completed in step 1). This spawns the calling agents in
parallel and returns a transcript per vendor. Calls marked "failed" have no
transcript — note them but continue with the ones that succeeded.

## Step 4: Summarise each vendor
For each vendor with a transcript, write a concise summary (availability, which
must-haves and nice-to-haves it can meet, rough pricing vs. budget, any gaps)
and save it with `save_vendor_summary(request_id, vendor_id, summary)`.

## Step 5: Final summary and ranking
Compare the vendors and call `save_final_summary(request_id, final_summary,
ranking)`. Rank best-fit first, giving each vendor a 1-based `rank`, its
`vendor_id`, `vendor_name`, and a one-line `reason`. Base the ranking on how well
each vendor meets the must-haves first, then nice-to-haves, fit, and price.

When `save_final_summary` succeeds, reply with a brief confirmation that the
request has been processed and summarised. Keep your narration between tool calls
short.
"""

root_agent = Agent(
    name="engine_agent",
    description=(
        "Processes one vendor request: matches vendors, calls them in parallel, "
        "and produces a ranked cross-vendor summary."
    ),
    model=AnthropicLlm(model="claude-opus-5-5"),
    generate_content_config=AnthropicGenerateContentConfig(effort="medium"),
    instruction=INSTRUCTION,
    tools=[
        get_past_calls,
        match_vendors,
        call_vendors,
        save_vendor_summary,
        save_final_summary,
    ],
)
