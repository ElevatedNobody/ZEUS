"""
ZEUS NETWORK client - the part of ZEUS that runs on each device.

Offline mode (the default) uses none of this: every local feature works with no
network. Online mode, with an API key the user enters, connects this device to
the ZEUS server (crudex_server/) over wss:// so it can reach the user's other
devices wherever they are.

    settings.py   mode, server address (CRUDEX_SERVER_URL / config.ini), TLS trust, sharing categories
    keystore.py   API key + device private key in Windows Credential Manager
    identity.py   device id / name / platform / version / Ed25519 key pair
    store.py      the local database: outbox, received, messages, tasks, transfers, items, devices, folders
    client.py     the one connection: states, reconnect, outbox, deliveries, resumable transfers
    tasks.py      remote tasks (fixed allowlist, run locally through the policy engine)
    items.py      shared items (memory, knowledge, conversation, preferences, projects)
    folders.py    bidirectional folder sync
    sharing.py    LOCAL_ONLY / SHARED / ASK, and the rule that secrets never leave
    service.py    the operations the interface calls
    tools.py      the registered tools (policy-checked, audited)

See docs/CRUDEX_NETWORK.md.
"""
