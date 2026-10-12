"""
ZEUS NETWORK server - the coordination backend two (or more) ZEUS devices
connect to over the internet in Online mode.

    protocol.py   the wire protocol, permissions, limits (shared with the client)
    store.py      SQLite: accounts, hashed API keys, devices, pairings, grants,
                  the delivery queue, tasks, transfers, audit
    server.py     the WebSocket server (QtWebSockets over TLS)
    devcert.py    a private CA + server certificate for self-hosted servers and tests
    __main__.py   python -m crudex_server serve | admin ... | make-cert

It runs on any machine both devices can reach (a small VPS, a server in the
office with a public address, or behind a reverse proxy). It needs Python 3.10+,
PySide6 (QtWebSockets) and cryptography - see docs/CRUDEX_NETWORK.md.
"""
