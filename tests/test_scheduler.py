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
DEFAULT_FINISH = 0.4  # fallback run length when a job has no explicit _finish_after (settable per scenario)


def fake_run_one(job_id):
    jd = app.JOBS / job_id
    jd.mkdir(parents=True, exist_ok=True)
    with app._lock:
        meta = dict(app._jobs.get(job_id, {}))
        if meta.get("status") in ("paused", "held"):
            app._save_meta(job_id)
            return
        app._jobs[job_id]["status"] = "running"
        app._jobs[job_id]["started"] = time.time()
        app._jobs[job_id]["finished"] = None
        was_auto = bool(app._jobs[job_id].get("auto"))
        # Register as current UNDER THE COMMIT LOCK (mirrors the real _run_one): a preempt landing
        # before the proc launches must still see _current and write the control flag.
        app._current["job_id"], app._current["proc"] = job_id, None
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
    t0 = time.time()
    ctl = ""
    while True:
        # re-read the deadline live so a test can extend a job's run after it has started
        with app._lock:
            finish_after = float(app._jobs.get(job_id, {}).get("_finish_after", meta.get("_finish_after", DEFAULT_FINISH)))
        if time.time() - t0 >= finish_after:
            break
        try:
            ctl = (jd / "control").read_text().strip()
        except Exception:
            ctl = ""
        if ctl in ("pause", "cancel", "hold"):
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
    if ctl not in ("pause", "cancel", "hold"):
        (jd / "dataroom.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        (jd / "run_meta.json").write_text(json.dumps(
            {"done": True, "stop_reason": "done",
             "floor": {"substantive_files": 3, "min_files": 1}}))
    with app._lock:
        app._current["job_id"], app._current["proc"] = None, None
        was_auto2 = bool(app._jobs.get(job_id, {}).get("auto"))
        started = app._jobs.get(job_id, {}).get("started")
        now = time.time()
        # mirror real _run_one cumulative accounting. _sim_run_seconds lets a test inflate one run's
        # billed time so the cumulative cap is reachable without real wall-clock.
        billed = float(meta.get("_sim_run_seconds") or (now - started if started else 0))
        app._jobs[job_id]["cum_seconds"] = int(app._jobs.get(job_id, {}).get("cum_seconds") or 0) + max(0, int(billed))
        if ctl == "hold":
            app._jobs[job_id]["status"], app._jobs[job_id]["stop_reason"] = "held", "held"
        elif ctl in ("pause", "cancel"):
            if was_auto2 and int(app._jobs[job_id].get("cum_seconds") or 0) >= app.MAX_CUMULATIVE_SECONDS:
                app._jobs[job_id]["status"], app._jobs[job_id]["stop_reason"] = "stopped", "budget_exhausted"
            else:
                app._jobs[job_id]["status"], app._jobs[job_id]["stop_reason"] = "paused", "paused"
        else:
            st, sr = app._status_for(jd)
            app._jobs[job_id]["status"], app._jobs[job_id]["stop_reason"] = st, sr
        app._jobs[job_id]["rc"] = 0
        app._jobs[job_id]["finished"] = now
        app._jobs[job_id]["auto"] = False
    app._save_meta(job_id)


app._run_one = fake_run_one


def make_paused(job_id, query, finished, finish_after=0.4, status="paused",
                control="pause", **extra):
    """Craft a paused/held job directly on disk + in memory (the backfill pool)."""
    jd = app.JOBS / job_id
    jd.mkdir(parents=True, exist_ok=True)
    meta = {"status": status, "query": query, "finished": finished,
            "stop_reason": status, "_finish_after": finish_after}
    meta.update(extra)
    (jd / "meta.json").write_text(json.dumps(meta))
    (jd / "control").write_text(control)
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
# Scenario C: a fresh submit PREEMPTS a running foreground job (#59922). The preempted job
# returns to the pool tagged preempted=True, and resumes ahead of bulk backfill.
# ---------------------------------------------------------------------------
print("C: fresh submit preempts a running foreground job (#59922)")
assert app.PREEMPT_FOREGROUND, "this scenario assumes PREEMPT_FOREGROUND=1 (the default)"
OBSERVED.clear()
DEFAULT_FINISH = 3.0   # f1 runs long enough to still be running when f2 lands
f1 = app.create(app.JobReq(query="first foreground job, long-running"))["job_id"]
wait_until(lambda: status(f1) == "running", msg="first foreground running")
with app._lock:
    app._jobs[f1]["_finish_after"] = 3.0   # pin f1 long explicitly...
