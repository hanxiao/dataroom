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
_queue: list[str] = []
_cond = threading.Condition(_lock)
_current: dict = {"job_id": None, "proc": None}


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
    # alone cannot distinguish pause vs cancel vs a real interruption.
    ctl = ""
    cf = job_dir / "control"
    if cf.exists():
        try:
            ctl = cf.read_text(errors="ignore").strip()
        except Exception:
            ctl = ""
    if ctl in ("pause", "cancel"):   # one resumable state; 'cancel' is legacy
        return "paused", "paused"
    rmeta = _run_meta(job_dir)
    sr = rmeta.get("stop_reason")
    if (job_dir / "dataroom.zip").exists():
        status = "done" if rmeta.get("done") else "stopped"
    elif rc not in (None, 0):
        status = "failed"
    else:
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
        # auto-re-queues these (auto-resume); shown as 'paused' until it picks back up.
        meta["status"] = "paused"
    # Legacy terminal-stop states collapse into the single resumable 'paused'.
    if meta.get("status") in ("interrupted", "cancelled"):
        meta["status"] = "paused"
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
    with _lock:
        meta = dict(_jobs.get(job_id, {}))
    query = meta.get("query", "")
    (job_dir / "query.txt").write_text(query)
    # Clear any stale control flag from a previous pause/cancel before (re)starting.
    try:
        (job_dir / "control").unlink()
    except FileNotFoundError:
        pass
    cmd = [sys.executable, "-m", "server.run_dataroom", "--query", query, "--out", str(job_dir)]
    if meta.get("max_turns"):
        cmd += ["--max-turns", str(meta["max_turns"])]
    if meta.get("max_seconds"):
        cmd += ["--max-seconds", str(meta["max_seconds"])]
    env = dict(os.environ)
    if meta.get("min_files"):
        env["MIN_FILES"] = str(int(meta["min_files"]))
    with _lock:
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["started"] = time.time()
    _save_meta(job_id)
    log = open(job_dir / "orchestrator.log", "a")
    # start_new_session=True: own process group, so cancel can SIGTERM the whole orchestrator+pi.
    proc = subprocess.Popen(cmd, cwd=str(HERE.parent), env=env, stdout=log,
                            stderr=subprocess.STDOUT, start_new_session=True)
    with _lock:
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
        if ctl in ("pause", "cancel"):   # one resumable state; 'cancel' is legacy
            _jobs[job_id]["status"], _jobs[job_id]["stop_reason"] = "paused", "paused"
        else:
            status, stop_reason = _status_for(job_dir, rc)
            _jobs[job_id]["status"], _jobs[job_id]["stop_reason"] = status, stop_reason
        _jobs[job_id]["rc"] = rc
        _jobs[job_id]["finished"] = time.time()
    _save_meta(job_id)


