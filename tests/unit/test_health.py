from structlog.testing import capture_logs

from radar.services.health import check_readiness


def test_ready_when_ping_succeeds() -> None:
    assert check_readiness(lambda: True) is True


def test_not_ready_when_ping_fails() -> None:
    assert check_readiness(lambda: False) is False


def test_exception_means_not_ready_and_hides_message() -> None:
    def ping() -> bool:
        raise ConnectionError("postgresql://radar:s3cret@db:5432/radar unreachable")

    with capture_logs() as logs:
        assert check_readiness(ping) is False

    assert logs == [
        {"event": "就緒檢查失敗", "error_type": "ConnectionError", "log_level": "warning"}
    ]
