#!/usr/bin/env python3
"""Orchestrator: runs the autonomous Pi harness loop for one dataroom job.

- Boots the per-job index sidecar (jina-embeddings-v5-nano) backing `dataroom_index`.
- Writes per-job Pi config (models.json -> local Qwen, mcp.json -> Jina MCP).
- Loops `pi --mode json --continue` (same per-cwd session) so Qwen keeps driving its own
  loop, using Jina MCP + dataroom_index + bash.

Stopping is OUTCOME-FIRST, not budget-first. `DONE` is only honored once a measurable
coverage floor is met (enough substantive sourced files + a SUMMARY + no open questions);
a premature DONE is rejected and the loop is nudged to keep going. The loop otherwise runs
as long as it makes progress, with diminishing-returns early-stop, and hard safety ceilings
(wall-clock / turns / paid-Jina-calls) so it can never run truly forever. The reason it
stopped is persisted to run_meta.json and surfaced on the dashboard.

Most of the intelligence lives in the agent, not here: we expose tools + a one-page skill
and let Qwen run the harness. This file only supervises the loop and enforces the floor/ceiling.
"""
import argparse, json, os, subprocess, sys, time, zipfile, signal, socket, urllib.request, urllib.error
from collections import deque
from pathlib import Path

from server.stats import floor_metrics  # shared floor definition (also used by /stats)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def write_pi_config(agent_dir: Path, llama_url: str, jina_key: str, index_url: str):
    """Per-job, isolated Pi agent dir so default LLM = local Qwen and Jina MCP is wired."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    # Context window = the llama-server --ctx-size (default 131072, Qwen3.6 native max).
    # Pi's built-in auto-compaction (core, fires in --mode json too) triggers at
    # ctx > window - reserveTokens; keepRecentTokens of recent context survives and older
    # turns become an LLM summary. We scale reserve/keep with the window so we never
    # overflow and don't compact prematurely.
    ctx = int(os.environ.get("CONTEXT_WINDOW", os.environ.get("CTX_SIZE", "131072")))
    max_tokens = 8192
    reserve = max(max_tokens + 2048, ctx // 8)   # >= maxTokens, scales with window
    keep_recent = max(16000, ctx // 3)           # healthy recent window survives compaction
    # default LLM: self-hosted Qwen3.6 (OpenAI-compatible llama.cpp server)
    (agent_dir / "models.json").write_text(json.dumps({
        "providers": {
            "local": {
                "baseUrl": f"{llama_url}/v1",
                "api": "openai-completions",
                "apiKey": "sk-local",
                "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
                "models": [{"id": "qwen3.6", "contextWindow": ctx, "maxTokens": max_tokens}],
            }
        }
    }, indent=2))
    (agent_dir / "settings.json").write_text(json.dumps({
        "defaultProvider": "local",
        "defaultModel": "qwen3.6",
        "defaultThinkingLevel": "off",
        "enableInstallTelemetry": False,
        "compaction": {"enabled": True, "keepRecentTokens": keep_recent,
                       "reserveTokens": reserve},
    }, indent=2))
    # Jina MCP (search_web / read_url / embeddings) — hosted endpoint. pi has no built-in
    # MCP client; pi-mcp-adapter (pi's official MCP extension) bridges this as a proxy tool.
    (agent_dir / "mcp.json").write_text(json.dumps({
        "mcpServers": {
            "jina": {
                "url": "https://mcp.jina.ai/v1",
                "headers": {"Authorization": f"Bearer {jina_key}"},
                "lifecycle": "lazy",
            }
        }
    }, indent=2))
    # dataroom_index extension target
    os.environ["DATAROOM_INDEX_URL"] = index_url


def boot_index(job_dir: Path, index_port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["DATAROOM_DIR"] = str(job_dir / "dataroom")
    env["INDEX_PORT"] = str(index_port)
    # EMBED_DEVICE (default cuda) is inherited from the container env; v5-nano shares
    # the L4 with the LLM. Set EMBED_DEVICE=cpu to avoid VRAM contention if needed.
    return subprocess.Popen(
        [sys.executable, str(HERE / "index_service.py")],
        env=env, stdout=open(job_dir / "index.log", "a"),
        stderr=subprocess.STDOUT, start_new_session=True,
    )


def wait_http(url: str, timeout: int = 120) -> bool:
    """Up = any HTTP response. POST-only endpoints answer GET with 405; that still means up."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except urllib.error.HTTPError:
            return True                      # 405/4xx => server is up
        except Exception:
            time.sleep(2)
    return False


