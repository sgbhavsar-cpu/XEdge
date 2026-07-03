"""End-to-end smoke test: config load -> logging -> watchdog -> graceful shutdown.

Exercises the same wiring as `xedge --config ...` (XEDGE-004), without
depending on OS signal delivery — asyncio.Event is set directly in place of
waiting on SIGTERM, mirroring how the app behaves in production.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from xedge.core import main as main_module

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MINIMAL_CONFIG = REPO_ROOT / "config" / "examples" / "modbus-minimal.yaml"


async def test_async_main_starts_and_shuts_down_cleanly(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def immediate_shutdown() -> None:
        return None

    monkeypatch.setattr(main_module, "_wait_for_shutdown_signal", immediate_shutdown)

    exit_code = await asyncio.wait_for(
        main_module.async_main(MINIMAL_CONFIG, main_module._DEFAULT_SCHEMA_PATH),
        timeout=5.0,
    )
    assert exit_code == 0
