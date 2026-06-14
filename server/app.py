#!/usr/bin/env python3
"""DaaS API: submit a query, get an async job that builds a dataroom, download the zip.

POST /jobs            {query}            -> {job_id}   (queued; a single worker runs jobs serially)
POST /jobs/{id}/pause                     -> pause an unfinished job (queue advances to the next)
POST /jobs/{id}/resume                    -> re-enqueue a paused job (continues from on-disk dataroom)
GET  /jobs/{id}                          -> {status, turns, ...}
GET  /jobs/{id}/result                   -> final dataroom.zip (when status=done)
GET  /jobs/{id}/snapshot                 -> dataroom-so-far.zip, zipped live (any time)
GET  /jobs/{id}/log                      -> tail of pi.log
GET  /health
"""
import io, json, os, signal, subprocess, sys, threading, uuid, time, zipfile
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, PlainTextResponse, HTMLResponse
from pydantic import BaseModel
import uvicorn

from server.stats import job_stats, floor_metrics, _status_progress

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
JOBS = Path(os.environ.get("JOBS_DIR", "/data/jobs")).resolve()
JOBS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Dataroom-as-a-Service")
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
# Single-worker serial queue: the L4 has one llama slot (--parallel 1), so jobs run one at a
# time. _queue is FIFO of job_ids waiting; the worker skips paused jobs and advances to the next
# runnable one. _current holds the live orchestrator Popen so pause/cancel can signal it.
#
# Job lifecycle states:
#   queued   - foreground, waiting for the slot (a fresh submit or an explicit resume)
#   running  - on the GPU right now
#   pausing  - a cooperative stop is in flight (-> 'held' for a user pause, 'paused' for a preempt)
#   held     - USER-paused. NOT auto-resumed; only an explicit resume revives it.
#   paused   - idle/preemptible pool; auto-backfill may resume it to keep the GPU busy. A preempted
#              FOREGROUND job carries preempted=True and outranks bulk paused jobs when backfilling.
#   done / stopped / failed - terminal (a dataroom.zip was written, or it errored / hit a cap)
_queue: list[str] = []
_cond = threading.Condition(_lock)
_current: dict = {"job_id": None, "proc": None}
# Auto-backfill: when no freshly submitted/resumed job is queued, keep the single GPU slot busy by
# resuming the least-recently-active paused job. Such a backfill run is preemptible - a new submission
# (or an explicit resume) pauses it and takes the slot. Set AUTO_BACKFILL=0 to disable.
AUTO_BACKFILL = os.environ.get("AUTO_BACKFILL", "1") != "0"
# Foreground preemption (#59922): a fresh submit / explicit resume pauses ANY job in the slot -
# including another foreground job - back to the pool and takes over. The preempted foreground job
# carries preempted=True so it resumes ahead of bulk backfill. Set PREEMPT_FOREGROUND=0 to keep the
# old behavior (a new query waits for the running foreground job to finish; only backfill is preempted).
PREEMPT_FOREGROUND = os.environ.get("PREEMPT_FOREGROUND", "1") != "0"
# Cumulative wall-clock cap across ALL auto-backfill runs of one job. max_seconds is per-run, so a job
# that never reaches its coverage floor would otherwise be re-picked by backfill forever, burning the
# GPU. Once cumulative spend crosses this, auto-backfill retires the job (terminal 'stopped',
# stop_reason=budget_exhausted). An EXPLICIT resume is user intent and bypasses this cap.
MAX_CUMULATIVE_SECONDS = int(os.environ.get("MAX_CUMULATIVE_SECONDS", str(4 * 3600)))


def _save_meta(job_id: str):
    """Persist job meta so status survives an app restart."""
    try:
        (JOBS / job_id / "meta.json").write_text(json.dumps(_jobs.get(job_id, {})))
    except Exception:
        pass


def _run_meta(job_dir) -> dict:
    """The orchestrator's end-of-run record: {stop_reason, done, floor, ...}."""
    rm = job_dir / "run_meta.json"
    if rm.exists():
        try:
            return json.loads(rm.read_text())
        except Exception:
            return {}
    return {}


