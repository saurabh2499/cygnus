"""Isolated test of the Cron Job (scheduler).

Verifies the cron's own responsibilities without the real engine/LLM:
  A. fetches pending jobs and spawns exactly one engine per job
  B. honours bounded concurrency (max_concurrency)
  C. a successful job -> completed (not retried)
  D. a failing job is retried up to max_retries, then marked failed
  E. claiming is atomic (no job processed twice)

The engine run is mocked, so this is fast, free, and deterministic. Uses a
throwaway database, so the real running cron/engine are not involved.

Usage:  python scripts/test_cron.py
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["VENDOR_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="vm_cron_"), "cron.db"
)

import engine_agent.db as db          # noqa: E402
import scheduler.cron as cron         # noqa: E402

_failures = 0


def check(name, ok, detail=""):
    global _failures
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ->  {detail}" if detail else ""))
    if not ok:
        _failures += 1


REQS = {"event_type": "dinner", "guest_count": 40, "location": "downtown",
        "budget": "$3000", "must_haves": ["catering"]}


def seed(request_ids, max_retries=3):
    for rid in request_ids:
        db.register_request(rid, REQS, max_retries=max_retries)


async def main():
    db.init_db()

    # --- Mock engine: records which jobs it ran and the peak concurrency. ---
    calls = {}            # request_id -> number of times the engine was spawned
    active = 0
    peak = 0
    FLAKY = "job-flaky"   # this one always fails, to exercise retries

    async def fake_engine(request_id):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls[request_id] = calls.get(request_id, 0) + 1
        await asyncio.sleep(0.05)        # simulate work so overlap is observable
        active -= 1
        if request_id == FLAKY:
            return {"ok": False, "status": "error", "error": "simulated failure"}
        db.set_final_summary(request_id, "completed", "ok", [{"rank": 1, "vendor_id": "v_olive"}])
        return {"ok": True, "status": "completed", "error": None}

    cron.process_request = fake_engine

    cfg = cron.CronConfig()
    cfg.max_concurrency = 3
    cfg.retry_backoff = 0               # make retries immediately due
    cfg.max_retries = 3

    # --- Sweep 1: 5 good jobs + 1 flaky -------------------------------------
    good = [f"job-{i}" for i in range(5)]
    seed(good, max_retries=3)
    seed([FLAKY], max_retries=3)

    print("Sweep 1: 6 pending jobs (5 good, 1 flaky), max_concurrency=3\n")
    processed = await cron.run_sweep(cfg)

    # A. one engine per job
    spawned_once = all(calls.get(j, 0) == 1 for j in good) and calls.get(FLAKY, 0) == 1
    check("A. spawns exactly one engine per job", spawned_once and processed == 6,
          f"processed={processed}, calls={ {k: calls[k] for k in sorted(calls)} }")

    # B. bounded concurrency
    check("B. respects max_concurrency (peak <= 3)", 1 < peak <= 3, f"peak concurrency={peak}")

    # C. good jobs completed
    states = {db.get_request_result(j)["job_status"] for j in good}
    check("C. successful jobs marked completed (not retried)", states == {"completed"},
          f"states={states}")

    # D. flaky job retried then failed -- drive remaining sweeps
    for _ in range(5):
        st = db.get_request_result(FLAKY)["job_status"]
        if st in ("failed", "completed"):
            break
        await cron.run_sweep(cfg)
    flaky = db.get_request_result(FLAKY)
    with db.connect() as c:
        attempts = c.execute("SELECT attempt_count FROM jobs WHERE request_id=?", (FLAKY,)).fetchone()[0]
    check("D. failing job retried up to the cap then failed",
          flaky["job_status"] == "failed" and attempts == 3 and calls[FLAKY] == 3,
          f"status={flaky['job_status']}, attempts={attempts}, engine_runs={calls[FLAKY]}")

    # E. atomic claim: two concurrent sweeps must not double-process
    seed(["job-race-1", "job-race-2"])
    before = dict(calls)
    await asyncio.gather(cron.run_sweep(cfg), cron.run_sweep(cfg))
    double = any(calls.get(j, 0) > 1 for j in ("job-race-1", "job-race-2"))
    check("E. atomic claim — no job processed twice under concurrent sweeps",
          not double, f"race runs={ {j: calls.get(j,0) for j in ('job-race-1','job-race-2')} }")

    # Idle sweep processes nothing
    idle = await cron.run_sweep(cfg)
    check("F. idle sweep processes nothing when queue is drained", idle == 0, f"processed={idle}")

    print("-" * 56)
    print("RESULT:", "cron PASS ✅" if not _failures else f"{_failures} check(s) FAILED ❌")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
