"""Test-only SMTP server for xedge.core.smtp's tests, backed by aiosmtpd
(Apache-2.0) — see ADR-006 / pyproject.toml test extras. Python 3.12
removed the stdlib `smtpd` module this project would otherwise have used
for exactly this role (a real local server to exercise a client against,
same reasoning as pymodbus/amqtt) — aiosmtpd is that role's direct
replacement, not a new testing philosophy.
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, Envelope, LoginPassword, Session

from xedge.api.tls import load_or_create_server_certificate


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class CapturedEmail:
    mail_from: str | None
    rcpt_tos: list[str]
    content: bytes


class _CapturingHandler:
    def __init__(self) -> None:
        self.messages: list[CapturedEmail] = []

    async def handle_DATA(self, server: Any, session: Session, envelope: Envelope) -> str:
        self.messages.append(
            CapturedEmail(envelope.mail_from, list(envelope.rcpt_tos), envelope.content)  # type: ignore[arg-type]
        )
        return "250 OK"


@dataclass
class FakeSmtpServer:
    host: str
    port: int
    _handler: _CapturingHandler
    ca_cert_path: Path | None = field(default=None)
    """Set only by `smtp_server_starttls` — the self-signed cert a
    connecting client should verify the server against."""

    @property
    def messages(self) -> list[CapturedEmail]:
        return self._handler.messages


@pytest.fixture
async def smtp_server() -> AsyncIterator[FakeSmtpServer]:
    """Plaintext, no authentication required — the common "internal relay
    on a trusted network" case."""
    handler = _CapturingHandler()
    host, port = "127.0.0.1", free_port()
    controller = Controller(handler, hostname=host, port=port)
    controller.start()
    try:
        yield FakeSmtpServer(host, port, handler)
    finally:
        controller.stop()


def _make_authenticator(username: str, password: str) -> Any:
    expected_login = username.encode("ascii")
    expected_password = password.encode("ascii")

    def authenticator(
        server: Any, session: Session, envelope: Envelope, mechanism: str, auth_data: Any
    ) -> AuthResult:
        if not isinstance(auth_data, LoginPassword):
            return AuthResult(success=False)
        ok = auth_data.login == expected_login and auth_data.password == expected_password
        return AuthResult(success=ok)

    return authenticator


# Fixed rather than parameterized: every test using either auth fixture
# below just needs *some* real credential pair to exercise the login
# path against, not a specific value of its own choosing.
SMTP_TEST_USERNAME = "test-user"
SMTP_TEST_PASSWORD = "test-pass"


@pytest.fixture
async def smtp_server_requiring_auth() -> AsyncIterator[FakeSmtpServer]:
    handler = _CapturingHandler()
    host, port = "127.0.0.1", free_port()
    controller = Controller(
        handler,
        hostname=host,
        port=port,
        auth_required=True,
        authenticator=_make_authenticator(SMTP_TEST_USERNAME, SMTP_TEST_PASSWORD),
        auth_require_tls=False,
    )
    controller.start()
    try:
        yield FakeSmtpServer(host, port, handler)
    finally:
        controller.stop()


@pytest.fixture
async def smtp_server_starttls(tmp_path: Path) -> AsyncIterator[FakeSmtpServer]:
    """Requires STARTTLS before AUTH/DATA -- proves `tls_mode: starttls`
    against a real TLS handshake, not just a plaintext round trip."""
    cert_path, key_path = tmp_path / "smtp-cert.pem", tmp_path / "smtp-key.pem"
    load_or_create_server_certificate(cert_path, key_path, "127.0.0.1", 90)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(cert_path), str(key_path))

    handler = _CapturingHandler()
    host, port = "127.0.0.1", free_port()
    controller = Controller(
        handler,
        hostname=host,
        port=port,
        tls_context=server_context,
        require_starttls=True,
        auth_required=True,
        authenticator=_make_authenticator(SMTP_TEST_USERNAME, SMTP_TEST_PASSWORD),
    )
    controller.start()
    try:
        yield FakeSmtpServer(host, port, handler, ca_cert_path=cert_path)
    finally:
        controller.stop()
