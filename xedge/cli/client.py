"""`xedge-cli`: a thin, one-shot remote diagnostic client (Sprint 17,
XEDGE-140) — connects to a running xEdge instance's diagnostic WebSocket
(`xedge.api.diagnostics`), reusing the same session-cookie auth the Web UI
and REST API already use (log in once via `POST /api/v1/auth/login`,
attach the resulting cookie to the WebSocket handshake).

Deliberately one-shot per invocation (connect, log in, send exactly one
command, print the response, disconnect) rather than an interactive REPL —
simpler, scriptable, standard Unix CLI ergonomics. The Web UI's own
embedded console (`xedge.api.ui`'s `/ui/diagnostics`) covers the
persistent/interactive use case for a browser session already logged in.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import ssl
import sys
from typing import Any

import httpx
import websockets

from xedge import __version__
from xedge.api.auth import SESSION_COOKIE_NAME

_DEFAULT_BASE_URL = "https://127.0.0.1:8080"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="xedge-cli", description="Remote diagnostic client for a running xEdge instance"
    )
    parser.add_argument(
        "--base-url", default=_DEFAULT_BASE_URL, help=f"xEdge base URL (default: {_DEFAULT_BASE_URL})"
    )
    parser.add_argument("--username", default="admin", help="Account username (default: admin)")
    parser.add_argument(
        "--password", default=None, help="Account password (prompted securely if omitted)"
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (for the auto-generated self-signed dev cert)",
    )
    parser.add_argument("--version", action="version", version=f"xedge-cli {__version__}")
    parser.add_argument("command", nargs="+", help="e.g. status | driver restart <id> | self-test")
    return parser.parse_args(argv)


class CliError(Exception):
    """A reportable failure (login rejected, connection refused, command
    denied) — printed as a clean error message, never a raw traceback."""


async def run_command(
    base_url: str, username: str, password: str, command: list[str], *, insecure: bool
) -> dict[str, Any]:
    verify: bool | str = not insecure
    async with httpx.AsyncClient(base_url=base_url, verify=verify) as http_client:
        response = await http_client.post(
            "/api/v1/auth/login", json={"username": username, "password": password}
        )
        if response.status_code != 200:
            raise CliError(f"Login failed: {response.status_code} {response.text}")
        token = response.cookies.get(SESSION_COOKIE_NAME)
        if not token:
            raise CliError("Login succeeded but no session cookie was returned")

    ws_scheme = "wss" if base_url.startswith("https") else "ws"
    ws_url = f"{ws_scheme}://{base_url.split('://', 1)[1]}/ws/diagnostics"
    ssl_context: ssl.SSLContext | None = None
    if ws_scheme == "wss":
        ssl_context = ssl.create_default_context()
        if insecure:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

    async with websockets.connect(
        ws_url,
        additional_headers={"Cookie": f"{SESSION_COOKIE_NAME}={token}"},
        ssl=ssl_context,
    ) as ws:
        await ws.send(json.dumps({"command": command[0], "args": command[1:]}))
        raw_response = await ws.recv()
        result: dict[str, Any] = json.loads(raw_response)
        return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    password = args.password or getpass.getpass(f"Password for {args.username}: ")
    try:
        result = asyncio.run(
            run_command(
                args.base_url, args.username, password, args.command, insecure=args.insecure
            )
        )
    except (httpx.HTTPError, OSError, CliError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0 if result.get("status") == "ok" else 1


def run(argv: list[str] | None = None) -> int:
    """Synchronous entrypoint (registered as the `xedge-cli` console script)."""
    return main(argv)


if __name__ == "__main__":
    sys.exit(run())
