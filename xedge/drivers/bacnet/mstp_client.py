"""BACnet MS/TP client driver (Sprint P7, XEDGE-166/171).

Unlike `xedge.drivers.bacnet.client.BacnetIpDriver` (built directly on
`bacpypes3`, which has no MS/TP implementation at all -- see
`docs/planning/license-audit.md` §4 item 11), this driver never speaks
MS/TP itself. It supervises a standalone C daemon
(`xedge/drivers/bacnet/mstp_daemon/`, linked against the vendored
`third_party/bacnet-stack`) that owns the actual MS/TP token-passing
lifecycle for one RS-485 port, and talks to it over a local Unix domain
socket -- one daemon subprocess per driver instance, matching the
Sprint P7 architecture decision (daemon-per-port over IPC, not an
in-process C binding).

A daemon crash or a broken socket surfaces here as `DriverConnectionError`
from `connect()`/mid-`run()`, exactly like any other driver's connection
failure -- `DriverSupervisor`'s existing restart-with-backoff handles
respawning the daemon the same way it handles any other driver
reconnect, so this driver adds no new supervisory concept of its own.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opentelemetry.trace import Status, StatusCode

from xedge.drivers.base import (
    BaseDriver,
    DriverConfig,
    DriverConnectionError,
    DriverMetrics,
    Quality,
    TagUpdate,
    TagValue,
    WriteResult,
)
from xedge.observability.logging import get_logger
from xedge.observability.tracing import get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

_DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
_DEFAULT_PROPERTY_ID = "present-value"
_DEFAULT_BAUD_RATE = 38400
_DEFAULT_MAX_INFO_FRAMES = 1
_DEFAULT_MAX_MASTER = 127
_DEFAULT_DAEMON_PATH = "xedge-bacnet-mstp-daemon"
_SOCKET_READY_TIMEOUT_SECONDS = 10.0
_DAEMON_TERMINATE_TIMEOUT_SECONDS = 5.0

_BINARY_OBJECT_TYPES = frozenset({"binary-input", "binary-output", "binary-value"})


class BacnetMstpDriverStateError(RuntimeError):
    """Raised when a lifecycle method is called out of order."""


class BacnetMstpDriver(BaseDriver):
    def __init__(self) -> None:
        self._config: DriverConfig | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._socket_path: Path | None = None
        self._request_lock = asyncio.Lock()
        self._metrics = DriverMetrics()

    def _require_config(self) -> DriverConfig:
        if self._config is None:
            raise BacnetMstpDriverStateError("configure() must be called before this operation")
        return self._config

    async def configure(self, config: DriverConfig) -> None:
        self._config = config

    async def connect(self) -> None:
        cfg = self._require_config()
        options = cfg.config
        port = options["port"]
        mac_address = options["mac_address"]
        daemon_path = options.get("daemon_path", _DEFAULT_DAEMON_PATH)

        socket_name = f"xedge-bacnet-mstp-{uuid.uuid4().hex}.sock"
        self._socket_path = Path(tempfile.gettempdir()) / socket_name
        args = [
            "--iface", str(port),
            "--mac", str(mac_address),
            "--baud", str(options.get("baud_rate", _DEFAULT_BAUD_RATE)),
            "--max-info-frames", str(options.get("max_info_frames", _DEFAULT_MAX_INFO_FRAMES)),
            "--max-master", str(options.get("max_master", _DEFAULT_MAX_MASTER)),
            "--device-instance", str(options["device_instance"]),
            "--socket", str(self._socket_path),
        ]  # fmt: skip
        try:
            self._process = await asyncio.create_subprocess_exec(
                daemon_path, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
        except OSError as exc:
            raise DriverConnectionError(
                f"failed to start BACnet MS/TP daemon {daemon_path!r}: {exc}"
            ) from exc

        deadline = asyncio.get_running_loop().time() + _SOCKET_READY_TIMEOUT_SECONDS
        while not self._socket_path.exists():
            if self._process.returncode is not None:
                output = await self._drain_daemon_output()
                raise DriverConnectionError(
                    f"BACnet MS/TP daemon exited during startup (code {self._process.returncode}): "
                    f"{output}"
                )
            if asyncio.get_running_loop().time() > deadline:
                await self._kill_process()
                raise DriverConnectionError(
                    f"BACnet MS/TP daemon did not create its socket within "
                    f"{_SOCKET_READY_TIMEOUT_SECONDS}s"
                )
            await asyncio.sleep(0.1)

        try:
            self._reader, self._writer = await asyncio.open_unix_connection(str(self._socket_path))
        except OSError as exc:
            await self._kill_process()
            raise DriverConnectionError(
                f"failed to connect to BACnet MS/TP daemon socket: {exc}"
            ) from exc

    async def _drain_daemon_output(self) -> str:
        if self._process is None or self._process.stdout is None:
            return ""
        data = await self._process.stdout.read()
        return data.decode(errors="replace").strip()

    async def _kill_process(self) -> None:
        if self._process is None:
            return
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(
                    self._process.wait(), timeout=_DAEMON_TERMINATE_TIMEOUT_SECONDS
                )
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        self._process = None

    async def disconnect(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
            self._reader = None
        await self._kill_process()
        if self._socket_path is not None:
            self._socket_path.unlink(missing_ok=True)
            self._socket_path = None

    async def run(self, output: asyncio.Queue[TagUpdate]) -> None:
        config = self._require_config()
        group_tasks = [
            asyncio.create_task(self._poll_group(group, output)) for group in config.tag_groups
        ]
        try:
            await asyncio.gather(*group_tasks)
        finally:
            for task in group_tasks:
                task.cancel()
            await asyncio.gather(*group_tasks, return_exceptions=True)

    async def write(self, tag_id: str, value: TagValue) -> WriteResult:
        # WriteProperty has no caller anywhere in xEdge yet, matching every
        # other driver's current scope (including the BACnet/IP driver).
        return WriteResult(success=False, tag_id=tag_id, error_message="write not yet supported")

    def get_metrics(self) -> DriverMetrics:
        return self._metrics

    async def _poll_group(self, group: dict[str, Any], output: asyncio.Queue[TagUpdate]) -> None:
        instance_id = self._require_config().instance_id
        interval_seconds = group["scan_rate_ms"] / 1000
        timeout_seconds = self._require_config().config.get(
            "request_timeout_seconds", _DEFAULT_REQUEST_TIMEOUT_SECONDS
        )
        tags = group["tags"]
        while True:
            for tag in tags:
                update = await self._read_tag(instance_id, tag, timeout_seconds)
                await output.put(update)
            await asyncio.sleep(interval_seconds)

    async def _read_tag(
        self, instance_id: str, tag: dict[str, Any], timeout_seconds: float
    ) -> TagUpdate:
        tag_id = f"{instance_id}/{tag['id']}"
        object_type = tag["object_type"]
        source_address = f"{tag['device_instance']}/{object_type}:{tag['object_instance']}"
        with tracer.start_as_current_span(
            "driver.read",
            attributes={"driver.instance_id": instance_id, "tag.id": tag["id"]},
        ) as span:
            try:
                response = await asyncio.wait_for(self._request(tag), timeout=timeout_seconds)
                if not response.get("ok"):
                    raise BacnetMstpRequestError(str(response.get("error", "unknown error")))
                value = _coerce_value(response["value"], object_type, tag.get("property_id"))
                self._metrics.tag_read_count += 1
                self._metrics.last_successful_read = datetime.now(UTC)
                span.set_attribute("quality", Quality.GOOD.value)
                return TagUpdate(
                    tag_id=tag_id,
                    timestamp=datetime.now(UTC),
                    value=value,
                    quality=Quality.GOOD,
                    source_driver=instance_id,
                    source_address=source_address,
                )
            # DriverConnectionError (raised by _request() when the daemon
            # has closed the connection) is deliberately *not* caught
            # here -- unlike the per-tag failures below, a dead daemon
            # breaks every subsequent read on this connection, not just
            # this one, so it propagates out of run() for the supervisor
            # to treat as a connection failure and reconnect (respawning
            # the daemon), rather than becoming one Bad-quality TagUpdate
            # while every following read keeps failing the same way.
            except (BacnetMstpRequestError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                self._metrics.error_count += 1
                span.set_attribute("quality", Quality.BAD.value)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
                logger.warning(
                    "bacnet_mstp.read_failed",
                    instance_id=instance_id,
                    tag_id=tag["id"],
                    error=str(exc),
                )
                return TagUpdate(
                    tag_id=tag_id,
                    timestamp=datetime.now(UTC),
                    value=0,
                    quality=Quality.BAD,
                    source_driver=instance_id,
                    source_address=source_address,
                    metadata={"bacnet_mstp_error": str(exc)},
                )

    async def _request(self, tag: dict[str, Any]) -> dict[str, Any]:
        if self._reader is None or self._writer is None:
            raise DriverConnectionError("not connected to the BACnet MS/TP daemon")
        request = {
            "device_instance": tag["device_instance"],
            "mac": tag["mac_address"],
            "object_type": tag["object_type"],
            "object_instance": tag["object_instance"],
            "property_id": tag.get("property_id", _DEFAULT_PROPERTY_ID),
        }
        # One request in flight at a time (the daemon's own design, see
        # mstp_daemon/README.md) -- this lock is what makes that safe to
        # rely on even though multiple tag-group poll loops share one
        # daemon connection.
        async with self._request_lock:
            self._writer.write((json.dumps(request) + "\n").encode())
            await self._writer.drain()
            line = await self._reader.readline()
        if not line:
            raise DriverConnectionError("BACnet MS/TP daemon closed the connection")
        result: dict[str, Any] = json.loads(line)
        return result


class BacnetMstpRequestError(Exception):
    """Raised for a well-formed {"ok": false, "error": ...} daemon response."""


def _coerce_value(value: Any, object_type: str, property_id: str | None) -> TagValue:
    """The daemon emits plain JSON types (bool/int/float/str) with no
    semantic coercion (mstp_daemon/main.c's own design choice -- see its
    module docstring). The one coercion applied here, mirroring
    `xedge.drivers.bacnet.client._coerce_value`: a binary object's
    present-value is BACnet's Enumerated tag on the wire (0/1), not a
    Boolean tag, so the daemon reports it as a plain number -- becomes a
    real bool here, the same way bacpypes3's BinaryPV is special-cased
    for the IP driver."""
    is_binary_present_value = (
        object_type in _BINARY_OBJECT_TYPES
        and (property_id or _DEFAULT_PROPERTY_ID) == "present-value"
    )
    if is_binary_present_value and isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(int(value))
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)
