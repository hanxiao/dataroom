#!/usr/bin/env python3
"""Live stats for a dataroom job, derived from Pi's JSON event stream (pi.log) + the dataroom dir.

Pi `--mode json` emits one JSON object per line. We care about:
  - {"type":"message_end","message":{... "usage":{input,output,cacheRead,cacheWrite,total,cost}}}
  - {"type":"tool_execution_start","toolName":"...", ...}
Context utilization = latest message total tokens / model context window.
"""
import json, os
from pathlib import Path

CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "16384"))


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


def parse_pi_log(log_path: Path) -> dict:
    tool_counts: dict[str, int] = {}
    tool_calls = 0
    last_usage = None
    turns = 0
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
                if t == "tool_execution_start":
                    name = ev.get("toolName") or "unknown"
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                    tool_calls += 1
                elif t == "agent_start":
                    turns += 1
                elif t == "message_end":
                    u = (ev.get("message") or {}).get("usage")
                    if u:
                        last_usage = u
    ctx_tokens = int(last_usage.get("total", 0)) if last_usage else 0
    return {
        "tool_calls": tool_calls,
        "tool_distribution": dict(sorted(tool_counts.items(),
                                         key=lambda kv: -kv[1])),
        "turns": turns,
        "usage": last_usage or {},
        "context": {
            "tokens": ctx_tokens,
            "window": CONTEXT_WINDOW,
            "percent": round(100 * ctx_tokens / CONTEXT_WINDOW, 1) if CONTEXT_WINDOW else 0,
        },
    }


def job_stats(job_dir: Path) -> dict:
    dataroom = job_dir / "dataroom"
    tree, size = _walk_tree(dataroom)
    log = parse_pi_log(job_dir / "pi.log")
    # file count
    file_count = sum(1 for p in dataroom.rglob("*")
                     if p.is_file() and not p.name.startswith(".index")) if dataroom.exists() else 0
    status = ""
    sp = dataroom / "STATUS.md"
    done = False
    if sp.exists():
        status = sp.read_text(errors="ignore")
        done = status.lstrip()[:16].upper().startswith("DONE")
    return {
        **log,
        "dataroom": {"tree": tree, "size_bytes": size, "file_count": file_count},
        "status_md": status[:6000],
        "done": done,
    }
