#!/usr/bin/env python3
"""Live stats for a dataroom job, derived from Pi's JSON event stream (pi.log) + the dataroom dir.

Pi `--mode json` emits one JSON object per line. We surface:
  - context utilization (live KV occupancy from llama.cpp /slots),
  - tool-call count + distribution (with the `mcp` proxy split into mcp:<inner>),
  - a live ACTIVITY feed (what the agent is doing right now / recently),
  - a WARNINGS/ERRORS list (failed tool calls + index/llama failures),
  - PROGRESS toward the outcome floor (STATUS.md checkboxes + substantive-file floor),
  - the stop_reason once the run ends (from run_meta.json).
"""
import json, os, urllib.request
from pathlib import Path

CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", os.environ.get("CTX_SIZE", "131072")))
LLAMA_URL = os.environ.get("LLAMA_URL", "http://llama-server:8080")

# Outcome floor (shared with the orchestrator in run_dataroom.py).
MIN_FILES = int(os.environ.get("MIN_FILES", "100"))
MIN_FILE_BYTES = int(os.environ.get("MIN_FILE_BYTES", "500"))


# --- outcome floor -----------------------------------------------------------
def floor_metrics(dataroom: Path) -> dict:
    """Measurable, hard-to-game completion floor.

    A "substantive" file is a note under topics/ or reports/ that is non-trivial AND
    carries a `## Sources` section (i.e. evidence-backed, not scaffolding). Raw file_count
    is deliberately NOT used: it counts STATUS/OUTLINE/CONTRACT and stub files.
    """
    substantive = 0
    if dataroom.exists():
        for sub in ("topics", "reports"):
            d = dataroom / sub
            if not d.exists():
                continue
            for p in d.rglob("*.md"):
                try:
                    if p.stat().st_size < MIN_FILE_BYTES:
                        continue
                    txt = p.read_text(errors="ignore")
                except Exception:
                    continue
                if "## sources" in txt.lower():
                    substantive += 1
    summary = dataroom / "reports" / "SUMMARY.md"
    summary_exists = summary.exists() and summary.stat().st_size > 200
    open_q = _status_progress(dataroom)["open"]
    floor_met = (substantive >= MIN_FILES) and summary_exists and (open_q == 0)
    return {
        "substantive_files": substantive,
        "summary_exists": bool(summary_exists),
        "open_questions": open_q,
        "min_files": MIN_FILES,
        "floor_met": bool(floor_met),
    }


def _status_progress(dataroom: Path) -> dict:
    """Sub-question progress from STATUS.md `- [ ]` / `- [x]` checkboxes."""
    done = total = 0
    sp = dataroom / "STATUS.md"
    if sp.exists():
        for line in sp.read_text(errors="ignore").splitlines():
            s = line.strip().lower()
            if s.startswith("- [x]"):
                done += 1; total += 1
            elif s.startswith("- [ ]"):
                total += 1
    return {"done": done, "total": total, "open": max(0, total - done)}


# --- llama.cpp live signals --------------------------------------------------
def llama_kv() -> dict:
    """Live KV-cache occupancy from llama.cpp /slots (pi's usage tokens come back 0 from
    the llama.cpp OpenAI endpoint, so this is the accurate context-utilization source)."""
    try:
        with urllib.request.urlopen(f"{LLAMA_URL}/slots", timeout=3) as r:
            d = json.loads(r.read())
        s = d[0] if isinstance(d, list) and d else d
        n_ctx = int(s.get("n_ctx") or CONTEXT_WINDOW)
        tokens = int(s.get("n_prompt_tokens") or s.get("n_past") or 0)
        return {"tokens": tokens, "window": n_ctx,
                "processing": bool(s.get("is_processing"))}
    except Exception:
        return {}


def llama_tps() -> dict:
    """Live throughput (tok/s) from llama.cpp Prometheus /metrics (needs --metrics)."""
    out = {}
    try:
        with urllib.request.urlopen(f"{LLAMA_URL}/metrics", timeout=3) as r:
            for line in r.read().decode(errors="ignore").splitlines():
                if line.startswith("#") or " " not in line:
                    continue
                k, v = line.rsplit(" ", 1)
                try:
                    val = float(v)
                except ValueError:
                    continue
                if k == "llamacpp:predicted_tokens_seconds":
                    out["decode"] = round(val, 1)
                elif k == "llamacpp:prompt_tokens_seconds":
                    out["prefill"] = round(val, 1)
    except Exception:
        pass
    return out


def _walk_tree(root: Path) -> tuple[dict, int]:
    """Return (nested tree dict, total bytes), skipping index sidecar files."""
    total = 0

    def node(p: Path) -> dict:
        nonlocal total
        if p.is_dir():
            children = []
            for c in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name)):
                if c.name.startswith(".index"):
                    continue
                children.append(node(c))
            return {"name": p.name, "type": "dir", "children": children}
        sz = p.stat().st_size
        total += sz
        return {"name": p.name, "type": "file", "size": sz}

    if not root.exists():
        return {"name": root.name, "type": "dir", "children": []}, 0
    tree = node(root)
    return tree, total


