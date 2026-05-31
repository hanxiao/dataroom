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
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
import uvicorn

HERE = Path(__file__).resolve().parent
JOBS = Path(os.environ.get("JOBS_DIR", "/data/jobs")).resolve()
JOBS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Dataroom-as-a-Service")
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


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
    log = open(job_dir / "orchestrator.log", "a")
    rc = subprocess.call(cmd, cwd=str(HERE.parent), stdout=log, stderr=subprocess.STDOUT)
    zip_path = job_dir / "dataroom.zip"
    with _lock:
        _jobs[job_id]["status"] = "done" if zip_path.exists() and rc == 0 else "failed"
        _jobs[job_id]["rc"] = rc
        _jobs[job_id]["finished"] = time.time()


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
    t = threading.Thread(target=_run, args=(job_id, req.query, req.max_turns,
                                            req.max_seconds), daemon=True)
    t.start()
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
def status(job_id: str):
    with _lock:
        j = _jobs.get(job_id)
    if not j:
        # recover from disk if process restarted
        if (JOBS / job_id / "dataroom.zip").exists():
            return {"status": "done"}
        raise HTTPException(404, "unknown job")
    return j


@app.get("/jobs/{job_id}/result")
def result(job_id: str):
    zip_path = JOBS / job_id / "dataroom.zip"
    if not zip_path.exists():
        raise HTTPException(409, "not ready")
    return FileResponse(str(zip_path), media_type="application/zip",
                        filename=f"dataroom-{job_id}.zip")


@app.get("/jobs/{job_id}/log")
def joblog(job_id: str):
    p = JOBS / job_id / "pi.log"
    if not p.exists():
        raise HTTPException(404, "no log yet")
    data = p.read_text(errors="ignore")
    return PlainTextResponse(data[-8000:])


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
