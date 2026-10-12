---
title: Industry 4.0, IIoT and Industrial Digitalisation
domain: automation_digitalization
default_claim_type: historical_fact
---

## Origin of the term {claim: historical_fact}
"Industrie 4.0" was introduced as a German national strategic initiative and publicised
at the Hannover Messe in 2011. General Electric popularised the parallel term "Industrial
Internet" from 2012. Both describe the same shift: connecting physical production assets
to analytics through pervasive sensing and networking.

## Enabling technologies {claim: historical_fact}
The practical enablers were cheap MEMS sensors, wireless field networks, low-cost
storage and compute, and lightweight protocols. MQTT, created in 1999 by Andy
Stanford-Clark and Arlen Nipper for constrained telemetry links, and OPC UA, released in
2008 as a platform-independent industrial interoperability standard, became the common
transport and semantic layers.

## Wireless in the plant {claim: historical_fact}
WirelessHART and ISA100.11a, standardised in the late 2000s, allowed instrumentation to
be added without cable routing, which changed the economics of monitoring non-critical
equipment that had never justified wiring.

## Edge and on-premise computing {claim: historical_fact}
Concern about latency, bandwidth, and above all confidentiality of operating data drove
processing back toward the plant. Edge and on-premise architectures now carry much of
the analytics that early industrial IoT marketing had assumed would run in a cloud.

## What actually generates value {claim: engineering_principle}
IIoT value comes from three places: detecting degradation earlier, closing a control or
optimisation loop that was previously manual, and removing manual data collection from
people. A deployment that does none of these produces dashboards and no benefit.

## Data quality is the limiting factor {claim: engineering_principle}
Analytics on plant data are usually limited by tag naming inconsistency, unrecorded
instrument recalibration, missing equipment hierarchy and unlabelled failure history -
not by algorithm choice. Preparing the asset register and failure coding usually
contributes more than the model.

## Confidentiality and sovereignty {claim: engineering_principle}
Operating data - yields, run lengths, failures, incidents - is commercially sensitive and
in many jurisdictions regulated. On-premise processing that never transmits the data
removes an entire class of exposure and is often the only architecture acceptable for
defence, nuclear and critical infrastructure operators.

## OT and IT convergence risk {claim: engineering_principle}
Connecting control networks to business networks creates a path that did not previously
exist. Segmentation, one-way data diodes for historian replication, and strictly
controlled remote access are the standard mitigations; an air-gapped or one-way
architecture remains the strongest.

## Refinery and petrochemical digitalisation {claim: modern_best_practice}
Typical current applications are: real-time yield accounting, energy monitoring per unit,
fouling and corrosion rate tracking from process data, machinery condition monitoring
with automatic alerting, mobile operator rounds with digital forms, and document
intelligence over technical archives.

## Realistic assessment {claim: modern_best_practice}
Sustained benefits are concentrated in unglamorous applications - reliable data capture,
early fault detection on rotating machinery, and getting engineering knowledge out of
paper archives - rather than in fully autonomous operation, which remains rare in
hazardous continuous process plants.
