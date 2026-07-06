from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509

from xedge.api.tls import load_or_create_server_certificate


def test_generates_valid_pem_cert_and_key(tmp_path: Path) -> None:
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"

    load_or_create_server_certificate(cert_path, key_path, "xedge.local", 825)

    assert cert_path.is_file()
    assert key_path.is_file()
    certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert certificate.subject.rfc4514_string() == "CN=xedge.local"


def test_validity_window_matches_configured_days(tmp_path: Path) -> None:
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"

    load_or_create_server_certificate(cert_path, key_path, "xedge.local", 30)

    certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    span = certificate.not_valid_after_utc - certificate.not_valid_before_utc
    assert timedelta(days=29) <= span <= timedelta(days=31)


def test_second_call_reuses_existing_pair_unchanged(tmp_path: Path) -> None:
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"

    load_or_create_server_certificate(cert_path, key_path, "xedge.local", 825)
    first_cert = cert_path.read_bytes()
    first_key = key_path.read_bytes()

    load_or_create_server_certificate(cert_path, key_path, "xedge.local", 825)

    assert cert_path.read_bytes() == first_cert
    assert key_path.read_bytes() == first_key


def test_certificate_not_yet_expired(tmp_path: Path) -> None:
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"

    load_or_create_server_certificate(cert_path, key_path, "xedge.local", 825)

    certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert certificate.not_valid_after_utc > datetime.now(UTC)
