"""
A private certificate authority and a server certificate for a ZEUS server.

For a server on the public internet, prefer a certificate from a public CA
(Let's Encrypt through Caddy or certbot) - clients then trust it with no extra
step. This module is for the other cases: a self-hosted server with no public
name, and the automated two-device test. Clients trust the private CA by
pointing CRUDEX_CA_FILE (or config.ini [crudex] ca_file) at ca.pem; TLS
verification and host-name checking are never switched off.

    python -m crudex_server make-cert --out certs --host crudex.example.org --host 203.0.113.7
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import os
from pathlib import Path
from typing import Dict, List

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ZEUS"),
                      x509.NameAttribute(NameOID.COMMON_NAME, common)])


def _write_private(path: Path, key) -> None:
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def make(out: Path, hosts: List[str], days: int = 825) -> Dict[str, str]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    now = _dt.datetime.now(_dt.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = _name("ZEUS private CA")
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - _dt.timedelta(minutes=5)).not_valid_after(now + _dt.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    names = []
    for host in hosts or ["localhost", "127.0.0.1"]:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(_name(hosts[0] if hosts else "localhost")).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(minutes=5)).not_valid_after(now + _dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    (out / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (out / "server.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _write_private(out / "server.key", key)
    # The CA's private key is not needed after signing; it is not kept, so it cannot leak.
    return {"ca": str(out / "ca.pem"), "cert": str(out / "server.pem"), "key": str(out / "server.key")}
