#!/usr/bin/env python3
r"""
ZEUS - User Editable Account, Admin ID & Password Manager
==================================================================

You can use this file in TWO ways:

METHOD A - EDIT THIS FILE DIRECTLY:
    Fill in the USER EDITABLE SETTINGS below, save the file, and run:
        ..\.venv\Scripts\python.exe "User Editable Files\SET_PASSWORD.py"

METHOD B - INTERACTIVE MENU:
    Leave the settings below blank ("") and run this file to open an
    interactive menu where you can:
      1) Set / Reset a forgotten password for any account (clears lockout)
      2) Change / Rename an existing Admin ID (username) & optionally its password
      3) Create a new Admin or Standard account
      4) Promote an existing account to Admin (or change its role)
      5) Turn off Face Login for an account (if camera is unavailable)
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

# ============================================================================
# USER EDITABLE SETTINGS (Edit these values to apply changes automatically!)
# ============================================================================

# 1. Target account username (e.g. "admin").
#    Leave "" to use the interactive menu instead.
TARGET_USERNAME: str = ""

# 2. Optional: Rename the account (change Admin ID / username) to this new name.
#    Leave "" to keep the current username.
NEW_ADMIN_OR_USER_ID: str = ""

# 3. Optional: Set or reset the password for the account to this new password.
#    (Must be at least 8 characters with letters and numbers/symbols, or set
#     BYPASS_COMPLEXITY_CHECK = True below to allow any non-empty password.)
NEW_PASSWORD: str = ""

# 4. Should this account be granted full Administrator privileges?
MAKE_ADMIN: bool = True

# 5. Allow any non-empty password when setting via this script?
BYPASS_COMPLEXITY_CHECK: bool = False

# 6. Turn off Face Login requirement for this account?
DISABLE_FACE_LOGIN: bool = False

# ============================================================================

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if (HERE.parent / "core").is_dir() else HERE
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _hash_with_optional_validation(password: str, bypass_rules: bool = False) -> str:
    from auth import password_manager as pm
    if not password:
        raise ValueError("Password cannot be empty.")
    if not bypass_rules:
        pm.validate_password(password)
    return pm.hash_password(password)


def _ask_password(bypass_rules: bool = False) -> str:
    from auth import password_manager as pm
    while True:
        first = getpass.getpass("New password (hidden): ")
        if not first:
            print("  Password cannot be empty.")
            continue
        if not bypass_rules:
            try:
                pm.validate_password(first)
            except Exception as error:
                print(f"  {error}")
                continue
        if getpass.getpass("Type it again: ") != first:
            print("  The two entries do not match. Try again.")
            continue
        return first


def _apply_configured_settings() -> int:
    """Apply the top-of-file USER EDITABLE SETTINGS if TARGET_USERNAME is set."""
    from auth import password_manager as pm
    from auth.auth_manager import auth_manager
    from memory import database

    database.init_db()
    target = TARGET_USERNAME.strip()
    user = database.get_user(target)

    print("=" * 64)
    print(" ZEUS - Applying Edited Account & Password Settings")
    print("=" * 64)

    if not user:
        if not NEW_PASSWORD:
            print(f"\n[ERROR] Account '{target}' does not exist, and NEW_PASSWORD is empty.")
            print("        Set NEW_PASSWORD in this file to create the account automatically.")
            return 1
        final_username = (NEW_ADMIN_OR_USER_ID or target).strip()
        hashed = _hash_with_optional_validation(NEW_PASSWORD, BYPASS_COMPLEXITY_CHECK)
        role = "admin" if MAKE_ADMIN else "operator"
        user_id = database.create_user(
            final_username,
            hashed,
            display_name=final_username,
            algorithm=pm.algorithm_of(hashed),
            is_admin=MAKE_ADMIN,
            role=role,
        )
        database.log_auth_event("account_created_offline", final_username, f"role={role} via SET_PASSWORD.py")
        print(f"\n[OK] Created new {'ADMIN' if MAKE_ADMIN else 'USER'} account '{final_username}' (id={user_id}).")
        return 0

    user_id = user["id"]
    current_username = user["username"]

    if NEW_ADMIN_OR_USER_ID.strip() and NEW_ADMIN_OR_USER_ID.strip() != current_username:
        new_name = NEW_ADMIN_OR_USER_ID.strip()
        database.rename_user(user_id, new_name, new_name)
        database.log_auth_event("username_changed_offline", new_name, f"renamed from {current_username} via SET_PASSWORD.py")
        print(f"[OK] Changed account ID from '{current_username}' -> '{new_name}'.")
        current_username = new_name

    if NEW_PASSWORD:
        hashed = _hash_with_optional_validation(NEW_PASSWORD, BYPASS_COMPLEXITY_CHECK)
        database.update_user_password(user_id, hashed, pm.algorithm_of(hashed))
        auth_manager.reset_lock(current_username)
        database.log_auth_event("password_reset_offline", current_username, "reset with SET_PASSWORD.py")
        print(f"[OK] Updated password and cleared lockout for '{current_username}'.")

    if MAKE_ADMIN:
        database.set_user_role(user_id, "admin")
        print(f"[OK] Confirmed ADMIN privileges for '{current_username}'.")

    if DISABLE_FACE_LOGIN:
        from security import face_auth
        result = face_auth.remove(user_id, current_username, by="SET_PASSWORD.py at this machine")
        print(f"[OK] Face login for '{current_username}': "
              f"{'removed' if result.get('removed') else 'was not enrolled'}.")

    return 0


def main() -> int:
    if TARGET_USERNAME.strip():
        return _apply_configured_settings()

    from auth import password_manager as pm
    from auth.auth_manager import auth_manager
    from memory import database

    database.init_db()
    users = database.list_users()
    print("=" * 64)
    print(" ZEUS - Account, Admin ID & Password Recovery")
    print("=" * 64)
    if users:
        print("\nExisting local accounts:")
        for user in users:
            role_tag = " (ADMIN)" if user.get("is_admin") else ""
            print(f"  - {user['username']}{role_tag}")
    else:
        print("\nNo accounts exist yet - you can create your Admin account now.")

    print("\n  1) Set / Reset password for an account (Forgot Password)")
    print("  2) Change Admin ID / Rename an existing account (& optional password)")
    print("  3) Create a new Admin or Standard account")
    print("  4) Promote an existing account to Admin")
    print("  5) Turn off Face Login for an account")
    print("  6) Exit")
    choice = input("\nChoice [1-6]: ").strip()

    if choice == "1":
        username = input("Account name: ").strip()
        user = database.get_user(username)
        if not user:
            print(f"No account named '{username}'.")
            return 1
        password = _ask_password(BYPASS_COMPLEXITY_CHECK)
        hashed = pm.hash_password(password)
        database.update_user_password(user["id"], hashed, pm.algorithm_of(hashed))
        auth_manager.reset_lock(username)
        database.log_auth_event("password_reset_offline", username,
                                "reset with SET_PASSWORD.py at this machine")
        print(f"\n[OK] The password for '{username}' has been changed and its lockout cleared.")
        print("     The reset is recorded in the audit log.")
        return 0

    if choice == "2":
        old_name = input("Current account / Admin ID: ").strip()
        user = database.get_user(old_name)
        if not user:
            print(f"No account named '{old_name}'.")
            return 1
        new_name = input("New account / Admin ID: ").strip()
        if not new_name:
            print("New account name cannot be empty.")
            return 1
        try:
            database.rename_user(user["id"], new_name, new_name)
            database.log_auth_event("username_changed_offline", new_name,
                                    f"renamed from {old_name} via SET_PASSWORD.py")
            print(f"\n[OK] Account ID changed from '{old_name}' to '{new_name}'.")
        except Exception as error:
            print(f"Could not rename account: {error}")
            return 1

        change_pw = input("Would you also like to set a new password for this account? [y/N]: ").strip().lower()
        if change_pw == "y":
            password = _ask_password(BYPASS_COMPLEXITY_CHECK)
            hashed = pm.hash_password(password)
            database.update_user_password(user["id"], hashed, pm.algorithm_of(hashed))
            auth_manager.reset_lock(new_name)
            database.log_auth_event("password_reset_offline", new_name,
                                    "reset with SET_PASSWORD.py at this machine")
            print(f"[OK] Password updated for '{new_name}'.")
        return 0

    if choice == "3":
        username = input("New account name: ").strip()
        make_admin_in = input("Make this account an Administrator? [Y/n]: ").strip().lower()
        is_admin = make_admin_in in ("", "y", "yes")
        password = _ask_password(BYPASS_COMPLEXITY_CHECK)
        try:
            hashed = pm.hash_password(password)
            role = "admin" if is_admin else "operator"
            database.create_user(
                username,
                hashed,
                display_name=username,
                algorithm=pm.algorithm_of(hashed),
                is_admin=is_admin,
                role=role,
            )
            database.log_auth_event("account_created_offline", username, f"role={role}")
        except Exception as error:
            print(f"Not created: {error}")
            return 1
        print(f"\n[OK] {'Admin' if is_admin else 'Standard'} account '{username}' created.")
        return 0

    if choice == "4":
        username = input("Account name to promote to Admin: ").strip()
        user = database.get_user(username)
        if not user:
            print(f"No account named '{username}'.")
            return 1
        database.set_user_role(user["id"], "admin")
        database.log_auth_event("role_changed_offline", username, "promoted to admin via SET_PASSWORD.py")
        print(f"\n[OK] Account '{username}' now has Administrator privileges.")
        return 0

    if choice == "5":
        username = input("Account name: ").strip()
        user = database.get_user(username)
        if not user:
            print(f"No account named '{username}'.")
            return 1
        from security import face_auth
        result = face_auth.remove(user["id"], username, by="SET_PASSWORD.py at this machine")
        print(f"\n[OK] Face template for '{username}' "
              f"{'removed - password alone signs in again' if result['removed'] else 'was not enrolled'}.")
        print("     This is recorded in the audit log.")
        return 0

    return 0


if __name__ == "__main__":
    code = main()
    if sys.stdin and sys.stdin.isatty():
        input("\nPress Enter to close...")
    sys.exit(code)
