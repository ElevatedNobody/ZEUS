#!/usr/bin/env python3
r"""
ZEUS - Quick Password Reset & Admin ID Recovery (User Editable)
=======================================================================

Edit the variables below and run this file to immediately reset a forgotten
password or change your Admin ID, OR leave them blank ("") to launch the
interactive recovery menu in SET_PASSWORD.py.

Run with:
    ..\.venv\Scripts\python.exe "User Editable Files\RESET_PASSWORD.py"
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

# ============================================================================
# USER EDITABLE QUICK RESET SETTINGS
# ============================================================================

# Account username to reset (e.g. "admin"). Leave "" for interactive menu:
ACCOUNT_USERNAME: str = ""

# Optional: New Admin ID / username if you want to rename the account:
NEW_ACCOUNT_USERNAME: str = ""

# Optional: New password to set immediately:
NEW_PASSWORD: str = ""

# Ensure this account has Admin privileges:
ENSURE_ADMIN: bool = True

# ============================================================================

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if (HERE.parent / "core").is_dir() else HERE
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    if ACCOUNT_USERNAME.strip():
        import SET_PASSWORD as sp
        sp.TARGET_USERNAME = ACCOUNT_USERNAME
        sp.NEW_ADMIN_OR_USER_ID = NEW_ACCOUNT_USERNAME
        sp.NEW_PASSWORD = NEW_PASSWORD
        sp.MAKE_ADMIN = ENSURE_ADMIN
        sys.exit(sp.main())
    else:
        runpy.run_path(str(HERE / "SET_PASSWORD.py"), run_name="__main__")
