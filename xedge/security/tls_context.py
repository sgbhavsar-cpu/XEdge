"""Client-side TLS `ssl.SSLContext` construction (Sprint C4, XEDGE-441/442):
shared by the fleet agent's heartbeat/rotation client and the MQTT
northbound connector's connection to an external broker.

Deliberately builds one `ssl.SSLContext` up front rather than handing a
consuming library separate cert-path/CA-path parameters: httpx's
`cert=`/`verify=<str>` combination hit a real bug in this project's pinned
httpx/httpcore version — not just the `verify=<str>` deprecation warning it
also emits — confirmed by a from-scratch raw `asyncio`+`ssl` repro showing
the certificates and handshake were fine independent of httpx. A fully
built `SSLContext` handed to `verify=`/`tls_set_context()` sidesteps that
class of bug regardless of which client library ends up consuming it.
`ssl.create_default_context` plus `load_cert_chain` on the same context is
also just how Python's own docs recommend configuring a client-auth
context, independent of that bug.
"""

from __future__ import annotations

import ssl
from pathlib import Path


def build_client_tls_context(
    *,
    ca_certs_path: Path | None = None,
    certfile_path: Path | None = None,
    keyfile_path: Path | None = None,
) -> ssl.SSLContext:
    """General client-side TLS context: verifies the server against
    `ca_certs_path` (the system trust store if `None` — e.g. a broker with
    a publicly-trusted certificate), and optionally presents
    `(certfile_path, keyfile_path)` as a client certificate for mTLS.
    `certfile_path` without `keyfile_path` is valid (a PEM containing
    both, matching `ssl.SSLContext.load_cert_chain`'s own contract)."""
    context = ssl.create_default_context(cafile=str(ca_certs_path) if ca_certs_path else None)
    if certfile_path is not None:
        context.load_cert_chain(str(certfile_path), str(keyfile_path) if keyfile_path else None)
    return context


def build_mtls_client_context(
    cert_path: Path, key_path: Path, ca_cert_path: Path
) -> ssl.SSLContext:
    """Fleet-agent-specific convenience over `build_client_tls_context`:
    for the device port, all three are mandatory — there is no "verify
    against the system trust store" or "no client cert" case once a
    device holds fleet-CA-issued credentials (XEDGE-442)."""
    return build_client_tls_context(
        ca_certs_path=ca_cert_path, certfile_path=cert_path, keyfile_path=key_path
    )
