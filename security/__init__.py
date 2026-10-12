"""
ZEUS security architecture.

Security is enforced by code, never by the model. Every tool call - from the
interface, the orchestrator, the planner, the CLI or a LAN peer - passes the
same choke point in `core.tool_registry.ToolRegistry.execute`, which asks
`security.policy` whether the current identity may run it:

    identity -> role -> data classification -> tool permission (read / write /
    execute) -> protected paths -> approval if required -> execution -> audit

Modules
-------
pathguard        path normalisation and the protected-location list
events           security events (persisted) and live listeners for alerts
audit_chain      tamper-evident, HMAC-chained audit log
policy           RBAC, tool classification, the authorisation decision
approvals        the human approval gateway (UI dialog, CLI prompt, or deny)
egress           in-process network egress guard, counters, kill switch
integrity        trusted model registry (Ollama digests pinned at setup)
injection        prompt-injection scanner and untrusted-data framing for RAG
sandbox          resource-limited, network-guarded code execution
firewall         Windows Firewall rule generation and status (applied by an admin)
lockdown         emergency lockdown
sovereignty      the measured "prove it" report

Nothing in this package claims a control is active unless a check says so.
"""
