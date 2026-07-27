from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xedge.core.alarms import AlarmState, AlarmStatus
from xedge.core.pipeline import UnifiedTag
from xedge.core.smtp import (
    ScheduledReportConfig,
    _format_alarm_notification,
    _format_report_body,
    _is_alarming,
    _is_shelved,
    _resolve_report_tag_ids,
    build_alarm_notification_config,
    build_scheduled_report_configs,
    build_smtp_config,
)
from xedge.drivers.base import Quality
from xedge.store.latest_values import LatestValueStore


def _tag(tag_id: str, value: object, quality: Quality = Quality.GOOD) -> UnifiedTag:
    return UnifiedTag(
        tag_id=tag_id,
        timestamp=datetime.now(UTC),
        value=value,  # type: ignore[arg-type]
        data_type="FLOAT64",
        quality=quality,
        source_driver=tag_id.split("/")[0],
        source_address="0",
    )


class TestIsAlarming:
    def test_normal_is_not_alarming(self) -> None:
        assert _is_alarming(AlarmState.NORMAL) is False

    def test_active_is_alarming(self) -> None:
        assert _is_alarming(AlarmState.ACTIVE) is True

    def test_active_acked_is_alarming(self) -> None:
        assert _is_alarming(AlarmState.ACTIVE_ACKED) is True


class TestIsShelved:
    def test_no_shelve_set_is_not_shelved(self) -> None:
        status = AlarmStatus(tag_id="d1/t1")
        assert _is_shelved(status, datetime.now(UTC)) is False

    def test_within_shelve_window_is_shelved(self) -> None:
        status = AlarmStatus(tag_id="d1/t1", shelved_until=datetime.now(UTC) + timedelta(hours=1))
        assert _is_shelved(status, datetime.now(UTC)) is True

    def test_after_shelve_window_is_not_shelved(self) -> None:
        status = AlarmStatus(
            tag_id="d1/t1", shelved_until=datetime.now(UTC) - timedelta(seconds=1)
        )
        assert _is_shelved(status, datetime.now(UTC)) is False


class TestFormatAlarmNotification:
    def test_raised_notification_mentions_alarm_and_condition(self) -> None:
        status = AlarmStatus(
            tag_id="d1/temp",
            state=AlarmState.ACTIVE,
            condition="high_high",
            last_value=99.5,
            active_since=datetime.now(UTC),
        )
        subject, body = _format_alarm_notification(status, raised=True)
        assert "ALARM" in subject
        assert "d1/temp" in subject
        assert "high_high" in body
        assert "99.5" in body
        assert "Active since" in body

    def test_cleared_notification_mentions_cleared(self) -> None:
        status = AlarmStatus(tag_id="d1/temp", state=AlarmState.NORMAL, last_value=50.0)
        subject, body = _format_alarm_notification(status, raised=False)
        assert "CLEARED" in subject
        assert "Active since" not in body


class TestResolveReportTagIds:
    def test_plain_tag_ids_pass_through(self) -> None:
        config = ScheduledReportConfig(id="r1", include_tag_ids=("d1/t1", "d1/t2"))
        assert _resolve_report_tag_ids(config, []) == ["d1/t1", "d1/t2"]

    def test_asset_ids_resolve_to_their_parameters(self) -> None:
        config = ScheduledReportConfig(id="r1", include_asset_ids=("pump-101",))
        assets_config = [
            {
                "id": "pump-101",
                "name": "Pump",
                "parameters": [{"tag_ref": "d1/pressure"}, {"tag_ref": "d1/temp"}],
            }
        ]
        assert _resolve_report_tag_ids(config, assets_config) == ["d1/pressure", "d1/temp"]

    def test_unknown_asset_id_is_silently_skipped(self) -> None:
        config = ScheduledReportConfig(id="r1", include_asset_ids=("no-such-asset",))
        assert _resolve_report_tag_ids(config, []) == []

    def test_tag_ids_and_asset_ids_combine(self) -> None:
        config = ScheduledReportConfig(
            id="r1", include_tag_ids=("d1/t1",), include_asset_ids=("pump-101",)
        )
        assets_config = [
            {"id": "pump-101", "name": "Pump", "parameters": [{"tag_ref": "d1/pressure"}]}
        ]
        assert _resolve_report_tag_ids(config, assets_config) == ["d1/t1", "d1/pressure"]


