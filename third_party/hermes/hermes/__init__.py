"""cognis-hermes: a model-agnostic, portable long-term memory framework for AI agents.

The public surface is intentionally tiny:

    from hermes import MemoryStore

    mem = MemoryStore("agent.sqlite")
    mem.remember("the sky is blue", tags=["fact"])
    hits = mem.recall("what color is the sky")

Everything is backed by a single SQLite file and a self-contained TF-IDF + cosine
similarity ranker. No external services, no embedding APIs, standard library only.

This is a clean-room reimplementation of the broadly-known "Hermes" agent-memory
pattern (model-agnostic persistent store with lexical recall). It uses classic,
long-established information-retrieval techniques (TF-IDF, cosine similarity).
"""

from .memory import Memory, MemoryStore, RecallHit, tokenize

__all__ = ["Memory", "MemoryStore", "RecallHit", "tokenize"]

__version__ = "0.1.0"