def _status_for(job_dir, rc=None) -> tuple:
    """Single source of truth for terminal status, shared by _run and _load_meta.

    A zip is written for EVERY completed run (clean DONE or budget/ceiling stop), and is
    independently downloadable, so its existence -- not rc -- decides done-vs-failed.
    """
    # The API's control flag is authoritative for paused/cancelled and survives a restart: the
    # orchestrator only records a generic 'interrupted' for the SIGTERM it receives, so run_meta
    # alone cannot distinguish pause vs cancel vs a real interruption. 'hold' = a USER pause
    # (sticky, manual resume only); 'pause'/'cancel' = system-preemptible idle.
    ctl = ""
    cf = job_dir / "control"
    if cf.exists():
        try:
            ctl = cf.read_text(errors="ignore").strip()
        except Exception:
            ctl = ""
    if ctl == "hold":
        return "held", "held"
    if ctl in ("pause", "cancel"):   # preemptible idle; 'cancel' is legacy
        return "paused", "paused"
    rmeta = _run_meta(job_dir)
    sr = rmeta.get("stop_reason")
    if (job_dir / "dataroom.zip").exists():
        status = "done" if rmeta.get("done") else "stopped"
    else:
        # No zip means the run did not complete (the orchestrator writes one for every clean DONE
        # or safety-ceiling stop), so it is failed regardless of rc.
        status = "failed"
    return status, sr


def _load_meta(job_id: str) -> dict:
    """Recover job meta from disk (after a restart). Reconciles stale 'running' state."""
    job_dir = JOBS / job_id
    if not job_dir.exists():
        return {}
    meta = {}
    mp = job_dir / "meta.json"
    if mp.exists():
        try:
            meta = json.loads(mp.read_text())
        except Exception:
            meta = {}
    if (job_dir / "dataroom.zip").exists():
        meta["status"], meta["stop_reason"] = _status_for(job_dir)
    elif meta.get("status") == "running":
        # app restarted mid-run; the worker thread is gone. Resumable: the recovery loop
        # auto-re-queues these (auto-resume); shown as 'paused' until it picks back up. But a
        # user 'hold' control flag wins (the user paused it just before the restart).
        cf = job_dir / "control"
        held = False
        if cf.exists():
            try:
                held = cf.read_text(errors="ignore").strip() == "hold"
            except Exception:
                held = False
        meta["status"] = "held" if held else "paused"
    # Legacy terminal-stop states collapse into the single resumable 'paused'.
    if meta.get("status") in ("interrupted", "cancelled"):
        meta["status"] = "paused"
    # A 'pausing' snapshot persisted just before a restart never reached its terminal state; treat
    # it as the pause it was heading to (held for a user pause, paused for a preempt).
    if meta.get("status") == "pausing":
        cf = job_dir / "control"
        ctl = ""
        if cf.exists():
            try:
                ctl = cf.read_text(errors="ignore").strip()
            except Exception:
                ctl = ""
        meta["status"] = "held" if ctl == "hold" else "paused"
    if not meta.get("query") and (job_dir / "query.txt").exists():
        meta["query"] = (job_dir / "query.txt").read_text(errors="ignore").strip()
    return meta


def _elapsed_seconds(meta: dict) -> int:
    """Per-run wall-clock elapsed, defensive against stale/inconsistent timestamps.

    `started` is reset on every (re)start and `finished` is cleared on resume, so the budget
    is per-run (the orchestrator gets a fresh --max-seconds each run). A live job uses the wall
    clock; a terminal job uses `finished` only when it is sane (>= started). Never returns a
    negative value: a stale finished < started (e.g. a resume that left the prior finished in
    place) would otherwise read as 'minus minutes left'."""
    started = meta.get("started")
    if not started:
        return 0
    if meta.get("status") in ("running", "queued", "pausing"):
        return max(0, int(time.time() - started))
    finished = meta.get("finished")
    if finished and finished >= started:
        return max(0, int(finished - started))
    return 0   # terminal but timestamps missing/inconsistent -> avoid showing garbage


class JobReq(BaseModel):
    query: str
    max_turns: int | None = None
    max_seconds: int | None = None
    min_files: int | None = None         # outcome-floor "budget": files before it may stop


