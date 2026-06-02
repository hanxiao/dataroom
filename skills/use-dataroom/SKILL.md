---
name: use-dataroom
description: Commission deep, fully-cited background research from a hosted Dataroom-as-a-Service. Submit a query with a time budget (minutes), wait for the autonomous agent to finish, then download and unzip the resulting structured dataroom folder. Use when you need a comprehensive, sourced research corpus on a topic before a long-horizon task (implementation, analysis, a report).
---

# Use Dataroom-as-a-Service

Dataroom-as-a-Service (DaaS) runs an autonomous research agent that, given a query, crawls and
reads the web and assembles a structured, fully-cited **dataroom** on disk (topics, sources,
reports, a SUMMARY). You commission a job, wait, then download the dataroom as a `.zip` and
unzip it locally. Treat it like handing a research task to an intern for a fixed amount of time.

**Endpoint (default):** `https://dataroom.hanxiao.io` (override `BASE` for your own deployment).

## Budget: time, in minutes

You give the agent a **time box**: `max_seconds`. It works up to that long, then stops and hands
over whatever it has assembled. It may stop earlier if it judges the topic exhausted. So a
30-minute budget = "spend up to 30 minutes on this." Bigger budget = more thorough, capped at
**60 minutes** (a larger `max_seconds` is clamped to 3600). There is no separate file target to set.

## One-shot: submit, wait, download, unzip

```bash
BASE="https://dataroom.hanxiao.io"
QUERY="Competitive landscape of self-hosted small embedding models in 2026"
MINUTES=30                      # time box: agent works up to this long, then hands over

# 1. Submit -> job_id  (JSON-escape the query safely; set the time budget)
JOB=$(curl -s -X POST "$BASE/jobs" -H 'content-type: application/json' \
  -d "{\"query\": $(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$QUERY"), \"max_seconds\": $((MINUTES*60))}" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])')
echo "job=$JOB   live: $BASE/jobs/$JOB/dashboard"

# 2. Poll until the job reaches a terminal state (done | stopped | failed)
#    'done'    = it judged the dataroom comprehensive
#    'stopped' = it hit the time box and handed over what it had (still a full result)
while true; do
  S=$(curl -s "$BASE/jobs/$JOB" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status","?"))')
  echo "status=$S"
  case "$S" in done|stopped|failed) break;; esac
  sleep 30
done

# 3. Download the result zip and unzip into ./$JOB/  ->  ./$JOB/dataroom/{topics,sources,reports,...}
if [ "$S" = failed ]; then
  echo "job failed; see $BASE/jobs/$JOB/dashboard"
else
  curl -s -OJ "$BASE/jobs/$JOB/result"      # writes dataroom-$JOB.zip
  unzip -oq "dataroom-$JOB.zip" -d "$JOB"
  echo "dataroom is at: $JOB/dataroom"
  ls "$JOB/dataroom"
fi
```

Want a partial result without waiting for the job to finish? `GET /jobs/$JOB/snapshot` zips the
dataroom as it is *right now* (works mid-run):
```bash
curl -s -OJ "$BASE/jobs/$JOB/snapshot" && unzip -oq dataroom-$JOB-*.zip -d "$JOB-partial"
```

## What you get

The unzipped `dataroom/` folder is organized: `topics/` (the synthesized findings),
`sources/` (cleaned source material with URLs), `reports/SUMMARY.md` (the synthesis),
`OUTLINE.md`, `STATUS.md`. Start with `reports/SUMMARY.md` and `OUTLINE.md`.

## API reference

| Call | Purpose |
| --- | --- |
| `POST /jobs` `{query, max_seconds?, max_turns?}` | Submit. Returns `{job_id, status}`. `max_seconds` is the time box. |
| `GET /jobs/{id}` | Status: `queued` / `running` / `paused` / `done` / `stopped` / `failed`. |
| `GET /jobs/{id}/stats` | Live metrics (progress, tokens, tool calls, file tree). |
| `GET /jobs/{id}/result` | Final `dataroom.zip` (HTTP 409 until the job stops). |
| `GET /jobs/{id}/snapshot` | `dataroom-so-far.zip`, zipped on demand (works any time, even mid-run). |
| `GET /jobs/{id}/dashboard` | Human-watchable live dashboard (open in a browser). |
| `POST /jobs/{id}/pause` / `/resume` | Pause an unfinished job (the queue advances to the next); resume continues it. |

## Notes
- **Serial queue:** one job runs at a time, so a freshly submitted job may sit `queued` behind
  another before it starts running. The poll loop handles this.
- **`stopped` is a success, not an error:** it means the time box was reached and the agent
  handed over the dataroom it built. `failed` is the only real failure.
- **Public + unauthenticated:** the demo endpoint runs jobs on a shared budget/GPU. Be
  considerate with the time box, and prefer your own deployment for heavy use.
- **Dependencies:** the script needs `curl`, `python3`, and `unzip` on PATH.
