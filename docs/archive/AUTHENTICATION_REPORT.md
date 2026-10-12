# Authentication Report

Mandatory local login for ZEUS. Entirely offline: accounts live in the same
SQLite database as the rest of the application and are verified in-process.
No auth server, no REST endpoint, no cloud identity provider, no default account.

**41 authentication tests plus 12 login-screen tests pass** against a real database and the real hashing
backend - nothing about authentication is stubbed.

---

## 1. Startup sequence

```
START ZEUS
   ↓  load core (logging, database schema - the account table lives there)
   ↓  load auth system
   ↓  LOGIN SCREEN                    <- first visible thing, always
   ↓  authenticate (local, hashed)
   ↓  session created, memory bound to that user
   ↓  MedhaAssistant constructed     <- engine, tools, memory, voice, industrial
   ↓  READY
```

`main.py` cannot reach `MedhaAssistant(...)` without passing
`authenticate(use_ui)` first, and a test asserts that ordering in the source.
Before the gate, nothing protected is initialised: no decision engine, no
memory manager, no voice models, no industrial layer.

Failed authentication returns to the login screen. There is no bypass flag.

## 2. Files

**Created**

| file | role |
|---|---|
| `auth/__init__.py` | lazy public API |
| `auth/password_manager.py` | hashing: Argon2id → bcrypt → scrypt |
| `auth/auth_manager.py` | accounts, policy, lockout, audit |
| `auth/session_manager.py` | the live session, lock/unlock/logout, active memory user |
| `ui/login_window.py` | the login / first-run / unlock screen |
| `tests/test_authentication.py` | 41 tests |
| `tests/test_login_window.py` | 12 tests |

**Modified**

| file | change |
|---|---|
| `memory/database.py` | `users`, `sessions`, `auth_events` tables; `user_id` migration for the personal tables; active-user scoping |
| `main.py` | authentication gate, CLI login, `--setup-user`, `--change-password`, `--auth-status`, lock/logout handling |
| `ui/dashboard.py` | LOCK and LOG OUT buttons, operator caption |
| `config.ini` | `[auth]` policy section |
| `requirements.txt` | `argon2-cffi`, `bcrypt` |
| `HOW_TO_RUN.txt`, `README.md`, `start_medha_industrial.bat` | login documentation |

## 3. Password hashing

Chosen at runtime, strongest first, and recorded inside the stored string:

| order | algorithm | parameters |
|---|---|---|
| 1 | **Argon2id** (`argon2-cffi`) | m=64 MB, t=3, p=4, 16-byte salt |
| 2 | bcrypt | cost 12 |
| 3 | **scrypt** (`hashlib`, standard library) | n=16384, r=8, p=1, 32-byte key |

The third tier matters: ZEUS must install on a disconnected machine with
nothing but the standard library, and scrypt is a genuine memory-hard KDF that
has shipped in `hashlib` since Python 3.6. **This module cannot produce a bare
SHA digest** - there is no code path to one.

Stored records are self-describing (`$argon2id$v=19$m=65536,...`,
`bcrypt$...`, `scrypt$n=16384,r=8,p=1$salt$hash`), so:

- a database written where Argon2 is installed still verifies elsewhere, or
  **fails closed** if the algorithm cannot be verified - never fails open;
- `needs_rehash()` upgrades an account to a stronger backend on the next
  successful login, when the password is legitimately in hand.

Verified in this environment across all three backends, including a legacy
scrypt record verifying and being flagged for upgrade after `argon2-cffi` was
installed.

## 4. Database

The existing SQLite database was **extended**, not replaced. No second user store.

```sql
users(id, username UNIQUE COLLATE NOCASE, display_name, password_hash,
      password_algorithm, created_at, last_login, failed_attempts,
      locked_until, is_active, is_admin)

sessions(id TEXT PK, user_id, created_at, last_activity, logout_time,
         locked, client)

auth_events(id, username, event, detail, created_at)   -- audit, no secrets
```

Plus an additive migration adding a nullable `user_id` to `messages`, `facts`,
`todos`, `calendar_events`, `notes`, `schedules`, with an index each. Existing
rows keep `user_id NULL`, which is exactly what preserves the old single-user
behaviour.

## 5. Sessions, lock and logout

`SessionManager` holds one session per process; the database row exists for the
audit trail, and no token from outside the process is ever accepted.

- **LOCK** (top bar) - `is_authenticated` becomes False, the window hides, the
  login screen returns in *unlock* mode for the same user. The application
  keeps running.
- **LOG OUT** - the session ends, the memory layer is detached
  (`set_active_user(None)`), and a fresh login screen appears. A "switch user"
  path exists from the unlock screen.
- **EXIT** (login screen) is the only thing that terminates ZEUS.
- **Idle timeout** - `session_idle_minutes` (0 = off) locks an unattended
  workstation.
- **Restart** always requires login: a new process starts with no session, and
  a persisted session row grants nothing.

