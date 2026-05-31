#!/usr/bin/env python3
"""Dataroom index sidecar: jina-embeddings-v5-nano backing the `dataroom_index` Pi tool.

Per-job, file-backed vector index (numpy). Keeps the dataroom de-duplicated and structured.

Correctness does NOT depend on the agent remembering to call op=add: every /search and
/outline first RECONCILES the index against what is actually on disk (hash each note,
re-embed new/changed files, drop deleted ones). op=add remains as an optional fast-path.

jina-embeddings-v5 retrieval is ASYMMETRIC: queries and passages use different prompts.
We embed queries with the "query" prompt and stored notes with the "passage"/"document"
prompt so cosine scores (and DUP_THRESHOLD) are calibrated.

Endpoints (POST JSON):
  /search  {query, k=5}            -> top-k existing chunks w/ cosine + dup flag
  /add     {path, text}            -> embed + persist a note (chunked) [optional fast-path]
  /stats   {}                      -> {count, files}
  /outline {}                      -> dataroom tree + STATUS.md head + any unindexed files
"""
import os, json, glob, hashlib
import numpy as np
from fastapi import FastAPI, Request
import uvicorn

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# v5-nano is ~239M params (<1GB VRAM in fp16). cuda is the default for speed (shares the
# L4); set EMBED_DEVICE=cpu for zero VRAM contention if you raise the LLM ctx/parallel.
EMBED_DEVICE = os.environ.get("EMBED_DEVICE", "cuda")

DATAROOM = os.path.abspath(os.environ.get("DATAROOM_DIR", "dataroom"))
PARENT = os.path.dirname(DATAROOM)
INDEX_PATH = os.path.join(DATAROOM, ".index.npz")
META_PATH = os.path.join(DATAROOM, ".index.jsonl")
FILES_PATH = os.path.join(DATAROOM, ".index.files.json")   # per-file content hashes
MODEL_NAME = os.environ.get("EMBED_MODEL", "jinaai/jina-embeddings-v5-text-nano")
DUP_THRESHOLD = float(os.environ.get("DUP_THRESHOLD", "0.85"))
EMBED_TASK = os.environ.get("EMBED_TASK", "retrieval")

# Which on-disk files the index covers: synthesized notes only. NOT the STATUS/OUTLINE/
# CONTRACT/REJECTED control files (root *.md) and NOT raw sources/ dumps -- dedup is about
# the notes the agent writes, and control files would pollute the nearest-neighbour results.
INDEX_GLOBS = ("topics/**/*.md", "reports/**/*.md", "data/**/*.md")

app = FastAPI()
_model = None
_embs = None                      # (N, D) float32, L2-normalized
_meta = []                        # [{path, chunk, hash, text}]
_file_hash = {}                   # {canonical_path: sha1(full text)}


def model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        dev = EMBED_DEVICE
        if dev.startswith("cuda"):
            try:
                import torch
                if not torch.cuda.is_available():
                    print("[index] CUDA not available, falling back to CPU")
                    dev = "cpu"
            except Exception:
                dev = "cpu"
        print(f"[index] loading {MODEL_NAME} on {dev}")
        _model = SentenceTransformer(MODEL_NAME, device=dev, trust_remote_code=True)
    return _model


def _encode(texts, role: str) -> np.ndarray:
    """Encode with the retrieval adapter and the role-specific prompt (query vs passage).

    jina-v5 selects the prompt via `prompt_name`; older ST builds may not accept it, so we
    degrade gracefully to a plain retrieval encode rather than crash.
    """
    m = model()
    prompt_name = "query" if role == "query" else "passage"
    for kwargs in ({"task": EMBED_TASK, "prompt_name": prompt_name},
                   {"task": EMBED_TASK, "prompt_name": role},   # some builds name it 'document'
                   {"task": EMBED_TASK}):
        try:
            e = m.encode(texts, normalize_embeddings=True, **kwargs)
            return np.asarray(e, dtype=np.float32)
        except TypeError:
            continue
    e = m.encode(texts, normalize_embeddings=True)
    return np.asarray(e, dtype=np.float32)


def load():
    global _embs, _meta, _file_hash
    if _embs is not None:
        return
    if os.path.exists(INDEX_PATH) and os.path.exists(META_PATH):
        _embs = np.load(INDEX_PATH)["embs"]
        _meta = [json.loads(l) for l in open(META_PATH) if l.strip()]
    else:
        _embs = np.zeros((0, 0), dtype=np.float32)
        _meta = []
    if os.path.exists(FILES_PATH):
        try:
            _file_hash = json.load(open(FILES_PATH))
        except Exception:
            _file_hash = {}
    else:
        _file_hash = {}


def persist():
    os.makedirs(DATAROOM, exist_ok=True)
    np.savez_compressed(INDEX_PATH, embs=_embs if _embs is not None else np.zeros((0, 0)))
    with open(META_PATH, "w") as f:
        for m in _meta:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    with open(FILES_PATH, "w") as f:
        json.dump(_file_hash, f)


def chunk(text: str, size: int = 1200, overlap: int = 150) -> list:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        i += size - overlap
    return out


