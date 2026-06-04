#!/usr/bin/env python3
"""Scheduler logic test for the two-tier preemptive queue (no GPU / no real orchestrator).

Monkeypatches app._run_one with a fake that honors the same contract (commit-to-running under the
lock, watch the control flag for a cooperative pause, write a terminal zip+run_meta on completion)
but does no LLM work, so we can exercise: backfill of paused jobs, foreground priority, preemption
of a running backfill by a fresh submit, and that a normal foreground job is NOT preempted.
"""
import os, sys, json, time, tempfile, subprocess, threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["JOBS_DIR"] = tempfile.mkdtemp(prefix="sched-test-")
os.environ["AUTO_BACKFILL"] = "1"

from server import app  # noqa: E402

OBSERVED = []  # (job_id, was_auto_at_run_start)
_obs_lock = threading.Lock()


def fake_run_one(job_id):
    jd = app.JOBS / job_id
    jd.mkdir(parents=True, exist_ok=True)
    with app._lock:
        meta = dict(app._jobs.get(job_id, {}))
        if meta.get("status") == "paused":
            app._save_meta(job_id)
            return
        app._jobs[job_id]["status"] = "running"
        app._jobs[job_id]["started"] = time.time()
        app._jobs[job_id]["finished"] = None
        was_auto = bool(app._jobs[job_id].get("auto"))
        try:
            (jd / "control").unlink()
        except FileNotFoundError:
            pass
    app._save_meta(job_id)
    with _obs_lock:
        OBSERVED.append((job_id, was_auto))
    # Real proc so preemption's killpg has a valid target; the loop also watches the control file.
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    with app._lock:
        app._current["job_id"], app._current["proc"] = job_id, proc
    finish_after = float(meta.get("_finish_after", 0.4))
    t0 = time.time()
    ctl = ""
    while time.time() - t0 < finish_after:
        try:
            ctl = (jd / "control").read_text().strip()
        except Exception:
            ctl = ""
        if ctl in ("pause", "cancel"):
            break
        time.sleep(0.02)
    try:
        ctl = (jd / "control").read_text().strip()
    except Exception:
        ctl = ""
    try:
        proc.terminate()
    except Exception:
        pass
    if ctl not in ("pause", "cancel"):
        (jd / "dataroom.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        (jd / "run_meta.json").write_text(json.dumps(
            {"done": True, "stop_reason": "done",
             "floor": {"substantive_files": 3, "min_files": 1}}))
    with app._lock:
        app._current["job_id"], app._current["proc"] = None, None
        if ctl in ("pause", "cancel"):
            app._jobs[job_id]["status"], app._jobs[job_id]["stop_reason"] = "paused", "paused"
        else:
            st, sr = app._status_for(jd)
            app._jobs[job_id]["status"], app._jobs[job_id]["stop_reason"] = st, sr
        app._jobs[job_id]["rc"] = 0
        app._jobs[job_id]["finished"] = time.time()
        app._jobs[job_id]["auto"] = False
    app._save_meta(job_id)


app._run_one = fake_run_one


def make_paused(job_id, query, finished, finish_after=0.4):
    """Craft a paused job directly on disk + in memory (the backfill pool)."""
    jd = app.JOBS / job_id
    jd.mkdir(parents=True, exist_ok=True)
    meta = {"status": "paused", "query": query, "finished": finished,
            "stop_reason": "paused", "_finish_after": finish_after}
    (jd / "meta.json").write_text(json.dumps(meta))
    (jd / "control").write_text("pause")
    (jd / "query.txt").write_text(query)
    with app._lock:
        app._jobs[job_id] = dict(meta)


def status(job_id):
    with app._lock:
        return app._jobs.get(job_id, {}).get("status")


def wait_until(pred, timeout=15, msg=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.03)
    raise AssertionError(f"timeout waiting: {msg}")


PASS = []


def check(name, cond):
    assert cond, f"FAIL: {name}"
    PASS.append(name)
    print(f"  ok: {name}")


# ---------------------------------------------------------------------------
# Scenario A: backfill drains the paused pool, oldest-idle first.
# ---------------------------------------------------------------------------
print("A: backfill drains paused pool (oldest-idle first)")
make_paused("pa_old", "old paused job query", finished=100.0)
make_paused("pa_new", "new paused job query", finished=200.0)
# kick the worker (it may be asleep from import-time with nothing to do)
with app._cond:
    app._cond.notify_all()
