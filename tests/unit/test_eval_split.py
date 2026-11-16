"""調整組／測試組（`domain/eval_split.py`，Story 2.10、ADR-0042）。

- 黃金值：雜湊與組別寫死在這裡，之後改算法（或 `TUNING_PERCENT`）悄悄重分時會失敗。
- 同一個 DOI 不管寫法、呼叫幾次、在哪個行程（不同 `PYTHONHASHSEED`）都同組。
- 沒有 DOI 是測試組；多個 DOI 只要有一個是測試組就是測試組。
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from radar.domain.eval_split import TUNING_PERCENT, EvalSplit, split_for_doi, split_for_dois
from radar.domain.fingerprints import eval_split_sha256, trial_sample_sha256
from radar.domain.identifiers import InvalidIdentifierError

ROOT = Path(__file__).resolve().parents[2]

# （正規化後的 DOI, eval_split_sha256, 組別）：2026-11-16 算出來後寫死。改了這些值等於重新切分，
# 之前拿調整組調過的提示詞可能碰過新的測試組；真的要改先寫 ADR。
GOLDEN = [
    (
        "10.1/abc",
        "b59f9a10a64ed7838ab2c27eba85a819562a0eb4f8a9179c21b3a1c3836fd25b",
        EvalSplit.TEST,
    ),
    (
        "10.1287/isre.2023.0001",
        "ff0f0f730066c68feafc347268cae2c800e54df29ffc1574be7f7951d05a72bb",
        EvalSplit.TEST,
    ),
    (
        "10.25300/misq/2024/17801",
        "4ab6c800b0d1b483ce754abe8f8a1515e96155a3803541abeb0d566458601702",
        EvalSplit.TUNING,
    ),
    (
        "10.1/trial0",
        "4ed301d08bd39dc828aa86d7171c0702ccf6bff40cc8548208d6518c2e215655",
        EvalSplit.TUNING,
    ),
    (
        "10.1/trial3",
        "12ea2ea209a713adb10b6d3c70f3ad347a5b8492162cce7724019e93e99227db",
        EvalSplit.TEST,
    ),
]


def test_tuning_percent_is_half() -> None:
    assert TUNING_PERCENT == 50


@pytest.mark.parametrize(("doi", "digest", "split"), GOLDEN)
def test_golden_values(doi: str, digest: str, split: EvalSplit) -> None:
    assert eval_split_sha256(doi) == digest
    assert split_for_doi(doi) is split


@pytest.mark.parametrize(("doi", "digest", "split"), GOLDEN)
def test_golden_digest_matches_the_documented_algorithm(
    doi: str, digest: str, split: EvalSplit
) -> None:
    # 獨立照文件算一次：正規 JSON（sort_keys、緊湊、不跳脫非 ASCII）的 SHA-256；前 8 個十六進位字
    # 當整數 mod 100，小於 50 是調整組。
    canonical = json.dumps(
        {"v": 1, "purpose": "eval_split", "doi": doi},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == digest
    bucket = int(digest[:8], 16) % 100
    assert (bucket < 50) == (split is EvalSplit.TUNING)


def test_trial_sample_golden_value() -> None:
    assert (
        trial_sample_sha256(7, "10.1/abc")
        == "e8f73cd33f5445a969128f61d490633d983b74bb1e05dae5623243a56c61188f"
    )


@pytest.mark.parametrize(
    "spelling",
    [
        "10.25300/MISQ/2024/17801",
        "https://doi.org/10.25300/misq/2024/17801",
        "HTTP://DX.DOI.ORG/10.25300/MISQ%2F2024%2F17801",
        "doi: 10.25300/misq/2024/17801",
        "  10.25300/misq/2024/17801  ",
    ],
)
def test_spellings_of_the_same_doi_land_in_the_same_split(spelling: str) -> None:
    assert split_for_doi(spelling) is EvalSplit.TUNING
    assert split_for_doi(spelling) is split_for_doi("10.25300/misq/2024/17801")


def test_same_doi_same_split_on_repeated_calls() -> None:
    dois = [f"10.9/x{n}" for n in range(200)]
    first = [split_for_doi(doi) for doi in dois]
    assert [split_for_doi(doi) for doi in dois] == first


def test_same_split_in_processes_with_different_hash_seeds() -> None:
    # 不用 Python 內建的 hash()：換 PYTHONHASHSEED 的子行程算出來的組別一樣。
    script = (
        "from radar.domain.eval_split import split_for_doi\n"
        "print(','.join(split_for_doi(f'10.9/x{n}').value for n in range(100)))\n"
    )
    outputs = []
    for seed in ("0", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
            env=env,
            timeout=60,
        )
        outputs.append(completed.stdout.strip())
    expected = ",".join(split_for_doi(f"10.9/x{n}").value for n in range(100))
    assert outputs == [expected, expected]


def test_split_is_roughly_half_and_half() -> None:
    splits = [split_for_doi(f"10.9/x{n}") for n in range(2000)]
    tuning = splits.count(EvalSplit.TUNING)
    assert 900 <= tuning <= 1100


def test_no_doi_is_test_split() -> None:
    assert split_for_dois([]) is EvalSplit.TEST
    assert split_for_dois(()) is EvalSplit.TEST


def test_any_test_doi_makes_the_paper_test_split() -> None:
    tuning = "10.25300/misq/2024/17801"
    test = "10.1/abc"
    assert split_for_dois([tuning]) is EvalSplit.TUNING
    assert split_for_dois([tuning, "10.1/trial0"]) is EvalSplit.TUNING
    assert split_for_dois([tuning, test]) is EvalSplit.TEST
    assert split_for_dois([test, tuning]) is EvalSplit.TEST


def test_rejects_a_single_string_and_bad_dois() -> None:
    with pytest.raises(TypeError):
        split_for_dois("10.1/abc")
    with pytest.raises(InvalidIdentifierError):
        split_for_doi("not a doi")
    with pytest.raises(InvalidIdentifierError):
        split_for_dois(["10.1/abc", "nope"])


def test_fingerprints_only_take_normalized_dois() -> None:
    with pytest.raises(ValueError, match="正規化"):
        eval_split_sha256("10.1/ABC")
    with pytest.raises(ValueError, match="正規化"):
        trial_sample_sha256(1, "https://doi.org/10.1/abc")
    with pytest.raises(ValueError, match="seed"):
        trial_sample_sha256(True, "10.1/abc")