def _canon(path: str) -> str:
    """Canonical 'dataroom/...'-relative key, so /add and reconcile agree on identity."""
    p = path.replace("\\", "/")
    if os.path.isabs(p):
        ap = os.path.abspath(p)
    elif p.startswith("dataroom/") or p == "dataroom":
        ap = os.path.abspath(os.path.join(PARENT, p))
    else:
        ap = os.path.abspath(os.path.join(DATAROOM, p))
    rel = os.path.relpath(ap, PARENT)
    return rel.replace("\\", "/")


def _index_text(canon_path: str, text: str):
    """(Re)index a single note: drop its old chunks, embed the new ones as passages."""
    global _embs, _meta
    keep = [j for j, m in enumerate(_meta) if m["path"] != canon_path]
    if _embs is not None and _embs.shape[0]:
        _embs = _embs[keep] if keep else np.zeros((0, _embs.shape[1] if _embs.ndim == 2 else 0),
                                                  dtype=np.float32)
    _meta = [_meta[j] for j in keep]
    chunks = chunk(text)
    if not chunks:
        _file_hash[canon_path] = hashlib.sha1((text or "").encode()).hexdigest()
        return 0
    new = _encode(chunks, role="passage")
    _embs = new if (_embs is None or _embs.shape[0] == 0) else np.vstack([_embs, new])
    for ci, c in enumerate(chunks):
        _meta.append({"path": canon_path, "chunk": ci,
                      "hash": hashlib.sha1(c.encode()).hexdigest()[:12], "text": c})
    _file_hash[canon_path] = hashlib.sha1(text.encode()).hexdigest()
    return len(chunks)


def _disk_notes() -> dict:
    """All indexable notes currently on disk -> {canonical_path: abs_path}."""
    found = {}
    for g in INDEX_GLOBS:
        for ap in glob.glob(os.path.join(DATAROOM, g), recursive=True):
            if not os.path.isfile(ap) or os.path.basename(ap).startswith(".index"):
                continue
            found[_canon(ap)] = ap
    return found


def reconcile() -> dict:
    """Make the index match disk: embed new/changed notes, drop deleted ones.

    This is what makes op=add optional: a note written by edit/bash/write still becomes
    searchable on the next /search, so dedup never silently misses it.
    """
    global _embs, _meta
    load()
    disk = _disk_notes()
    changed = removed = 0
    for canon_path, ap in disk.items():
        try:
            text = open(ap, errors="ignore").read()
        except Exception:
            continue
        h = hashlib.sha1(text.encode()).hexdigest()
        if _file_hash.get(canon_path) != h:
            _index_text(canon_path, text)
            changed += 1
    # drop notes that no longer exist on disk
    gone = [p for p in list(_file_hash) if p not in disk]
    if gone:
        goneset = set(gone)
        keep = [j for j, m in enumerate(_meta) if m["path"] not in goneset]
        if _embs is not None and _embs.shape[0]:
            _embs = _embs[keep] if keep else np.zeros((0, _embs.shape[1] if _embs.ndim == 2 else 0),
                                                      dtype=np.float32)
        _meta = [_meta[j] for j in keep]
        for p in gone:
            _file_hash.pop(p, None)
        removed = len(gone)
    if changed or removed:
        persist()
    return {"reconciled_changed": changed, "reconciled_removed": removed}


@app.post("/search")
async def search(req: Request):
    reconcile()
    body = await req.json()
    q = body.get("query", "")
    k = int(body.get("k", 5))
    if _embs is None or _embs.shape[0] == 0 or not q:
        return {"results": [], "duplicate": False, "count": 0, "dup_threshold": DUP_THRESHOLD}
    qe = _encode([q], role="query")[0]
    sims = _embs @ qe
    order = np.argsort(-sims)[:k]
    results = [{
        "path": _meta[i]["path"],
        "chunk": _meta[i]["chunk"],
        "score": round(float(sims[i]), 4),
        "preview": _meta[i]["text"][:240],
    } for i in order]
    dup = bool(results and results[0]["score"] >= DUP_THRESHOLD)
    return {"results": results, "duplicate": dup, "dup_threshold": DUP_THRESHOLD,
            "count": int(_embs.shape[0])}


@app.post("/add")
async def add(req: Request):
    """Optional fast-path: index a note immediately. Reconcile would catch it anyway."""
    load()
    body = await req.json()
    path = _canon(body.get("path", ""))
    text = body.get("text", "")
    n = _index_text(path, text)
    persist()
    return {"added": n, "count": int(_embs.shape[0]) if _embs is not None and _embs.size else 0}


@app.post("/stats")
async def stats(_: Request):
    load()
    files = sorted({m["path"] for m in _meta})
    return {"count": int(_embs.shape[0]) if _embs is not None and _embs.size else 0,
            "files": files}


@app.post("/outline")
async def outline(_: Request):
    reconcile()
    tree = []
    for p in sorted(glob.glob(os.path.join(DATAROOM, "**", "*"), recursive=True)):
        if os.path.isfile(p) and not os.path.basename(p).startswith(".index"):
            tree.append(os.path.relpath(p, DATAROOM))
    status = ""
    sp = os.path.join(DATAROOM, "STATUS.md")
    if os.path.exists(sp):
        status = open(sp).read()[:4000]
    indexed = {m["path"] for m in _meta}
    unindexed = [c for c in _disk_notes() if c not in indexed]
    return {"tree": tree, "status": status, "indexed_files": len(indexed),
            "unindexed": unindexed}


if __name__ == "__main__":
    os.makedirs(DATAROOM, exist_ok=True)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("INDEX_PORT", "8077")))
