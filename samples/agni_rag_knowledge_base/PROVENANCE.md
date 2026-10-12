# Provenance - ZEUS RAG knowledge base (SAMPLE DATA)

Supplied by the owner on 2026-09-27 as `AGNI_RAG_KNOWLEDGE_BASE.zip`
(sha256 bde5f85f331aeaff808130f6eb6577a2ca53fd6015e3a910abdeac3764258e26).

**Its own README states the content is FICTIONAL** - "All names, GSTINs, amounts,
and registrations are fake and for RAG testing only." It is therefore ingested
with origin `sample`:

* every answer built on it is labelled SAMPLE - it is never presented as this
  company's records or history;
* it does not count as company coverage in the 1989-2026 report;
* it ranks below company documents of the same kind.

What is here: `data/` - the five registers (Excel) and eleven documents
(Markdown), ingested with:

    python -m agni_ops rag ingest-dir samples/agni_rag_knowledge_base/data --origin sample

Not copied: `raw_text/` (byte-for-byte duplicates of `word_md/`),
`metadata/chunks.jsonl` (naive fixed-size chunks - the knowledge fabric
builds its own hierarchy), `ingest_to_text.py` (a script, not data; not run).

To remove it again: `python -m agni_ops rag remove <document id>` for each
document (listed by `python -m agni_ops rag status`), or delete this folder
and run RAG_REINDEX.bat.

Files:
* data/excel/01_Lease_Register_Master.xlsx  sha256 ae4eb03972e5a9d6
* data/excel/02_Property_Asset_Master.xlsx  sha256 bf240df991b1ab6d
* data/excel/03_Employee_HR_Master.xlsx  sha256 658888b349743064
* data/excel/04_Tax_Compliance_Master.xlsx  sha256 17db183c147e7a74
* data/excel/05_Vendor_Contracts_Register.xlsx  sha256 eaec03da93aba411
* data/word_md/ehs_safety_brief.md  sha256 fe27158981eb272f
* data/word_md/employee_handbook_extract.md  sha256 9fd4e381e9193ef2
* data/word_md/gst_sop.md  sha256 98a984829ed23891
* data/word_md/lease_agreement_template.md  sha256 bee36d17fcbf181e
* data/word_md/posh_policy_summary.md  sha256 1f88544eb55eec91
* data/word_md/property_title_summary.md  sha256 13a8d11f7a5c5c3e
* data/word_md/tds_sop.md  sha256 03bc8698fc5b5ba1
* data/word_md/vendor_msa_clauses.md  sha256 3a69fefbfa5f0556
* data/pptx_md/HR_Policy_Map.md  sha256 155279c13bba8ad9
* data/pptx_md/Lease_Portfolio_Review.md  sha256 bc0d6de559998ba1
* data/pptx_md/Tax_Compliance_Dashboard.md  sha256 eae4689f652c93b1