def _run_one(job_id: str):
    """Run one job to completion in the worker thread. Captures the orchestrator Popen (own
    process group) so pause/cancel can signal it, and maps the exit to a terminal/paused state."""
    job_dir = JOBS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    # Commit to running under the lock. If a pause raced in during the worker's dequeue window
    # (job already off _queue but status still 'queued'), pause() set status='paused'; honor it and
    # do not start, otherwise the pause would be silently lost. Clearing the stale control flag also
    # happens here, under the lock, so a fresh pause control written just after we commit survives.
    with _lock:
        meta = dict(_jobs.get(job_id, {}))
        if meta.get("status") in ("paused", "held"):
            _save_meta(job_id)
            return
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["started"] = time.time()
        _jobs[job_id]["finished"] = None
        # Register as the current job UNDER THE SAME LOCK as the status flip, so a preempt landing in
        # the window before the subprocess is launched is not silently lost (it sees _current and
        # writes the cooperative control flag; run_dataroom honors it at its first cycle boundary).
        _current["job_id"], _current["proc"] = job_id, None
        try:
            (job_dir / "control").unlink()
        except FileNotFoundError:
            pass
    _save_meta(job_id)
    query = meta.get("query", "")
    (job_dir / "query.txt").write_text(query)
    cmd = [sys.executable, "-m", "server.run_dataroom", "--query", query, "--out", str(job_dir)]
    if meta.get("max_turns"):
        cmd += ["--max-turns", str(meta["max_turns"])]
    if meta.get("max_seconds"):
        cmd += ["--max-seconds", str(meta["max_seconds"])]
    env = dict(os.environ)
    if meta.get("min_files"):
        env["MIN_FILES"] = str(int(meta["min_files"]))
    log = open(job_dir / "orchestrator.log", "a")
    # start_new_session=True: own process group, so cancel can SIGTERM the whole orchestrator+pi.
    proc = subprocess.Popen(cmd, cwd=str(HERE.parent), env=env, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    with _lock:
        # A preempt may have already flipped status to 'pausing' (and written control) in the launch
        # window; keep that, just attach the proc so the SIGTERM path can reach it.
        _current["job_id"], _current["proc"] = job_id, proc
    rc = proc.wait()
    # The control flag (if we wrote one) is authoritative over zip-existence for paused/cancelled.
    ctl = ""
    try:
        ctl = (job_dir / "control").read_text(errors="ignore").strip()
    except Exception:
        pass
    with _lock:
        _current["job_id"], _current["proc"] = None, None
        was_auto = bool(_jobs.get(job_id, {}).get("auto"))
        # Accumulate this run's wall-clock into the job's lifetime spend (per-run budget is reset on
        # every (re)start, so without this an auto-backfilled job could be re-picked forever).
        started = _jobs.get(job_id, {}).get("started")
        now = time.time()
        if started:
            _jobs[job_id]["cum_seconds"] = int(_jobs.get(job_id, {}).get("cum_seconds") or 0) + max(0, int(now - started))
        if ctl == "hold":
            _jobs[job_id]["status"], _jobs[job_id]["stop_reason"] = "held", "held"
        elif ctl in ("pause", "cancel"):   # preemptible idle; 'cancel' is legacy
            # A backfill run that exhausted its cumulative budget retires instead of returning to the
            # pool, so it stops being re-picked. An explicit (foreground) pause keeps it preemptible.
            if was_auto and int(_jobs[job_id].get("cum_seconds") or 0) >= MAX_CUMULATIVE_SECONDS:
                _jobs[job_id]["status"], _jobs[job_id]["stop_reason"] = "stopped", "budget_exhausted"
            else:
                _jobs[job_id]["status"], _jobs[job_id]["stop_reason"] = "paused", "paused"
        else:
            status, stop_reason = _status_for(job_dir, rc)
            _jobs[job_id]["status"], _jobs[job_id]["stop_reason"] = status, stop_reason
        _jobs[job_id]["rc"] = rc
        _jobs[job_id]["finished"] = now
        _jobs[job_id]["auto"] = False    # the backfill marker only applies to a live backfill run
        # If the user hit resume WHILE this job was still 'pausing' (the cooperative stop was already
        # in flight, so we could not keep it running), honor that intent now: re-queue as foreground
        # instead of settling into held/paused. Without this a quick pause->resume gets stuck.
        if _jobs[job_id].pop("resume_pending", False) and _jobs[job_id]["status"] in ("held", "paused"):
            _jobs[job_id].update({"status": "queued", "stop_reason": None, "auto": False,
                                  "preempted": False, "started": None, "finished": None})
            try:
                (job_dir / "control").unlink()
            except FileNotFoundError:
                pass
            if job_id not in _queue:
                _queue.append(job_id)
    _save_meta(job_id)


def _next_foreground_locked():
    """Oldest tier-1 'queued' job (freshly submitted or explicitly resumed), in FIFO order, or
    None. Caller holds _cond."""
    for j in _queue:
        if _jobs.get(j, {}).get("status") == "queued":
            return j
    return None


def _paused_on_disk() -> list:
    """The backfill pool, in resume priority order. Scans the jobs dir (cheap: small meta/control
    reads) and reuses _load_meta, so a restart-interrupted job that reads as 'paused' is included.

    USER-held jobs ('held') are excluded - they are sticky and only an explicit resume revives them.
    Within the preemptible 'paused' pool, a job that USED to be foreground but got preempted
    (preempted=True) outranks bulk paused jobs, so a preempted run is not starved at the back of a
    large backlog (it had foreground priority; preemption should not demote it below bulk work).
    Then oldest `finished` first, so backfill round-robins fairly: a freshly preempted/paused job
    gets a new `finished` and rotates to the back of its tier."""
    out = []
    if JOBS.exists():
        for p in sorted(JOBS.iterdir()):
            if not p.is_dir():
                continue
            m = _load_meta(p.name)
            if m.get("status") == "paused":
                # tier 0 = preempted foreground, tier 1 = bulk; then oldest-active first
                tier = 0 if m.get("preempted") else 1
                out.append((p.name, tier, m.get("finished") or 0))
    out.sort(key=lambda t: (t[1], t[2]))
    return [jid for jid, _, _ in out]


def _select_next():
    """Pick the next job to run: (job_id, is_backfill). Tier 1 (foreground 'queued') always wins and
    is removed from the wait queue. Tier 2 (backfill) only when AUTO_BACKFILL and there is no
    foreground work: the oldest-idle paused job, committed to 'queued'+auto so _run_one resumes it
    from its on-disk dataroom. Returns (None, False) when there is nothing to do (or a foreground job
    appeared mid-scan - the worker loops and re-picks it)."""
    with _cond:
        fg = _next_foreground_locked()
        if fg is not None:
            _queue.remove(fg)
            return fg, False
    if not AUTO_BACKFILL:
        return None, False
    for cand in _paused_on_disk():
        with _cond:
            if _next_foreground_locked() is not None:
                return None, False                         # foreground has priority; re-pick it
            if _jobs.get(cand, {}).get("status") in ("queued", "running", "pausing"):
                continue                                   # already taken (e.g. an explicit resume)
            m = _load_meta(cand)
            if m.get("status") != "paused":
                continue
            # Cumulative-budget guard: a job that already burned its lifetime cap should not keep
            # being auto-resumed (it would hog the GPU forever without reaching its floor). Retire
            # it terminally; an explicit user resume bypasses this (handled in resume()).
            if int(m.get("cum_seconds") or 0) >= MAX_CUMULATIVE_SECONDS:
                m.update({"status": "stopped", "stop_reason": "budget_exhausted", "auto": False})
                _jobs[cand] = m
                _save_meta(cand)
                continue
            # Becoming a live backfill run: clear the per-run clock but PRESERVE cum_seconds, and
            # clear the preempted marker (it is getting the slot now, no longer a waiting victim).
            m.update({"status": "queued", "auto": True, "stop_reason": None,
                      "preempted": False, "started": None, "finished": None})
            _jobs[cand] = m
            _save_meta(cand)
            return cand, True
    return None, False


def _preempt_backfill_locked():
    """Free the GPU slot for freshly queued foreground work by pausing whatever is running.

    Always preempts a backfill (auto) job. When PREEMPT_FOREGROUND, also preempts a running
    FOREGROUND job (#59922: a new query / explicit resume takes the slot immediately rather than
    waiting up to the per-run budget). The preempted job gets control='pause' -> returns to the
    preemptible pool; a foreground victim is tagged preempted=True so it outranks bulk backfill
    when the slot frees again. Caller holds _cond; returns the proc to SIGTERM OUTSIDE the lock
    (a long agent cycle would otherwise delay the yield)."""
    jid = _current["job_id"]
    if not jid:
        return None
    cur = _jobs.get(jid, {})
    # 'running' = live; 'pausing' = already being preempted (idempotent); both are committed to the
    # slot. status may briefly be the pre-commit value, but _current is only set at/after commit.
    if cur.get("status") not in ("running", "pausing"):
        return None
    if cur.get("status") == "pausing":
        return None                              # a preempt is already in flight for this job
    is_auto = bool(cur.get("auto"))
    if not is_auto and not PREEMPT_FOREGROUND:
        return None                              # old behavior: only backfill is preemptible
    try:
        (JOBS / jid / "control").write_text("pause")
    except Exception:
        return None
    _jobs[jid]["status"] = "pausing"
    if not is_auto:
        _jobs[jid]["preempted"] = True           # a preempted foreground run keeps priority
    _save_meta(jid)
    return _current["proc"]                       # may be None if the subprocess is mid-launch; the
                                                 # cooperative control flag still stops it cleanly


def _worker():
    """Serial job runner for the single GPU slot. Each cycle: run the next foreground job, or - if
    none and AUTO_BACKFILL - resume the oldest-idle paused job to keep the GPU busy (preemptible).
    Sleeps only when there is no foreground work and nothing to backfill."""
    while True:
        job_id, backfill = _select_next()
        if job_id is None:
            with _cond:
                # Nothing selected. A new paused job can only appear while a job is running (and the
                # worker is then in _run_one, not here), so every producer of work - create/resume/
                # recover - notifies; re-check foreground under the lock to avoid a lost wakeup, sleep.
                if _next_foreground_locked() is None:
                    _cond.wait()
            continue
        if backfill:
            print(f"[scheduler] backfill resume {job_id}", file=sys.stderr, flush=True)
        try:
            _run_one(job_id)
        except Exception as e:
            with _lock:
                _jobs.setdefault(job_id, {})["status"] = "failed"
                _jobs[job_id]["error"] = str(e)[:300]
            _save_meta(job_id)
        with _cond:
            _cond.notify_all()


_worker_thread = threading.Thread(target=_worker, daemon=True)
_worker_thread.start()


def _recover_queue():
    """On startup, auto-resume jobs that were mid-flight (queued OR running, i.e. interrupted by
    the restart) when the previous app instance stopped - the queue is in-memory. User-held jobs
    are left held (manual resume); a foreground job that was mid-preempt (control='pause') goes
    back to the preemptible pool; finished jobs (a terminal zip) are not re-run.

    Foreground recoveries are re-queued in SUBMIT order (oldest first), not directory/uuid order,
    so FIFO fairness survives a restart."""
    if not JOBS.exists():
        return
    recovered = []
    for p in sorted(JOBS.iterdir()):
        if not p.is_dir():
            continue
        mp = p / "meta.json"
        raw = {}
        if mp.exists():
            try:
                raw = json.loads(mp.read_text())
            except Exception:
                raw = {}
        if raw.get("status") in ("queued", "running", "pausing") and not (p / "dataroom.zip").exists():
            # A user hold written just before the crash wins: keep it held (manual resume only).
            cf = p / "control"
            if cf.exists():
                try:
                    if cf.read_text(errors="ignore").strip() == "hold":
                        raw["status"] = "held"
                        (mp).write_text(json.dumps(raw))
                        continue
                except Exception:
                    pass
            if raw.get("auto") or raw.get("status") == "pausing":
                # A backfill job, or any job caught mid-preempt: return it to the preemptible pool
                # rather than let it jump ahead of real foreground work. Preserve the preempted
                # marker so a preempted foreground run keeps its priority when backfill re-picks it.
                raw["status"], raw["auto"] = "paused", False
                try:
                    (mp).write_text(json.dumps(raw))
                except Exception:
                    pass
                continue
            m = _load_meta(p.name)          # full meta: query + budgets
            m["status"] = "queued"
            recovered.append((p.name, m, m.get("submitted") or m.get("started") or 0))
    recovered.sort(key=lambda t: t[2])      # oldest submit first -> FIFO across restart
    with _cond:
        for jid, m, _ in recovered:
            _jobs[jid] = m
            if jid not in _queue:
                _queue.append(jid)
        _cond.notify_all()


_recover_queue()


@app.get("/health")
def health():
    return {"ok": True}


MAX_BUDGET_SECONDS = 3600   # 60-minute ceiling on a single job's time-box (one L4 slot, fair use)
MIN_QUERY_LEN = 10          # reject empty / trivially short queries


@app.post("/jobs")
def create(req: JobReq):
    query = (req.query or "").strip()
    if len(query) < MIN_QUERY_LEN:
        raise HTTPException(400, "query too short: give a real research question (a few words at least)")
    job_id = uuid.uuid4().hex[:12]
    mf = int(req.min_files) if req.min_files and req.min_files > 0 else None
    # 60-min ceiling: clamp an over-budget request down rather than rejecting it.
    max_seconds = (min(int(req.max_seconds), MAX_BUDGET_SECONDS)
                   if req.max_seconds and req.max_seconds > 0 else None)
    with _cond:
        _jobs[job_id] = {"status": "queued", "query": query, "min_files": mf,
                         "max_turns": req.max_turns, "max_seconds": max_seconds,
                         "submitted": time.time()}
        _queue.append(job_id)
        proc = _preempt_backfill_locked()   # a fresh query takes the slot from any backfill job
        _cond.notify_all()                  # wake the worker to pick it up (runs when the slot is free)
    (JOBS / job_id).mkdir(parents=True, exist_ok=True)
    _save_meta(job_id)
    if proc is not None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            pass
    return {"job_id": job_id, "status": "queued"}


def _cur_status(job_id: str) -> str | None:
    with _lock:
        if job_id in _jobs:
            return _jobs[job_id].get("status")
    m = _load_meta(job_id)
    return m.get("status") if m else None


@app.post("/jobs/{job_id}/pause")
def pause(job_id: str):
    """USER pause an unfinished job. A user pause is STICKY: the job goes to 'held' (control='hold')
    and is NOT eligible for auto-backfill - only an explicit resume revives it. (System preemption
    is a different path: it writes control='pause' -> the preemptible 'paused' pool.)

    A queued job just leaves the run queue; a running job gets the cooperative 'hold' flag and stops
    at its next cycle boundary (status -> pausing -> held). The worker then advances.

    The status read + dispatch run under _lock so a pause landing in the worker's dequeue window
    (job off _queue but not yet flipped to 'running') is resolved against the live status, and
    _run_one re-checks for held/paused under the same lock before it starts - so the pause is not lost."""
    proc = None
    with _cond:
        st = (_jobs.get(job_id) or _load_meta(job_id) or {}).get("status")
        if st == "queued":
            _jobs.setdefault(job_id, {}).update({"status": "held", "stop_reason": "held",
                                                 "preempted": False})
            try:
                (JOBS / job_id / "control").write_text("hold")
            except Exception:
                pass
            if job_id in _queue:
                _queue.remove(job_id)
            _save_meta(job_id)
            return {"status": "held"}
        if st in ("running", "pausing"):
            # 'hold' (authoritative for the resulting status) AND SIGTERM for promptness: a long
            # agent cycle would otherwise delay the cooperative checkpoint by minutes. The
            # orchestrator unwinds cleanly (reaps pi + index, zips); control=hold -> held.
            (JOBS / job_id / "control").write_text("hold")
            if job_id in _jobs:
                _jobs[job_id]["status"] = "pausing"
                _jobs[job_id]["preempted"] = False   # a user hold is not a preempt; drop priority tag
            proc = _current["proc"] if _current["job_id"] == job_id else None
            _save_meta(job_id)
        else:
            raise HTTPException(409, f"cannot pause a job in state {st}")
    if proc is not None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            pass
    return {"status": "pausing"}


@app.post("/jobs/{job_id}/resume")
def resume(job_id: str):
    """Re-enqueue a held/paused job as foreground work. The worker continues it from the on-disk
    dataroom (a fresh pi session re-reads STATUS.md/OUTLINE and keeps building). An explicit resume
    is user intent: it preempts whatever is running (per PREEMPT_FOREGROUND) and bypasses the
    cumulative-budget cap (a budget-exhausted job can be force-resumed)."""
    st = _cur_status(job_id)
    # Resume requested while a cooperative stop is still in flight ('pausing'): we can't keep the
    # subprocess (already SIGTERM'd), so record the intent and let _run_one's finalize re-queue it
    # as foreground. A quick pause->resume thus becomes a no-op rather than getting stuck in held.
    if st == "pausing":
        with _cond:
            cur = _jobs.get(job_id, {})
            if _current["job_id"] == job_id and cur.get("status") == "pausing":
                cur["resume_pending"] = True
                _save_meta(job_id)
                _cond.notify_all()
                return {"status": "queued"}
        st = _cur_status(job_id)   # it settled to held/paused meanwhile; fall through to normal resume
    if st not in ("paused", "held", "stopped"):
        raise HTTPException(409, f"cannot resume a job in state {st}")
    # Only a budget-exhausted 'stopped' is resumable (it banked real work); a clean done/failed is not.
    if st == "stopped":
        m = _jobs.get(job_id) or _load_meta(job_id) or {}
        if m.get("stop_reason") != "budget_exhausted":
            raise HTTPException(409, "cannot resume a completed job")
    with _cond:
        if job_id not in _jobs:
            _jobs[job_id] = _load_meta(job_id)      # restore query + budgets after a restart
        # Clear the prior run's clock: _run_one sets a fresh `started`, and leaving the old
        # `finished` in place makes the resumed run read a finished < started (negative elapsed).
        # auto=False: an explicit resume is foreground work, not a preemptible backfill.
        # cum_seconds is reset so a force-resumed budget-exhausted job gets a fresh lifetime budget.
        _jobs[job_id].update({"status": "queued", "stop_reason": None, "auto": False,
                              "preempted": False, "started": None, "finished": None})
        if st == "stopped":
            _jobs[job_id]["cum_seconds"] = 0
        # Drop any stale control flag (hold/pause) so _run_one does not see it and bail.
        try:
            (JOBS / job_id / "control").unlink()
        except FileNotFoundError:
            pass
        if job_id not in _queue:
            _queue.append(job_id)
        proc = _preempt_backfill_locked()   # an explicit resume preempts whatever is running
        _cond.notify_all()
    _save_meta(job_id)
    if proc is not None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            pass
    return {"status": "queued"}


# Per-job row cache for the homepage list. Terminal jobs never change, so their (expensive) row -
# floor metrics + an rglob file count - is computed once and reused. Live jobs (queued/running/
# pausing) get a short TTL so a 4s dashboard poll over a large backlog does not re-floor+rglob the
# whole jobs dir every time. Keyed by job_id -> (row, computed_at, status).
_row_cache: dict = {}
_LIVE_ROW_TTL = float(os.environ.get("LIST_ROW_TTL", "3.0"))
_LIVE_STATES = ("queued", "running", "pausing")


def _job_row(jid: str) -> dict | None:
    """Build (or fetch from cache) one homepage list row for a job. Cheap for terminal jobs
    (cached forever) and TTL-cached for live ones; never parses pi.log / polls llama."""
    with _lock:
        meta = dict(_jobs.get(jid, {}))
    if not meta or meta.get("status") not in ("queued", "running"):
        disk = _load_meta(jid)                       # reconciles terminal status from disk
        disk.update({k: v for k, v in meta.items() if v is not None})
        meta = disk or meta
    if not meta:
        return None
    status = meta.get("status", "unknown")
    cached = _row_cache.get(jid)
    if cached:
        row, ts, cst = cached
        if cst == status:
            if status not in _LIVE_STATES:
                return row                                   # terminal: cache forever
            if (time.time() - ts) < _LIVE_ROW_TTL:
                return row                                   # live: within TTL
    job_dir = JOBS / jid
    dataroom = job_dir / "dataroom"
    # Cheap path for terminal jobs: reuse the floor the orchestrator persisted at stop rather
    # than re-reading every note's full text. Only live states recompute (files still changing).
    rfloor = _run_meta(job_dir).get("floor") if status not in _LIVE_STATES else None
    if rfloor and "substantive_files" in rfloor:
        fm = rfloor
    else:
        fm = floor_metrics(dataroom, meta.get("min_files"))
    pr = _status_progress(dataroom)
    fc = (sum(1 for p in dataroom.rglob("*")
              if p.is_file() and not p.name.startswith(".index")) if dataroom.exists() else 0)
    row = {
        "job_id": jid,
        "status": status,
        "auto": bool(meta.get("auto")),
        "preempted": bool(meta.get("preempted")),
        "stop_reason": meta.get("stop_reason"),
        "query": (meta.get("query") or "")[:200],
        "started": meta.get("started"),
        "finished": meta.get("finished"),
        "submitted": meta.get("submitted"),
        "max_seconds": meta.get("max_seconds"),
        "cum_seconds": meta.get("cum_seconds"),
        "substantive_files": fm["substantive_files"],
        "min_files": fm["min_files"],
        "progress": pr,
        "file_count": fc,
        "zip_ready": (job_dir / "dataroom.zip").exists(),
    }
    _row_cache[jid] = (row, time.time(), status)
    return row


@app.get("/jobs")
def list_jobs(limit: int = 100, offset: int = 0):
    """Paginated summary of jobs (live + on-disk) for the homepage list, newest first.

    Deliberately cheap: per-row floor/progress are cached (terminal rows forever, live rows for a
    few seconds), so a 4s poll over a large backlog does not re-scan + re-floor everything. Returns
    {jobs, total, limit, offset}. Pass limit<=0 for all (legacy callers)."""
    ids = set(_jobs.keys())
    if JOBS.exists():
        for p in JOBS.iterdir():
            if p.is_dir():
                ids.add(p.name)
    # Drop cache entries for jobs that no longer exist (retention/cleanup) so it cannot grow unbounded.
    for stale in [k for k in _row_cache if k not in ids]:
        _row_cache.pop(stale, None)
    # Order by recency using cheap meta (started/submitted) WITHOUT building every row first, then
    # only build rows for the requested page - so pagination actually bounds the per-request cost.
    def _recency(jid: str) -> float:
        with _lock:
            m = _jobs.get(jid)
        if not m:
            mp = JOBS / jid / "meta.json"
            if mp.exists():
                try:
                    m = json.loads(mp.read_text())
                except Exception:
                    m = {}
            else:
                m = {}
        return float(m.get("started") or m.get("submitted") or m.get("finished") or 0)
    ordered = sorted(ids, key=_recency, reverse=True)
    total = len(ordered)
    page = ordered if (limit is None or limit <= 0) else ordered[offset:offset + limit]
    rows = [r for r in (_job_row(jid) for jid in page) if r is not None]
    return {"jobs": rows, "total": total, "limit": limit, "offset": offset}


@app.get("/jobs/{job_id}")
def status(job_id: str):
    with _lock:
        j = dict(_jobs.get(job_id, {}))
    if not j:
        j = _load_meta(job_id)
        if not j:
            raise HTTPException(404, "unknown job")
    return j


@app.get("/jobs/{job_id}/result")
def result(job_id: str):
    zip_path = JOBS / job_id / "dataroom.zip"
    if not zip_path.exists():
        raise HTTPException(409, "not ready")
    return FileResponse(str(zip_path), media_type="application/zip",
                        filename=f"dataroom-{job_id}.zip")


@app.get("/jobs/{job_id}/snapshot")
def snapshot(job_id: str):
    """Zip the dataroom AS IT IS RIGHT NOW (works mid-run), timestamped filename.

    Unlike /result (the final zip the orchestrator writes when the job stops), this builds
    the archive on demand from whatever is on disk, so the download is always available and
    always current."""
    job_dir = JOBS / job_id
    if not job_dir.exists():
        raise HTTPException(404, "unknown job")
    dataroom = job_dir / "dataroom"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if dataroom.exists():
            for p in sorted(dataroom.rglob("*")):
                if p.is_file() and not p.name.startswith(".index"):
                    z.write(p, p.relative_to(dataroom.parent))
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fn = f"dataroom-{job_id}-{ts}.zip"
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


_IMG = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp"}


@app.get("/jobs/{job_id}/file")
def get_file(job_id: str, path: str):
    """Return one dataroom file's content for the dashboard preview pane.

    `path` is relative to the job's dataroom dir. Resolved + guarded against traversal
    outside the dataroom. Images are served with their media type; everything else as text."""
    dataroom = (JOBS / job_id / "dataroom").resolve()
    if not dataroom.exists():
        raise HTTPException(404, "no dataroom")
    target = (dataroom / path).resolve()
    try:
        target.relative_to(dataroom)                 # traversal guard
    except ValueError:
        raise HTTPException(403, "outside dataroom")
    if not target.is_file() or target.name.startswith(".index"):
        raise HTTPException(404, "not found")
    ext = target.suffix.lower()
    if ext in _IMG:
        return FileResponse(str(target), media_type=_IMG[ext])
    try:
        data = target.read_text(errors="ignore")
    except Exception:
        raise HTTPException(415, "not previewable (binary)")
    # cap very large files so the preview stays snappy
    return PlainTextResponse(data[:500000])


@app.get("/jobs/{job_id}/stats")
def stats_ep(job_id: str):
    job_dir = JOBS / job_id
    if not job_dir.exists():
        raise HTTPException(404, "unknown job")
    with _lock:
        meta = dict(_jobs.get(job_id, {}))
    if not meta:
        meta = _load_meta(job_id)   # recover after app restart
    status_val = meta.get("status") or ("done" if (job_dir / "dataroom.zip").exists() else "unknown")
    # live = this job is the one actually running on the shared llama-server (single-worker queue),
    # so its context/throughput are real; otherwise they'd bleed another job's global llama state.
    s = job_stats(job_dir, meta.get("min_files"), live=(status_val == "running"))
    s["job_id"] = job_id
    s["job_status"] = status_val
    # Align the banner's stop_reason with the control-flag-aware status (else a cancelled job can
    # show the orchestrator's generic 'interrupted' banner).
    if meta.get("stop_reason") is not None:
        s["stop_reason"] = meta["stop_reason"]
    # Time-box progress: elapsed vs the job's max_seconds budget (the homepage/skill set this).
    max_seconds = meta.get("max_seconds")
    elapsed = _elapsed_seconds(meta)
    s["budget"] = {"max_seconds": max_seconds, "elapsed_seconds": elapsed,
                   "percent": round(min(100, 100 * elapsed / max_seconds), 1) if max_seconds else None}
    query = meta.get("query", "")
    qf = job_dir / "query.txt"
    if not query and qf.exists():
        query = qf.read_text(errors="ignore").strip()
    s["query"] = query
    return s


@app.get("/jobs/{job_id}/dashboard", response_class=HTMLResponse)
def dashboard(job_id: str):
    html = (WEB / "dashboard.html").read_text()
    return HTMLResponse(html.replace("__JOB_ID__", job_id))


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((WEB / "index.html").read_text())


@app.get("/favicon.svg")
def favicon_svg():
    return FileResponse(str(WEB / "favicon.svg"), media_type="image/svg+xml")


@app.get("/favicon.ico")
def favicon_ico():
    # No .ico asset; serve the SVG (modern browsers accept it) so the auto-request isn't a 404.
    return FileResponse(str(WEB / "favicon.svg"), media_type="image/svg+xml")


@app.get("/og.png")
def og_image():
    return FileResponse(str(HERE.parent / "assets" / "banner.png"), media_type="image/png")


@app.get("/jobs/{job_id}/log")
def joblog(job_id: str):
    p = JOBS / job_id / "pi.log"
    if not p.exists():
        raise HTTPException(404, "no log yet")
    data = p.read_text(errors="ignore")
    return PlainTextResponse(data[-8000:])


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
