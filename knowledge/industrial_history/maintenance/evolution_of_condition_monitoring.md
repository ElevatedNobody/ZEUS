---
title: Evolution of Condition Monitoring
domain: maintenance
default_claim_type: historical_fact
---

## Human senses first {claim: historical_fact}
The original condition monitoring instruments were the maintenance fitter's screwdriver
against the bearing housing and hand on the casing. Experienced staff detected changes in
sound and temperature reliably, but the assessment could not be recorded, trended or
transferred.

## Vibration measurement {claim: historical_fact}
Broadband vibration meters appeared in the 1950s and 1960s, and portable FFT analysers in
the 1970s and 1980s made spectral diagnosis practical in the field. ISO standards for
evaluating machine vibration on non-rotating parts gave common acceptance zones.

## Oil analysis {claim: historical_fact}
Spectrometric oil analysis, developed for railway and military fleets, spread into
industry with wear-metal trending, particle counting and ferrography, detecting bearing
and gear wear that vibration finds only later.

## Infrared thermography {claim: historical_fact}
Thermal imaging became affordable for industrial use during the 1980s and 1990s and is
now standard for electrical connections, refractory and insulation survey, steam trap
checking and heat exchanger performance.

## Other techniques {claim: historical_fact}
Airborne and structure-borne ultrasound for leak and early bearing detection, motor
current signature analysis for rotor bar and load anomalies, acoustic emission for crack
growth, and performance monitoring against reference curves each cover a different
failure mode.

## Online monitoring {claim: historical_fact}
Critical machines moved from periodic route-based measurement to permanently installed
sensors feeding protection and monitoring systems, following API 670 for machinery
protection. Wireless sensors extended continuous monitoring to balance-of-plant assets
that could never justify cabling.

## Technique selection {claim: engineering_principle}
Choose the technique by the failure mode and its P-F interval, not by availability of an
instrument. Vibration finds unbalance, misalignment and bearing defects; oil analysis
finds wear and contamination earlier for slow-speed machines; thermography finds
electrical and insulation faults; performance monitoring finds internal wear and fouling
that none of the others detect.

## Baselines and trends {claim: engineering_principle}
A single reading means little. Condition monitoring works on trend against that machine's
own baseline, established after commissioning and re-established after every major
intervention. A repair without a new baseline destroys the trend it was meant to protect.

## False alarms are expensive {claim: engineering_principle}
An alerting system that cries wolf is switched off within months. Alarm thresholds must
be set from the machine's own statistics and reviewed after commissioning, and every
alert should be closed out with a recorded finding so the system's precision can be
measured.

## Modern practice {claim: modern_best_practice}
Current practice combines online monitoring on critical machines, route-based collection
on the rest, a single condition monitoring database linked to the CMMS equipment
hierarchy, and analyst review with documented recommendations that convert into work
orders rather than into reports nobody reads.
