# knowledge_sources/

Put the documents you want ZEUS to know about in this folder, then run:

    python scripts/ingest_industrial_knowledge.py

Supported: PDF (including scanned), DOCX, XLSX, PPTX, CSV, TXT/MD, PNG, JPG.
Optional (needs `unstructured`): HTML, EML, RTF, ODT, XML.

Subfolders are fine and are scanned recursively. Suggested layout:

    knowledge_sources/
      inspection_reports/
      maintenance_reports/
      sops/
      drawings/
      datasheets/
      vendor_manuals/
      incident_reports/
      shift_logs/

Everything ingested from here is tagged `user_document` and always outranks the
built-in industrial knowledge corpus when both are relevant. Nothing leaves this
machine: extraction, OCR, embedding and search all run locally.
