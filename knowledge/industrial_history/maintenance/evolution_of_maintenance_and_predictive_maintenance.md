---
title: Evolution of Maintenance and Predictive Maintenance
domain: maintenance
default_claim_type: historical_fact
---

## Breakdown maintenance {claim: historical_fact}
Until the mid twentieth century most industrial maintenance was corrective: run the
machine until it failed, then repair it. This was tolerable when equipment was simple,
cheap and lightly loaded, and when a stoppage cost little.

## Time-based preventive maintenance {claim: historical_fact}
Preventive maintenance on fixed intervals spread after the Second World War, borrowing
from aviation and military practice. It reduced some failures but introduced two new
problems: unnecessary intervention on healthy equipment, and infant mortality
introduced by the intervention itself.

## The Nowlan and Heap study {claim: historical_fact}
The 1978 report by F. Stanley Nowlan and Howard Heap, prepared for the United States
Department of Defense from United Airlines reliability work, showed that only a small
minority of failure modes have an age-related wear-out pattern. Most exhibit random or
infant-mortality patterns, for which scheduled overhaul does nothing useful. This
finding is the intellectual basis of reliability centred maintenance and of
condition-based maintenance.

## Condition monitoring {claim: historical_fact}
Vibration analysis with portable data collectors and FFT spectrum analysis became
practical in the 1970s and 1980s. Oil analysis, infrared thermography, ultrasonic
airborne leak detection and motor current signature analysis followed, each detecting a
different degradation mechanism at a different point in the failure progression.

## Total productive maintenance {claim: historical_fact}
TPM was formalised in Japan by the Japan Institute of Plant Maintenance in the early
1970s, adding operator-performed basic care, autonomous maintenance, and Overall
Equipment Effectiveness as a combined measure of availability, performance and quality.

## Computerised maintenance management {claim: historical_fact}
CMMS and later enterprise asset management systems moved work orders, equipment
registers, spares and history from paper cards to databases from the 1980s. The value
was never the software: it was that equipment history became searchable, which is what
makes reliability analysis possible at all.

## Predictive and prescriptive maintenance {claim: historical_fact}
From the 2010s, cheap sensors, continuous online monitoring and machine learning on
historian data extended condition monitoring into failure prediction and remaining
useful life estimation. The reliable applications remain those with a clear physical
degradation signal; models without a physical basis tend to produce false alarms that
destroy operator trust.

## The P-F interval {claim: engineering_principle}
Between the point at which a failure becomes detectable (P) and the point of functional
failure (F) there is an interval. The monitoring technique must have a detection point
early enough, and an inspection frequency short enough relative to the P-F interval, to
give time to act. Choosing a technique without considering this interval is the most
common error in condition monitoring programmes.

## Maintenance strategy selection {claim: engineering_principle}
For each failure mode the choice is: predictive if there is a detectable degradation
signal; preventive if there is a genuine age-related wear-out; redesign if consequences
are unacceptable and neither works; and run-to-failure, deliberately and documented,
where consequences are low and repair is cheap.

## Backlog and planning {claim: engineering_principle}
Reactive plants are recognisable by their metrics: low schedule compliance, high
emergency work percentage, large overdue preventive maintenance backlog, and wrench
time far below planned hours. Improving reliability nearly always starts with planning
and scheduling discipline rather than new technology.

## Modern practice {claim: modern_best_practice}
Current maintenance practice combines criticality-ranked equipment registers, RCM or
streamlined FMEA for critical assets, condition-based tasks for machinery, risk-based
inspection for static equipment, planned turnaround work lists driven by inspection
findings, and defect elimination for repeat failures.
