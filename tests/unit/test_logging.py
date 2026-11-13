import json
import logging

import pytest
import structlog

from radar.entrypoints.logging_config import configure_logging


def _records(out: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_structlog_writes_json_to_stdout_with_unescaped_chinese(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging()
    structlog.get_logger("t").info("正常關機", service="api")

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "正常關機" in captured.out  # 中文沒有被跳脫成 \uXXXX
    (record,) = _records(captured.out)
    assert record["event"] == "正常關機"
    assert record["level"] == "info"
    assert record["service"] == "api"
    assert "timestamp" in record
    assert {k: record[k] for k in ("request_id", "member_id", "job_id")} == dict.fromkeys(
        ("request_id", "member_id", "job_id")
    )


def test_bound_request_id_is_included(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    with structlog.contextvars.bound_contextvars(request_id="rid-1"):
        structlog.get_logger("t").info("x")

    (record,) = _records(capsys.readouterr().out)
    assert record["request_id"] == "rid-1"


def test_stdlib_logging_is_json_too(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    logging.getLogger("uvicorn.error").info("Started server process [%d]", 7)

    captured = capsys.readouterr()
    assert captured.err == ""
    (record,) = _records(captured.out)
    assert record["event"] == "Started server process [7]"
    assert {k: record[k] for k in ("request_id", "member_id", "job_id")} == dict.fromkeys(
        ("request_id", "member_id", "job_id")
    )


def test_debug_is_filtered_at_info_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    structlog.get_logger("t").debug("noise")

    assert capsys.readouterr().out == ""