DEFAULT_FINISH = 0.4                        # ...so f2 (and everything after) can finish quickly
f2 = app.create(app.JobReq(query="second foreground job that should preempt the first"))["job_id"]
wait_until(lambda: status(f1) == "paused", msg="first foreground preempted to paused")
check("running foreground job was preempted to paused", status(f1) == "paused")
check("preempted foreground job carries the preempted marker", app._jobs[f1].get("preempted") is True)
wait_until(lambda: status(f2) == "done", msg="second foreground ran to done")
check("preempting foreground job completed", status(f2) == "done")
wait_until(lambda: status(f1) == "done", msg="preempted foreground resumed to done")
check("preempted foreground resumed and finished", status(f1) == "done")
starts = [j for j, _ in OBSERVED]
check("f2 started before f1 resumed",
      starts.index(f2) < (starts.index(f1, starts.index(f2)) if f1 in starts[starts.index(f2):] else 10**9))

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

# ---------------------------------------------------------------------------
# Scenario E: a USER-HELD job is sticky - auto-backfill must NOT resume it.
# ---------------------------------------------------------------------------
print("E: user-held job is never auto-backfilled")
OBSERVED.clear()
make_paused("pe_held", "a job the user explicitly paused", finished=5.0,
            status="held", control="hold")
with app._cond:
    app._cond.notify_all()
time.sleep(1.0)   # give the worker ample time to (wrongly) pick it up
check("held job stays held (not auto-backfilled)", status("pe_held") == "held")
check("held job never started running", "pe_held" not in [j for j, _ in OBSERVED])
# but an explicit resume revives it
app.resume("pe_held")
wait_until(lambda: status("pe_held") == "done", msg="held job resumes on explicit resume")
check("explicit resume revives a held job", status("pe_held") == "done")

# ---------------------------------------------------------------------------
# Scenario F: a backfill job that exhausts its cumulative budget retires (stopped,
# budget_exhausted) instead of being auto-resumed forever.
# ---------------------------------------------------------------------------
print("F: cumulative budget cap retires a runaway backfill job")
OBSERVED.clear()
# Pre-load cum_seconds just under the cap and bill a big run so the next stop crosses it.
over = app.MAX_CUMULATIVE_SECONDS + 10
make_paused("pf_runaway", "a job that never reaches its floor", finished=1.0,
            finish_after=3.0, _sim_run_seconds=over)
with app._cond:
    app._cond.notify_all()
wait_until(lambda: status("pf_runaway") == "running", msg="runaway backfill started")
# preempt it so it stops via the control=pause path (where the budget check lives)
fx = app.create(app.JobReq(query="a fresh job to preempt the runaway backfill"))["job_id"]
wait_until(lambda: status("pf_runaway") in ("stopped", "paused"), msg="runaway stopped after preempt")
check("runaway backfill retired on budget", status("pf_runaway") == "stopped"
      and app._jobs["pf_runaway"].get("stop_reason") == "budget_exhausted")
wait_until(lambda: status(fx) == "done", msg="fresh job finished")
# a retired (budget-exhausted) job is NOT re-picked by backfill
OBSERVED.clear()
with app._cond:
    app._cond.notify_all()
time.sleep(0.8)
check("budget-exhausted job is not auto-resumed", "pf_runaway" not in [j for j, _ in OBSERVED])
# but the user can force-resume it (fresh budget)
app.resume("pf_runaway")
check("force-resume clears the cumulative budget", app._jobs["pf_runaway"].get("cum_seconds") == 0)
wait_until(lambda: status("pf_runaway") == "done", msg="force-resumed runaway finished")
check("force-resumed budget-exhausted job runs again", status("pf_runaway") == "done")

print(f"\nALL {len(PASS)} CHECKS PASSED")
