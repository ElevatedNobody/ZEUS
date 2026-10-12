---
title: Industrial AI, Digital Twins, Robotics and Additive Manufacturing
domain: automation_digitalization
default_claim_type: historical_fact
---

## Early industrial computing and expert systems {claim: historical_fact}
Rule-based expert systems for diagnosis and control advice were tried in process
industries in the 1980s. Most failed to survive because the rules were expensive to
maintain as plants changed - a lesson that applies equally to modern models.

## Machine learning for condition monitoring {claim: historical_fact}
From the 2010s, statistical and machine learning models trained on historian data were
applied to anomaly detection on rotating equipment and process units. The successful
applications generally combine a data-driven detector with a physical explanation of the
signal it responds to.

## Digital twins {claim: historical_fact}
The digital twin concept was articulated by Michael Grieves around 2002-2003 in product
lifecycle management and was developed further by NASA for spacecraft. In process
industries it usually means a calibrated simulation kept synchronised with plant data
rather than a full physics replica.

## Industrial robotics {claim: historical_fact}
The first industrial robot, Unimate, was installed at a General Motors plant in 1961.
Robotics spread through discrete manufacturing, and reached process plants later and
mainly for inspection: tank crawlers, pipe crawlers, and remotely operated vehicles for
subsea work.

## Autonomous inspection {claim: historical_fact}
Unmanned aerial vehicles for flare tip, stack and structure inspection became routine
during the 2010s, removing scaffolding and rope-access exposure for visual survey.
Crawlers with ultrasonic probes extended this to wall thickness measurement on tanks and
vessels without erecting access.

## Additive manufacturing {claim: historical_fact}
Stereolithography was patented by Chuck Hull in the mid 1980s, followed by selective
laser sintering and fused deposition. Industrial metal additive manufacturing reached
production use in the 2010s; GE's additively manufactured LEAP engine fuel nozzle,
entering production around 2015, is the standard reference case.

## Additive manufacturing in maintenance {claim: modern_best_practice}
For plants, the practical use of additive manufacturing is obsolete-part replacement -
impellers, wear rings, small castings for equipment whose vendor no longer supports it -
subject to qualification of material properties, which remains the main obstacle.

## What AI can and cannot do in a plant {claim: engineering_principle}
Models are good at detecting deviation from a learned normal pattern and at retrieving
relevant information from large document sets. They are poor at extrapolating outside
their training conditions, which is exactly where safety-critical decisions live. This is
why an industrial assistant must ground its answers in retrieved documents and expose
its evidence rather than assert conclusions.

## Trust and traceability {claim: engineering_principle}
An industrial user cannot act on an unattributed statement. Any automated finding must
carry the document, page, revision and measurement it came from, and must separate what
was read from what was inferred. A system that cannot show its evidence cannot be used in
an approval workflow.

## Sovereign on-premise AI {claim: modern_best_practice}
Running open-weight models locally keeps confidential engineering documents inside the
plant boundary, removes dependence on external service availability, and makes the
system auditable. The trade-off is model size against available hardware, which is
managed by routing each task to the smallest model capable of it.
