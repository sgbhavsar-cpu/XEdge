"""SMTP client (Sprint C6, XEDGE-466) against a real local server
(aiosmtpd, see tests/fixtures/smtp_server.py) — same "test against real
infrastructure, not mocks" pattern as every other protocol client in
this codebase, applied to the one place Python 3.12 removed the stdlib
tool (`smtpd`) this project would otherwise have reached for.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from tests.fixtures.smtp_server import (
    SMTP_TEST_PASSWORD,
    SMTP_TEST_USERNAME,
    FakeSmtpServer,
)
from xedge.core.alarms import AlarmEngine, AlarmRule
from xedge.core.pipeline import UnifiedTag
from xedge.core.smtp import (
    AlarmNotificationConfig,
    ScheduledReportConfig,
    SmtpConfig,
    SmtpStatus,
    alarm_notification_loop,
    scheduled_report_loop,
    send_email,
)
from xedge.drivers.base import Quality
from xedge.store.latest_values import LatestValueStore


def _tag(tag_id: str, value: object) -> UnifiedTag:
    return UnifiedTag(
        tag_id=tag_id,
        timestamp=datetime.now(UTC),
        value=value,  # type: ignore[arg-type]
        data_type="FLOAT64",
        quality=Quality.GOOD,
        source_driver=tag_id.split("/")[0],
        source_address="0",
    )


async def _wait_until(predicate, attempts: int = 200, interval: float = 0.02) -> None:  # type: ignore[no-untyped-def]
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


class TestSendEmail:
    async def test_plaintext_send_is_captured_by_the_real_server(
        self, smtp_server: FakeSmtpServer
    ) -> None:
        config = SmtpConfig(
            host=smtp_server.host,
            port=smtp_server.port,
            from_address="xedge@example.com",
            tls_mode="none",
        )
        status = SmtpStatus()

        await send_email(config, ["ops@example.com"], "Test Subject", "Test body", status)

        assert len(smtp_server.messages) == 1
        message = smtp_server.messages[0]
        assert message.mail_from == "xedge@example.com"
        assert message.rcpt_tos == ["ops@example.com"]
        assert b"Test Subject" in message.content
        assert b"Test body" in message.content
        assert status.last_send_ok is True
        assert status.emails_sent == 1
        assert status.last_error is None

    async def test_correct_credentials_authenticate_and_send(
        self, smtp_server_requiring_auth: FakeSmtpServer
    ) -> None:
        config = SmtpConfig(
            host=smtp_server_requiring_auth.host,
            port=smtp_server_requiring_auth.port,
            from_address="xedge@example.com",
            tls_mode="none",
            username=SMTP_TEST_USERNAME,
            password=SMTP_TEST_PASSWORD,
        )
        status = SmtpStatus()

        await send_email(config, ["ops@example.com"], "Subject", "Body", status)

        assert len(smtp_server_requiring_auth.messages) == 1
        assert status.last_send_ok is True

    async def test_wrong_password_fails_without_raising(
        self, smtp_server_requiring_auth: FakeSmtpServer
    ) -> None:
        config = SmtpConfig(
            host=smtp_server_requiring_auth.host,
            port=smtp_server_requiring_auth.port,
            from_address="xedge@example.com",
            tls_mode="none",
            username=SMTP_TEST_USERNAME,
            password="wrong-password",
        )
        status = SmtpStatus()

        await send_email(config, ["ops@example.com"], "Subject", "Body", status)

        assert smtp_server_requiring_auth.messages == []
        assert status.last_send_ok is False
        assert status.last_error is not None

    async def test_starttls_send_is_captured_by_the_real_server(
        self, smtp_server_starttls: FakeSmtpServer
    ) -> None:
        config = SmtpConfig(
            host=smtp_server_starttls.host,
            port=smtp_server_starttls.port,
            from_address="xedge@example.com",
            tls_mode="starttls",
            tls_ca_certs_path=str(smtp_server_starttls.ca_cert_path),
            username=SMTP_TEST_USERNAME,
            password=SMTP_TEST_PASSWORD,
        )
        status = SmtpStatus()

        await send_email(config, ["ops@example.com"], "Over STARTTLS", "Body", status)

        assert len(smtp_server_starttls.messages) == 1
        assert status.last_send_ok is True

    async def test_unreachable_host_fails_without_raising(self) -> None:
        config = SmtpConfig(
            host="127.0.0.1", port=1, from_address="xedge@example.com", connect_timeout_seconds=1.0
        )
        status = SmtpStatus()

        await send_email(config, ["ops@example.com"], "Subject", "Body", status)

        assert status.last_send_ok is False
        assert status.last_error is not None


class TestAlarmNotificationLoop:
    async def test_raising_and_clearing_each_send_exactly_one_email(
        self, smtp_server: FakeSmtpServer
    ) -> None:
        engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
        smtp_config = SmtpConfig(
            host=smtp_server.host,
            port=smtp_server.port,
            from_address="xedge@example.com",
            tls_mode="none",
        )
        notification_config = AlarmNotificationConfig(enabled=True, recipients=("ops@example.com",))
        status = SmtpStatus()
        task = asyncio.create_task(
            alarm_notification_loop(
                engine, smtp_config, notification_config, status, poll_interval_seconds=0.05
            )
        )
        try:
            engine.evaluate(_tag("d1/temp", 50.0))  # normal, no alarm
            await asyncio.sleep(0.2)
            assert smtp_server.messages == []

            engine.evaluate(_tag("d1/temp", 95.0))  # raises
            await _wait_until(lambda: len(smtp_server.messages) == 1)
            assert b"ALARM" in smtp_server.messages[0].content

            engine.acknowledge("d1/temp", "operator1")  # ack -- must NOT notify
            await asyncio.sleep(0.2)
            assert len(smtp_server.messages) == 1

            engine.evaluate(_tag("d1/temp", 50.0))  # clears
            await _wait_until(lambda: len(smtp_server.messages) == 2)
            assert b"CLEARED" in smtp_server.messages[1].content
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_shelved_alarm_does_not_notify(self, smtp_server: FakeSmtpServer) -> None:
        engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
        engine.shelve("d1/temp", duration_seconds=60)
        smtp_config = SmtpConfig(
            host=smtp_server.host,
            port=smtp_server.port,
            from_address="xedge@example.com",
            tls_mode="none",
        )
        notification_config = AlarmNotificationConfig(enabled=True, recipients=("ops@example.com",))
        status = SmtpStatus()
        task = asyncio.create_task(
            alarm_notification_loop(
                engine, smtp_config, notification_config, status, poll_interval_seconds=0.05
            )
        )
        try:
            engine.evaluate(_tag("d1/temp", 95.0))
            await asyncio.sleep(0.3)
            assert smtp_server.messages == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_no_recipients_configured_sends_nothing(
        self, smtp_server: FakeSmtpServer
    ) -> None:
        engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
        smtp_config = SmtpConfig(
            host=smtp_server.host,
            port=smtp_server.port,
            from_address="xedge@example.com",
            tls_mode="none",
        )
        notification_config = AlarmNotificationConfig(enabled=True, recipients=())
        status = SmtpStatus()
        task = asyncio.create_task(
            alarm_notification_loop(
                engine, smtp_config, notification_config, status, poll_interval_seconds=0.05
            )
        )
        try:
            engine.evaluate(_tag("d1/temp", 95.0))
            await asyncio.sleep(0.3)
            assert smtp_server.messages == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestScheduledReportLoop:
    async def test_fires_on_interval_with_alarms_and_tag_values(
        self, smtp_server: FakeSmtpServer, tmp_path
    ) -> None:
        from xedge.core.config import ConfigStore

        engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
        engine.evaluate(_tag("d1/temp", 95.0))
        latest_values = LatestValueStore()
        latest_values.update(_tag("d1/pressure", 12.5))
        store = ConfigStore({"schema_version": "0.1", "assets": []})

        smtp_config = SmtpConfig(
            host=smtp_server.host,
            port=smtp_server.port,
            from_address="xedge@example.com",
            tls_mode="none",
        )
        report_config = ScheduledReportConfig(
            id="quick-report",
            recipients=("ops@example.com",),
            interval_seconds=0.05,
            include_tag_ids=("d1/pressure",),
        )
        status = SmtpStatus()
        task = asyncio.create_task(
            scheduled_report_loop(report_config, smtp_config, store, engine, latest_values, status)
        )
        try:
            await _wait_until(lambda: len(smtp_server.messages) >= 1)
            content = smtp_server.messages[0].content
            assert b"quick-report" in content
            assert b"d1/temp" in content
            assert b"d1/pressure" in content
            assert b"12.5" in content
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_no_recipients_never_sends(self, smtp_server: FakeSmtpServer, tmp_path) -> None:
        from xedge.core.config import ConfigStore

        store = ConfigStore({"schema_version": "0.1"})
        smtp_config = SmtpConfig(
            host=smtp_server.host,
            port=smtp_server.port,
            from_address="xedge@example.com",
            tls_mode="none",
        )
        report_config = ScheduledReportConfig(id="r1", recipients=(), interval_seconds=0.05)
        status = SmtpStatus()
        task = asyncio.create_task(
            scheduled_report_loop(
                report_config, smtp_config, store, None, LatestValueStore(), status
            )
        )
        try:
            await asyncio.sleep(0.3)
            assert smtp_server.messages == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
