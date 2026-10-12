"""
Application-level network egress control for the ZEUS process.

What this is: a guard installed on Python's socket layer inside THIS process.
Every outbound connection and every DNS lookup made by ZEUS's own Python code
(and by any library it loads - httpx, huggingface_hub, a stray telemetry
client) passes through it, is counted, and is allowed or refused according to
the mode:

    monitor     allow everything, record external destinations
    sovereign   loopback + configured LAN ranges only (the default)
                + the APPROVED destinations: while the ZEUS NETWORK is in
                Online mode, exactly one server (its host name, port, and the
                addresses that name resolved to). Approving and withdrawing
                are audited; nothing else outside the LAN becomes reachable.
    lockdown    loopback only - the kill switch (no approved destinations)

What this is NOT: an operating-system firewall. It cannot see traffic from
other processes (Ollama's own update check, the browser, Windows itself). That
layer is `security.firewall`, applied by an administrator, and the Security
Centre reports the two separately rather than letting one stand in for the
other.

The embedded Chromium that draws the interface runs in its own process; it is
restricted separately by a URL request interceptor (see ui/agni_shell.py) that
refuses every non-local URL.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
import traceback
from collections import deque
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from core.logger import get_logger

logger = get_logger("security.egress")

MODES = ("monitor", "sovereign", "lockdown")
DEFAULT_LAN = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
               "fc00::/7", "fe80::/10")
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", ""}

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_getaddrinfo = socket.getaddrinfo
_original_create_connection = socket.create_connection


class EgressBlocked(ConnectionRefusedError):
    """Raised instead of opening a connection the policy forbids."""


class EgressGuard:
    def __init__(self):
        self.mode = "sovereign"
        self.installed = False
        self._lan = [ipaddress.ip_network(net) for net in DEFAULT_LAN]
        self._lock = threading.Lock()
        self._bypass = threading.local()
        self.stats = {"attempts": 0, "allowed_local": 0, "allowed_lan": 0,
                      "allowed_external": 0, "allowed_approved": 0, "blocked": 0,
                      "dns_blocked": 0, "dns_local": 0}
        # host name (lower case) -> ports; resolved address -> ports
        self._approved: Dict[str, set] = {}
        self._approved_addrs: Dict[str, set] = {}
        self._resolved_by: Dict[str, set] = {}
        self.recent: deque = deque(maxlen=200)
        self.installed_at: Optional[float] = None

    # -- configuration ------------------------------------------------------

    def configure(self, mode: Optional[str] = None, lan_ranges: Optional[List[str]] = None) -> None:
        if mode:
            if mode not in MODES:
                raise ValueError(f"unknown network mode '{mode}'")
            previous, self.mode = self.mode, mode
            if previous != mode:
                logger.warning(f"network mode: {previous} -> {mode}")
                self._audit("network_mode", f"{previous} -> {mode}")
        if lan_ranges is not None:
            networks = []
            for text in lan_ranges:
                try:
                    networks.append(ipaddress.ip_network(text.strip(), strict=False))
                except ValueError:
                    logger.warning(f"ignoring invalid LAN range '{text}'")
            self._lan = networks

    def lan_ranges(self) -> List[str]:
        return [str(n) for n in self._lan]

    # -- approved destinations (the ZEUS NETWORK server) --------------------

    def approve_destination(self, host: str, port: int, reason: str) -> None:
        """Let THIS process reach one server outside the LAN in sovereign mode:
        `host` (a name or an address) on `port` only. Used by the ZEUS NETWORK
        client while Online mode is on. Lockdown still refuses it."""
        host = (host or "").strip().strip("[]").lower()
        if not host or not isinstance(port, int) or not 0 < port < 65536:
            raise ValueError("an approved destination needs a host and a port")
        with self._lock:
            added = port not in self._approved.get(host, set())
            self._approved.setdefault(host, set()).add(port)
            try:
                ipaddress.ip_address(host.split("%")[0])
                self._approved_addrs.setdefault(host.split("%")[0], set()).add(port)
            except ValueError:
                pass
        if added:
            logger.info(f"approved destination: {host}:{port} ({reason})")
            self._audit("egress_destination_approved", f"{host}:{port} - {reason}")

    def withdraw_destination(self, host: str, reason: str = "") -> None:
        """Forget an approved destination and every address its name resolved to."""
        host = (host or "").strip().strip("[]").lower()
        with self._lock:
            ports = self._approved.pop(host, None)
            forget = self._resolved_by.pop(host, set()) | {host.split("%")[0]}
            still_needed = set().union(*self._resolved_by.values()) if self._resolved_by else set()
            still_needed |= set(self._approved)
            for address in forget - still_needed:
                self._approved_addrs.pop(address, None)
        if ports:
            logger.info(f"approved destination withdrawn: {host} ({reason})")
            self._audit("egress_destination_withdrawn", f"{host} - {reason}")

    def approved_destinations(self) -> List[str]:
        with self._lock:
            return [f"{host}:{port}" for host, ports in sorted(self._approved.items()) for port in sorted(ports)]

    def _approved_address(self, host: str, port) -> bool:
        with self._lock:
            return port in self._approved_addrs.get(str(host).split("%")[0].lower(), set())

    # -- classification -----------------------------------------------------

    def classify(self, host: str) -> str:
        """'local' | 'lan' | 'external' | 'name' (an unresolved host name)."""
        host = (host or "").strip().strip("[]").lower()
        if host in _LOCAL_NAMES:
            return "local"
        try:
            address = ipaddress.ip_address(host.split("%")[0])
        except ValueError:
            return "name"
        if address.is_loopback or address.is_unspecified:
            return "local"
        if any(address in network for network in self._lan):
            return "lan"
        return "external"

    def _decide(self, where: str) -> bool:
        if where == "local":
            return True
        if self.mode == "lockdown":
            return False
        if where == "lan":
            return True
        return self.mode == "monitor"

    def url_allowed(self, url: str) -> Tuple[bool, str]:
        parsed = urlparse(url)
        if parsed.scheme in ("file", "agni", "qrc", "data", ""):
            return True, ""
        where = self.classify(parsed.hostname or "")
        if where == "name":
            # A name that is not 'localhost' would need an external DNS lookup.
            where = "external"
        if self._decide(where):
            return True, ""
        return False, (f"{parsed.hostname} is outside this machine and the network "
                       f"mode is '{self.mode}'")

    # -- recording ----------------------------------------------------------

    def _caller(self) -> str:
        for frame in reversed(traceback.extract_stack(limit=18)[:-3]):
            name = frame.filename.replace("\\", "/")
            if "/security/egress.py" in name or "/socket.py" in name:
                continue
            parts = name.split("/")
            return "/".join(parts[-2:]) + f":{frame.lineno}"
        return "unknown"

    def _record(self, kind: str, host: str, port, allowed: bool, where: str) -> None:
        with self._lock:
            self.stats["attempts"] += 1 if kind == "connect" else 0
            if kind == "dns":
                self.stats["dns_local" if allowed else "dns_blocked"] += 1
            elif allowed:
                self.stats[{"local": "allowed_local", "lan": "allowed_lan",
                            "approved": "allowed_approved"}.get(where, "allowed_external")] += 1
            else:
                self.stats["blocked"] += 1
            entry = {"ts": time.time(), "kind": kind, "host": str(host), "port": port,
                     "where": where, "allowed": allowed}
            if not allowed or where not in ("local",):
                entry["caller"] = self._caller()
                self.recent.appendleft(entry)
        if not allowed:
            try:
                from security import events
                events.record("egress_blocked" if kind == "connect" else "dns_blocked",
                              f"{host}:{port} ({where}) from {entry.get('caller', '?')}",
                              severity="warning")
            except Exception:
                pass

    def _audit(self, event: str, detail: str) -> None:
        try:
            from security.audit_chain import audit_chain
            audit_chain.append(event, detail)
        except Exception:
            pass

    # -- the guard ----------------------------------------------------------

    def _check(self, address) -> None:
        if getattr(self._bypass, "on", False):
            return
        host, port = (address[0], address[1]) if isinstance(address, tuple) and len(address) >= 2 \
            else (str(address), None)
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        where = self.classify(str(host))
        if where == "name":
            where = "external"
        allowed = self._decide(where)
        if not allowed and where == "external" and self.mode == "sovereign"                 and self._approved_address(host, port):
            allowed, where = True, "approved"
        if where != "local" or not allowed:
            self._record("connect", host, port, allowed, where)
        else:
            with self._lock:
                self.stats["attempts"] += 1
                self.stats["allowed_local"] += 1
        if not allowed:
            raise EgressBlocked(f"ZEUS network policy ({self.mode}) blocked a connection "
                                f"to {host}:{port}")

    def install(self) -> None:
        if self.installed:
            return
        guard = self

        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                guard._check(address)
            return _original_connect(sock, address)

        def connect_ex(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                try:
                    guard._check(address)
                except EgressBlocked:
                    return 10013          # WSAEACCES
            return _original_connect_ex(sock, address)

        def getaddrinfo(host, *args, **kwargs):
            if getattr(guard._bypass, "on", False):
                return _original_getaddrinfo(host, *args, **kwargs)
            text = host.decode() if isinstance(host, bytes) else (host or "")
            where = guard.classify(text)
            if where == "name" and guard.mode == "sovereign":
                with guard._lock:
                    ports = set(guard._approved.get(text.strip().lower(), set()))
                if ports:
                    results = _original_getaddrinfo(host, *args, **kwargs)
                    with guard._lock:
                        resolved = guard._resolved_by.setdefault(text.strip().lower(), set())
                        for info in results:
                            address = str(info[4][0]).split("%")[0].lower()
                            resolved.add(address)
                            guard._approved_addrs.setdefault(address, set()).update(ports)
                    guard._record("dns", text, args[0] if args else None, True, "approved")
                    return results
            if where == "name":
                if guard.mode != "monitor":
                    guard._record("dns", text, args[0] if args else None, False, "external")
                    raise socket.gaierror(socket.EAI_NONAME,
                                          f"ZEUS network policy ({guard.mode}) blocked a DNS "
                                          f"lookup for '{text}'")
                guard._record("dns", text, args[0] if args else None, True, "external")
            return _original_getaddrinfo(host, *args, **kwargs)

        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        socket.getaddrinfo = getaddrinfo
        self.installed = True
        self.installed_at = time.time()
        logger.info(f"egress guard installed (mode: {self.mode}; LAN: {', '.join(self.lan_ranges())})")
        self._audit("egress_guard_installed", f"mode={self.mode}")

    def uninstall(self) -> None:
        socket.socket.connect = _original_connect
        socket.socket.connect_ex = _original_connect_ex
        socket.getaddrinfo = _original_getaddrinfo
        self.installed = False

    class _Bypass:
        def __init__(self, guard, reason):
            self.guard, self.reason = guard, reason

        def __enter__(self):
            self.guard._bypass.on = True
            self.guard._audit("egress_bypass", self.reason)
            return self

        def __exit__(self, *exc):
            self.guard._bypass.on = False
            return False

    def bypass(self, reason: str):
        """For an EXPLICIT, audited operator action only (the network
        environment probe in the Security Centre)."""
        return self._Bypass(self, reason)

    def status(self) -> Dict:
        with self._lock:
            stats = dict(self.stats)
            recent = list(self.recent)[:25]
        external = stats["allowed_external"]
        return {"layer": "application (this process)", "installed": self.installed,
                "mode": self.mode, "lan_ranges": self.lan_ranges(), "stats": stats,
                "approved_destinations": self.approved_destinations(),
                "external_egress_allowed": external,
                "external_egress_blocked": stats["blocked"] + stats["dns_blocked"],
                "recent": recent, "installed_at": self.installed_at}


egress_guard = EgressGuard()


def configure_from_config() -> None:
    """Read [security] network_mode / lan_ranges from config.ini."""
    import configparser
    from core.paths import config_path
    parser = configparser.ConfigParser()
    try:
        parser.read(config_path(), encoding="utf-8")
    except Exception:
        pass
    mode = parser.get("security", "network_mode", fallback="sovereign").strip().lower()
    ranges = parser.get("security", "lan_ranges", fallback="").strip()
    egress_guard.configure(mode if mode in MODES else "sovereign",
                           [r for r in ranges.split(",") if r.strip()] if ranges else None)


def environment_probe(timeout: float = 1.5) -> Dict:
    """Is the NETWORK ENVIRONMENT connected to the internet?

    This deliberately opens one TCP connection to a public resolver, bypassing
    the guard - so it is only ever run when an operator presses the button, and
    it is audited. Nothing runs it automatically.
    """
    result = {"probed_at": time.time()}
    try:
        import psutil
        up = [name for name, stats in psutil.net_if_stats().items()
              if stats.isup and not name.lower().startswith(("loopback", "lo"))]
        result["adapters_up"] = up
    except Exception:
        result["adapters_up"] = []
    with egress_guard.bypass("operator requested network environment probe"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            _original_connect(sock, ("1.1.1.1", 443))
            result["internet"] = "reachable"
        except Exception as error:
            result["internet"] = "unreachable"
            result["detail"] = str(error)[:120]
        finally:
            sock.close()
    return result