# --- pi.log parsing ----------------------------------------------------------
def _as_obj(v):
    """Args may arrive as a dict or a JSON string; return a dict best-effort."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return {}


def _inner_mcp_tool(args) -> str:
    """The real Jina tool behind the `mcp` proxy call, for the distribution chart."""
    a = _as_obj(args)
    if a.get("tool"):
        return str(a["tool"])
    if a.get("search"):
        return "discover"
    return "mcp"


def _summarize(tool: str, args) -> str:
    """One-line, human-readable description of a tool call for the activity feed."""
    a = _as_obj(args)
    try:
        if tool == "mcp":
            inner = _inner_mcp_tool(a)
            inner_args = _as_obj(a.get("args"))
            detail = (inner_args.get("url") or inner_args.get("query")
                      or inner_args.get("queries") or a.get("search") or "")
            return f"{inner} {str(detail)[:80]}".strip()
        if tool == "dataroom_index":
            ia = _as_obj(a.get("args"))
            op = ia.get("op", "?")
            return f"index {op} {str(ia.get('query') or ia.get('path') or '')[:60]}".strip()
        if tool == "bash":
            return f"bash {str(a.get('command') or a.get('cmd') or '')[:90]}".strip()
        if tool in ("read", "write", "edit"):
            return f"{tool} {str(a.get('path') or a.get('file') or '')[:80]}".strip()
    except Exception:
        pass
    return tool


def _index_log_errors(job_dir: Path) -> list:
    """Surface a down/erroring index sidecar (tracebacks in index.log)."""
    out = []
    p = job_dir / "index.log"
    if p.exists():
        try:
            tail = p.read_text(errors="ignore").splitlines()[-400:]
        except Exception:
            tail = []
        for line in tail:
            if "Traceback" in line or "Error" in line:
                out.append({"turn": None, "tool": "index", "text": line.strip()[:200]})
    return out[-10:]


def parse_pi_log(log_path: Path, job_dir: Path) -> dict:
    tool_counts: dict = {}
    tool_calls = 0
    last_usage = None
    turns = 0
    recent = []     # ring buffer of recent activity
    errors = []     # failed tool calls
    if log_path.exists():
        with open(log_path, errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] != "{":
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                t = ev.get("type")
                if t == "agent_start":
                    turns += 1
                elif t == "tool_execution_start":
                    name = ev.get("toolName") or "unknown"
                    args = ev.get("args")
                    key = f"mcp:{_inner_mcp_tool(args)}" if name == "mcp" else name
                    tool_counts[key] = tool_counts.get(key, 0) + 1
                    tool_calls += 1
                    recent.append({"turn": turns, "tool": name, "text": _summarize(name, args)})
                    if len(recent) > 14:
                        recent.pop(0)
                elif t == "tool_execution_end":
                    res = ev.get("result") or {}
                    is_err = ev.get("isError") or (isinstance(res, dict) and res.get("isError"))
                    if is_err:
                        txt = ""
                        if isinstance(res, dict):
                            cont = res.get("content")
                            if isinstance(cont, list) and cont:
                                txt = str((cont[0] or {}).get("text") or "")
                        txt = txt or str(ev.get("error") or "tool error")
                        errors.append({"turn": turns, "tool": ev.get("toolName") or "?",
                                       "text": txt[:200]})
                        if len(errors) > 50:
                            errors.pop(0)
                elif t == "message_end":
                    u = (ev.get("message") or {}).get("usage")
                    if u:
                        last_usage = u
                    txt = (ev.get("message") or {}).get("text") or ev.get("text")
                    if txt:
                        recent.append({"turn": turns, "tool": "say", "text": str(txt)[:120]})
                        if len(recent) > 14:
                            recent.pop(0)

    errors = (errors + _index_log_errors(job_dir))[-50:]
    pi_tokens = 0
    if last_usage:
        pi_tokens = int(last_usage.get("total") or last_usage.get("totalTokens") or 0)
    kv = llama_kv()
    window = kv.get("window") or CONTEXT_WINDOW
    ctx_tokens = kv.get("tokens") or pi_tokens
    return {
        "tool_calls": tool_calls,
        "tool_distribution": dict(sorted(tool_counts.items(), key=lambda kv: -kv[1])),
        "turns": turns,
        "usage": last_usage or {},
        "recent": recent,
        "errors": errors,
        "context": {
            "tokens": ctx_tokens,
            "window": window,
            "percent": round(100 * ctx_tokens / window, 1) if window else 0,
            "processing": kv.get("processing", False),
        },
        "tps": llama_tps(),
    }


def job_stats(job_dir: Path) -> dict:
    dataroom = job_dir / "dataroom"
    tree, size = _walk_tree(dataroom)
    log = parse_pi_log(job_dir / "pi.log", job_dir)
    file_count = sum(1 for p in dataroom.rglob("*")
                     if p.is_file() and not p.name.startswith(".index")) if dataroom.exists() else 0
    status = ""
    sp = dataroom / "STATUS.md"
    done = False
    if sp.exists():
        status = sp.read_text(errors="ignore")

    floor = floor_metrics(dataroom)
    progress = _status_progress(dataroom)

    # stop_reason + done come from run_meta.json (written by the orchestrator at the end).
    stop_reason, zip_ready = None, (job_dir / "dataroom.zip").exists()
    rm = job_dir / "run_meta.json"
    if rm.exists():
        try:
            meta = json.loads(rm.read_text())
            stop_reason = meta.get("stop_reason")
            done = bool(meta.get("done"))
        except Exception:
            pass

    return {
        **log,
        "dataroom": {"tree": tree, "size_bytes": size, "file_count": file_count},
        "status_md": status[:6000],
        "floor": floor,
        "progress": progress,
        "stop_reason": stop_reason,
        "zip_ready": zip_ready,
        "done": done,
    }