def _worker():
    """Serial job runner: pick the oldest queued job, run it, repeat. Paused jobs are not
    'queued' so they are naturally skipped; resume re-enqueues them."""
    while True:
        with _cond:
            while not any(_jobs.get(j, {}).get("status") == "queued" for j in _queue):
                _cond.wait()
            job_id = next(j for j in _queue if _jobs.get(j, {}).get("status") == "queued")
            _queue.remove(job_id)
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
    the restart) when the previous app instance stopped - the queue is in-memory. User-paused
    jobs are left paused (manual resume); finished jobs (a terminal zip) are not re-run."""
    if not JOBS.exists():
        return
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
        if raw.get("status") in ("queued", "running") and not (p / "dataroom.zip").exists():
            m = _load_meta(p.name)          # full meta: query + budgets
            m["status"] = "queued"
            with _cond:
                _jobs[p.name] = m
                if p.name not in _queue:
                    _queue.append(p.name)
                _cond.notify_all()


_recover_queue()


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/jobs")
def create(req: JobReq):
    if not req.query.strip():
        raise HTTPException(400, "query required")
    job_id = uuid.uuid4().hex[:12]
    mf = int(req.min_files) if req.min_files and req.min_files > 0 else None
    with _cond:
        _jobs[job_id] = {"status": "queued", "query": req.query, "min_files": mf,
                         "max_turns": req.max_turns, "max_seconds": req.max_seconds,
                         "submitted": time.time()}
        _queue.append(job_id)
        _cond.notify_all()           # wake the worker to pick it up (runs when the slot is free)
    (JOBS / job_id).mkdir(parents=True, exist_ok=True)
    _save_meta(job_id)
    return {"job_id": job_id, "status": "queued"}


def _cur_status(job_id: str) -> str | None:
    with _lock:
        if job_id in _jobs:
            return _jobs[job_id].get("status")
    m = _load_meta(job_id)
    return m.get("status") if m else None


@app.post("/jobs/{job_id}/pause")
def pause(job_id: str):
    """Pause an unfinished job. A queued job just leaves the run queue; a running job gets a
    cooperative 'pause' flag and stops at its next cycle boundary (status -> pausing -> paused).
    The worker then advances to the next queued job."""
    st = _cur_status(job_id)
    if st == "queued":
        with _cond:
            _jobs.setdefault(job_id, {}).update({"status": "paused", "stop_reason": "paused"})
            if job_id in _queue:
                _queue.remove(job_id)
        _save_meta(job_id)
        return {"status": "paused"}
    if st in ("running", "pausing"):
        # Flag it (authoritative for the resulting status) AND signal for promptness: a long agent
        # cycle would otherwise delay the cooperative checkpoint by minutes. SIGTERM -> the
        # orchestrator's handler unwinds cleanly (reaps pi + index, zips); control=pause -> paused.
        (JOBS / job_id / "control").write_text("pause")
        with _lock:
            if job_id in _jobs:
                _jobs[job_id]["status"] = "pausing"
            proc = _current["proc"] if _current["job_id"] == job_id else None
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass
        _save_meta(job_id)
        return {"status": "pausing"}
    raise HTTPException(409, f"cannot pause a job in state {st}")


@app.post("/jobs/{job_id}/resume")
def resume(job_id: str):
    """Re-enqueue a paused job. The worker continues it from the on-disk dataroom (a fresh pi
    session re-reads STATUS.md/OUTLINE and keeps building); it goes to the back of the queue."""
    st = _cur_status(job_id)
    if st != "paused":
        raise HTTPException(409, f"cannot resume a job in state {st}")
    with _cond:
        if job_id not in _jobs:
            _jobs[job_id] = _load_meta(job_id)      # restore query + budgets after a restart
        # Clear the prior run's clock: _run_one sets a fresh `started`, and leaving the old
        # `finished` in place makes the resumed run read a finished < started (negative elapsed).
        _jobs[job_id].update({"status": "queued", "stop_reason": None,
                              "started": None, "finished": None})
        if job_id not in _queue:
            _queue.append(job_id)
        _cond.notify_all()
    _save_meta(job_id)
    return {"status": "queued"}


@app.get("/jobs")
def list_jobs():
    """Lightweight summary of every job (live + on-disk) for the homepage list.

    Deliberately cheap: status/query from meta + the on-disk floor/progress; it does NOT
    parse pi.log or poll llama (that is the per-job dashboard's job)."""
    ids = set(_jobs.keys())
    if JOBS.exists():
        for p in JOBS.iterdir():
            if p.is_dir():
                ids.add(p.name)
    rows = []
    for jid in ids:
        with _lock:
            meta = dict(_jobs.get(jid, {}))
        if not meta or meta.get("status") not in ("queued", "running"):
            disk = _load_meta(jid)                       # reconciles terminal status from disk
            disk.update({k: v for k, v in meta.items() if v is not None})
            meta = disk or meta
        job_dir = JOBS / jid
        dataroom = job_dir / "dataroom"
        fm = floor_metrics(dataroom, meta.get("min_files"))
        pr = _status_progress(dataroom)
        fc = (sum(1 for p in dataroom.rglob("*")
                  if p.is_file() and not p.name.startswith(".index")) if dataroom.exists() else 0)
        rows.append({
            "job_id": jid,
            "status": meta.get("status", "unknown"),
            "stop_reason": meta.get("stop_reason"),
            "query": (meta.get("query") or "")[:200],
            "started": meta.get("started"),
            "finished": meta.get("finished"),
            "max_seconds": meta.get("max_seconds"),
            "substantive_files": fm["substantive_files"],
            "min_files": fm["min_files"],
            "progress": pr,
            "file_count": fc,
            "zip_ready": (job_dir / "dataroom.zip").exists(),
        })
    rows.sort(key=lambda r: (r.get("started") or 0), reverse=True)
    return {"jobs": rows}


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
