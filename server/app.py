#!/usr/bin/env python3
"""DaaS API: submit a query, get an async job that builds a dataroom, download the zip.

POST /jobs            {query}            -> {job_id}
GET  /jobs/{id}                          -> {status, turns, ...}
GET  /jobs/{id}/result                   -> final dataroom.zip (when status=done)
GET  /jobs/{id}/snapshot                 -> dataroom-so-far.zip, zipped live (any time)
GET  /jobs/{id}/log                      -> tail of pi.log
GET  /health
"""
import io, json, os, subprocess, sys, threading, uuid, time, zipfile
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
    min_files: int | None = None         # outcome-floor "budget": files before it may stop


def _run(job_id: str, query: str, max_turns: int | None, max_seconds: int | None,
         min_files: int | None):
    job_dir = JOBS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "query.txt").write_text(query)
    cmd = [sys.executable, "-m", "server.run_dataroom", "--query", query,
           "--out", str(job_dir)]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    if max_seconds:
        cmd += ["--max-seconds", str(max_seconds)]
    # The orchestrator + floor read MIN_FILES from the environment; override it per job.
    env = dict(os.environ)
    if min_files:
        env["MIN_FILES"] = str(int(min_files))
    with _lock:
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["started"] = time.time()
    _save_meta(job_id)
    log = open(job_dir / "orchestrator.log", "a")
    rc = subprocess.call(cmd, cwd=str(HERE.parent), env=env, stdout=log, stderr=subprocess.STDOUT)
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
    mf = int(req.min_files) if req.min_files and req.min_files > 0 else None
    with _lock:
        _jobs[job_id] = {"status": "queued", "query": req.query, "min_files": mf}
    (JOBS / job_id).mkdir(parents=True, exist_ok=True)
    _save_meta(job_id)
    t = threading.Thread(target=_run, args=(job_id, req.query, req.max_turns,
                                            req.max_seconds, mf), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "queued"}


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
    s = job_stats(job_dir, meta.get("min_files"))
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
