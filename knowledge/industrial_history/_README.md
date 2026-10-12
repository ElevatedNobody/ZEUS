# Industrial Knowledge Corpus

Local, offline industrial knowledge for ZEUS. This corpus is retrieved through RAG -
no base model was fine-tuned to hold it, and none should be.

## Why a corpus instead of fine-tuning
Fine-tuning bakes claims into weights where they cannot be cited, corrected or dated.
A retrieved corpus can be quoted with a file and section, edited by an engineer, and
kept clearly separate from the plant's own documents.

## Claim types
Every section declares what kind of statement it is. The tag travels with the chunk into
the knowledge base and appears on every piece of evidence:

| claim type            | meaning                                                     |
|-----------------------|-------------------------------------------------------------|
| historical_fact       | dated, verifiable industry event or development             |
| engineering_principle | general physical/engineering principle, not site-specific   |
| modern_best_practice  | current industry practice, not a mandatory requirement      |
| company_specific      | information specific to one company or plant                |
| user_document         | content from a document the user supplied (never in here)   |
| ai_inference          | model conclusion, not present in any source                 |

ZEUS must never present a statement from this corpus as if it came from a user's own
inspection report, drawing or procedure.

## File format
```
---
title: History of Petroleum Refining
domain: petroleum_refining
default_claim_type: historical_fact
---

## Thermal cracking {claim: historical_fact}
...text...

## Why cracking works {claim: engineering_principle}
...text...
```
Files beginning with `_` (like this one) are ignored by the ingester.

## Adding your own knowledge
Create a `.md` file in the right domain folder with the frontmatter above, then run:

    python scripts/ingest_industrial_knowledge.py --corpus-only

Company-specific material should use `default_claim_type: company_specific` so it is
never mistaken for general industry background.

## Domains
28 domains from the problem statement plus `industrial_manufacturing` and
`automation_digitalization`, which carry manufacturing history, industrial automation,
Industry 4.0, IIoT, industrial AI, digital twins, robotics and additive manufacturing.
