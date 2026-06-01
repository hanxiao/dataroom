#!/usr/bin/env python3
"""Orchestrator: runs the autonomous Pi harness for one dataroom job.

- Boots the per-job index sidecar (jina-embeddings-v5-nano) backing `dataroom_index`.
- Writes per-job Pi config (models.json -> local Qwen). Jina access is the `jina` CLI on PATH.
- Drives ONE persistent `pi --mode rpc` session over stdin/stdout JSONL: send the initial
  prompt, then after each agent cycle (an `agent_end` event) re-engage with another `prompt`
  if the work is not done, or `abort` when a ceiling trips. Pi keeps the session,
  auto-compacts its own context, and runs its internal loop continuously - no per-turn
  process re-spawn, no `--continue` replay. A "turn" is just one re-nudge (one agent cycle).

Stopping is OUTCOME-FIRST, not budget-first. `DONE` is only honored once a measurable
coverage floor is met (enough substantive sourced files + a SUMMARY + no open questions);
a premature DONE is rejected and the agent is nudged to keep going. It otherwise runs as
long as it makes progress, with a diminishing-returns early-stop and hard safety ceilings
(wall-clock / turns / paid-Jina-calls). The reason it stopped is persisted to run_meta.json.

Most of the intelligence lives in the agent, not here: we expose tools + a one-page skill
and let Qwen run the harness. This file only supervises the floor/ceiling.
"""
import argparse, json, os, re, subprocess, sys, time, zipfile, signal, socket, threading, urllib.request, urllib.error
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
    """Per-job, isolated Pi agent dir so default LLM = local Qwen."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    # Context window = the llama-server --ctx-size (default 131072, Qwen3.6 native max).
    # Pi's built-in auto-compaction triggers at ctx > window - reserveTokens; keepRecentTokens
    # of recent context survives and older turns become an LLM summary. We scale reserve/keep
    # with the window so we never overflow and don't compact prematurely. In rpc mode this all
    # happens inside the single long-lived session.
    ctx = int(os.environ.get("CONTEXT_WINDOW", os.environ.get("CTX_SIZE", "131072")))
    # Agent-facing model id (must agree between models.json and settings.json below).
    # Free label for llama.cpp's OpenAI endpoint; default qwen3.6 reproduces today exactly.
    model_id = os.environ.get("MODEL_ID", "qwen3.6")
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
                "models": [{"id": model_id, "contextWindow": ctx, "maxTokens": max_tokens}],
            }
        }
    }, indent=2))
    (agent_dir / "settings.json").write_text(json.dumps({
        "defaultProvider": "local",
        "defaultModel": model_id,
        # Thinking ON for best quality (reference MTP tune enables it). Generates reasoning
        # tokens per cycle (more tokens, slower); set "medium"/"off" if throughput/ctx suffer.
        "defaultThinkingLevel": "high",
        "enableInstallTelemetry": False,
        "compaction": {"enabled": True, "keepRecentTokens": keep_recent,
                       "reserveTokens": reserve},
    }, indent=2))
    # No Jina MCP: the agent uses the `jina` CLI via its bash tool (search/read/rerank/...).
    # jina-cli reads JINA_API_KEY from the environment, which the pi subprocess inherits.
    os.environ["DATAROOM_INDEX_URL"] = index_url   # dataroom_index extension target


def boot_index(job_dir: Path, index_port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["DATAROOM_DIR"] = str(job_dir / "dataroom")
    env["INDEX_PORT"] = str(index_port)
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
    """True when STATUS.md's first line declares completion (bare DONE or `STATUS: DONE`)."""
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
    """Paid Jina calls so far = `jina` CLI invocations in the agent's bash commands.

    A piped command (`jina search | jina rerank`) is two API calls, so we count each
    `jina ` occurrence, not each bash call."""
    n = 0
    if not log_path.exists():
        return n
    cap = 64 * 1024 * 1024            # bound work; logs are small (deltas filtered at write)
    size = log_path.stat().st_size
    with open(log_path, "rb") as f:
        if size > cap:
            f.seek(size - cap)
            f.readline()              # discard partial line
        for raw in f:
            if b'"tool_execution_start"' not in raw or b"bash" not in raw:
                continue
            try:
                ev = json.loads(raw.decode("utf-8", "ignore"))
            except Exception:
                continue
            if ev.get("type") == "tool_execution_start" and ev.get("toolName") == "bash":
                args = ev.get("args") or {}
                cmd = (args.get("command") or args.get("cmd") or "") if isinstance(args, dict) else ""
                n += len(re.findall(r"\bjina\s", cmd))
    return n


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
    "Build the dataroom under ./dataroom. Read state, pick the highest-value open question, "
    "research with the jina CLI (jina search / jina read; fan out many with xargs -P), dedup "
    "via dataroom_index before writing, enrich existing notes (read+edit) rather than only "
    "adding new ones, verify with code when it matters, and keep STATUS.md/OUTLINE.md current."
)
CONT_PROMPT = (
    "Continue building the dataroom. Read STATUS.md and the dataroom_index outline first, then "
    "advance the next highest-value open question. Dedup before writing; prefer enriching an "
    "existing note over creating a new one. Only write `STATUS: DONE` on the first line of "
    "STATUS.md once the coverage floor is met (>= {min_files} substantive sourced files, all "
    "open questions closed, reports/SUMMARY.md present)."
)
STALL_PROMPT = (
    "The last cycle added no new substantive sourced files. Do not repeat the same searches. "
    "Open NEW angles (jina search --arxiv, expand the query) on the most under-covered open "
    "question in STATUS.md, read primary sources, and write or enrich at least one sourced note. "
    "Dedup before writing."
)
CORRECTIVE_PROMPT = (
    "You wrote DONE but the dataroom is NOT comprehensive yet: {substantive_files}/{min_files} "
    "substantive sourced files, {open_questions} open question(s) still unchecked, "
    "SUMMARY.md present={summary_exists}. Remove DONE from STATUS.md, keep researching the open "
    "questions with new sources, and only write DONE again once the floor is actually met."
)
# Forced consolidation cycle (the mechanical edit/merge lever). Injected every N cycles so the
# agent actually read+edits existing notes instead of only ever writing new ones.
CONSOLIDATE_PROMPT = (
    "CONSOLIDATION pass - do NOT do new web research this cycle. Run dataroom_index "
    "({args:'{\"op\":\"outline\"}'}) and look for topics/ files that cover the same sub-topic or "
    "overlap heavily. For each overlapping set: `read` them, MERGE into one richer, well-structured "
    "file (keep every sourced claim and the union of their ## Sources), `edit` the survivor, and "
    "`rm` the redundant files (the index drops them on the next search). Fix cross-links and update "
    "OUTLINE.md. Also enrich any thin stub note you find. Goal: fewer, deeper, non-duplicated files."
)


