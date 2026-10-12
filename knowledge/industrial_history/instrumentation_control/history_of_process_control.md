---
title: Development of Process Control and Instrumentation
domain: instrumentation_control
default_claim_type: historical_fact
---

## Mechanical regulators {claim: historical_fact}
Automatic control predates electronics. The float valve, the temperature regulator of
Cornelis Drebbel in the seventeenth century, and James Watt's centrifugal flyball
governor applied to steam engines from 1788 are all closed-loop controllers. Maxwell's
1868 paper On Governors began the mathematical theory of control.

## Industrial measurement {claim: historical_fact}
Eugene Bourdon patented the Bourdon tube pressure gauge in 1849, still the most common
mechanical pressure element in plants. Thermocouples, resistance thermometers, orifice
plates and differential pressure cells made the standard process variables measurable
and transmittable.

## Pneumatic control {claim: historical_fact}
From the 1930s, pneumatic transmitters and controllers with a 3-15 psi signal range
allowed measurement and control to be moved off the equipment and into a central control
room. Ziegler and Nichols published their controller tuning rules in 1942.

## Electronic transmission {claim: historical_fact}
The 4-20 mA current loop became the dominant analogue standard from the 1950s and
1960s. Using a live zero of 4 mA meant a broken wire read as an obvious fault rather
than as a valid zero measurement - an early example of designing for failure detection.

## Programmable logic controllers {claim: historical_fact}
The Modicon 084, developed by Dick Morley and colleagues at Bedford Associates in 1969
for General Motors, replaced banks of relays with a programmable digital controller.
Ladder logic was chosen deliberately so that existing electricians could read it.

## Distributed control systems {claim: historical_fact}
Honeywell's TDC 2000 and Yokogawa's CENTUM, both introduced in 1975, distributed
control among microprocessor-based controllers linked by a data highway to shared
operator consoles. This is the architecture still used across process industries.

## Digital field communication {claim: historical_fact}
The HART protocol, developed by Rosemount in the late 1980s, superimposed digital data
on the 4-20 mA signal, allowing configuration and diagnostics without new wiring.
Foundation Fieldbus and Profibus PA followed in the 1990s as fully digital buses.

## Advanced process control {claim: historical_fact}
Model predictive control emerged from industrial practice in the late 1970s - Dynamic
Matrix Control at Shell and IDCOM in France - and became standard on refinery units
because it handles constraints and interactions that single loops cannot.

## Safety instrumented systems {claim: historical_fact}
IEC 61508 (published from 1998) and its process sector application IEC 61511 (2003,
second edition 2016) formalised safety integrity levels, the safety lifecycle and proof
testing, separating protection functions from regulatory control.

## Control loop fundamentals {claim: engineering_principle}
A loop is only as good as its measurement and its final element. Most poorly performing
loops are limited by a sticky control valve, a badly located transmitter, or an
inappropriate control structure, not by controller tuning.

## Alarm management {claim: modern_best_practice}
After incidents in which operators were overwhelmed by alarm floods, EEMUA 191 and
later IEC 62682 established that every alarm must be unique, require operator action,
and arrive with time to act. Rationalising an over-alarmed console is one of the
highest-value low-capital improvements available in an old plant.

## Cyber security of control systems {claim: modern_best_practice}
IEC 62443 defines zones, conduits and security levels for industrial automation.
Sovereign, on-premise operation - no external connectivity from the control domain -
remains the strongest practical control for plants that do not need remote access.
