from __future__ import annotations

from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from xedge.security.ca import InvalidCsrError, load_or_create_ca
from xedge.security.csr import generate_key_and_csr


def test_creates_a_self_signed_ca_certificate(tmp_path: Path) -> None:
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "xedge-fleet-ca", 3650)

    assert ca.certificate.subject == ca.certificate.issuer
    assert ca.certificate.subject.rfc4514_string() == "CN=xedge-fleet-ca"
    basic_constraints = ca.certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic_constraints.value.ca is True


def test_reloading_an_existing_ca_returns_the_same_key_and_certificate(tmp_path: Path) -> None:
    cert_path, key_path = tmp_path / "ca.pem", tmp_path / "ca.key"
    first = load_or_create_ca(cert_path, key_path, "xedge-fleet-ca", 3650)

    second = load_or_create_ca(cert_path, key_path, "xedge-fleet-ca", 3650)

    assert second.certificate.serial_number == first.certificate.serial_number
    assert second.certificate_pem == first.certificate_pem


def test_sign_csr_issues_a_leaf_certificate_for_the_requested_common_name(tmp_path: Path) -> None:
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "xedge-fleet-ca", 3650)
    _private_key_pem, csr_pem = generate_key_and_csr("gateway-007")

    cert_pem = ca.sign_csr(csr_pem, common_name="gateway-007", validity_days=90)

    certificate = x509.load_pem_x509_certificate(cert_pem)
    assert certificate.subject.rfc4514_string() == "CN=gateway-007"
    assert certificate.issuer == ca.certificate.subject
    basic_constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic_constraints.value.ca is False


def test_sign_csr_ignores_the_csrs_own_subject_and_uses_the_caller_supplied_name(
    tmp_path: Path,
) -> None:
    """A CSR is unauthenticated input — the manager decides the identity
    granted (via the join-token/device_id it already validated), never
    whatever CN a client happened to put in its own CSR."""
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "xedge-fleet-ca", 3650)
    _private_key_pem, csr_pem = generate_key_and_csr("attacker-claims-to-be-admin")

    cert_pem = ca.sign_csr(csr_pem, common_name="gateway-007", validity_days=90)

    certificate = x509.load_pem_x509_certificate(cert_pem)
    assert certificate.subject.rfc4514_string() == "CN=gateway-007"


def test_sign_csr_validity_window_matches_configured_days(tmp_path: Path) -> None:
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "xedge-fleet-ca", 3650)
    _private_key_pem, csr_pem = generate_key_and_csr("gateway-007")

    cert_pem = ca.sign_csr(csr_pem, common_name="gateway-007", validity_days=30)

    certificate = x509.load_pem_x509_certificate(cert_pem)
    span = certificate.not_valid_after_utc - certificate.not_valid_before_utc
    assert timedelta(days=29) <= span <= timedelta(days=31)
    assert certificate.not_valid_after_utc > datetime.now(UTC)


def test_sign_csr_rejects_a_tampered_csr(tmp_path: Path) -> None:
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "xedge-fleet-ca", 3650)
    _private_key_pem, csr_pem = generate_key_and_csr("gateway-007")

    # Flip a byte in the middle of the base64 body to invalidate the
    # self-signature without corrupting the PEM framing.
    lines = csr_pem.splitlines()
    body_index = len(lines) // 2
    tampered_line = bytearray(lines[body_index])
    tampered_line[0] ^= 0xFF
    lines[body_index] = bytes(tampered_line)
    tampered_csr_pem = b"\n".join(lines)

    with pytest.raises((InvalidCsrError, ValueError)):
        ca.sign_csr(tampered_csr_pem, common_name="gateway-007", validity_days=90)


def test_sign_csr_with_no_sans_requested_omits_the_extension(tmp_path: Path) -> None:
    """Device client certificates deliberately carry no SAN — mTLS never
    verifies the client by hostname, so there is nothing for one to do."""
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "xedge-fleet-ca", 3650)
    _private_key_pem, csr_pem = generate_key_and_csr("gateway-007")

    cert_pem = ca.sign_csr(csr_pem, common_name="gateway-007", validity_days=90)

    certificate = x509.load_pem_x509_certificate(cert_pem)
    with pytest.raises(x509.ExtensionNotFound):
        certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)


def test_sign_csr_with_sans_requested_includes_them(tmp_path: Path) -> None:
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "xedge-fleet-ca", 3650)
    _private_key_pem, csr_pem = generate_key_and_csr("fleet-manager")

    cert_pem = ca.sign_csr(
        csr_pem,
        common_name="fleet-manager",
        validity_days=90,
        san_dns_names=("fleet-manager.local",),
        san_ip_addresses=(ip_address("127.0.0.1"),),
    )

    certificate = x509.load_pem_x509_certificate(cert_pem)
    san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["fleet-manager.local"]
    assert san.get_values_for_type(x509.IPAddress) == [ip_address("127.0.0.1")]


def test_issue_or_load_leaf_identity_creates_a_cert_signed_by_the_ca(tmp_path: Path) -> None:
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "xedge-fleet-ca", 3650)
    leaf_cert_path, leaf_key_path = tmp_path / "manager.pem", tmp_path / "manager.key"

    ca.issue_or_load_leaf_identity(leaf_cert_path, leaf_key_path, "fleet-manager", 90)

    assert leaf_cert_path.is_file()
    assert leaf_key_path.is_file()
    certificate = x509.load_pem_x509_certificate(leaf_cert_path.read_bytes())
    assert certificate.subject.rfc4514_string() == "CN=fleet-manager"
    assert certificate.issuer == ca.certificate.subject
    private_key = load_pem_private_key(leaf_key_path.read_bytes(), password=None)
    assert isinstance(private_key, rsa.RSAPrivateKey)
    assert private_key.public_key().public_numbers() == certificate.public_key().public_numbers()


def test_issue_or_load_leaf_identity_is_idempotent(tmp_path: Path) -> None:
    ca = load_or_create_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "xedge-fleet-ca", 3650)
    leaf_cert_path, leaf_key_path = tmp_path / "manager.pem", tmp_path / "manager.key"
    ca.issue_or_load_leaf_identity(leaf_cert_path, leaf_key_path, "fleet-manager", 90)
    first_cert = leaf_cert_path.read_bytes()

    ca.issue_or_load_leaf_identity(leaf_cert_path, leaf_key_path, "fleet-manager", 90)

    assert leaf_cert_path.read_bytes() == first_cert


def test_generate_key_and_csr_returns_a_matching_keypair() -> None:
    private_key_pem, csr_pem = generate_key_and_csr("gateway-007")

    private_key = load_pem_private_key(private_key_pem, password=None)
    csr = x509.load_pem_x509_csr(csr_pem)
    assert isinstance(private_key, rsa.RSAPrivateKey)
    assert private_key.public_key().public_numbers() == csr.public_key().public_numbers()
    assert csr.is_signature_valid
