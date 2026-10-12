"""
agni_ops - the diagnostics, repair and verification backend behind the
operator BAT files (STEP 1 : INSTALL, STEP 2 : START, REPAIR.bat,
FIREWALL_AGNI.bat, TEST_AGNI.bat).

The BAT files are the interface; every check lives here exactly once, so the
installer, the start preflight, the repair tool and the test suite cannot
disagree about whether something works.

    python -m agni_ops test all            (what TEST_AGNI.bat runs)
    python -m agni_ops preflight           (what STEP 2 : START runs)
    python -m agni_ops repair              (what REPAIR.bat runs)
    python -m agni_ops firewall status     (what FIREWALL_AGNI.bat runs)

The rule every module here follows: PASS means the component's real function
was exercised and produced a validated result. A file that exists, a package
that imports, a model that is downloaded or a checksum that matches is never,
on its own, a PASS.
"""