def drive_rpc(job_dir: Path, agent_dir: Path, args, dataroom: Path,
              min_files: int, sat_window: int, min_new: int, consolidate_every: int):
    """Drive one persistent `pi --mode rpc` session. Returns (turns, stop_reason, floor)."""
    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    env["PI_SKIP_VERSION_CHECK"] = "1"
    pi_bin = os.environ.get("PI_BIN", "pi")
    cmd = [
        pi_bin, "--mode", "rpc",
        "--skill", str(REPO / "pi" / "skills" / "dataroom"),
        "--extension", str(REPO / "pi" / "extensions" / "dataroom-index.ts"),
    ]
    log = open(job_dir / "pi.log", "a")
    log.write(f"\n\n===== RPC SESSION @ {time.ctime()} =====\n")
    log.flush()
    log_path = job_dir / "pi.log"

    proc = subprocess.Popen(cmd, cwd=str(job_dir), env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            start_new_session=True)
    lock = threading.Lock()

    def send(obj):
        with lock:
            try:
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()
            except Exception:
                pass

    hard = {"reason": None}

    def hard_kill(reason):
        hard["reason"] = reason
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # Absolute wall-clock backstop: if the loop ever wedges, this guarantees termination
    # (kills the process -> stdout EOF -> we break out).
    global_wd = threading.Timer(args.max_seconds + 30, lambda: hard_kill("ceiling_seconds"))
    global_wd.start()

    start = time.time()
    turn = 0
    stop_reason = "error_pi_exited"
    recent_deltas = deque(maxlen=sat_window)
    prev = 0
    fm = floor_metrics(dataroom)
    cont = CONT_PROMPT.format(min_files=min_files)

    # Kick off the session. Pipe-buffers until pi finishes initializing, then it runs.
    send({"type": "prompt", "message": FIRST_PROMPT.format(query=args.query)})

    try:
        while True:
            # --- read one agent cycle (until agent_end), dropping per-token deltas ---
            # Per-cycle watchdog: if a single cycle exceeds turn_timeout, `abort` it (pi then
            # emits agent_end and we proceed). The global backstop covers a total hang.
            cycle_wd = threading.Timer(max(1, args.turn_timeout),
                                       lambda: send({"type": "abort"}))
            cycle_wd.start()
            ended = False
            try:
                while True:
                    line = proc.stdout.readline()
                    if line == "":
                        break                      # EOF -> pi exited / was killed
                    if '"type":"message_update"' in line:
                        continue                   # streaming token delta -> keep log small
                    log.write(line)
                    if '"type":"agent_end"' in line:
                        ended = True
                        break
            finally:
                cycle_wd.cancel()

            if not ended:
                stop_reason = hard["reason"] or "error_pi_exited"
                break

            turn += 1
            log.flush()

            # Cooperative pause/cancel: the API writes job_dir/control at the user's request;
            # we honor it at the cycle boundary (breaks the loop -> finally reaps pi, main reaps
            # the index sidecar + zips). A SIGTERM (prompt cancel) routes through the same finally.
            cf = job_dir / "control"
            if cf.exists():
                ctl = cf.read_text(errors="ignore").strip()
                if ctl == "cancel":
                    stop_reason = "cancelled"; break
                if ctl == "pause":
                    stop_reason = "paused"; break

            # --- evaluate floor / budget (identical policy to the old per-turn loop) ---
            elapsed = time.time() - start
            fm = floor_metrics(dataroom)
            sub = fm["substantive_files"]
            recent_deltas.append(sub - prev)
            prev = sub
            jina_calls = count_jina_calls(log_path)
            print(f"[orchestrator] cycle {turn} files={sub}/{min_files} jina={jina_calls}")

            if elapsed > args.max_seconds:
                stop_reason = "ceiling_seconds"; break
            if turn >= args.max_turns:
                stop_reason = "ceiling_turns"; break
            if jina_calls > args.max_jina_calls:
                stop_reason = "ceiling_cost"; break

            # H5: DONE only honored once the floor is met; otherwise reject + correct.
            if status_done(dataroom):
                if fm["floor_met"]:
                    stop_reason = "done_floor_met"; break
                print(f"[orchestrator] DONE rejected, floor unmet: {fm}")
                send({"type": "prompt", "message": CORRECTIVE_PROMPT.format(
                    min_files=min_files,
                    **{k: fm[k] for k in ("substantive_files", "open_questions", "summary_exists")})})
                continue

            # Diminishing-returns early stop, only once the floor is satisfied.
            saturated = (len(recent_deltas) == sat_window and
                         all(d < min_new for d in recent_deltas))
            if saturated and fm["floor_met"]:
                stop_reason = "done_saturated"; break

            # Re-engage the idle agent for the next cycle. NB: we use `prompt` (not `follow_up`)
            # because a follow_up sent to an already-idle rpc session is not delivered as a new
            # run; `prompt` is valid here since agent_end means the agent is no longer streaming.
            # Every Nth cycle force a consolidation pass (read+merge) instead of new research, so
            # the agent enriches existing notes rather than only ever adding new files.
            if consolidate_every and turn % consolidate_every == 0:
                msg = CONSOLIDATE_PROMPT
            elif recent_deltas and recent_deltas[-1] <= 0:
                msg = STALL_PROMPT
            else:
                msg = cont
            send({"type": "prompt", "message": msg})
    except KeyboardInterrupt:
        stop_reason = "interrupted"
    finally:
        global_wd.cancel()
        send({"type": "abort"})
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except Exception:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            log.flush()
        except Exception:
            pass

    return turn, stop_reason, fm


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

    if args.max_turns < 1:
        print("ERROR: --max-turns must be >= 1", file=sys.stderr); sys.exit(2)
    if args.max_seconds < 1:
        print("ERROR: --max-seconds must be >= 1", file=sys.stderr); sys.exit(2)

    # SIGTERM (sent by the API for a prompt cancel) -> KeyboardInterrupt so the finally blocks
    # run: pi is reaped in drive_rpc, the GPU-holding index sidecar is reaped in main below.
    signal.signal(signal.SIGTERM, lambda *_a: (_ for _ in ()).throw(KeyboardInterrupt()))

    min_files = int(os.environ.get("MIN_FILES", "100"))
    sat_window = int(os.environ.get("SATURATION_WINDOW", "3"))
    min_new = int(os.environ.get("MIN_NEW_FILES_PER_TURN", "2"))
    consolidate_every = int(os.environ.get("CONSOLIDATE_EVERY", "4"))   # 0 = off

    llama_url = os.environ.get("LLAMA_URL", "http://localhost:8080")
    jina_key = os.environ.get("JINA_API_KEY", "")
    if not jina_key:
        print("ERROR: JINA_API_KEY required", file=sys.stderr); sys.exit(2)

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

    start = time.time()
    turn, stop_reason, fm = 0, "error_pi_exited", floor_metrics(dataroom)
    try:
        turn, stop_reason, fm = drive_rpc(job_dir, agent_dir, args, dataroom,
                                          min_files, sat_window, min_new, consolidate_every)
    except KeyboardInterrupt:
        stop_reason = "interrupted"
    finally:
        # Reap the index sidecar deterministically (SIGTERM, wait, then SIGKILL).
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
                   jina_calls=count_jina_calls(job_dir / "pi.log"),
                   elapsed_seconds=round(time.time() - start, 1))
    print(f"[orchestrator] wrote {out_zip} (stop_reason={stop_reason})")
    print(json.dumps({"zip": str(out_zip), "turns": turn, "done": done,
                      "stop_reason": stop_reason, "floor": fm}))


if __name__ == "__main__":
    main()
