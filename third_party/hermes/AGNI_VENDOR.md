# Hermes (cognis-hermes) - vendored for AGNI

Source:   https://github.com/cognis-digital/hermes
Commit:   7bf0ec39c475876018adf92ea80d57e03d16bbbc (2026-06-22, "Add JSONL import/export subcommands + 8 real-use-case demos")
Package:  cognis-hermes 0.1.0 (`hermes/__init__.py`), pyproject 0.1.1
Status:   the upstream repository is ARCHIVED (read-only since 2026-06-26)
Vendored: 2026-09-25

Only the core library was taken, UNMODIFIED - each file was written from the
upstream git object itself (`git cat-file -p HEAD:<path>`), so its git blob id
equals upstream's. Upstream stores these files with CRLF line endings; the
SHA-256 values are of those exact bytes.

| file | SHA-256 | git blob |
|---|---|---|
| hermes/__init__.py | 02fb347c0e4f01ad68f32ee16714d8b4604b27d89fffb04d6c644e7bf4a2ef80 | 1b1c0b9113f8aefb0f24f45fc879676bbbc76efd |
| hermes/memory.py   | a83926c64d41b63fc63a98d566dd0a9e4a18a8549d9a58bcc89dfc4f8d568212 | 78bdb11894870f670fef75c7c8a38a40cd852c01 |
| hermes/py.typed    | abf6304ba16d05e74eb5d61c5579af366bd24ad4c787e503fc02b0061ca2b0d8 | a9b88ffee2498c9e003036fb3bfcc41dc1165f6a |
| LICENSE            | 6c6400d2d9d630d85d85aa149e8b0bc7cd46fa089c39625699c068cec1365d5e | dbde1ebcb92ba4b1db13fc6aa82aabad8744f5af |
| NOTICE             | 782fb1432e6c3d68582a2154290afaeadd19a9b381a218cd6dd1d14542731839 | 43ed96d2128320a3ca272c25a0c5a38385836f3e |

Deliberately NOT vendored: `livesearch.py`, `hermes/connect.py`,
`hermes/mcp_server.py`, `hermes/cli.py`, `integrations/` (network, IPC and
CLI surfaces AGNI does not use). The vendored code was read in full before use:
standard library only (sqlite3, json, math, re, time), no network access, no
subprocess, no dynamic code execution.

AGNI never edits these files. It reaches Hermes only through
`memory/hermes_store.py`, which verifies the SHA-256 values above at load time
and refuses a changed copy.

## LICENSE - READ THIS

Hermes is NOT MIT-licensed, whatever the pyproject classifier says. It is
distributed under the **Cognis Open Collaboration License (COCL) v1.0**, a
source-available, **non-commercial** license (see LICENSE):

* permitted: personal projects, internal evaluation, research, education
  (e.g. a hackathon build), with LICENSE and NOTICE retained;
* NOT permitted without a separate commercial license from Cognis Digital LLC
  (licensing@cognis.digital): production use to operate a business,
  redistribution as or inside a commercial product, or use by a for-profit
  entity beyond a 30-day internal evaluation.

If AGNI is deployed commercially, either obtain that license or switch the
memory backend to AGNI's own implementation, which shares no code with Hermes:

    config.ini
    [memory]
    backend = native
