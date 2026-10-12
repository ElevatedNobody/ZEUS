#!/usr/bin/env python3
"""
Install the bundled ZEUS knowledge pack into the local RAG index.

    python scripts/install_knowledge_pack.py
    python scripts/install_knowledge_pack.py --check

Indexes knowledge/JARVIS_PACK/markdown/*.md into SQLite so that
ask knowledge base / search knowledge / rag write document / create document
can use Nexus Precision + ZEUS project data offline.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PACK = ROOT / "knowledge" / "JARVIS_PACK"
MD = PACK / "markdown"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if not MD.is_dir():
        print(f"[FAIL] Knowledge pack not found at {MD}")
        return 1

    files = sorted(MD.glob("*.md"))
    print(f"Knowledge pack: {len(files)} markdown files in {MD}")
    for f in files:
        print(f"  - {f.name} ({f.stat().st_size} bytes)")

    if args.check:
        return 0

    from memory.database import init_db, add_chunk, delete_chunks_by_source, get_all_chunks, list_indexed_sources
    from core.builtin_tools import _chunk_text

    init_db()

    # Optional embeddings
    embed_fn = None
    try:
        from core.ollama_client import ollama_client
        from core.capabilities import embedding_provider
        if ollama_client.is_available() and embedding_provider.resolve():
            embed_fn = embedding_provider.embed
            print("[OK] Embeddings available — semantic RAG enabled")
        else:
            print("[WARN] No embedding model — keyword RAG still works")
    except Exception as e:
        print(f"[WARN] Embeddings unavailable ({e}) — keyword RAG still works")

    chunks_total = 0
    files_ok = 0
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            continue
        source = str(path)
        delete_chunks_by_source(source)
        chunks = _chunk_text(text)
        for i, chunk in enumerate(chunks):
            emb = None
            if embed_fn:
                try:
                    emb = embed_fn(chunk)
                except Exception:
                    emb = None
            add_chunk(source, i, chunk, emb if emb is not None else [])
            chunks_total += 1
        files_ok += 1
        print(f"  indexed {path.name}: {len(chunks)} chunks")

    sources = list_indexed_sources()
    all_c = get_all_chunks()
    print()
    print(f"[OK] Indexed {files_ok} files, {chunks_total} chunks")
    print(f"     Total in DB: {len(sources or [])} sources, {len(all_c or [])} chunks")
    print()
    print("Try in ZEUS chat:")
    print("  ask knowledge base What is Nexus Precision?")
    print("  search knowledge base FY25 revenue")
    print("  rag write document about Nexus Precision company history")
    print("  Create a presentation about Nexus Precision deals")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
