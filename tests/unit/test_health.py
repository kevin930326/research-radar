import pytest
from structlog.testing import capture_logs

from radar.domain.schema import SchemaState
from radar.services.health import check_readiness


def test_ready_when_ping_succeeds() -> None:
    assert check_readiness(lambda: True, lambda: SchemaState.CURRENT) is True


def test_not_ready_when_ping_fails() -> None:
    assert check_readiness(lambda: False, lambda: SchemaState.CURRENT) is False


def test_exception_means_not_ready_and_hides_message() -> None:
    def ping() -> bool:
        raise ConnectionError("postgresql://radar:s3cret@db:5432/radar unreachable")

    with capture_logs() as logs:
        assert check_readiness(ping, lambda: SchemaState.CURRENT) is False

    assert logs == [
        {"event": "就緒檢查失敗", "error_type": "ConnectionError", "log_level": "warning"}
    ]


def _ready_with(state: SchemaState) -> bool:
    return check_readiness(lambda: True, lambda: state)


def test_ready_when_schema_is_current() -> None:
    with capture_logs() as logs:
        assert _ready_with(SchemaState.CURRENT) is True

    assert logs == []


@pytest.mark.parametrize("state", [SchemaState.NOT_MIGRATED, SchemaState.BEHIND])
def test_not_ready_when_schema_not_migrated_or_behind(state: SchemaState) -> None:
    with capture_logs() as logs:
        assert _ready_with(state) is False

    assert [(r["log_level"], r["schema_state"]) for r in logs] == [("warning", state.value)]


def test_ready_with_warning_when_database_is_ahead() -> None:
    # 退版時資料庫的版本比程式新；回 503 的話退版會讓服務整個停掉（AD-29）。
    with capture_logs() as logs:
        assert _ready_with(SchemaState.AHEAD) is True

    assert [(r["log_level"], r["schema_state"]) for r in logs] == [("warning", "ahead")]


def test_schema_state_not_checked_when_ping_fails() -> None:
    calls: list[None] = []

    def schema_state() -> SchemaState:
        calls.append(None)
        return SchemaState.CURRENT

    assert check_readiness(lambda: False, schema_state) is False
    assert calls == []


def test_schema_state_exception_means_not_ready_and_hides_message() -> None:
    def schema_state() -> SchemaState:
        raise RuntimeError("postgresql://radar_app:s3cret@db:5432/radar permission denied")

    with capture_logs() as logs:
        assert check_readiness(lambda: True, schema_state) is False

    assert logs == [{"event": "就緒檢查失敗", "error_type": "RuntimeError", "log_level": "warning"}]