## 6. Failed-login protection

```
max_failed_attempts = 5      # config.ini [auth]
lockout_seconds     = 60
```

Counting is per account. Below the limit the message states how many attempts
remain; at the limit the account locks and even the correct password is refused
until it expires. **The lock always expires** - a mistyped password can never
brick the installation - and an owner can clear it with
`AuthManager.reset_lock()`. Both values are configurable, so development is not
painful.

## 7. What the login screen does not reveal

- An unknown username costs the same work as a known one (a real hash
  verification against a dummy record), so **timing** cannot enumerate accounts.
- An unknown username also gets the same **countdown**. A real account answers
  "Invalid credentials. 3 attempt(s) left." and then locks; an invented name now
  answers identically, from a process-local counter. Without that the wording
  itself was an oracle - the shorter reply meant "no such account". The phantom
  counter is never written to the database (inventing rows for names that do not
  exist would be worse), is bounded at 512 entries against spraying, and its
  lock always expires, so a name refused today can still become a real account
  tomorrow.
- Wrong username and wrong password produce the same wording, and a test
  compares them turn by turn rather than trusting the claim.
- No account list, no database path, no version banner, no hashes.
- `auth_events` records *what happened* and to which username - never a
  password, never a hash. Tests assert that the audit trail and the status
  output contain neither.
- `database.list_users()` deliberately omits password columns.

## 7b. The login screen itself

Redesigned around the dashboard's own HUD language: the rotating orb from
`ui/hud_widgets.py`, an angular frame with corner brackets, and the form in a
glow card beside it. The orb turns amber while a password is being verified,
so the screen visibly works rather than appearing to hang.

Three defects were fixed at the same time, all of which showed as "glitchy":

1. **The GUI thread was blocked during verification.** Argon2id takes 0.3-0.5s
   at m=64MB, and the old screen ran it inline with `QApplication.processEvents()`
   to keep painting. That froze the window mid-click and let a second Return
   re-enter the same handler. Verification now runs on a `QThread`; the submit
   handler returns in under 15 ms and a test asserts it.
2. **A worker callback could outlive the dialog.** A `finished` lambda calling
   `deleteLater()`, and `accept()` called from inside the worker's own signal
   delivery, both segfaulted the interpreter once a second login screen was
   opened - which is exactly what LOCK and LOG OUT do. Workers are now parented,
   disconnected and waited for in `done()`, and the dialog closes on the next
   turn of the event loop.
3. **The layout jumped and clipped.** One fixed size for every mode squeezed the
   setup form; the error text resized the card as it appeared. The window is
   sized per mode and the message area has reserved height, so nothing moves.

Also added: Caps Lock detection (a wrong password and a stuck Caps Lock were
indistinguishable, and the difference costs real lockout attempts), a red flash
on the offending field, and every timer stopped on close so a hidden screen
does not keep repainting.

## 8. Per-user memory

`SessionManager` is the only place that tells the memory layer who is active.
With a session, every read and write on the personal tables is scoped to that
user; with no session, behaviour is identical to before login existed - which
is what keeps every pre-existing test, script and tool working.

| data | scope | why |
|---|---|---|
| conversation history, facts, to-dos, notes, calendar, schedules | **per user** | personal memory |
| industrial documents, knowledge corpus, chunks, assets | **shared** | plant documents are workbench data, not one person's memory |
| task history, audit log | shared | operational record of the machine |

Fact keys are namespaced per user internally (`u3::note_912`) because the
original `facts.key` column is UNIQUE - two operators can now store the same
key without overwriting each other, and the prefix is stripped on read so no
caller sees it.

**Upgrade path:** a database created before login existed has rows with no
owner. The first account to log in adopts them (only when exactly one account
exists - with two, ownership would be a guess), so upgrading never looks like
losing your history. Verified by test.

## 9. Access control

After the gate, everything ZEUS could do before remains available - all 92
tools, all 17 agents, the dashboard, the industrial layer. Before it, the only
reachable surface is the login screen.

Honest limit: this protects the **running application**, not the disk. Anyone
with a shell on the machine and write access to the project can edit the source
or read `database/medha.db` directly. Password hashes are safe there (that is
the point of Argon2id), but conversation history and industrial documents are
not encrypted at rest. Full-disk encryption (BitLocker) is the right control
for that, and it is out of scope for this change.

## 10. Commands

| purpose | command |
|---|---|
| normal start (login screen first) | `start_medha_industrial.bat` |
| create an account | `python main.py --setup-user` |
| change a password | `python main.py --change-password` |
| inspect the policy | `python main.py --auth-status` |
| text-only login | `python main.py --cli` |
| run the auth tests | `python -m pytest tests\test_authentication.py -q` |

Forgotten password: there is no recovery back door by design. Create another
account with `--setup-user`, or delete `database\medha.db` to start over
(which also deletes the history).
