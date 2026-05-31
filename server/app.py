#!/usr/bin/env python3
"""DaaS API: submit a query, get an async job that builds a dataroom, download the zip.

POST /jobs            {query}            -> {job_id}
GET  /jobs/{id}                          -> {status, turns, ...}
GET  /jobs/{id}/result                   -> dataroom.zip (when status=done)
GET  /jobs/{id}/log                      -> tail of pi.log
GET  /health
"""
import json, os, subprocess, sys, threading, uuid, time
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, HTMLResponse
from pydantic import BaseModel
import uvicorn

from server.stats import job_stats

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
JOBS = Path(os.environ.get("JOBS_DIR", "/data/jobs")).resolve()
JOBS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Dataroom-as-a-Service")
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


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
    rmeta = _run_meta(job_dir)
    if (job_dir / "dataroom.zip").exists():
        status = "done" if rmeta.get("done") else "stopped"
    elif rc not in (None, 0):
        status = "failed"
    else:
        status = "failed"
    return status, rmeta.get("stop_reason")


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
        # app restarted mid-run; the worker thread is gone -> mark interrupted
        meta["status"] = "interrupted"
    if not meta.get("query") and (job_dir / "query.txt").exists():
        meta["query"] = (job_dir / "query.txt").read_text(errors="ignore").strip()
    return meta


class JobReq(BaseModel):
    query: str
    max_turns: int | None = None
    max_seconds: int | None = None


def _run(job_id: str, query: str, max_turns: int | None, max_seconds: int | None):
    job_dir = JOBS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "query.txt").write_text(query)
    cmd = [sys.executable, "-m", "server.run_dataroom", "--query", query,
           "--out", str(job_dir)]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    if max_seconds:
        cmd += ["--max-seconds", str(max_seconds)]
    with _lock:
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["started"] = time.time()
    _save_meta(job_id)
    log = open(job_dir / "orchestrator.log", "a")
    rc = subprocess.call(cmd, cwd=str(HERE.parent), stdout=log, stderr=subprocess.STDOUT)
    status, stop_reason = _status_for(job_dir, rc)
    with _lock:
        _jobs[job_id]["status"] = status
        _jobs[job_id]["stop_reason"] = stop_reason
        _jobs[job_id]["rc"] = rc
        _jobs[job_id]["finished"] = time.time()
    _save_meta(job_id)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/jobs")
def create(req: JobReq):
    if not req.query.strip():
        raise HTTPException(400, "query required")
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {"status": "queued", "query": req.query}
    (JOBS / job_id).mkdir(parents=True, exist_ok=True)
    _save_meta(job_id)
    t = threading.Thread(target=_run, args=(job_id, req.query, req.max_turns,
                                            req.max_seconds), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "queued"}


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


@app.get("/jobs/{job_id}/stats")
def stats_ep(job_id: str):
    job_dir = JOBS / job_id
    if not job_dir.exists():
        raise HTTPException(404, "unknown job")
    with _lock:
        meta = dict(_jobs.get(job_id, {}))
    if not meta:
        meta = _load_meta(job_id)   # recover after app restart
    s = job_stats(job_dir)
    s["job_id"] = job_id
    s["job_status"] = meta.get("status") or ("done" if (job_dir / "dataroom.zip").exists() else "unknown")
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


@app.get("/jobs/{job_id}/log")
def joblog(job_id: str):
    p = JOBS / job_id / "pi.log"
    if not p.exists():
        raise HTTPException(404, "no log yet")
    data = p.read_text(errors="ignore")
    return PlainTextResponse(data[-8000:])


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
