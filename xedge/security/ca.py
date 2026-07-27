"""Fleet certificate authority (Sprint C4, XEDGE-440; ADR-013 §4): a single
in-house CA signs both device identity certificates (issued during fleet
enrollment, XEDGE-442) and the Fleet Manager's own mTLS server identity —
one CA, one trust store, per ADR-013's "two consumers, built once" rationale
(no clean-room concern here, unlike ADR-006's protocol stacks: this is
xEdge's own PKI, not a reimplementation of a third-party spec).

Same "load if it already exists, else generate and persist" shape as
`xedge.api.tls.load_or_create_server_certificate` — the CA keypair is
generated once and never silently rotates.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from xedge.security.csr import generate_key_and_csr

# The CA key is long-lived (years) and signs every device and manager
# identity in the fleet, so it gets a larger key than the leaf certificates
# it issues (xedge.api.tls's / xedge.security.csr's 2048-bit convention) —
# a compromise of this key is fleet-wide, not single-device.
_CA_RSA_KEY_SIZE = 4096
_RSA_PUBLIC_EXPONENT = 65537


class InvalidCsrError(Exception):
    """Raised by `CertificateAuthority.sign_csr` for a structurally invalid
    CSR, or one whose self-signature doesn't match its own public key."""


class CertificateAuthority:
    """A loaded fleet CA: its own keypair/certificate, plus the ability to
    sign CSRs against it. Construct via `load_or_create`, not directly."""

    def __init__(self, private_key: rsa.RSAPrivateKey, certificate: x509.Certificate) -> None:
        self._private_key = private_key
        self.certificate = certificate

    @property
    def certificate_pem(self) -> bytes:
        return self.certificate.public_bytes(serialization.Encoding.PEM)

    def sign_csr(
        self,
        csr_pem: bytes,
        *,
        common_name: str,
        validity_days: int,
        san_dns_names: tuple[str, ...] = (),
        san_ip_addresses: tuple[IPv4Address | IPv6Address, ...] = (),
    ) -> bytes:
        """Verify `csr_pem`'s self-signature (proof the requester holds the
        private key for the enclosed public key), then issue a leaf
        certificate for that public key — under a subject *this call
        specifies*, never whatever subject the CSR itself claims. A CSR is
        unauthenticated input until the join-token or mTLS check around
        this call vouches for it (XEDGE-442/443); trusting its embedded CN
        would let anyone request any identity.

        `san_dns_names`/`san_ip_addresses` matter only for a certificate a
        *client* will verify by hostname — the Fleet Manager's own server
        identity (`issue_or_load_leaf_identity`). A device's own client
        certificate has no such use (mTLS authenticates the server to the
        client by hostname; it never authenticates the client to the
        server that way), so `sign_csr` calls issuing one simply omit them —
        matching `xedge.api.tls.load_or_create_server_certificate`'s
        existing SAN convention rather than inventing a second one."""
        csr = x509.load_pem_x509_csr(csr_pem)
        if not csr.is_signature_valid:
            raise InvalidCsrError("CSR signature does not match its own public key")

        now = datetime.now(UTC)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(self.certificate.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=validity_days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        )
        if san_dns_names or san_ip_addresses:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(
                    [x509.DNSName(name) for name in san_dns_names]
                    + [x509.IPAddress(ip) for ip in san_ip_addresses]
                ),
                critical=False,
            )
        certificate = builder.sign(self._private_key, hashes.SHA256())
        return certificate.public_bytes(serialization.Encoding.PEM)

    def issue_or_load_leaf_identity(
        self,
        cert_path: Path,
        key_path: Path,
        common_name: str,
        validity_days: int,
        *,
        san_dns_names: tuple[str, ...] = (),
        san_ip_addresses: tuple[IPv4Address | IPv6Address, ...] = (),
    ) -> None:
        """Ensure `cert_path`/`key_path` hold a certificate this CA issued
        for `common_name`, generating and signing one locally — no CSR
        network round-trip needed, since this CA already holds its own
        signing key — if either file is missing. For identities the CA's
        own operator holds directly, e.g. the Fleet Manager's own mTLS
        server identity on the device listener (XEDGE-440). A *device*'s
        identity always goes through `sign_csr` over the network instead
        (XEDGE-442): its private key must never leave the device."""
        if cert_path.is_file() and key_path.is_file():
            return
        private_key_pem, csr_pem = generate_key_and_csr(common_name)
        cert_pem = self.sign_csr(
            csr_pem,
            common_name=common_name,
            validity_days=validity_days,
            san_dns_names=san_dns_names,
            san_ip_addresses=san_ip_addresses,
        )
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.parent.mkdir(parents=True, exist_ok=True)
        cert_path.write_bytes(cert_pem)
        key_path.write_bytes(private_key_pem)


def load_or_create_ca(
    cert_path: Path, key_path: Path, common_name: str, validity_days: int
) -> CertificateAuthority:
    """Ensure `cert_path`/`key_path` hold a matching self-signed CA keypair,
    generating one if either file is missing, and return it loaded. Unlike
    `xedge.api.tls.load_or_create_server_certificate` (which only needs to
    guarantee the files exist — uvicorn reads them by path), this CA is
    used to sign CSRs for as long as the manager process runs, so the
    keypair must come back in memory either way."""
    if cert_path.is_file() and key_path.is_file():
        private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise TypeError(f"fleet CA key at {key_path} must be RSA, found {type(private_key)}")
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        return CertificateAuthority(private_key, certificate)

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    private_key = rsa.generate_private_key(
        public_exponent=_RSA_PUBLIC_EXPONENT, key_size=_CA_RSA_KEY_SIZE
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
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
    return CertificateAuthority(private_key, certificate)
