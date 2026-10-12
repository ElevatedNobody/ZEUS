# ZEUS Industrial RAG Knowledge Base

Fictional but realistic **leases, property, employee, tax, vendor** data for ZEUS RAG demos (SIH / industrial AI).

## Contents

### Excel
- `excel/01_Lease_Register_Master.xlsx` – 40 leases + rent schedule sample
- `excel/02_Property_Asset_Master.xlsx` – 25 properties + compliance checklist
- `excel/03_Employee_HR_Master.xlsx` – 60 employees + policy index
- `excel/04_Tax_Compliance_Master.xlsx` – GST, TDS, property tax, ITR index
- `excel/05_Vendor_Contracts_Register.xlsx` – 20 vendor contracts

### Word-style (Markdown → convert to DOCX if needed)
- Lease agreement template
- Property title summary
- Employee handbook extract
- GST & TDS SOPs
- POSH summary, EHS brief, Vendor MSA clauses

### PPT-style outlines (Markdown)
- Lease portfolio review
- Tax compliance dashboard
- HR policy map

### Metadata
- `metadata/rag_manifest.json` – collections for your vector DB
- Run `python ingest_to_text.py` → `metadata/chunks.jsonl` for embedding

## Wire into ZEUS RAG

1. Copy this folder into e.g. `ZEUS/knowledge_sources/industrial_rag/`
2. `pip install openpyxl`
3. `python ingest_to_text.py`
4. Embed each line of `chunks.jsonl` with your local embedding model (Ollama `nomic-embed-text` / `bge-m3`)
5. Store vectors in Chroma/LanceDB/FAISS with metadata: source, doc_type, collection

## Example questions for testing

- Which leases expire in the next 90 days?
- What is the security deposit for LS-2024-1001 style lease terms?
- Summarize GST filing responsibilities.
- What documents belong in an employee folder?
- Property tax unpaid items?
- POSH complaint handling summary?

## Disclaimer

All names, GSTINs, amounts, and registrations are **fake** and for **RAG testing only**. Not legal, tax, or HR advice.
