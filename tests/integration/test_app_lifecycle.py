"""End-to-end smoke test: config load -> logging -> watchdog -> graceful shutdown.

Exercises the same wiring as `xedge --config ...` (XEDGE-004), without
depending on OS signal delivery — asyncio.Event is set directly in place of
waiting on SIGTERM, mirroring how the app behaves in production.
"""

from __future__ import annotations

import asyncio
import ssl
from pathlib import Path

import httpx
import pytest

from xedge.core import main as main_module

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MINIMAL_CONFIG = REPO_ROOT / "config" / "examples" / "modbus-minimal.yaml"
_TEST_API_PORT = 18765
_TEST_HTTPS_API_PORT = 18766
_TEST_MQTT_BROKER_PORT = 18767


async def test_async_main_starts_and_shuts_down_cleanly(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    async def immediate_shutdown() -> None:
        return None

    monkeypatch.setattr(main_module, "_wait_for_shutdown_signal", immediate_shutdown)

    # Override `data_dir` so this doesn't touch the production default
    # (/data) on the machine running the tests — one setting now relocates
    # every persistent path (Sprint 0, XEDGE-402). Overriding only
    # `store.directory`, as this did before, left
    # `config_management.history_directory` pointing at /data/config-history
    # and failed in CI with PermissionError.
    #
    # The REST API is disabled here — its live behavior has its own dedicated
    # test below, and the default port can hit host-specific bind
    # restrictions (e.g. WinError 10013 on some Windows setups) unrelated to
    # this test.
    config_path = tmp_path / "xedge.yaml"
    config_path.write_text(
        MINIMAL_CONFIG.read_text(encoding="utf-8")
        + f"\ndata_dir: {tmp_path}\n"
        + "\napi:\n  enabled: false\n"
        # Only test_async_main_serves_rest_api_over_real_http exercises the
        # real configure_metrics() wiring (with a live /metrics assertion) —
        # every call registers a fresh PrometheusMetricReader against the
        # shared global prometheus_client registry, and since this whole
        # file's tests share one pytest process, repeated calls would pile
        # up duplicate metric registrations across tests that don't
        # otherwise care about metrics.
        + "\nmetrics:\n  enabled: false\n",
        encoding="utf-8",
    )

    exit_code = await asyncio.wait_for(
        main_module.async_main(config_path, main_module._DEFAULT_SCHEMA_PATH),
        timeout=5.0,
    )
    assert exit_code == 0


async def test_async_main_serves_rest_api_over_real_http(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Live smoke test: actually binds uvicorn and makes real HTTP requests
    against it while async_main is running, rather than only exercising
    create_app() in-process (see tests/unit/test_api.py for that)."""
    shutdown_event = asyncio.Event()

    async def wait_for_test_shutdown() -> None:
        await shutdown_event.wait()

    monkeypatch.setattr(main_module, "_wait_for_shutdown_signal", wait_for_test_shutdown)

    config_path = tmp_path / "xedge.yaml"
    config_path.write_text(
        MINIMAL_CONFIG.read_text(encoding="utf-8")
        + f"\ndata_dir: {tmp_path}\n"
        + f"\napi:\n  port: {_TEST_API_PORT}\n"
        # tls.enabled now defaults to true (Sprint 13, XEDGE-107/280) — this
        # test is specifically about the plain-HTTP path, so it opts out;
        # see test_async_main_serves_rest_api_over_https for the HTTPS path.
        + "\ntls:\n  enabled: false\n",
        encoding="utf-8",
    )

    task = asyncio.create_task(
        main_module.async_main(config_path, main_module._DEFAULT_SCHEMA_PATH)
    )
    try:
        base_url = f"http://127.0.0.1:{_TEST_API_PORT}"
        async with httpx.AsyncClient() as client:
            response = None
            for _ in range(100):
                try:
                    response = await client.get(f"{base_url}/health", timeout=0.5)
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                pytest.fail("REST API never became reachable")
            assert response is not None
            assert response.json() == {"status": "ok"}

            # First-login setup (ADR-007): protected endpoints require an
            # authenticated session now that the API is write-capable.
            setup_response = await client.post(
                f"{base_url}/api/v1/auth/setup", json={"password": "test-password-123"}
            )
            assert setup_response.status_code == 200

            status_response = await client.get(f"{base_url}/api/v1/status")
            assert status_response.status_code == 200
            assert "version" in status_response.json()

            drivers_response = await client.get(f"{base_url}/api/v1/drivers")
            assert drivers_response.status_code == 200

            config_response = await client.get(f"{base_url}/api/v1/config")
            assert config_response.status_code == 200

            # Prometheus metrics (Sprint 16, XEDGE-128/130): unauthenticated,
            # served from the same app the rest of this test already booted.
            # modbus-minimal.yaml's one driver is disabled, so there are no
            # per-driver observations here (see test_otel_metrics.py for
            # that, with a real driver/ring-buffer present) — this only
            # proves the endpoint is live and serving valid exposition
            # format through the real running app.
            metrics_response = await client.get(f"{base_url}/metrics")
            assert metrics_response.status_code == 200
            assert "# HELP" in metrics_response.text
            assert "# TYPE" in metrics_response.text
    finally:
        shutdown_event.set()
        exit_code = await asyncio.wait_for(task, timeout=5.0)
        assert exit_code == 0


async def test_async_main_serves_rest_api_over_https(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Live smoke test for Sprint 13 (XEDGE-107/280): tls.enabled defaults to
    true, so a fresh instance auto-generates a self-signed certificate and
    serves the Web UI/REST API over HTTPS out of the box — verified here by
    connecting a real httpx client with that self-signed cert as its own
    trust anchor (`verify=<cert path>`), the same way a browser would need
    to explicitly trust it on first visit."""
    shutdown_event = asyncio.Event()

    async def wait_for_test_shutdown() -> None:
        await shutdown_event.wait()

    monkeypatch.setattr(main_module, "_wait_for_shutdown_signal", wait_for_test_shutdown)

    config_path = tmp_path / "xedge.yaml"
    config_path.write_text(
        MINIMAL_CONFIG.read_text(encoding="utf-8")
        + f"\ndata_dir: {tmp_path}\n"
        + f"\napi:\n  port: {_TEST_HTTPS_API_PORT}\n"
        + "\nmetrics:\n  enabled: false\n",
        encoding="utf-8",
    )
    cert_path = tmp_path / "webui" / "server_cert.pem"

    task = asyncio.create_task(
        main_module.async_main(config_path, main_module._DEFAULT_SCHEMA_PATH)
    )
    try:
        base_url = f"https://127.0.0.1:{_TEST_HTTPS_API_PORT}"
        for _ in range(100):
            if cert_path.is_file():
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("TLS certificate was never generated")

        ssl_context = ssl.create_default_context(cafile=str(cert_path))
        async with httpx.AsyncClient(verify=ssl_context) as client:
            response = None
            for _ in range(100):
                try:
                    response = await client.get(f"{base_url}/health", timeout=0.5)
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                pytest.fail("HTTPS REST API never became reachable")
            assert response is not None
            assert response.json() == {"status": "ok"}

            setup_response = await client.post(
                f"{base_url}/api/v1/auth/setup", json={"password": "test-password-123"}
            )
            assert setup_response.status_code == 200
            assert "Secure" in setup_response.headers["set-cookie"]

            status_response = await client.get(f"{base_url}/api/v1/status")
            assert status_response.status_code == 200
    finally:
        shutdown_event.set()
        exit_code = await asyncio.wait_for(task, timeout=5.0)
        assert exit_code == 0


async def test_async_main_survives_rest_api_bind_failure(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Regression test: uvicorn.Server.serve() calls sys.exit() internally
    on a bind failure (e.g. port already in use, or — as discovered during
    manual verification — a port already reserved system-wide by another
    service, like Windows IIS/http.sys on 8080), raising SystemExit — a
    BaseException — inside an unawaited asyncio.Task. Left unguarded, that
    crashed the entire xedge process (driver polling, northbound
    publishing, everything). The REST API failing to start must not take
    down the rest of the app.

    Synthesizes the failure directly (monkeypatching uvicorn.Server.serve)
    rather than relying on a real OS-level port conflict: what's under test
    is xedge's own exception-isolation code in _run_api_server, not uvicorn
    or OS/platform port-binding semantics, which vary too much across
    platforms (e.g. Windows doesn't reliably reject binds to low "privileged"
    ports the way POSIX does, and Windows SO_REUSEADDR semantics make a
    real same-process socket-conflict reproduction unreliable)."""
    import uvicorn

    def _fake_serve(self: uvicorn.Server) -> None:  # type: ignore[no-untyped-def]
        raise SystemExit(3)  # uvicorn.server.STARTUP_FAILURE

    monkeypatch.setattr(uvicorn.Server, "serve", _fake_serve)

    shutdown_event = asyncio.Event()

    async def wait_for_test_shutdown() -> None:
        await shutdown_event.wait()

    monkeypatch.setattr(main_module, "_wait_for_shutdown_signal", wait_for_test_shutdown)

    config_path = tmp_path / "xedge.yaml"
    config_path.write_text(
        MINIMAL_CONFIG.read_text(encoding="utf-8")
        + f"\ndata_dir: {tmp_path}\n"
        + "\nmetrics:\n  enabled: false\n",
        encoding="utf-8",
    )

    task = asyncio.create_task(
        main_module.async_main(config_path, main_module._DEFAULT_SCHEMA_PATH)
    )
    try:
        # Give the (synthetic) API startup failure time to fire before
        # checking whether the app is still alive and well.
        await asyncio.sleep(0.5)
        assert not task.done(), (
            "async_main crashed instead of surviving the REST API startup failure "
            f"(exception: {task.exception() if task.done() else None})"
        )
    finally:
        shutdown_event.set()
        exit_code = await asyncio.wait_for(task, timeout=5.0)
        assert exit_code == 0


async def test_async_main_starts_the_embedded_mqtt_broker_when_enabled(monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    """XEDGE-453: main.py owns this service's start/stop lifecycle the same
    way it owns the OPC UA server's. Confirms the actual async_main wiring
    end-to-end (import, build+start in the startup sequence, stop in the
    shutdown sequence) -- MqttBrokerService's own behavior (auth, ACLs,
    TLS) is covered in depth by tests/integration/test_mqtt_broker.py."""
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion

    shutdown_event = asyncio.Event()

    async def wait_for_test_shutdown() -> None:
        await shutdown_event.wait()

    monkeypatch.setattr(main_module, "_wait_for_shutdown_signal", wait_for_test_shutdown)

    config_path = tmp_path / "xedge.yaml"
    config_path.write_text(
        MINIMAL_CONFIG.read_text(encoding="utf-8")
        + f"\ndata_dir: {tmp_path}\n"
        + "\napi:\n  enabled: false\n"
        + "\nmetrics:\n  enabled: false\n"
        + "\nmqtt_broker:\n"
        + "  enabled: true\n"
        + f"  port: {_TEST_MQTT_BROKER_PORT}\n"
        + "  allow_anonymous: true\n",
        encoding="utf-8",
    )

    task = asyncio.create_task(
        main_module.async_main(config_path, main_module._DEFAULT_SCHEMA_PATH)
    )
    try:
        connected = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_connect(c, userdata, flags, reason_code, properties=None):  # type: ignore[no-untyped-def]
            loop.call_soon_threadsafe(connected.set)

        client = mqtt.Client(CallbackAPIVersion.VERSION2, reconnect_on_failure=False)
        client.on_connect = on_connect
        # Retries a real MQTT connect attempt rather than probing the port
        # with a bare TCP connect-and-close first: amqtt's Broker.shutdown()
        # can hang indefinitely if any connection ever reached the listener
        # without completing a handshake (confirmed directly against
        # amqtt.broker.Broker -- see xedge.northbound.mqtt_broker's module
        # docstring), which a bare probe here would trigger for this test's
        # own later shutdown.
        for _ in range(100):
            try:
                await asyncio.to_thread(client.connect, "127.0.0.1", _TEST_MQTT_BROKER_PORT, 10)
                break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            pytest.fail("embedded MQTT broker never became reachable")
        client.loop_start()
        try:
            await asyncio.wait_for(connected.wait(), timeout=3.0)
        finally:
            client.loop_stop()
            client.disconnect()
    finally:
        shutdown_event.set()
        exit_code = await asyncio.wait_for(task, timeout=5.0)
        assert exit_code == 0
