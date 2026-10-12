# Knowledge-fabric test fixture corpus

SYNTHETIC TEST FIXTURE - NOT REAL DATA.

"Test Fixture Refinery Ltd" does not exist. Every figure, name and event in
`corpus/` was written by hand to exercise the ZEUS knowledge fabric: temporal
questions, table facts, entity aliases (Unit 5 / U5 / Hydrogen Generation Unit),
causal statements, a conflicting figure between two reports, a confidential
document, and a document carrying a prompt-injection attempt.

The tests and the RAG evaluation ingest it into a TEMPORARY database with the
origin `test_fixture`. It is never ingested into the real knowledge base and
never counted as company history.
