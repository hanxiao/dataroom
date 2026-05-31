#!/usr/bin/env python3
"""Orchestrator: runs the autonomous Pi harness loop for one dataroom job.

- Boots the per-job index sidecar (jina-embeddings-v5-nano, CPU).
- Writes per-job Pi config (models.json -> local Qwen, mcp.json -> Jina MCP).
- Loops `pi --mode json --continue` (same session) so Qwen keeps driving its own loop,
  using Jina MCP + dataroom_index + bash, until STATUS.md starts with DONE or budget hit.
- Zips the dataroom.

Most of the intelligence lives in the agent, not here: we expose tools + a one-page skill
and let Qwen run the harness. This file only supervises the loop and enforces budgets.
"""
import argparse, json, os, shutil, subprocess, sys, time, zipfile, signal, socket
from pathlib import Path

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
    # Jina MCP (search_web / read_url / embeddings) — hosted endpoint
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
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except Exception:
            try:
                # POST endpoints 405 on GET but server is up
                import urllib.error
                pass
            except Exception:
                pass
            # any connection (even 405) means it's up
            try:
                req = urllib.request.Request(url, method="POST", data=b"{}",
                                             headers={"content-type": "application/json"})
                urllib.request.urlopen(req, timeout=3)
                return True
            except urllib.error.HTTPError:
                return True
            except Exception:
                time.sleep(2)
    return False


def status_done(dataroom: Path) -> bool:
    sp = dataroom / "STATUS.md"
    if not sp.exists():
        return False
    head = sp.read_text(errors="ignore").lstrip()[:16].upper()
    return head.startswith("DONE")


def run_turn(job_dir: Path, agent_dir: Path, prompt: str, timeout: int) -> int:
    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)  # isolate config per job
    env["PI_SKIP_VERSION_CHECK"] = "1"
    pi_bin = os.environ.get("PI_BIN", "pi")
    cmd = [
        pi_bin, "--mode", "json", "--continue",
        "--skill", str(REPO / "pi" / "skills" / "dataroom"),
        "--extension", str(REPO / "pi" / "extensions" / "dataroom-index.ts"),
        prompt,
    ]
    log = open(job_dir / "pi.log", "a")
    log.write(f"\n\n===== TURN @ {time.ctime()} =====\n")
    log.flush()
    try:
        return subprocess.call(cmd, cwd=str(job_dir), env=env,
                               stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.write("\n[orchestrator] turn timed out\n")
        return 124


def zip_dataroom(dataroom: Path, out_zip: Path):
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in dataroom.rglob("*"):
            if p.is_file() and not p.name.startswith(".index"):
                z.write(p, p.relative_to(dataroom.parent))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True)
    ap.add_argument("--out", default="./out")
    ap.add_argument("--max-turns", type=int, default=int(os.environ.get("MAX_TURNS", "60")))
    ap.add_argument("--max-seconds", type=int, default=int(os.environ.get("MAX_SECONDS", "10800")))
    ap.add_argument("--turn-timeout", type=int, default=int(os.environ.get("TURN_TIMEOUT", "1200")))
    args = ap.parse_args()

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
        idx.terminate()
        sys.exit(3)

    first = (
        f"Research query: {args.query}\n\n"
        "Load and follow the `dataroom` skill. You are in autonomous dataroom-building mode. "
        "Build the dataroom under ./dataroom. Drive your own loop this turn: read state, pick "
        "the highest-value open question, research with Jina MCP (search_web/read_url), dedup via "
        "dataroom_index before writing, write sourced notes, verify with code when it matters, and "
        "keep STATUS.md/OUTLINE.md current. Do as much as you can before ending the turn."
    )
    cont = (
        "Continue building the dataroom. Read STATUS.md and dataroom_index outline first, then "
        "advance the next highest-value open question. Dedup before writing. When fully "
        "comprehensive and reports/SUMMARY.md exists, write DONE on the first line of STATUS.md."
    )

    start = time.time()
    try:
        for turn in range(1, args.max_turns + 1):
            if time.time() - start > args.max_seconds:
                print(f"[orchestrator] budget (max-seconds) reached at turn {turn}")
                break
            prompt = first if turn == 1 else cont
            print(f"[orchestrator] turn {turn}/{args.max_turns}")
            rc = run_turn(job_dir, agent_dir, prompt, args.turn_timeout)
            print(f"[orchestrator] turn {turn} rc={rc}")
            if status_done(dataroom):
                print("[orchestrator] STATUS.md=DONE, stopping")
                break
    finally:
        try:
            os.killpg(os.getpgid(idx.pid), signal.SIGTERM)
        except Exception:
            idx.terminate()

    out_zip = job_dir / "dataroom.zip"
    zip_dataroom(dataroom, out_zip)
    print(f"[orchestrator] wrote {out_zip}")
    print(json.dumps({"zip": str(out_zip), "turns": turn,
                      "done": status_done(dataroom)}))


if __name__ == "__main__":
    main()
