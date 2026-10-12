"""
python -m crudex_server <command>

    serve       --data DIR [--host 0.0.0.0] [--port 8765] --cert server.pem --key server.key
                --data DIR --host 127.0.0.1 --port 8765 --behind-proxy   (TLS ends at a reverse proxy)
    make-cert   --out DIR --host NAME [--host 203.0.113.7 ...]     a private CA + server certificate
    admin       --data DIR create-account NAME [--key-out FILE]     prints (or writes) the API key ONCE
                --data DIR issue-key ACCOUNT_ID [--label L] [--key-out FILE]
                --data DIR revoke-key KEY_ID
                --data DIR revoke-device DEVICE_ID
                --data DIR list

An API key is shown exactly once, when it is issued; the server keeps only its
SHA-256 hash and its last four characters.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _write_key(key: str, target: str) -> str:
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key + "\n")
    return str(path)


def _serve(args) -> int:
    from PySide6.QtCore import QCoreApplication, QTimer
    from crudex_server.server import CrudexServer
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(data / "crudex_server.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stderr)])
    app = QCoreApplication(sys.argv[:1])
    server = CrudexServer(data, args.host, args.port, args.cert, args.key, behind_proxy=args.behind_proxy,
                          max_file_bytes=int(args.max_file_gb * 1024 ** 3))
    port = server.start()
    print(f"ZEUS SERVER LISTENING {args.host} {port}", flush=True)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    pulse = QTimer()                      # lets Python deliver Ctrl+C / SIGTERM to the Qt loop
    pulse.timeout.connect(lambda: None)
    pulse.start(250)
    code = app.exec()
    server.stop()
    return code


def _admin(args) -> int:
    from crudex_server.store import ServerStore
    store = ServerStore(Path(args.data))
    if args.action == "create-account":
        account_id, key = store.create_account(args.name, label="first key")
        if args.key_out:
            print(json.dumps({"account_id": account_id, "key_file": _write_key(key, args.key_out)}))
        else:
            print(json.dumps({"account_id": account_id}))
            print(f"API KEY (shown once - give it to the user, it is not stored): {key}")
        return 0
    if args.action == "issue-key":
        key = store.issue_key(args.name, args.label or "")
        if args.key_out:
            print(json.dumps({"account_id": args.name, "key_file": _write_key(key, args.key_out)}))
        else:
            print(f"API KEY (shown once): {key}")
        return 0
    if args.action == "revoke-key":
        print(json.dumps({"revoked": store.revoke_key(args.name)}))
        return 0
    if args.action == "revoke-device":
        print(json.dumps({"revoked_peers": store.revoke_device(args.name, by="server-admin")}))
        return 0
    if args.action == "list":
        accounts = store.q("SELECT account_id, name, created_at, disabled FROM accounts")
        for account in accounts:
            account["keys"] = store.q("SELECT key_id, last4, label, created_at, revoked_at FROM api_keys "
                                      "WHERE account_id = ?", (account["account_id"],))
            account["devices"] = store.account_devices(account["account_id"])
        print(json.dumps(accounts, indent=2, default=str))
        return 0
    return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="crudex_server", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--data", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--cert")
    serve.add_argument("--key")
    serve.add_argument("--behind-proxy", action="store_true")
    serve.add_argument("--max-file-gb", type=float, default=20.0)
    cert = sub.add_parser("make-cert")
    cert.add_argument("--out", required=True)
    cert.add_argument("--host", action="append", default=[])
    admin = sub.add_parser("admin")
    admin.add_argument("--data", required=True)
    admin.add_argument("action", choices=("create-account", "issue-key", "revoke-key", "revoke-device", "list"))
    admin.add_argument("name", nargs="?", default="")
    admin.add_argument("--label")
    admin.add_argument("--key-out")
    args = parser.parse_args(argv)
    if args.command == "serve":
        if bool(args.cert) != bool(args.key):
            parser.error("--cert and --key go together")
        return _serve(args)
    if args.command == "make-cert":
        from crudex_server import devcert
        print(json.dumps(devcert.make(Path(args.out), args.host or ["localhost", "127.0.0.1"]), indent=2))
        return 0
    if args.action != "list" and not args.name:
        parser.error(f"admin {args.action} needs a value")
    return _admin(args)


if __name__ == "__main__":
    sys.exit(main())
