"""`domain/fingerprints.py` 的 LLM 指紋：`input_sha256`、`prompt_sha256`、`schema_sha256`、
`run_lock_key`。
"""

import hashlib
import re
import uuid

import pytest

from radar.domain.ai_outputs import (
    DocumentType,
    LlmInputBlock,
    LlmTask,
    RunKey,
    wrap_document,
)
from radar.domain.fingerprints import input_sha256, prompt_sha256, run_lock_key, schema_sha256
from radar.domain.policy import ContentSource

TITLE = LlmInputBlock(
    DocumentType.TITLE, ContentSource.TITLE, wrap_document(DocumentType.TITLE, "A study")
)
ABSTRACT = LlmInputBlock(
    DocumentType.ABSTRACT,
    ContentSource.OPENALEX_ABSTRACT,
    wrap_document(DocumentType.ABSTRACT, "We study things."),
)
PAPER = uuid.UUID("018f0000-0000-7000-8000-0000000000a1")


def _sha(
    blocks: tuple[LlmInputBlock, ...], task: LlmTask = LlmTask.ZH_SUMMARY, **versions: str | None
) -> str:
    return input_sha256(
        blocks,
        task,
        taxonomy_version=versions.get("taxonomy_version"),
        context_values_version=versions.get("context_values_version"),
    )


def test_input_sha256_is_stable_lowercase_hex() -> None:
    first = _sha((TITLE, ABSTRACT))

    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert _sha((TITLE, ABSTRACT)) == first


def test_input_sha256_changes_with_text_order_source_and_task() -> None:
    base = _sha((TITLE, ABSTRACT))
    changed_text = LlmInputBlock(
        DocumentType.ABSTRACT,
        ContentSource.OPENALEX_ABSTRACT,
        wrap_document(DocumentType.ABSTRACT, "We study other things."),
    )
    changed_source = LlmInputBlock(
        DocumentType.ABSTRACT, ContentSource.CROSSREF_ABSTRACT, ABSTRACT.text
    )

    others = {
        _sha((TITLE, changed_text)),
        _sha((ABSTRACT, TITLE)),
        _sha((TITLE, changed_source)),
        _sha((TITLE,)),
        _sha((TITLE, ABSTRACT), LlmTask.MERGE_JUDGE),
    }
    assert base not in others
    assert len(others) == 5


def test_classify_requires_taxonomy_version_and_it_changes_the_hash() -> None:
    with pytest.raises(ValueError, match="taxonomy_version"):
        _sha((TITLE,), LlmTask.CLASSIFY)

    v1 = _sha((TITLE,), LlmTask.CLASSIFY, taxonomy_version="2026-11")
    v2 = _sha((TITLE,), LlmTask.CLASSIFY, taxonomy_version="2026-12")
    assert v1 != v2


def test_extract_model_requires_context_values_version() -> None:
    with pytest.raises(ValueError, match="context_values_version"):
        _sha((TITLE, ABSTRACT), LlmTask.EXTRACT_MODEL)

    a = _sha((TITLE, ABSTRACT), LlmTask.EXTRACT_MODEL, context_values_version="v1")
    b = _sha((TITLE, ABSTRACT), LlmTask.EXTRACT_MODEL, context_values_version="v2")
    assert a != b


@pytest.mark.parametrize(
    ("task", "versions"),
    [
        (LlmTask.ZH_SUMMARY, {"taxonomy_version": "x"}),
        (LlmTask.ZH_SUMMARY, {"context_values_version": "x"}),
        (LlmTask.CLASSIFY, {"taxonomy_version": "x", "context_values_version": "y"}),
        (LlmTask.EXTRACT_MODEL, {"context_values_version": "y", "taxonomy_version": "x"}),
        (LlmTask.CLASSIFY, {"taxonomy_version": "  "}),
    ],
)
def test_other_tasks_must_not_carry_versions(task: LlmTask, versions: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="version"):
        _sha((TITLE,), task, **versions)


def test_input_sha256_needs_at_least_one_block() -> None:
    with pytest.raises(ValueError, match="區塊"):
        _sha(())


def test_prompt_sha256_is_sha256_of_raw_bytes() -> None:
    content = "---\nmax_tokens: 10\n---\n本文\n".encode()

    assert prompt_sha256(content) == hashlib.sha256(content).hexdigest()
    with pytest.raises(TypeError):
        prompt_sha256("not bytes")  # type: ignore[arg-type]


def test_schema_sha256_is_sha256_of_utf8_json() -> None:
    assert schema_sha256('{"a":1}') == hashlib.sha256(b'{"a":1}').hexdigest()
    with pytest.raises(TypeError):
        schema_sha256(b"{}")  # type: ignore[arg-type]


def test_run_lock_key_is_signed_64_bit_and_depends_on_every_field() -> None:
    key = RunKey(PAPER, LlmTask.ZH_SUMMARY, 1, "claude-haiku-5-5", "a" * 64)
    value = run_lock_key(key)

    assert -(2**63) <= value < 2**63
    assert run_lock_key(key) == value
    variants = [
        RunKey(uuid.uuid4(), key.task, 1, key.model, key.input_sha256),
        RunKey(PAPER, LlmTask.CLASSIFY, 1, key.model, key.input_sha256),
        RunKey(PAPER, key.task, 2, key.model, key.input_sha256),
        RunKey(PAPER, key.task, 1, "other-model", key.input_sha256),
        RunKey(PAPER, key.task, 1, key.model, "b" * 64),
    ]
    assert value not in {run_lock_key(variant) for variant in variants}
