"""`radar runs trial` 確認審查的修正（Story 2.10、Opus 確認審查 C1、C4）：

- C1：總表「這次呼叫模型」只算拿到回應的項目；`transient_failure`（送了請求、重試完還是沒拿到
  回應）另列「呼叫了但沒拿到回應」，拒收率的分母也不算它。
- C4：`--ids-file` 開頭有 UTF-8 BOM 也讀得懂。

都不碰資料庫、不連網路；論文 ID 都是自己編的。
"""

import uuid
from pathlib import Path

import radar.entrypoints.cli as cli
from radar.domain.ai_outputs import LlmTask
from radar.domain.eval_split import EvalSplit, split_for_doi
from radar.domain.trial_runs import (
    TrialItem,
    TrialItemStatus,
    TrialReport,
    render_trial_summary,
    trial_paper,
)

PAPER = uuid.UUID("01900000-0000-7000-8000-000000000011")
OTHER = uuid.UUID("01900000-0000-7000-8000-000000000012")
RUN = uuid.UUID("01900000-0000-7000-8000-0000000000bb")


def _tuning_doi() -> str:
    for n in range(1000):
        doi = f"10.7/c{n}"
        if split_for_doi(doi) is EvalSplit.TUNING:
            return doi
    raise AssertionError("找不到調整組的 DOI")


def _item(status: TrialItemStatus, task: LlmTask, **changes: object) -> TrialItem:
    values: dict[str, object] = {"paper_id": PAPER, "task": task, "status": status}
    values.update(changes)
    return TrialItem(**values)  # type: ignore[arg-type]


def _summary_line(lines: list[str]) -> str:
    [line] = [line for line in lines if line.startswith("這次呼叫模型：")]
    return line


# --- C1：沒拿到回應的不算「這次呼叫模型」 ------------------------------------------------------


def test_got_response_excludes_transient_failure() -> None:
    assert _item(TrialItemStatus.ACTIVE, LlmTask.CLASSIFY, run_id=RUN).got_response
    assert _item(TrialItemStatus.REJECTED, LlmTask.CLASSIFY, run_id=RUN).got_response
    assert _item(TrialItemStatus.CANDIDATE, LlmTask.CLASSIFY, run_id=RUN).got_response
    assert _item(TrialItemStatus.SUPERSEDED, LlmTask.CLASSIFY, run_id=RUN).got_response
    transient = _item(TrialItemStatus.TRANSIENT_FAILURE, LlmTask.CLASSIFY, run_id=RUN)
    assert transient.called_model
    assert not transient.got_response
    # 接續（沒有呼叫）與沒有 run 的都不算。
    assert not _item(
        TrialItemStatus.ACTIVE, LlmTask.CLASSIFY, run_id=RUN, resumed=True
    ).got_response
    assert not _item(TrialItemStatus.SKIPPED, LlmTask.CLASSIFY).got_response


def test_transient_failure_is_listed_as_no_response() -> None:
    papers = (trial_paper(PAPER, [_tuning_doi()]),)
    items = (
        _item(TrialItemStatus.ACTIVE, LlmTask.ZH_SUMMARY, run_id=RUN),
        _item(TrialItemStatus.ACTIVE, LlmTask.CLASSIFY, run_id=RUN),
        _item(
            TrialItemStatus.TRANSIENT_FAILURE,
            LlmTask.EXTRACT_MODEL,
            reason="llm_timeout",
            run_id=RUN,
        ),
    )
    report = TrialReport(papers, items)

    assert len(report.responded_items()) == 2
    assert report.no_response_by_status() == {"transient_failure": 1}
    assert report.not_called_by_status() == {}
    lines = render_trial_summary(report, verbose=False)
    assert _summary_line(lines) == (
        "這次呼叫模型：2 項（其中拒收 0 項）；呼叫了但沒拿到回應：1 項（transient_failure 1）"
        "；沒有呼叫模型：0 項"
    )


def test_rejection_ratio_denominator_skips_transient_failure() -> None:
    papers = (trial_paper(PAPER, [_tuning_doi()]), trial_paper(OTHER, [_tuning_doi()]))
    items = (
        _item(TrialItemStatus.ACTIVE, LlmTask.ZH_SUMMARY, run_id=RUN),
        _item(TrialItemStatus.REJECTED, LlmTask.CLASSIFY, reason="llm_output_invalid", run_id=RUN),
        _item(TrialItemStatus.TRANSIENT_FAILURE, LlmTask.EXTRACT_MODEL, run_id=RUN),
        _item(TrialItemStatus.TRANSIENT_FAILURE, LlmTask.ZH_SUMMARY, paper_id=OTHER, run_id=RUN),
        _item(TrialItemStatus.SKIPPED, LlmTask.CLASSIFY, paper_id=OTHER),
        _item(TrialItemStatus.ACTIVE, LlmTask.EXTRACT_MODEL, paper_id=OTHER, run_id=RUN),
    )

    lines = render_trial_summary(TrialReport(papers, items), verbose=False)

    assert _summary_line(lines) == (
        "這次呼叫模型：3 項（其中拒收 1 項）；呼叫了但沒拿到回應：2 項（transient_failure 2）"
        "；沒有呼叫模型：1 項（skipped 1）"
    )
    assert "有暫時性失敗：直接重跑同一個指令，做完的不會重付" in lines


# --- C4：名單檔有 BOM ----------------------------------------------------------------------


def test_ids_file_with_utf8_bom_is_read(tmp_path: Path) -> None:
    path = tmp_path / "ids.txt"
    path.write_bytes(f"{PAPER}\r\n# 備註\r\n{OTHER}\r\n".encode("utf-8-sig"))

    assert cli._read_ids_file(path).targets == (PAPER, OTHER)


def test_ids_file_with_bom_before_a_doi_is_read(tmp_path: Path) -> None:
    path = tmp_path / "dois.txt"
    path.write_bytes("10.1/ABC\n".encode("utf-8-sig"))

    assert cli._read_ids_file(path).targets == ("10.1/abc",)
