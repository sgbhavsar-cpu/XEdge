"""Self-signed server certificate for the Web UI / REST API (Sprint 13,
XEDGE-107 — partial scope, see docs/planning/sprint-planning.md: server-side
HTTPS only, not full mutual-TLS/client-certificate enforcement).

Same "load if it already exists, else generate and persist" shape as
`xedge.api.auth.load_or_create_secret_key` — a device generates its own
identity certificate on first boot and keeps it across restarts.
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_RSA_KEY_SIZE = 2048
_RSA_PUBLIC_EXPONENT = 65537


def load_or_create_server_certificate(
    cert_path: Path, key_path: Path, common_name: str, validity_days: int
) -> None:
    """Ensure `cert_path`/`key_path` hold a matching self-signed certificate
    and private key, generating one if either file is missing. Idempotent:
    an existing pair is left untouched (a device's identity certificate
    shouldn't silently rotate on every restart)."""
    if cert_path.is_file() and key_path.is_file():
        return

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    private_key = rsa.generate_private_key(
        public_exponent=_RSA_PUBLIC_EXPONENT, key_size=_RSA_KEY_SIZE
    )
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(common_name), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
