from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from xedge.security.ca import load_or_create_ca
from xedge.security.csr import generate_key_and_csr
from xedge.security.tls_context import build_mtls_client_context


def test_build_mtls_client_context_succeeds_for_a_matching_cert_and_key(tmp_path: Path) -> None:
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "test-ca", 3650)
    private_key_pem, csr_pem = generate_key_and_csr("dev1")
    cert_pem = ca.sign_csr(csr_pem, common_name="dev1", validity_days=90)
    cert_path, key_path = tmp_path / "dev1-cert.pem", tmp_path / "dev1-key.pem"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(private_key_pem)

    context = build_mtls_client_context(cert_path, key_path, tmp_path / "ca.pem")

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_build_mtls_client_context_rejects_a_mismatched_key(tmp_path: Path) -> None:
    """Guards against a cert/key pair getting out of sync on disk (e.g. a
    partially-written rotation) — OpenSSL's own `load_cert_chain` cross-
    checks the key against the certificate's public key at load time."""
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "test-ca", 3650)
    _matching_key_pem, csr_pem = generate_key_and_csr("dev1")
    cert_pem = ca.sign_csr(csr_pem, common_name="dev1", validity_days=90)
    other_key_pem, _other_csr_pem = generate_key_and_csr("dev1")

    cert_path, key_path = tmp_path / "dev1-cert.pem", tmp_path / "dev1-key.pem"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(other_key_pem)

    with pytest.raises(ssl.SSLError):
        build_mtls_client_context(cert_path, key_path, tmp_path / "ca.pem")


def test_build_mtls_client_context_raises_for_a_missing_ca_file(tmp_path: Path) -> None:
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "test-ca", 3650)
    private_key_pem, csr_pem = generate_key_and_csr("dev1")
    cert_pem = ca.sign_csr(csr_pem, common_name="dev1", validity_days=90)
    cert_path, key_path = tmp_path / "dev1-cert.pem", tmp_path / "dev1-key.pem"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(private_key_pem)

    with pytest.raises(FileNotFoundError):
        build_mtls_client_context(cert_path, key_path, tmp_path / "no-such-ca.pem")
