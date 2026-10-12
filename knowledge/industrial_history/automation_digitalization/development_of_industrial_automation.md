---
title: Development of Industrial Automation
domain: automation_digitalization
default_claim_type: historical_fact
---

## Mechanical and pneumatic automation {claim: historical_fact}
Industrial automation began mechanically - governors, cam-driven sequences, relay
interlocks - and became a plant-wide capability with pneumatic transmission in the 1930s,
which allowed measurement and control to leave the equipment and gather in a control
room.

## Relay logic and its replacement {claim: historical_fact}
Discrete sequencing was implemented in banks of electromechanical relays until the
programmable logic controller arrived in 1969. The PLC replaced physical rewiring with
software, which is the change that made flexible automation possible.

## Distributed control {claim: historical_fact}
The introduction of the DCS in 1975 distributed control functions across microprocessor
controllers connected by a data highway, with operator interaction through shared
consoles. It replaced hundreds of panel instruments with screens and made large-scale
data collection possible for the first time.

## Historians and information systems {claim: historical_fact}
Process historians from the late 1980s stored high-resolution time series for years,
which turned process data from something read on a chart recorder into something
analysable. Manufacturing execution systems and ISA-95 integration linked plant floor to
business systems from the 1990s.

## Batch and sequence standards {claim: historical_fact}
ISA-88 standardised the description of batch processes into procedural and physical
models, allowing recipes to be portable and sequences to be engineered consistently
rather than written ad hoc for each plant.

## Advanced control and optimisation {claim: historical_fact}
Model predictive control moved from research to standard refinery practice through the
1980s and 1990s, sitting above the regulatory layer and pushing the unit against its
active constraints. Real-time optimisation added an economic layer above that on some
sites.

## The automation pyramid {claim: engineering_principle}
The classical hierarchy runs from field instruments, through regulatory control, to
supervisory and advanced control, then to production management and enterprise systems.
Safety instrumented systems sit deliberately outside this stack, independent of the
control layer, so that a control failure cannot disable protection.

## Independence of protection {claim: engineering_principle}
A protection layer must be independent of the layer it protects. Using the same
controller, the same transmitter or the same final element for both control and trip
removes that independence and is one of the most consequential design errors in
automation.

## Automation does not remove the operator {claim: engineering_principle}
Automation changes the operator's role from continuous manual control to monitoring and
exception handling - a task humans do less well. This is the core of the "ironies of
automation" argument, and it is why alarm design, display design and training for
abnormal situations matter more as automation increases.

## Modern practice {claim: modern_best_practice}
Current practice includes control loop performance monitoring, alarm management to
IEC 62682, safety lifecycle management under IEC 61511, and industrial cyber security
zoning under IEC 62443 with strict separation of the control domain.