def status_done(dataroom: Path) -> bool:
    """True when STATUS.md's first line declares completion.

    Accepts a bare `DONE` (legacy) or the templated `STATUS: DONE` first line, so the
    early-stop fires regardless of which form the agent writes (see SKILL.md template)."""
    sp = dataroom / "STATUS.md"
    if not sp.exists():
        return False
    first = sp.read_text(errors="ignore").lstrip().splitlines()[:1]
    if not first:
        return False
    head = first[0].strip().upper()
    return head.startswith("DONE") or head.startswith("STATUS: DONE") or head.startswith("STATUS:DONE")


def index_count(index_url: str) -> int:
    """Chunk count from the index sidecar (best-effort)."""
    try:
        req = urllib.request.Request(f"{index_url}/stats", data=b"{}", method="POST",
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return int(json.loads(r.read()).get("count", 0))
    except Exception:
        return 0


def count_jina_calls(log_path: Path) -> int:
    """Paid Jina calls so far = tool_execution_start events on the `mcp` proxy tool.

    The self-hosted LLM is a sunk cost; Jina search_web/read_url/embeddings are billed
    per call and all funnel through the single `mcp` proxy tool, so this bounds spend."""
    n = 0
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
                if ev.get("type") == "tool_execution_start":
                    name = ev.get("toolName") or ""
                    if name == "mcp" or name.startswith("mcp:"):
                        n += 1
    return n


def run_turn(job_dir: Path, agent_dir: Path, prompt: str, timeout: int) -> int:
    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)  # isolate config per job
    env["PI_SKIP_VERSION_CHECK"] = "1"
    pi_bin = os.environ.get("PI_BIN", "pi")
    cmd = [
        pi_bin, "--mode", "json", "--continue",
        "--skill", str(REPO / "pi" / "skills" / "dataroom"),
        "--extension", str(REPO / "pi" / "extensions" / "dataroom-index.ts"),
    ]
    # pi-mcp-adapter exposes Jina MCP (search_web/read_url) as the `mcp` proxy tool.
    mcp_adapter = os.environ.get("PI_MCP_ADAPTER")
    if mcp_adapter and Path(mcp_adapter).exists():
        cmd += ["--extension", mcp_adapter]
    cmd.append(prompt)
    log = open(job_dir / "pi.log", "a")
    log.write(f"\n\n===== TURN @ {time.ctime()} =====\n")
    log.flush()
    try:
        return subprocess.call(cmd, cwd=str(job_dir), env=env,
                               stdout=log, stderr=subprocess.STDOUT, timeout=max(1, timeout))
    except subprocess.TimeoutExpired:
        log.write("\n[orchestrator] turn timed out\n")
        return 124


def zip_dataroom(dataroom: Path, out_zip: Path):
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in dataroom.rglob("*"):
            if p.is_file() and not p.name.startswith(".index"):
                z.write(p, p.relative_to(dataroom.parent))


def write_run_meta(job_dir: Path, **fields):
    """Persist stop_reason + floor metrics so /stats and the dashboard can show *why* it stopped."""
    try:
        (job_dir / "run_meta.json").write_text(json.dumps(fields, indent=2))
    except Exception:
        pass


