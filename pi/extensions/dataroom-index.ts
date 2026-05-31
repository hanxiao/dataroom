/**
 * dataroom-index: a single Pi tool `dataroom_index` backed by jina-embeddings-v5-nano.
 *
 * The agent calls this BEFORE writing anything into the dataroom, so the dataroom
 * stays de-duplicated and well-structured. The actual embedding/search work is done
 * by a tiny local sidecar (server/index_service.py) so the model stays on GPU and the
 * embedder stays on CPU. This extension is just a thin HTTP proxy exposed as one tool.
 *
 * Subcommands (args is a JSON string):
 *   { "op": "search", "query": "...", "k": 5 }          -> nearest existing notes (dedup check)
 *   { "op": "add",    "path": "dataroom/...md", "text": "..." }  -> index a new/updated note
 *   { "op": "stats" }                                   -> { count, sections }
 *   { "op": "outline" }                                 -> current dataroom tree + STATUS.md
 *
 * Env: DATAROOM_INDEX_URL (default http://127.0.0.1:8077)
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const BASE = process.env.DATAROOM_INDEX_URL || "http://127.0.0.1:8077";

async function call(op: string, body: Record<string, unknown>): Promise<string> {
  const res = await fetch(`${BASE}/${op}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`index service ${op} -> ${res.status}: ${text}`);
  return text;
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "dataroom_index",
    label: "Dataroom Index",
    description:
      "Semantic index over the dataroom (jina-embeddings-v5-nano). ALWAYS run op=search " +
      "before adding content to avoid duplicates and find where new material belongs. " +
      "search returns {results, duplicate, dup_threshold}: when duplicate==true the top hit " +
      "is a near-duplicate -- edit that file instead of creating a new one. The index " +
      "self-reconciles from disk, so op=add is an optional fast-path (files you write are " +
      "found on the next search anyway). ops: search{query,k}, add{path,text}, stats{}, " +
      "outline{}. `args` is a JSON string.",
    parameters: Type.Object({
      args: Type.String({
        description:
          'JSON, e.g. {"op":"search","query":"pricing of competitor X","k":5}',
      }),
    }),
    async execute(_id, params) {
      // Tolerate the common small-model failure modes: an already-parsed object, single
      // quotes, or a trailing comma. Only error after a repair attempt, and echo what we
      // received plus a known-good example so the model can self-correct in one step.
      const raw: any = (params as any).args;
      let p: any;
      if (raw && typeof raw === "object") {
        p = raw;
      } else {
        const s = String(raw ?? "");
        try {
          p = JSON.parse(s);
        } catch {
          try {
            p = JSON.parse(
              s.replace(/'/g, '"').replace(/,\s*([}\]])/g, "$1")
            );
          } catch {
            return {
              content: [{
                type: "text",
                text:
                  `args must be a JSON string. received: ${s.slice(0, 120)}\n` +
                  `example: {"op":"search","query":"competitor X pricing","k":5}`,
              }],
              isError: true,
              details: {},
            };
          }
        }
      }
      const op = String(p.op || "").toLowerCase();
      if (!["search", "add", "stats", "outline"].includes(op)) {
        return {
          content: [{ type: "text", text: `unknown op: ${op}` }],
          isError: true,
          details: {},
        };
      }
      try {
        const out = await call(op, p);
        return { content: [{ type: "text", text: out }], details: {} };
      } catch (e: any) {
        return {
          content: [{ type: "text", text: String(e?.message || e) }],
          isError: true,
          details: {},
        };
      }
    },
  });
}
