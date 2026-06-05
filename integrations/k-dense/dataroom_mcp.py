#!/usr/bin/env python3
"""Dataroom-as-a-Service MCP server for K-Dense BYOK (Kady).

Exposes the Dataroom job API (submit -> poll -> download) as MCP tools so Kady can
delegate long, fully-cited factual deep-dives to a self-hosted Dataroom instance.

Transport: stdio. Point it at your LOCAL Dataroom (default http://localhost:8000),
not the shared hosted demo. Base URL resolution order: argv[1] > $DATAROOM_BASE > default.

Register it in a project's custom_mcps.json (see custom_mcps.json in this folder).
"""

import os
import sys
import time
import zipfile

import httpx
from mcp.server.fastmcp import FastMCP

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DATAROOM_BASE", "http://localhost:8000")).rstrip("/")
TERMINAL = {"done", "stopped", "failed"}

mcp = FastMCP("dataroom")


def _client() -> httpx.Client:
    # Generous read timeout: submit/status are fast, but downloads of a finished
    # dataroom zip can take a moment. Connect timeout stays short to fail fast if
    # the local Dataroom isn't running.
    return httpx.Client(base_url=BASE, timeout=httpx.Timeout(120.0, connect=5.0))


@mcp.tool()
def commission_dataroom(query: str, minutes: int = 30, max_turns: int | None = None) -> dict:
    """Commission a deep, fully-cited background-research dataroom on a topic, then return
    immediately with a job id (non-blocking).

    Use this for comprehensive, sourced research corpora on a topic BEFORE a long-horizon
    task (implementation, analysis, a report) - not for quick factual lookups (use web search
    for those). `minutes` is a time box: the autonomous agent works up to that long, then hands
    over whatever it assembled. Poll with dataroom_status, then download with collect_dataroom.

    Returns: {job_id, status, dashboard_url}.
    """
    if not query.strip():
        raise ValueError("query must not be empty")
    payload: dict = {"query": query, "max_seconds": int(minutes) * 60}
    if max_turns is not None:
        payload["max_turns"] = int(max_turns)
    with _client() as c:
        r = c.post("/jobs", json=payload)
        r.raise_for_status()
        data = r.json()
    job_id = data["job_id"]
    return {"job_id": job_id, "status": data.get("status", "queued"),
            "dashboard_url": f"{BASE}/jobs/{job_id}/dashboard"}


@mcp.tool()
def dataroom_status(job_id: str) -> dict:
    """Check a dataroom job's progress.

    Returns live metrics: status (queued | running | paused | done | stopped | failed),
    the query, time-budget percent, and file/source counts. `done` and `stopped` are both
    successful terminal states (`stopped` = hit the time box and handed over a full result);
    `failed` is the only real failure.
    """
    with _client() as c:
        r = c.get(f"/jobs/{job_id}/stats")
        if r.status_code == 404:
            raise ValueError(f"unknown job_id: {job_id}")
        r.raise_for_status()
        s = r.json()
    status = s.get("job_status") or s.get("status")
    budget = s.get("budget") or {}
    return {"job_id": job_id, "status": status, "is_terminal": status in TERMINAL,
            "query": s.get("query"), "budget_percent": budget.get("percent"),
            "elapsed_seconds": budget.get("elapsed_seconds"),
            "dashboard_url": f"{BASE}/jobs/{job_id}/dashboard"}


@mcp.tool()
def collect_dataroom(job_id: str, dest_dir: str = ".", partial: bool = False) -> dict:
    """Download a dataroom result and unzip it locally, returning the folder path plus the
    text of reports/SUMMARY.md and OUTLINE.md so you can start reading immediately.

    Set partial=True to grab a snapshot of work-so-far while the job is still running
    (otherwise the final result is only available once the job reaches done/stopped).

    Returns: {path, summary, outline, files}.
    """
    endpoint = f"/jobs/{job_id}/snapshot" if partial else f"/jobs/{job_id}/result"
    out_root = os.path.abspath(dest_dir)
    os.makedirs(out_root, exist_ok=True)
    zip_path = os.path.join(out_root, f"dataroom-{job_id}.zip")
    with _client() as c:
        with c.stream("GET", endpoint) as r:
            if r.status_code == 409:
                raise ValueError("result not ready yet - job has not stopped; poll dataroom_status "
                                 "or call again with partial=True for a snapshot")
            if r.status_code == 404:
                raise ValueError(f"unknown job_id: {job_id}")
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    extract_dir = os.path.join(out_root, job_id)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(extract_dir)
    room = os.path.join(extract_dir, "dataroom")
    if not os.path.isdir(room):
        room = extract_dir

    def _read(rel: str, cap: int = 20000) -> str | None:
        p = os.path.join(room, rel)
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="ignore") as f:
                return f.read()[:cap]
        return None

    files = sorted(
        os.path.relpath(os.path.join(dp, fn), room)
        for dp, _, fns in os.walk(room) for fn in fns
        if not fn.startswith(".index")
    )
    return {"path": room, "summary": _read("reports/SUMMARY.md"),
            "outline": _read("OUTLINE.md"), "files": files}


@mcp.tool()
def research_with_dataroom(query: str, minutes: int = 30, poll_seconds: int = 30,
                           dest_dir: str = ".") -> dict:
    """One-shot deep research: commission a dataroom, wait for it to finish (up to its time
    box), download it, and return the SUMMARY plus the folder path. Blocking.

    Use when you want a complete, fully-cited research corpus in a single call and are willing
    to wait. For finer control (or to keep working while it runs), use commission_dataroom +
    dataroom_status + collect_dataroom instead.

    Returns: {job_id, status, path, summary, outline, files}.
    """
    job = commission_dataroom(query=query, minutes=minutes)
    job_id = job["job_id"]
    deadline = time.time() + int(minutes) * 60 + 300  # time box + a 5-min handover buffer
    status = job["status"]
    while time.time() < deadline:
        time.sleep(max(5, int(poll_seconds)))
        st = dataroom_status(job_id)
        status = st["status"]
        if st["is_terminal"]:
            break
    if status == "failed":
        return {"job_id": job_id, "status": status, "path": None, "summary": None,
                "outline": None, "files": [], "dashboard_url": job["dashboard_url"]}
    collected = collect_dataroom(job_id=job_id, dest_dir=dest_dir,
                                 partial=status not in TERMINAL)
    return {"job_id": job_id, "status": status, **collected}


if __name__ == "__main__":
    mcp.run()
