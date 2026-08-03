"""Device-side keypair + CSR generation (Sprint C4, XEDGE-440/442; ADR-013
§3): shared by the fleet agent (enrolling with the manager's CA) and the
Fleet Manager itself (its own mTLS server identity is just another leaf
certificate issued by the same CA it operates).

The private key generated here never leaves the caller's process; only the
CSR (public key plus a self-signature proving possession of the private
key) is meant to be transmitted, matching ADR-013 §3's "the private key
never leaves the device."
"""

from __future__ import annotations

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# Matches xedge.api.tls's existing leaf-certificate convention.
_RSA_KEY_SIZE = 2048
_RSA_PUBLIC_EXPONENT = 65537


def generate_key_and_csr(common_name: str) -> tuple[bytes, bytes]:
    """Return `(private_key_pem, csr_pem)` for a freshly generated RSA
    keypair. The CSR's subject is advisory only — whoever signs it
    (`xedge.security.ca.CertificateAuthority.sign_csr`) decides the
    identity actually granted, so a caller cannot self-assert an identity
    just by putting it in the CSR."""
    private_key = rsa.generate_private_key(
        public_exponent=_RSA_PUBLIC_EXPONENT, key_size=_RSA_KEY_SIZE
    )
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .sign(private_key, hashes.SHA256())
    )
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    return private_key_pem, csr_pem
