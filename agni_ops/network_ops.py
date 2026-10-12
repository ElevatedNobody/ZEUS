"""
ZEUS Network checks for the operator BAT files. Every PASS is something that
was actually done - a TLS handshake that verified the certificate, a real read
of Windows Credential Manager - never an assumption.

    check     mode, server address (TLS only), trust, a real TLS
              handshake to the server, Credential Manager, the
              firewall layer's exception for the server, local model
    selftest  the two-device test on THIS machine: a real server
              process and two separate device processes over TLS
"""

from __future__ import annotations

import os
import socket
import ssl
import time
from pathlib import Path
from typing import List

from agni_ops.status import FAIL, NOT_TESTED, PASS, UNAVAILABLE, WARN, Result

ROOT = Path(__file__).resolve().parent.parent


def _r(component, status, reason="", **extra) -> Result:
    return Result(component, status, reason=reason, group="network", **extra)


def check() -> List[Result]:
    from crudex_net import keystore, settings
    results: List[Result] = []
    try:
        mode = settings.mode()
    except Exception as error:
        mode = "offline"
        results.append(_r("mode", WARN, f"could not read the saved mode ({error}) - Offline is used"))
    results.append(_r("connection mode", PASS, f"{mode.upper()} (per installation; changed in the user menu)"))
    url, source = settings.server_url_source()
    if not url:
        results.append(_r("ZEUS server address", NOT_TESTED if mode == "offline" else FAIL,
                          "not configured - config.ini [crudex] server_url or CRUDEX_SERVER_URL (TLS only). "
                          "There is no built-in server.", fallback="Offline mode - every local feature works"))
    else:
        try:
            endpoint = settings.endpoint()
            results.append(_r("ZEUS server address", PASS, f"{endpoint['url']} (from {source})"))
        except ValueError as error:
            results.append(_r("ZEUS server address", FAIL, str(error), where=source))
            endpoint = None
        if endpoint:
            results.append(_tls_probe(endpoint))
    ca = settings.ca_file()
    if ca:
        results.append(_r("extra CA file", PASS if Path(ca).is_file() else FAIL, ca))
    results.append(_r("secure key storage", PASS if keystore.available() else UNAVAILABLE,
                      "Windows Credential Manager (DPAPI, per Windows user, not roaming)" if keystore.available()
                      else "pywin32 / Credential Manager unavailable - an API key is held in memory for one session only"))
    results += _firewall(url)
    results.append(_local_model())
    return results


def _tls_probe(endpoint) -> Result:
    """One real TLS handshake with certificate + host-name verification, outside
    the app (so the in-process egress guard does not answer - the OS firewall and
    the network do)."""
    from crudex_net import settings
    host, port = endpoint["host"], int(endpoint["port"])
    started = time.time()
    try:
        context = settings.ssl_context()
        with socket.create_connection((host, port), timeout=8) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                version = tls.version()
                cert = tls.getpeercert()
        subject = dict(x[0] for x in cert.get("subject", ())) if cert else {}
        return _r("TLS to the server", PASS, f"{host}:{port} {version}, certificate verified "
                  f"({subject.get('commonName', '?')}) in {round((time.time() - started) * 1000)} ms")
    except ssl.SSLCertVerificationError as error:
        return _r("TLS to the server", FAIL, f"certificate NOT trusted: {error.verify_message}",
                  fix_attempted="none", fallback="set config.ini [crudex] ca_file for a self-hosted server")
    except OSError as error:
        if getattr(error, "winerror", None) == 10013:
            return _r("TLS to the server", FAIL, "Windows Firewall blocks this Python from the internet. "
                      "Re-apply firewall rules as administrator after setting the server address.")
        return _r("TLS to the server", FAIL, f"{type(error).__name__}: {error}")


def _firewall(url: str) -> List[Result]:
    try:
        from security import firewall
        state = firewall.status()
    except Exception as error:
        return [_r("OS firewall layer", WARN, f"could not be read: {error}")]
    if not state.get("active"):
        return [_r("OS firewall layer", PASS, "no block rules applied - nothing at the OS layer stops the "
                                              "server connection")]
    legacy = state.get("legacy_rules") or []
    allow = []
    try:
        import json
        manifest = json.loads((ROOT / "config" / "firewall_manifest.json").read_text(encoding="utf-8"))
        allow = manifest.get("allow_remote") or []
    except Exception:
        pass
    wanted = firewall.crudex_server_addresses(ROOT) if url else []
    missing = [a for a in wanted if a not in allow]
    results = [_r("OS firewall layer", WARN if missing else PASS,
                  f"{len(state.get('rules', []))} block rule(s) active"
                  + (f" ({len(legacy)} legacy rule(s))" if legacy else "")
                  + (f"; the server {', '.join(missing)} is NOT excepted - Online mode will be blocked." if missing else
                     (f"; server exception: {', '.join(allow)}" if allow else "")))]
    return results


def _local_model() -> Result:
    try:
        from core.ollama_client import ollama_client
        up = ollama_client.is_available(force=True)
        models = ollama_client.list_models() if up else []
    except Exception as error:
        return _r("local model runtime", WARN, str(error))
    return _r("local model runtime", PASS if up else WARN,
              f"running, {len(models)} model(s) - works with no internet" if up else "not running (ollama serve)")


def selftest() -> List[Result]:
    from agni_ops import suites
    os.environ["CRUDEX_RUN_NETWORK_E2E"] = "1"
    try:
        return suites.regression(timeout=1800, paths=["tests/crudex"],
                                 component="ZEUS NETWORK two-device test (one machine, loopback TLS)")
    finally:
        os.environ.pop("CRUDEX_RUN_NETWORK_E2E", None)
