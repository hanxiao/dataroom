#!/usr/bin/env python3
"""Dataroom index sidecar: jina-embeddings-v5-nano (CPU) backing the `dataroom_index` Pi tool.

Per-job, file-backed vector index (numpy). Keeps the dataroom de-duplicated and structured.
Runs CPU-only so it never competes with the LLM for L4 VRAM (CUDA_VISIBLE_DEVICES="").

Endpoints (POST JSON):
  /search  {query, k=5}            -> top-k existing chunks w/ cosine + dup flag
  /add     {path, text}            -> embed + persist a note (chunked)
  /stats   {}                      -> {count, files}
  /outline {}                      -> dataroom tree + STATUS.md head
"""
import os, json, glob, hashlib
import numpy as np
from fastapi import FastAPI, Request
import uvicorn

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ["CUDA_VISIBLE_DEVICES"] = ""  # CPU-only embedder; keep VRAM for the LLM

DATAROOM = os.environ.get("DATAROOM_DIR", "dataroom")
INDEX_PATH = os.path.join(DATAROOM, ".index.npz")
META_PATH = os.path.join(DATAROOM, ".index.jsonl")
MODEL_NAME = os.environ.get("EMBED_MODEL", "jinaai/jina-embeddings-v5-text-nano")
DUP_THRESHOLD = float(os.environ.get("DUP_THRESHOLD", "0.93"))

app = FastAPI()
_model = None
_embs: np.ndarray | None = None   # (N, D) float32, L2-normalized
_meta: list[dict] = []            # [{path, chunk, hash, text}]


def model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME, device="cpu", trust_remote_code=True)
    return _model


# v5-nano task adapters: retrieval | text-matching | clustering | classification.
# Retrieval adapter is used for both queries and passages.
EMBED_TASK = os.environ.get("EMBED_TASK", "retrieval")


def embed(texts: list[str]) -> np.ndarray:
    e = model().encode(texts, task=EMBED_TASK, normalize_embeddings=True)
    return np.asarray(e, dtype=np.float32)


def load():
    global _embs, _meta
    if _embs is not None:
        return
    if os.path.exists(INDEX_PATH) and os.path.exists(META_PATH):
        _embs = np.load(INDEX_PATH)["embs"]
        _meta = [json.loads(l) for l in open(META_PATH) if l.strip()]
    else:
        _embs = np.zeros((0, 0), dtype=np.float32)
        _meta = []


def persist():
    os.makedirs(DATAROOM, exist_ok=True)
    np.savez_compressed(INDEX_PATH, embs=_embs if _embs is not None else np.zeros((0, 0)))
    with open(META_PATH, "w") as f:
        for m in _meta:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")


def chunk(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        i += size - overlap
    return out


@app.post("/search")
async def search(req: Request):
    load()
    body = await req.json()
    q = body.get("query", "")
    k = int(body.get("k", 5))
    if _embs is None or _embs.shape[0] == 0 or not q:
        return {"results": [], "duplicate": False, "count": 0}
    qe = embed([q])[0]
    sims = _embs @ qe
    order = np.argsort(-sims)[:k]
    results = [{
        "path": _meta[i]["path"],
        "chunk": _meta[i]["chunk"],
        "score": round(float(sims[i]), 4),
        "preview": _meta[i]["text"][:240],
    } for i in order]
    dup = bool(results and results[0]["score"] >= DUP_THRESHOLD)
    return {"results": results, "duplicate": dup, "count": int(_embs.shape[0])}


@app.post("/add")
async def add(req: Request):
    global _embs, _meta
    load()
    body = await req.json()
    path = body.get("path", "")
    text = body.get("text", "")
    chunks = chunk(text)
    if not chunks:
        return {"added": 0, "count": int(_embs.shape[0]) if _embs.size else 0}
    # drop existing chunks for this path (re-index on update)
    keep = [j for j, m in enumerate(_meta) if m["path"] != path]
    if _embs is not None and _embs.shape[0]:
        _embs = _embs[keep] if keep else np.zeros((0, _embs.shape[1]), dtype=np.float32)
    _meta = [_meta[j] for j in keep]
    new = embed(chunks)
    if _embs is None or _embs.shape[0] == 0:
        _embs = new
    else:
        _embs = np.vstack([_embs, new])
    for ci, c in enumerate(chunks):
        _meta.append({"path": path, "chunk": ci,
                      "hash": hashlib.sha1(c.encode()).hexdigest()[:12], "text": c})
    persist()
    return {"added": len(chunks), "count": int(_embs.shape[0])}


@app.post("/stats")
async def stats(_: Request):
    load()
    files = sorted({m["path"] for m in _meta})
    return {"count": int(_embs.shape[0]) if _embs is not None and _embs.size else 0,
            "files": files}


@app.post("/outline")
async def outline(_: Request):
    tree = []
    for p in sorted(glob.glob(os.path.join(DATAROOM, "**", "*"), recursive=True)):
        if os.path.isfile(p) and not os.path.basename(p).startswith(".index"):
            tree.append(os.path.relpath(p, DATAROOM))
    status = ""
    sp = os.path.join(DATAROOM, "STATUS.md")
    if os.path.exists(sp):
        status = open(sp).read()[:4000]
    return {"tree": tree, "status": status}


if __name__ == "__main__":
    os.makedirs(DATAROOM, exist_ok=True)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("INDEX_PORT", "8077")))
