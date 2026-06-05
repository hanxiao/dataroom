# Dataroom expert for K-Dense BYOK (Kady)

Lets Kady delegate **long, fully-cited factual deep-dives** to a self-hosted Dataroom, via MCP.
Kady stays the orchestrator; Dataroom becomes a research expert it can commission, poll, and
collect from.

`dataroom_mcp.py` is a stdio MCP server wrapping the Dataroom job API (the same
submit -> poll -> download contract documented in `skills/use-dataroom/SKILL.md`).

## Prerequisites

- A Dataroom instance running locally (`bash scripts/mac-run.sh`) on **http://localhost:8000**.
  Point the MCP at your local instance, **not** the shared hosted demo.
- [`uv`](https://docs.astral.sh/uv/) on PATH (K-Dense already installs it). `uv run` fetches the
  `mcp` and `httpx` deps ephemerally, so nothing extra to install.

## Install

1. Open K-Dense -> **Settings (gear) -> MCP Servers**, or edit `projects/<project>/custom_mcps.json`.
2. Paste the entry from [`custom_mcps.json`](./custom_mcps.json), then:
   - replace `/ABSOLUTE/PATH/TO/...` with the real absolute path to `dataroom_mcp.py`;
   - set the base URL arg if your Dataroom isn't on `http://localhost:8000`.
3. Save. K-Dense merges it with the built-in MCP servers and passes it to the expert.

## Tools exposed

| Tool | Use |
| --- | --- |
| `commission_dataroom(query, minutes=30)` | Submit a research job, return immediately with a `job_id`. |
| `dataroom_status(job_id)` | Poll status / time-budget (`done` and `stopped` are both success). |
| `collect_dataroom(job_id, dest_dir=".", partial=False)` | Download + unzip; returns folder path, SUMMARY, OUTLINE. |
| `research_with_dataroom(query, minutes=30)` | One-shot: commission, wait, collect. Blocking. |

## Routing rule (add to Kady's instructions / project prompt)

> When a request calls for a **comprehensive, fully-cited research corpus** on a topic before a
> long-horizon task - a literature/background deep-dive, competitive landscape, or "gather
> everything known about X" - and a time box of minutes-to-tens-of-minutes is acceptable,
> delegate to the **dataroom** MCP: call `commission_dataroom` (or `research_with_dataroom` for a
> single blocking call), then `collect_dataroom`, and build on `reports/SUMMARY.md`. Prefer plain
> web search for quick, single-fact lookups; prefer dataroom when breadth, citations, and a
> reusable on-disk corpus matter.

The tool docstrings restate this so model-driven tool selection works even without prompt changes;
the rule above makes the delegation explicit.

## Note

The hosted demo (`https://dataroom.hanxiao.io`) runs on a shared budget/GPU. For Kady delegation,
run your own instance and point the MCP at `http://localhost:8000`.
