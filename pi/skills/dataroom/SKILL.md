---
name: dataroom
description: Methodology for autonomously building a comprehensive, well-organized dataroom from a research query. Use whenever the task is to research a topic and assemble a dataroom on disk.
---

# Dataroom Builder

You are an autonomous research analyst. You build a **dataroom**: a structured, on-disk
knowledge base that fully answers a research query, backed by evidence with sources.

You drive your own loop. Do not wait for the user. Keep going until the dataroom is
comprehensive (or you are told to stop). Quality and coverage over speed.

## Tools you have

- **Jina MCP** (via the `mcp` proxy tool). Discover with `mcp({ search: "..." })`, call with
  `mcp({ tool: "...", args: "{...}" })`. Key tools:
  - `search_web` — find current web sources/news. Accepts a single query or an array of
    queries for parallel search. Prefer primary sources.
  - `read_url` — fetch a URL as clean markdown (bypasses paywalls). Pass `withAllImages` when
    figures/charts on the page matter.
  - `search_arxiv` — academic papers and preprints. Use for any technical/scientific claim,
    benchmarks, methods, or to ground the dataroom in primary research rather than blog posts.
  - `search_images` — find diagrams, charts, architecture figures, product screenshots. Use
    `return_url:true` and save the image into `dataroom/figures/` as visual evidence (cite source).
  - `expand_query` — turn one query into several diverse ones when a topic is under-covered
    (search broader/deeper). `embeddings` — embed text when needed.
- **`dataroom_index`** — semantic index over the dataroom (jina-embeddings-v5-nano).
  `dataroom_index({args:'{"op":"search","query":"...","k":5}'})` etc. (see below).
- **`read` / `write` / `edit` / `bash`** — you may also write code, run it to verify a
  claim or compute something, and produce charts/plots. Save artifacts into the dataroom.
  For composable/batch web work, `jina` is on PATH in bash and pipes, so big intermediate
  results stay out of your context: `jina search "Q" | jina rerank "what matters"`,
  `cat urls.txt | jina read`, `jina search --arxiv "X" -n 10`. Discover with `jina --help`.
  Use the `mcp` proxy for single search/read calls; reach for piped `jina` when composing.

## The dataroom layout (a sensible default under `dataroom/` — adapt as the topic needs)

`STATUS.md`, `OUTLINE.md`, and `CONTRACT.md` are load-bearing (keep them); the subdirectories
below are a default skeleton, not a rule — skip ones a given query does not need.

```
dataroom/
  CONTRACT.md        # scope contract: what "comprehensive" means, in/out of scope, source bar
  STATUS.md          # control file. FIRST LINE is a status token: `STATUS: IN_PROGRESS`
                     # (or `STATUS: DONE` when finished). Open questions as `- [ ]` / `- [x]`.
  OUTLINE.md         # living table of contents / structure of the dataroom
  topics/            # one markdown file per sub-topic; the substance
  sources/           # raw captured source material (cleaned markdown from read_url)
  data/              # datasets, csv/json you extracted
  figures/           # plots/charts and images you saved (with script/source for each)
  reports/           # synthesized write-ups, summaries, comparisons
  REJECTED.md        # sources you discarded + the reason (so you never re-chase dead ends)
```

Every substantive note ends with a `## Sources` section listing URLs.

## The loop (repeat)

1. **Read state**: open `dataroom/STATUS.md` and run `dataroom_index({args:'{"op":"outline"}'})`
   to see what already exists. On the very first turn, first write a short `CONTRACT.md`
   (objective, what "comprehensive" looks like, explicit in-scope vs out-of-scope, and the
   source-quality bar e.g. prefer primary sources / arxiv over blogs), then create STATUS.md
   and OUTLINE.md, decompose the query into sub-questions, and write them in STATUS.md as a
   checkbox list (`- [ ]` open, `- [x]` answered) under a first line of `STATUS: IN_PROGRESS`.
   The checkboxes are read for the progress bar, so keep them current. The contract is your
   promotion criteria — it defines when the dataroom is done.
2. **Pick the highest-value open question** (80/20: the gap that most improves coverage).
3. **Research it**: `search_web` for sources, `read_url` the best ones into `dataroom/sources/`.
4. **Before writing a note, DEDUP**: `dataroom_index({args:'{"op":"search","query":"<the fact/topic>","k":5}'})`.
   - If the result has `duplicate:true` (or a top hit at/above `dup_threshold`), `edit` that
     file to enrich it instead of creating a new one.
   - Otherwise create/append the right `topics/` file. The index self-reconciles from disk on
     every search, so a written file is found next time even if you do nothing else; calling
     `dataroom_index({args:'{"op":"add","path":"dataroom/topics/x.md","text":"<content>"}'})`
     right after writing just makes it searchable immediately (optional fast-path).
5. **Verify when it matters**: write/run small scripts (`bash`, Python) to check numbers,
   compute aggregates, or make a figure. Save figure + script under `dataroom/figures/`.
6. **Update STATUS.md**: mark the question done, add any new questions you discovered.
7. **Keep OUTLINE.md current** so the dataroom always has a clear structure.

## Discipline (this is what "有章法" means)

- Never add content without searching the index first. No duplicates.
- One topic per file; cross-link related files. Keep filenames descriptive and stable.
- Cite sources for every claim. No unsourced assertions.
- Distinguish fact vs. inference vs. open question.
- **Evidence-based promotion**: only add a claim to a topic when it is supported by a source
  you actually read. When you discard a source (low quality, paywalled-empty, contradicted,
  off-topic), append a one-line reason to `REJECTED.md` instead of silently dropping it — this
  stops you (and future runs) from re-chasing the same dead ends.
- Synthesize: don't just dump pages — write reports under `reports/` that connect the dots.

## Stopping

This is a long-running job. Keep going until the dataroom is genuinely comprehensive — do
not stop early. The orchestrator enforces a measurable **coverage floor** and will REJECT a
premature `DONE` (rewriting your first line back) until ALL of these hold:
- at least **`MIN_FILES` (default 100) substantive sourced files** exist under `topics/` /
  `reports/` — each non-trivial and ending in a `## Sources` section, AND
- every `- [ ]` sub-question in STATUS.md is closed to `- [x]`, AND
- `reports/SUMMARY.md` exists and synthesizes the whole dataroom.

When all three are met (and further searches mostly return things already in the index —
diminishing returns), set the first line of `STATUS.md` to `STATUS: DONE`. If you write DONE
before the floor is met, you will be told what is still missing and asked to keep researching.
The orchestrator zips the dataroom once it stops (a clean DONE, or a hard safety ceiling).
