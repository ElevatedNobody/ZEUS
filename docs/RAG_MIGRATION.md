# RAG migration - from the original stores to the knowledge fabric

## Principle

Nothing was migrated destructively. The original RAG keeps working exactly as before:

| Existing | Still used by | Read by the new layer through |
|---|---|---|
| `document_chunks` (medha.db) and its index tools | `search_documents`, `index_*`, the chat RAG path | `adapters.original_store` |
| industrial knowledge base (DEMO refinery, taxonomy, reranker) | industrial tools and agents | `adapters.industrial_kb`, `industrial.reranker`, `industrial.taxonomy` |
| `industrial.meetings.transcribe`, `doc_pipeline.extract_document` | meeting / document tools | `ingest._extract` |

Passages from the older stores enter the fused ranking labelled with their source
(`legacy_industrial` / `legacy_chunks`), carry their classification, and - because they have no
dates - only count for a dated question when the year appears in their text.

## Moving a corpus into the fabric

The fabric adds what the old stores cannot hold: hierarchy, table facts, entities, relations,
dates, authority, origin, classification. Re-ingest the source files (the old stores keep their
copy):

```
RAG_INGEST.bat <folder> --origin company            real company documents
RAG_INGEST.bat <folder> --origin sample             sample / demo material
RAG_INGEST.bat <folder> --origin general_knowledge  background reading
RAG_REINDEX.bat                                     vectors, graph, integrity
```

Done on 2026-09-26: `knowledge/industrial_history` (34, general_knowledge) and
`knowledge/JARVIS_PACK/markdown` (16, sample). The `originals/` folder of the pack holds the same
documents as XLSX / PPTX and was not ingested twice.

## Rolling back

Delete `database/knowledge_fabric.db` (and `database/knowledge_fabric_archives/`): every original
feature keeps working, the company-intelligence tools report an empty knowledge base. Nothing in
`medha.db` depends on the fabric.

## Model changes

Changing the embedding model: `RAG_REINDEX.bat --model-change` recomputes all vectors; vectors of
different dimensions are never compared (they are reported by `rag validate`).