wait_until(lambda: status("pa_old") == "done" and status("pa_new") == "done",
           msg="both paused jobs backfilled to done")
auto_a = {j: a for j, a in OBSERVED if j in ("pa_old", "pa_new")}
check("both paused jobs backfilled to done", status("pa_old") == "done" and status("pa_new") == "done")
check("backfilled jobs ran with auto=True", auto_a.get("pa_old") and auto_a.get("pa_new"))
order = [j for j, _ in OBSERVED if j in ("pa_old", "pa_new")]
check("oldest-idle (pa_old) backfilled before pa_new", order[0] == "pa_old")

# ---------------------------------------------------------------------------
# Scenario B: a fresh submit preempts a running backfill, runs first, then the
# preempted backfill job resumes and finishes.
# ---------------------------------------------------------------------------
print("B: fresh submit preempts a running backfill")
OBSERVED.clear()
make_paused("pb_bg", "background paused job to backfill", finished=50.0, finish_after=3.0)
with app._cond:
    app._cond.notify_all()
wait_until(lambda: status("pb_bg") == "running", msg="backfill job started running")
check("backfill job is running and marked auto", status("pb_bg") == "running"
      and app._jobs["pb_bg"].get("auto") is True)
# fresh query arrives mid-backfill
fid = app.create(app.JobReq(query="a fresh foreground research question"))["job_id"]
wait_until(lambda: status("pb_bg") == "paused", msg="backfill preempted back to paused")
check("backfill preempted back to paused", status("pb_bg") == "paused")
wait_until(lambda: status(fid) == "done", msg="fresh job ran to done")
check("fresh foreground job completed", status(fid) == "done")
# the order of run-starts after preemption: fresh job starts before pb_bg resumes
starts = [j for j, _ in OBSERVED]
check("fresh job started before the preempted backfill resumed",
      starts.index(fid) < (starts.index("pb_bg", starts.index(fid)) if "pb_bg" in starts[starts.index(fid):] else 10**9))
# and the preempted backfill eventually resumes and finishes (drains the pool)
wait_until(lambda: status("pb_bg") == "done", msg="preempted backfill resumed to done")
check("preempted backfill resumed and finished", status("pb_bg") == "done")

# ---------------------------------------------------------------------------
# Scenario C: a normal foreground job is NOT preempted by another fresh submit.
# ---------------------------------------------------------------------------
print("C: foreground job is not preempted by a second submit")
OBSERVED.clear()
f1 = app.create(app.JobReq(query="first foreground job that should run fully"))["job_id"]
# give it a longer run so the second submit lands while it is running
with app._lock:
    app._jobs[f1]["_finish_after"] = 1.5
wait_until(lambda: status(f1) == "running", msg="first foreground running")
f2 = app.create(app.JobReq(query="second foreground job queued behind the first"))["job_id"]
time.sleep(0.4)
check("first foreground job is NOT preempted (still running, not paused)", status(f1) == "running")
check("second foreground job waits queued", status(f2) == "queued")
wait_until(lambda: status(f1) == "done" and status(f2) == "done", msg="both foreground jobs done")
check("both foreground jobs completed in order", status(f1) == "done" and status(f2) == "done")
o = [j for j, _ in OBSERVED if j in (f1, f2)]
check("foreground ran FIFO (f1 before f2)", o.index(f1) < o.index(f2))

# ---------------------------------------------------------------------------
# Scenario D: a fresh queued job beats a paused backfill candidate.
# ---------------------------------------------------------------------------
print("D: fresh queued job beats a paused candidate")
OBSERVED.clear()
make_paused("pd_bg", "a paused job sitting in the pool", finished=10.0, finish_after=0.4)
fd = app.create(app.JobReq(query="fresh job that should preempt-or-precede backfill"))["job_id"]
wait_until(lambda: status(fd) == "done", msg="fresh job done")
o = [j for j, _ in OBSERVED]
check("fresh foreground job ran before the paused backfill",
      o.index(fd) < (o.index("pd_bg") if "pd_bg" in o else 10**9))
wait_until(lambda: status("pd_bg") == "done", msg="paused backfill eventually drained")
check("paused candidate eventually backfilled", status("pd_bg") == "done")

print(f"\nALL {len(PASS)} CHECKS PASSED")
