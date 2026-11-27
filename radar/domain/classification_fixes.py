"""成員修正分類（Story 3.11、FR-4、FR-16、EXPERIENCE.md 畫面 8 第 2 點、決定第 95～98 項）。

- **一次送兩欄**（決定第 98 項）：主題與方法各至少一個、每個都是**現在分類表**
  （`config/taxonomy.yaml`）的 `key`、不重複；整組和目前的有效值一樣（兩欄都沒變）就不收。
  所以不能改成 `unclassified`（它不是分類表的 `key`），送出後兩欄一定都是人工值（「已核對」）。
- **理由選填**：前後空白去掉；去掉後是空字串當作沒寫；上限 `REASON_MAX_LENGTH` 個字（Unicode
  code point，不是位元組也不是 UTF-16 單位；前端同一種算法）；不能含控制字元（一行的輸入框打不出來；
  NUL、換行、Tab 都算）、行分隔符 U+2028、段落分隔符 U+2029、格式字元（零寬空白、BOM、雙向覆寫
  這類看不見的字；零寬連接 U+200D 放行）與存不進資料庫的字。擋哪些、放行哪些和回報中文重點的說明
  共用 `member_text_characters` 同一份（epic-3m，Kevin 2026-11-26 決定 D14；NFC 正規化不做）。
- **分類沒有成功的論文先不收**（`NO_AI_CLASSIFICATION`）：能不能修正等 Kevin 決定（TASK 第 6 部分
  第 8 點），決定前後端拒收，和畫面不給入口一致。
- 成員送來的值一律當使用者輸入驗（AGENTS.md），前端的檢查不算數。錯誤只帶原因代號
  （`ClassificationFixProblem`），**不回顯成員送的值**（日誌與回應都不放）。
- 歷史（決定第 96 項）：一次送出寫兩筆 `judgment_event`（主題、方法；同一個交易，`created_at`
  相同），畫面上是一筆。`group_fix_history` 依（成員, 時間）合成一筆，新的在前；只帶姓名，
  不帶成員 ID 或 email。Story 2.7 之前或直接寫進去、只有一欄的那一筆，另一欄是 None。
- 同一篇的修正排隊做（`classification_fix_lock_key`）：「和目前不同」要在同一把鎖裡讀、比、寫。
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Self

from radar.domain.ai_blocks import AiBlock, AiBlockState, AiProduct, ClassificationValue
from radar.domain.fingerprints import named_lock_key
from radar.domain.member_text_characters import is_rejected_character
from radar.domain.papers import check_storable_text
from radar.domain.taxonomy import Taxonomy, TaxonomyField

# 理由的上限（EXPERIENCE.md 決定第 98 項：理由與說明上限 100 字）。
REASON_MAX_LENGTH: Final = 100

# 稽核事件的動作與對象（小寫識別字，寫出去之後不改名）。
CORRECT_CLASSIFICATION_ACTION: Final = "correct_classification"
PAPER_TARGET: Final = "paper"


class ClassificationFixProblem(StrEnum):
    """成員送來的修正為什麼不收。"""

    NOT_A_LIST = "not_a_list"  # 主題或方法不是字串的清單
    EMPTY = "empty"  # 主題或方法一個都沒選
    DUPLICATE = "duplicate"  # 同一個 key 選了兩次
    UNKNOWN_KEY = "unknown_key"  # 不是現在分類表這一欄的 key（含 unclassified、放錯欄位）
    UNCHANGED = "unchanged"  # 兩欄都和目前的有效值一樣
    REASON_NOT_TEXT = "reason_not_text"  # 理由不是字串
    REASON_TOO_LONG = "reason_too_long"  # 理由超過 100 字
    REASON_INVALID_CHARACTER = (
        "reason_invalid_character"  # 理由有控制字元、分隔符、格式字元或存不進資料庫的字
    )
    NO_AI_CLASSIFICATION = "no_ai_classification"  # 分類沒有成功的結果（等 Kevin 決定前不收）


class ClassificationFixInvalidError(ValueError):
    """成員送來的修正不收。訊息只有原因代號與欄位名稱，不含成員送的值。"""

    def __init__(self, problem: ClassificationFixProblem, field: str) -> None:
        self.problem = problem
        self.field = field
        super().__init__(f"{field}：{problem.value}")


@dataclass(frozen=True, slots=True)
class ClassificationFix:
    """驗過的一次修正：兩欄各是排序過、不重複的 `key`；理由去掉前後空白，沒寫是 None。"""

    topic: tuple[str, ...]
    method: tuple[str, ...]
    reason: str | None


@dataclass(frozen=True, slots=True)
class CurrentClassification:
    """修正前看到的有效分類：每一欄的 `key`（AI 的值是 AI 給的順序，人工值是排序過的）；
    沒有值是 None（還沒有分類結果等）。"""

    topic: tuple[str, ...] | None
    method: tuple[str, ...] | None

    @classmethod
    def from_block(cls, block: AiBlock) -> Self:
        """`repo/ai_blocks.find_ai_block` 判定的分類區塊 → 兩欄目前的值。只有 `ready` 帶值；其他狀態
        （還在產生、沒有產生成功等）兩欄都是 None。

        Raises:
            ValueError: 區塊不是分類。
        """
        if block.product is not AiProduct.CLASSIFICATION:
            raise ValueError("要是分類區塊")
        value = block.value
        if block.state is not AiBlockState.READY or not isinstance(value, ClassificationValue):
            return cls(topic=None, method=None)
        return cls(
            topic=None if value.topic is None else value.topic.keys,
            method=None if value.method is None else value.method.keys,
        )

    def keys(self, field: TaxonomyField) -> tuple[str, ...] | None:
        match TaxonomyField(field):
            case TaxonomyField.TOPIC:
                return self.topic
            case TaxonomyField.METHOD:
                return self.method


def _check_keys(value: object, field: TaxonomyField, taxonomy: Taxonomy) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ClassificationFixInvalidError(ClassificationFixProblem.NOT_A_LIST, field.value)
    keys = list(value)
    if not all(isinstance(key, str) for key in keys):
        raise ClassificationFixInvalidError(ClassificationFixProblem.NOT_A_LIST, field.value)
    if not keys:
        raise ClassificationFixInvalidError(ClassificationFixProblem.EMPTY, field.value)
    if len(set(keys)) != len(keys):
        raise ClassificationFixInvalidError(ClassificationFixProblem.DUPLICATE, field.value)
    allowed = set(taxonomy.keys(field))
    if any(key not in allowed for key in keys):
        raise ClassificationFixInvalidError(ClassificationFixProblem.UNKNOWN_KEY, field.value)
    return tuple(sorted(keys))


def _check_reason(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ClassificationFixInvalidError(ClassificationFixProblem.REASON_NOT_TEXT, "reason")
    # 先看字元再去空白：換行、Tab 這類控制字元在頭尾也不收（一行的輸入框打不出來）。行分隔符
    # U+2028（Zl）、段落分隔符 U+2029（Zp）不是控制字元，但一樣會讓理由變成好幾行，也不收；
    # 零寬空白、雙向覆寫這類格式字元（Cf）看不見卻會改變顯示，也不收（零寬連接 U+200D 放行）。
    if any(is_rejected_character(char) for char in value):
        raise ClassificationFixInvalidError(
            ClassificationFixProblem.REASON_INVALID_CHARACTER, "reason"
        )
    try:
        check_storable_text(value, "理由")
    except ValueError:
        raise ClassificationFixInvalidError(
            ClassificationFixProblem.REASON_INVALID_CHARACTER, "reason"
        ) from None
    reason = value.strip()
    if not reason:
        return None
    if len(reason) > REASON_MAX_LENGTH:
        raise ClassificationFixInvalidError(ClassificationFixProblem.REASON_TOO_LONG, "reason")
    return reason


def _sorted_or_none(keys: tuple[str, ...] | None) -> tuple[str, ...] | None:
    return None if keys is None else tuple(sorted(keys))


def check_classification_fix(
    *,
    topic: object,
    method: object,
    reason: object,
    taxonomy: Taxonomy,
    current: CurrentClassification,
) -> ClassificationFix:
    """驗成員送來的一次修正（見模組說明）。`current` 是同一把鎖裡讀到的目前有效值。

    Raises:
        ClassificationFixInvalidError: 不收的原因（主題先驗、再驗方法、理由，再看分類有沒有成功的
            結果，最後比對目前的值）。
    """
    topics = _check_keys(topic, TaxonomyField.TOPIC, taxonomy)
    methods = _check_keys(method, TaxonomyField.METHOD, taxonomy)
    cleaned = _check_reason(reason)
    # 分類沒有成功的結果（還在產生、沒有產生成功等）：能不能修正等 Kevin 決定，先不收
    # （ADR-0063 取捨）。
    if current.topic is None and current.method is None:
        raise ClassificationFixInvalidError(
            ClassificationFixProblem.NO_AI_CLASSIFICATION, "classification"
        )
    # 目前的值可能是 AI 給的順序（沒排序）：比的是集合（排好再比）。
    if topics == _sorted_or_none(current.topic) and methods == _sorted_or_none(current.method):
        raise ClassificationFixInvalidError(ClassificationFixProblem.UNCHANGED, "classification")
    return ClassificationFix(topic=topics, method=methods, reason=cleaned)


def classification_fix_lock_key(paper_id: uuid.UUID) -> int:
    """一篇論文的修正分類的交易級 advisory lock 鍵：同一篇的修正排隊做（讀目前的值、比對、寫入在
    同一把鎖裡）。名稱帶 `classification_fix:` 前綴，和其他鎖分在不同的命名空間。"""
    return named_lock_key(f"classification_fix:{uuid.UUID(str(paper_id))}")


# --- 歷史 -----------------------------------------------------------------------------------


def _check_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} 要是帶時區的時間")


@dataclass(frozen=True, slots=True)
class ClassificationFixEvent:
    """`judgment_event` 的一筆分類修正（`repo/judgments.list_classification_fix_events` 讀的）：
    成員 ID 只拿來把同一次送出的兩欄合在一起，不往外送。"""

    event_id: uuid.UUID
    paper_id: uuid.UUID
    member_id: uuid.UUID
    member_name: str
    fixed_at: datetime
    field: TaxonomyField
    keys: tuple[str, ...]
    reason: str | None

    def __post_init__(self) -> None:
        _check_aware(self.fixed_at, "fixed_at")
        TaxonomyField(self.field)


@dataclass(frozen=True, slots=True)
class ClassificationFixEntry:
    """畫面上的一筆修正：日期（UTC）、成員姓名、選的主題與方法（那一次沒送的欄位是 None）、理由。"""

    fixed_at: datetime
    member_name: str
    topic: tuple[str, ...] | None
    method: tuple[str, ...] | None
    reason: str | None

    def __post_init__(self) -> None:
        _check_aware(self.fixed_at, "fixed_at")
        if self.topic is None and self.method is None:
            raise ValueError("一筆修正至少要有一欄")


def group_fix_history(
    events: Iterable[ClassificationFixEvent],
) -> tuple[ClassificationFixEntry, ...]:
    """同一篇論文的修正事件 → 畫面上的歷史：同一位成員、同一個時間（同一個交易）的合成一筆；新的在前
    （時間相同時，事件 ID 大的在前）。同一筆裡同一欄有兩個事件時取事件 ID 大的（不會發生，保險）。

    Raises:
        ValueError: 事件不是同一篇論文。
    """
    groups: dict[tuple[uuid.UUID, datetime], list[ClassificationFixEvent]] = {}
    papers: set[uuid.UUID] = set()
    for event in events:
        papers.add(event.paper_id)
        groups.setdefault((event.member_id, event.fixed_at), []).append(event)
    if len(papers) > 1:
        raise ValueError("歷史只能是同一篇論文的事件")
    ordered = sorted(
        groups.values(),
        key=lambda group: (group[0].fixed_at, max(event.event_id for event in group)),
        reverse=True,
    )
    entries: list[ClassificationFixEntry] = []
    for group in ordered:
        latest = sorted(group, key=lambda event: event.event_id, reverse=True)
        picks: dict[TaxonomyField, tuple[str, ...]] = {}
        reason: str | None = None
        for event in latest:
            picks.setdefault(event.field, event.keys)
            if reason is None:
                reason = event.reason
        entries.append(
            ClassificationFixEntry(
                fixed_at=latest[0].fixed_at,
                member_name=latest[0].member_name,
                topic=picks.get(TaxonomyField.TOPIC),
                method=picks.get(TaxonomyField.METHOD),
                reason=reason,
            )
        )
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class MemberPicks:
    """管理指令的一位成員：每一欄各自最新的選擇與那一次的時間（UTC）。那一欄沒修正過是 None。

    和有效值 view 判定「意見不同」的單位一樣（同一欄，每位成員各取最新一筆），所以造成意見不同的
    那一欄一定印得出來，即使那位成員之後只改了另一欄。
    """

    member_name: str
    topic: tuple[str, ...] | None
    topic_at: datetime | None
    method: tuple[str, ...] | None
    method_at: datetime | None

    def __post_init__(self) -> None:
        if (self.topic is None) != (self.topic_at is None):
            raise ValueError("主題與它的時間要一起有或一起沒有")
        if (self.method is None) != (self.method_at is None):
            raise ValueError("方法與它的時間要一起有或一起沒有")
        if self.topic is None and self.method is None:
            raise ValueError("至少要有一欄")
        for at, name in ((self.topic_at, "topic_at"), (self.method_at, "method_at")):
            if at is not None:
                _check_aware(at, name)

    @property
    def latest_at(self) -> datetime:
        """兩欄裡比較新的那個時間。"""
        return max(at for at in (self.topic_at, self.method_at) if at is not None)


def latest_picks_per_member(events: Iterable[ClassificationFixEvent]) -> tuple[MemberPicks, ...]:
    """每位成員、每一欄各自最新的選擇（管理指令列出意見不同時用）。同一欄的新舊照（時間, 事件 ID）
    比，和有效值 view 的 `created_at DESC, id DESC` 一樣。同一位成員改了名字也算同一位
    （依成員 ID），
    姓名取最新那一筆事件的。成員照兩欄裡比較新的時間排，新的在前。"""
    latest: dict[tuple[uuid.UUID, TaxonomyField], ClassificationFixEvent] = {}
    newest_event: dict[uuid.UUID, ClassificationFixEvent] = {}
    for event in events:
        rank = (event.fixed_at, event.event_id)
        key = (event.member_id, TaxonomyField(event.field))
        kept = latest.get(key)
        if kept is None or rank > (kept.fixed_at, kept.event_id):
            latest[key] = event
        top = newest_event.get(event.member_id)
        if top is None or rank > (top.fixed_at, top.event_id):
            newest_event[event.member_id] = event
    picks: list[MemberPicks] = []
    for member_id, top in newest_event.items():
        topic = latest.get((member_id, TaxonomyField.TOPIC))
        method = latest.get((member_id, TaxonomyField.METHOD))
        picks.append(
            MemberPicks(
                member_name=top.member_name,
                topic=None if topic is None else topic.keys,
                topic_at=None if topic is None else topic.fixed_at,
                method=None if method is None else method.keys,
                method_at=None if method is None else method.fixed_at,
            )
        )
    return tuple(sorted(picks, key=lambda pick: pick.latest_at, reverse=True))
