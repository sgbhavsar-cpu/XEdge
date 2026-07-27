"""Client-side mTLS `ssl.SSLContext` construction (Sprint C4, XEDGE-442),
shared between the fleet agent's heartbeat/rotation client and anything
else presenting a certificate this fleet's CA issued.

Deliberately builds one `ssl.SSLContext` and passes it to httpx as
`verify=<context>`, rather than httpx's separate `cert=`/`verify=<path>`
parameters: combined, those two hit a real bug in this project's pinned
httpx/httpcore version — not just the `verify=<str>` deprecation warning
it also emits — confirmed by a from-scratch raw `asyncio`+`ssl` repro
showing the certificates and handshake are fine independent of httpx.
`ssl.create_default_context` plus `load_cert_chain` on the same context is
also just how Python's own docs recommend configuring a client-auth
context, independent of that bug.
"""

from __future__ import annotations

import ssl
from pathlib import Path


def build_mtls_client_context(
    cert_path: Path, key_path: Path, ca_cert_path: Path
) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(ca_cert_path))
    context.load_cert_chain(str(cert_path), str(key_path))
    return context