# Prompts ---------------------------------------------------------------------
FIRST_PROMPT = (
    "Research query: {query}\n\n"
    "Load and follow the `dataroom` skill. You are in autonomous dataroom-building mode. "
    "Build the dataroom under ./dataroom. Drive your own loop this turn: read state, pick "
    "the highest-value open question, research with Jina MCP (search_web/read_url), dedup via "
    "dataroom_index before writing, write sourced notes, verify with code when it matters, and "
    "keep STATUS.md/OUTLINE.md current. Do as much as you can before ending the turn."
)
CONT_PROMPT = (
    "Continue building the dataroom. Read STATUS.md and dataroom_index outline first, then "
    "advance the next highest-value open question. Dedup before writing. Only write DONE on "
    "the first line of STATUS.md once the coverage floor is met (>= {min_files} substantive "
    "sourced files, all open questions closed, reports/SUMMARY.md present)."
)
STALL_PROMPT = (
    "The last turn added no new substantive sourced files. Do not repeat the same searches. "
    "Use expand_query / search_arxiv to open NEW angles on the most under-covered open question "
    "in STATUS.md, read primary sources, and write at least one new sourced note this turn. "
    "Then dedup before writing."
)
CORRECTIVE_PROMPT = (
    "You wrote DONE but the dataroom is NOT comprehensive yet: {substantive_files}/{min_files} "
    "substantive sourced files, {open_questions} open question(s) still unchecked, "
    "SUMMARY.md present={summary_exists}. Remove DONE from STATUS.md, keep researching the open "
    "questions with new sources, and only write DONE again once the floor is actually met."
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--out", default="./out")
    # Safety ceilings (not the primary stop). The loop runs until the outcome floor is met
    # or it saturates; these only bound the worst case. Raise them freely.
    ap.add_argument("--max-turns", type=int, default=int(os.environ.get("MAX_TURNS", "300")))
    ap.add_argument("--max-seconds", type=int, default=int(os.environ.get("MAX_SECONDS", "21600")))
    ap.add_argument("--turn-timeout", type=int, default=int(os.environ.get("TURN_TIMEOUT", "1200")))
    ap.add_argument("--max-jina-calls", type=int, default=int(os.environ.get("MAX_JINA_CALLS", "2000")))
    args = ap.parse_args()

    # L3: a non-positive ceiling would make the loop never run and crash at report time.
    if args.max_turns < 1:
        print("ERROR: --max-turns must be >= 1", file=sys.stderr)
        sys.exit(2)
    if args.max_seconds < 1:
        print("ERROR: --max-seconds must be >= 1", file=sys.stderr)
        sys.exit(2)

    min_files = int(os.environ.get("MIN_FILES", "100"))
    sat_window = int(os.environ.get("SATURATION_WINDOW", "3"))
    min_new = int(os.environ.get("MIN_NEW_FILES_PER_TURN", "2"))

    llama_url = os.environ.get("LLAMA_URL", "http://localhost:8080")
    jina_key = os.environ.get("JINA_API_KEY", "")
    if not jina_key:
        print("ERROR: JINA_API_KEY required", file=sys.stderr)
        sys.exit(2)

    job_dir = Path(args.out).resolve()
    dataroom = job_dir / "dataroom"
    dataroom.mkdir(parents=True, exist_ok=True)
    agent_dir = job_dir / ".pi-agent"
    index_port = free_port()
    index_url = f"http://127.0.0.1:{index_port}"

    write_pi_config(agent_dir, llama_url, jina_key, index_url)
    idx = boot_index(job_dir, index_port)
    if not wait_http(f"{index_url}/stats", 120):
        print("ERROR: index sidecar did not come up", file=sys.stderr)
        try:
            os.killpg(os.getpgid(idx.pid), signal.SIGTERM)
        except Exception:
            idx.terminate()
        write_run_meta(job_dir, stop_reason="error_index_boot", turns=0, done=False)
        sys.exit(3)

    first = FIRST_PROMPT.format(query=args.query)
    cont = CONT_PROMPT.format(min_files=min_files)
    log_path = job_dir / "pi.log"

    start = time.time()
    turn = 0
    stop_reason = "ceiling_turns"          # default if the for-range exhausts
    recent_deltas = deque(maxlen=sat_window)
    prev_substantive = 0
    fm = floor_metrics(dataroom)

    try:
        for turn in range(1, args.max_turns + 1):
            elapsed = time.time() - start
            if elapsed > args.max_seconds:
                stop_reason = "ceiling_seconds"; break
            jina_calls = count_jina_calls(log_path)
            if jina_calls > args.max_jina_calls:
                stop_reason = "ceiling_cost"; break

            # Choose the nudge: first turn, stall steer, or normal continue.
            if turn == 1:
                prompt = first
            elif recent_deltas and recent_deltas[-1] <= 0:
                prompt = STALL_PROMPT          # M3/M4: don't re-inject an identical nudge into a stuck loop
            else:
                prompt = cont

            print(f"[orchestrator] turn {turn}/{args.max_turns} "
                  f"(files={prev_substantive}/{min_files}, jina_calls={jina_calls})")
            # L5: never run a turn past the wall-clock ceiling.
            remaining = max(1, int(args.max_seconds - (time.time() - start)))
            rc = run_turn(job_dir, agent_dir, prompt, min(args.turn_timeout, remaining))
            print(f"[orchestrator] turn {turn} rc={rc}")

            # Progress accounting.
            fm = floor_metrics(dataroom)
            sub = fm["substantive_files"]
            recent_deltas.append(sub - prev_substantive)
            prev_substantive = sub

            # H5: DONE is only honored when the floor is actually met; otherwise reject + correct.
            if status_done(dataroom):
                if fm["floor_met"]:
                    stop_reason = "done_floor_met"; break
                print(f"[orchestrator] DONE rejected, floor unmet: {fm}")
                cont = CORRECTIVE_PROMPT.format(min_files=min_files, **{
                    k: fm[k] for k in ("substantive_files", "open_questions", "summary_exists")})
                continue       # keep going; this turn's real delta is already recorded
            else:
                cont = CONT_PROMPT.format(min_files=min_files)   # reset any corrective prompt

            # Diminishing-returns early stop, but only once the floor is satisfied.
            saturated = (len(recent_deltas) == sat_window and
                         all(d < min_new for d in recent_deltas))
            if saturated and fm["floor_met"]:
                stop_reason = "done_saturated"; break
            # If saturated but floor still unmet, keep going (a STALL_PROMPT was/ will be sent)
            # up to the ceiling; the dashboard surfaces this as a coverage warning.
    except KeyboardInterrupt:
        stop_reason = "interrupted"
    finally:
        # N1: reap the sidecar deterministically (SIGTERM, wait, then SIGKILL).
        try:
            os.killpg(os.getpgid(idx.pid), signal.SIGTERM)
            try:
                idx.wait(timeout=10)
            except Exception:
                os.killpg(os.getpgid(idx.pid), signal.SIGKILL)
        except Exception:
            idx.terminate()

    out_zip = job_dir / "dataroom.zip"
    zip_dataroom(dataroom, out_zip)
    done = stop_reason in ("done_floor_met", "done_saturated")
    write_run_meta(job_dir, stop_reason=stop_reason, turns=turn, done=done,
                   floor=fm, index_count=index_count(index_url),
                   jina_calls=count_jina_calls(log_path),
                   elapsed_seconds=round(time.time() - start, 1))
    print(f"[orchestrator] wrote {out_zip} (stop_reason={stop_reason})")
    print(json.dumps({"zip": str(out_zip), "turns": turn, "done": done,
                      "stop_reason": stop_reason, "floor": fm}))


if __name__ == "__main__":
    main()