class TestFormatReportBody:
    def test_includes_active_alarms(self) -> None:
        from xedge.core.alarms import AlarmEngine, AlarmRule

        engine = AlarmEngine({"d1/temp": AlarmRule(tag_id="d1/temp", high=90)})
        engine.evaluate(_tag("d1/temp", 95.0))
        config = ScheduledReportConfig(id="r1")
        body = _format_report_body(config, engine, LatestValueStore(), [])
        assert "d1/temp" in body
        assert "high" in body

    def test_no_active_alarms_shows_none(self) -> None:
        from xedge.core.alarms import AlarmEngine

        config = ScheduledReportConfig(id="r1")
        body = _format_report_body(config, AlarmEngine({}), LatestValueStore(), [])
        assert "(none)" in body

    def test_none_alarm_engine_is_treated_as_no_alarms(self) -> None:
        config = ScheduledReportConfig(id="r1")
        body = _format_report_body(config, None, LatestValueStore(), [])
        assert "(none)" in body

    def test_includes_current_tag_values(self) -> None:
        latest_values = LatestValueStore()
        latest_values.update(_tag("d1/pressure", 42.0))
        config = ScheduledReportConfig(id="r1", include_tag_ids=("d1/pressure",))
        body = _format_report_body(config, None, latest_values, [])
        assert "d1/pressure" in body
        assert "42.0" in body
        assert "Good" in body

    def test_tag_with_no_data_yet_is_reported_as_such(self) -> None:
        config = ScheduledReportConfig(id="r1", include_tag_ids=("d1/never-seen",))
        body = _format_report_body(config, None, LatestValueStore(), [])
        assert "d1/never-seen: (no data)" in body


class TestBuildSmtpConfig:
    def test_defaults(self) -> None:
        config = build_smtp_config({})
        assert config.enabled is False
        assert config.port == 587
        assert config.tls_mode == "starttls"

    def test_reads_every_field(self) -> None:
        config = build_smtp_config(
            {
                "enabled": True,
                "host": "smtp.example.com",
                "port": 465,
                "tls_mode": "smtps",
                "username": "alerts",
                "password": "hunter2",
                "from_address": "xedge@example.com",
                "connect_timeout_seconds": 5.0,
                "tls_ca_certs_path": "/data/certs/mail-ca.pem",
            }
        )
        assert config.enabled is True
        assert config.host == "smtp.example.com"
        assert config.port == 465
        assert config.tls_mode == "smtps"
        assert config.username == "alerts"
        assert config.password == "hunter2"
        assert config.from_address == "xedge@example.com"
        assert config.connect_timeout_seconds == 5.0
        assert config.tls_ca_certs_path == "/data/certs/mail-ca.pem"


class TestBuildAlarmNotificationConfig:
    def test_defaults(self) -> None:
        config = build_alarm_notification_config({})
        assert config.enabled is False
        assert config.recipients == ()

    def test_reads_recipients(self) -> None:
        config = build_alarm_notification_config(
            {"alarm_notifications": {"enabled": True, "recipients": ["ops@example.com"]}}
        )
        assert config.enabled is True
        assert config.recipients == ("ops@example.com",)


class TestBuildScheduledReportConfigs:
    def test_empty_when_no_reports_configured(self) -> None:
        assert build_scheduled_report_configs({}) == []

    def test_parses_every_field(self) -> None:
        configs = build_scheduled_report_configs(
            {
                "scheduled_reports": [
                    {
                        "id": "daily",
                        "recipients": ["ops@example.com"],
                        "interval_seconds": 3600,
                        "include_tag_ids": ["d1/t1"],
                        "include_asset_ids": ["pump-101"],
                    }
                ]
            }
        )
        assert len(configs) == 1
        assert configs[0].id == "daily"
        assert configs[0].recipients == ("ops@example.com",)
        assert configs[0].interval_seconds == 3600
        assert configs[0].include_tag_ids == ("d1/t1",)
        assert configs[0].include_asset_ids == ("pump-101",)

    def test_disabled_reports_are_excluded(self) -> None:
        configs = build_scheduled_report_configs(
            {"scheduled_reports": [{"id": "daily", "enabled": False}]}
        )
        assert configs == []
