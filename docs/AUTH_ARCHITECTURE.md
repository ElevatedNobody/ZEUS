# ZEUS authentication architecture

The login is rebuilt VISUALLY from scratch inside the new interface. The security core
underneath is the existing, tested one and is not weakened:
`auth/auth_manager.py` (accounts, Argon2id, lockout), `auth/session_manager.py`
(sessions, lock, idle expiry, face sign-in, second factor), `security/face_auth.py`
(InsightFace, liveness, DPAPI templates), `security/policy.py` (RBAC),
`security/approvals.py` (human approval), `security/lockdown.py`, the vault and the
audit chain.

## 1. Rules

1. **The page decides nothing.** It renders states and forwards input. Every decision
   (password valid, face matched, live, locked out, role, lockdown) is made in Python and
   returned; the page cannot mark itself signed in.
2. **Only authentication methods exist before a session.** Every other bridge method
   is refused until `session_manager.is_authenticated` is true (the bridge contract's
   `auth` flag, checked in Python for every call).
3. **One attempt at a time.** A second login or face attempt while one is running is
   refused, not queued (the old screen's re-entry bug stays fixed).
4. **Generic failure wording.** No message reveals whether an account exists or which
   half of a credential was wrong (the equalised timing and wording of `authenticate`).
5. **Face never bypasses anything.** Face sign-in is only offered when
   `[face] login = face_or_password`, requires liveness, and the matched account's own
   state (disabled, locked out) still applies. `second_factor` requires the face AFTER
   the password. Three failed face attempts pause face sign-in until a password sign-in.
6. **Secure fallback always exists.** Camera missing, busy, or failing -> CAMERA ERROR
   with the reason, and the password path stays available.
7. **No backdoor.** No developer bypass, no test mode in the application
   (`AGNI_TEST_MODE` is removed at start and recorded as a security event), no default
   password.

## 2. Flows

```
first run         SETUP (create the administrator) -> ACCESS GRANTED -> INITIALIZING -> WORKSPACE
password          LOCKED -> credentials -> [SECOND FACTOR REQUIRED -> face flow] -> ACCESS GRANTED
face              LOCKED -> CAMERA READY -> SCANNING -> FACE DETECTED -> LIVENESS CHECK
                  -> VERIFYING IDENTITY -> IDENTITY VERIFIED -> ACCESS GRANTED | ACCESS DENIED
unlock            LOCKED (session kept) -> password or the LOCKED USER'S face -> WORKSPACE
switch user       unlock screen -> sign out -> LOCKED
idle expiry       WORKSPACE -> SESSION EXPIRED (session locked, vault locked) -> unlock
lockdown          any -> LOCKDOWN (only status and recovery; admin releases)
high-risk action  approval required -> human approval sheet (Deny / Allow once)
```

## 3. States and their backend source

| State | Shown when (backend fact) |
|---|---|
| LOCKED | no authenticated session (`session_manager.is_authenticated` false) |
| INITIALIZING | session authenticated; the workspace (engine, memory, voice) is being built - `workspace` events report each real step |
| CAMERA READY | the capture worker opened the camera and returned its first frame |
| SCANNING | frames are being captured (progress = frames kept / frames needed) |
| FACE DETECTED | `identify()` / `verify()` found exactly one face in an analysed frame |
| LIVENESS CHECK | the liveness model evaluated that face (live score reported per frame) |
| VERIFYING IDENTITY | templates are being compared |
| IDENTITY VERIFIED | match reached the threshold and margin in 2+ live frames |
| ACCESS GRANTED | `login_with_face` / `login` / `unlock*` returned ok **and** the session is authenticated |
| ACCESS DENIED | the backend refused; the message is the backend's (generic) |
| CAMERA ERROR | the camera could not be opened or returned no frames |
| AUTHENTICATION TIMEOUT | capture exceeded its limit without enough frames |
| SECOND FACTOR REQUIRED | password accepted, `pending_factor == "face"` |
| SESSION EXPIRED | idle limit reached (`[auth] session_idle_minutes`), session locked by the backend |
| LOCKDOWN | `security.lockdown.lockdown.engaged` |

"SECURE", "VERIFIED" or "PROTECTED" are never displayed as decoration; the words appear
only as the name of a state the backend reported.

## 4. Bridge methods (no session required)

| Method | Does |
|---|---|
| `auth_state` | phase (setup / login / unlock / lockdown), locked user for unlock, face availability + reason + attempts left, lockout, password policy |
| `auth_setup` | create the first account (password policy enforced) and sign in |
| `auth_login` | password sign-in; returns `pending_factor` when the face is also required |
| `auth_unlock` | password unlock of the locked session |
| `auth_face` | start a face capture for `login`, `unlock` or `second_factor`; progress arrives as `auth_face` events (states above, a small preview frame, result) |
| `auth_face_cancel` | stop the capture and release the camera |
| `auth_cancel_factor` | abandon a pending second factor (signs out) |
| `auth_switch_user` | from the unlock screen: sign out |
| `quit` | leave ZEUS from the login screen |

Camera frames are captured in Python, never by the page: the page receives small JPEG
previews for display only; recognition uses full frames that never leave Python and are
never written to disk.

## 5. Mechanisms available

| Mechanism | Status |
|---|---|
| Password (Argon2id) | available |
| Face (InsightFace + RGB liveness) | available when models are installed and a face is enrolled |
| Face as second factor | available (`[face] login = second_factor`) |
| PIN / passkey (Windows Hello) | **not available** - no PIN or passkey store exists in ZEUS; the login states this instead of showing a button that does nothing |
| Human approval for high-risk tools | available (`[security] confirm_threshold`) |
| Step-up face check for high-risk tools | **not implemented** - planned in the design, not built; high-risk tools use the human approval sheet |

Implemented 2026-09-26/27: the sign-in page (`ui/medha/js/auth.js`) and its controller
(`auth/web_login.py`) - every state above comes from the backend
(`tests/test_web_login.py`, 14 tests; `tests/e2e/test_medha_e2e.py` signs in through the
page). The previous Qt login window was removed on 2026-09-27.

## 6. Audit

Every attempt, success, lockout, face match/rejection, unlock, lock, expiry, switch and
lockdown is written to `auth_events` and, for security-relevant ones, to the security
event log (both already existed); new bridge methods add no path that skips them.
