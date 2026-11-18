"""中文重點的正規化與 domain 驗證（PRD FR-14、NFR-4、Story 2.4、ADR-0032）。

模型輸出是不可信的輸入：Pydantic（`ZhSummaryOutput`）只管形狀，這裡管內容。

**正規化**（`normalize_zh_summary`，`services/ai_results.py` 寫入前也用同一個函式）：每欄 NFC、
連續空白縮成一個、去頭尾；結果是空的，或整欄只是「摘要未提及／未提及／無／N/A」這類字，或整欄只是
講「摘要沒寫什麼」的說明句（「摘要未明確說明具體研究方法與資料來源」，封閉詞表組成，
`_whole_field_meta`，2026-11-18），存 NULL。四欄都存 NULL 時是一筆四欄全空的有效結果。

**拒收規則**（`validate_zh_summary`），依下面的順序檢查，第一個不過的就是原因代碼：

1. 存不進資料庫（NUL、單獨的代理字元）→ `llm_output_unstorable`。
2. 雙向控制字元、tag 字元、HTML 實體；夾標記或連結：標籤樣的 `<`、欄位開頭的 `>`（完整的比較式
   如 `p < .05`、`>50%`、`x<y` 不算）、反引號、粗體、Markdown 連結、網址、裸網域（輸入裡有的首字
   大寫品牌名稱如 `Amazon.com` 除外）→ `zh_summary_unsafe_text`。
3. 夾「摘要未提及…」這類講摘要本身的句子 → `zh_summary_placeholder_text`。
4. 任一欄正規化後的字元數超過 `max_chars_per_field` → `zh_summary_field_too_long`。
5. 不是繁中：沒有漢字、漢字比例低於 `min_cjk_percent`、或含簡體專用字 → `zh_summary_not_zh_hant`。
   漢字比例不算「中文名稱（英文）」括號裡的英文（緊接在漢字後面、有閉合、最外層而且不巢狀；整欄
   合計最多 8 個詞不算，超過的部分照算）。
6. 逐句翻譯：摘要句數 ≥ `translation_min_abstract_sentences`，而且輸出句數 ≥ 摘要句數
   → `zh_summary_sentence_translation`。
7. 編造：有輸入（標題＋摘要）沒有的數字（含正負號、指數、萬／億等單位、百分比）、字母開頭的英數詞
   （`B2B`、`N120`）或英文詞，或用了不支援的數字寫法（數字後面的上標、分數字元、`^`、`×10`、`፱`
   這類數字字元）→ `zh_summary_unsupported_content`。輸入用 `ExtendedVocabulary` 讀（2026-11-18）：
   空白千分位、月份英文名（只對「9 月」這種接「月」的數字）、年代（只對接「年代」的數字）、
   `10-40%`、`10% points`、輸出正數對輸入同值負數、英文詞的詞形變化、黏字、`vs`。數字的新規則一律
   以既有讀法讀到的「完整數字」為單位，不會從科學記號、指數、千分位、帶量詞或百分比的數字中間讀出
   片段；輸出裡英數詞、月份、年代的數字只能整個換掉，換完的每個完整數字都要是換之前同位置的那一個。
   通用縮寫的允許清單（`config/zh_summary_acronyms.yaml`，`ZhSummaryRules.acronyms`，2026-11-18）：
   輸出寫的縮寫和登記的一模一樣（大小寫也一樣），而且輸入有它登記的某一個全名（不分大小寫、
   空白／連字號／連在一起不分、只有最後一個詞容許詞形變化）時認得；只有縮寫在清單裡、輸入沒有
   全名的照樣擋。

第 2～7 條都在「檢查用副本」（`_check_copy`）上做：HTML 實體解碼、拿掉零寬等格式字元、NFKC、
各種文字的十進位數字換成 0～9、破折號類換成 `-`、空白縮成一個。存進資料庫的仍是
`normalize_text` 的結果。

判準的細節與抓不到的情況寫在 ADR-0032。
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from radar.domain.ai_outputs import (
    DocumentType,
    LlmInput,
    OutputRejectedError,
    ZhSummaryContent,
    check_output_storable,
    unwrap_document,
)
from radar.domain.errors import ErrorCode
from radar.domain.llm_schemas import ZhSummaryOutput

# 共用的文字檢查在 `text_checks.py`（2.6 搬過去共用）；這裡照原本的名字用。編造判準用的是中文重點
# 專用的 `ExtendedVocabulary`（2026-11-18 把輸入讀得更完整），研究模型照舊用 `InputVocabulary`。
# `SIMPLIFIED_ONLY`、`normalize_text` 是公開名稱，從這個模組也拿得到。
from radar.domain.validators.text_checks import BIDI_OR_TAG as _BIDI_OR_TAG
from radar.domain.validators.text_checks import ENGLISH_TOKEN as _ENGLISH_TOKEN
from radar.domain.validators.text_checks import ENGLISH_WORD as _ENGLISH_WORD
from radar.domain.validators.text_checks import HTML_ENTITY as _HTML_ENTITY
from radar.domain.validators.text_checks import SIMPLIFIED_ONLY as SIMPLIFIED_ONLY
from radar.domain.validators.text_checks import ExtendedVocabulary as _ExtendedVocabulary
from radar.domain.validators.text_checks import check_copy as _check_copy
from radar.domain.validators.text_checks import has_markup as _has_markup
from radar.domain.validators.text_checks import has_unsupported_number as _has_unsupported_number
from radar.domain.validators.text_checks import is_han as _is_han
from radar.domain.validators.text_checks import normalize_text as normalize_text
from radar.domain.validators.text_checks import numbers as _numbers

FIELDS: Final = ("research_question", "method", "theory", "findings")

# 允許清單的縮寫：英文字母開頭、只有英文字母與數字、至少 2 個字元（`DEMATEL`、`fNIRS`、`P2P`）。
# 連字號串（`PLS-SEM`）不登記，每一段各自登記。
_ACRONYM: Final = re.compile(r"[A-Za-z][A-Za-z0-9]++")
# 全名：英文詞（只有英文字母），詞與詞之間一個空白或一個連字號。分隔是固定的非字母字元，
# 每個位置只有一種讀法，線性時間。
_FULL_NAME: Final = re.compile(r"[A-Za-z]++(?:[ -][A-Za-z]++)*+")
_FULL_NAME_SEPARATOR: Final = re.compile(r"[ -]")
# 設定檔的上下限（第一輪修正，Codex X2、審查 A6）：全名比對對輸入長度是線性，耗時另外和清單的大小
# 成正比，所以清單的大小要有上限；全名太短（一個常見的單字）幾乎每篇摘要都會「剛好有全名」。
# 現有最長的全名是 MULTIMOORA 的 11 個詞，上限取 12。
MAX_FULL_NAME_WORDS: Final = 12
MAX_FULL_NAMES_PER_ACRONYM: Final = 12
MAX_ACRONYMS: Final = 200
# 只有一個詞的全名至少幾個字母（`electroencephalogram` 是 20 個）。
MIN_SINGLE_WORD_LETTERS: Final = 10
# 每個全名拿掉空白與連字號後至少幾個字母（第二輪修正，審查 L4：`a b` 這種太短的全名幾乎哪裡都
# 對得上）。現有最短的是 P2P 的 `peer to peer`，10 個字母。
MIN_FULL_NAME_LETTERS: Final = 6


def full_name_key(name: str) -> str:
    """全名比對用的寫法：小寫、拿掉空白與連字號（`multi-criteria`＝`multi criteria`＝
    `multicriteria`）。用來找重複的全名（同一筆裡、不同筆之間）。"""
    return name.lower().replace(" ", "").replace("-", "")


class AcronymEntry(BaseModel):
    """`config/zh_summary_acronyms.yaml` 的一筆：縮寫（輸出要照這個大小寫寫）與它的英文全名
    （可以有多個寫法；輸入裡出現其中一個才放行）。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    acronym: str
    # YAML 的清單是 list：這一欄不用 strict（strict 的 tuple 不收 list），項目照樣只收字串。
    full_names: tuple[str, ...] = Field(strict=False, min_length=1)

    @field_validator("acronym")
    @classmethod
    def _check_acronym(cls, value: str) -> str:
        if _ACRONYM.fullmatch(value) is None:
            raise ValueError("acronym 要是英文字母開頭、只有英文字母與數字、至少 2 個字元")
        return value

    @field_validator("full_names")
    @classmethod
    def _check_full_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_FULL_NAMES_PER_ACRONYM:
            raise ValueError(f"full_names 最多 {MAX_FULL_NAMES_PER_ACRONYM} 個寫法")
        for name in value:
            if _FULL_NAME.fullmatch(name) is None:
                raise ValueError(
                    "full_names 的每一項要是英文詞，詞與詞之間一個空白或一個連字號"
                    "（不能是空的、不能有前後空白、數字或其他符號）"
                )
            words = len(_FULL_NAME_SEPARATOR.split(name))
            if words > MAX_FULL_NAME_WORDS:
                raise ValueError(f"full_names 的每一項最多 {MAX_FULL_NAME_WORDS} 個詞")
            if words == 1 and len(name) < MIN_SINGLE_WORD_LETTERS:
                raise ValueError(
                    "full_names 的每一項至少要兩個詞；只有一個詞時至少要 "
                    f"{MIN_SINGLE_WORD_LETTERS} 個字母"
                )
            if len(full_name_key(name)) < MIN_FULL_NAME_LETTERS:
                raise ValueError(
                    f"full_names 的每一項拿掉空白與連字號後至少要 {MIN_FULL_NAME_LETTERS} 個字母"
                )
        if len({full_name_key(name) for name in value}) != len(value):
            raise ValueError(
                "full_names 有重複（不分大小寫，空白、連字號、連在一起都算同一種寫法）"
            )
        return value


def _unique_acronyms(entries: tuple[AcronymEntry, ...]) -> tuple[AcronymEntry, ...]:
    if len(entries) > MAX_ACRONYMS:
        raise ValueError(f"acronyms 最多 {MAX_ACRONYMS} 筆")
    seen: set[str] = set()
    owners: dict[str, str] = {}
    for entry in entries:
        key = entry.acronym.casefold()
        if key in seen:
            raise ValueError(f"acronym {entry.acronym} 寫了兩次（不分大小寫）")
        seen.add(key)
        for name in entry.full_names:
            owner = owners.setdefault(full_name_key(name), entry.acronym)
            if owner != entry.acronym:
                raise ValueError(
                    f"{owner} 與 {entry.acronym} 登記了同一個全名（不分大小寫，空白、連字號、"
                    "連在一起都算同一種寫法）"
                )
    return entries


class ZhSummaryAcronyms(BaseModel):
    """`config/zh_summary_acronyms.yaml`（通用縮寫的允許清單，只經 `radar/config.py` 讀）：
    `version` 與 `acronyms` 兩個鍵都要寫。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(strict=True, ge=1)
    acronyms: tuple[AcronymEntry, ...]

    @field_validator("acronyms")
    @classmethod
    def _check_unique(cls, value: tuple[AcronymEntry, ...]) -> tuple[AcronymEntry, ...]:
        return _unique_acronyms(value)


class ZhSummaryThresholds(BaseModel):
    """`config/zh_summary.yaml` 的門檻（只經 `radar/config.py` 讀）。三個鍵都要寫，只收整數，
    不收別的鍵（允許清單在另一個檔）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # 每欄正規化後最多幾個字元（PRD FR-14：預設 80）。
    max_chars_per_field: int = Field(strict=True, ge=1)
    # 摘要至少幾句才檢查逐句翻譯；1 句的摘要任何輸出都會被當成翻譯，所以最少 2。
    translation_min_abstract_sentences: int = Field(strict=True, ge=2)
    # 每欄漢字至少佔（漢字＋英文詞）的百分之幾。
    min_cjk_percent: int = Field(strict=True, ge=1, le=100)


class ZhSummaryRules(ZhSummaryThresholds):
    """中文重點的驗證規則：`config/zh_summary.yaml` 的門檻，加上 `config/zh_summary_acronyms.yaml`
    的允許清單（`radar/config.load_zh_summary_rules` 兩個檔一起讀）。直接建的時候允許清單預設是
    空的（沒有任何縮寫因為清單放行）。"""

    acronyms: tuple[AcronymEntry, ...] = ()

    @field_validator("acronyms")
    @classmethod
    def _check_unique(cls, value: tuple[AcronymEntry, ...]) -> tuple[AcronymEntry, ...]:
        return _unique_acronyms(value)

    def acronym_full_names(self) -> dict[str, tuple[str, ...]]:
        """縮寫 → 全名的寫法（交給 `ExtendedVocabulary.build`）。"""
        return {entry.acronym: entry.full_names for entry in self.acronyms}


# --- 正規化 -------------------------------------------------------------------------------

# 比對「整欄只是佔位字」時先拿掉的字元：空白、標點、底線（`\W` 不含漢字、字母、數字）。
_NOT_WORD: Final = re.compile(r"[\W_]+")
# 「講摘要本身」的主詞。整欄比對（存 NULL）認得全部主詞；夾在內容裡的比對分兩種（見下面的
# `_EMBEDDED_PLACEHOLDER`）：只有「摘要」一定是在講摘要；原文、文中、本文、本研究、研究、作者、
# 文章、論文也可能是研究材料或研究結果的開頭（「原文未揭露姓名時仍可推得作者身分」「本文發現…」）。
_ABSTRACT_SUBJECTS: Final = r"(?:摘要|原文|文中)"
_PAPER_SUBJECTS: Final = r"(?:本文|本研究|研究|作者|文章|論文)"
_NEGATION: Final = r"(?:中|裡|內)?(?:並|均|皆|也)?(?:未|沒有|沒|無|不)(?:明確|特別)?"
_META_VERBS: Final = r"(?:提及|提到|提供|說明|描述|指出|交代|報告|載明|詳述|揭露|涉及)"
_PLACEHOLDER_ZH: Final = re.compile(
    rf"(?:{_ABSTRACT_SUBJECTS}|{_PAPER_SUBJECTS})?{_NEGATION}"
    rf"{_META_VERBS}?(?:任何)?(?:相關)?(?:所使用|所採用|所用|使用|採用)?(?:的)?"
    r"(?:理論|理論框架|方法|研究方法|研究問題|問題|發現|結果|資訊|資料|內容)?"
    r"|不詳|未知|不明|從缺|不適用"
)
_PLACEHOLDER_EN: Final = re.compile(
    r"na|none|null|nil|unknown|notapplicable"
    r"|not(?:mentioned|specified|reported|stated|available|provided|given)(?:intheabstract|inabstract)?"
)


# 欄位名稱（「研究方法」「理論」…）。
_FIELD_NAMES: Final = (
    r"(?:研究問題|問題|研究方法|方法|理論框架|理論基礎|理論|研究發現|發現|研究結果|結果)"
)
# 欄位開頭的「欄位名稱＋冒號」（「理論：未提及」），整欄比對前先拿掉。
_FIELD_LABEL: Final = re.compile(rf"^{_FIELD_NAMES}\s?:")
# 欄位中間的「欄位名稱＋冒號」（「摘要未說明資料來源；研究方法：統計方法」）：有就不算整欄說明
# （第二輪修正，Codex Y1）。
_FIELD_LABEL_INSIDE: Final = re.compile(rf"{_FIELD_NAMES}\s?:")

# 整欄說明（ADR-0032 的「通用縮寫的允許清單與整欄說明存成空值」更新）：整欄（拿掉開頭的「欄位
# 名稱＋冒號」、頭尾的空白與標點之後；中間的標點保留）只由一句或幾句「講摘要沒寫什麼」的子句
# 組成，每個字都來自下面這些封閉的詞表；任何詞表外的字或符號出現就不算（所以「僅提及『禮物經濟』
# 的概念」「但採用了深度訪談」「受訪者未提及隱私疑慮」不算）。
# - 標點當邊界（第二輪修正，Codex Y1：原本拿掉標點才比對，研究要素又可以不帶連接詞連續出現，
#   「摘要未說明資料來源；研究方法：統計方法與實證分析」後半被吞進前一句的受詞）：逗號、句號、
#   分號、冒號、驚嘆號、問號、括號、空白（全形、半形）是子句邊界（`_META_BOUNDARY`），邊界之後
#   一定要開始一個新的子句或結尾的「填 null」；研究要素之間只能用連接詞或頓號「、」，兩個研究
#   要素直接相連不收；欄位中間出現「欄位名稱＋冒號」（`_FIELD_LABEL_INSIDE`）就不算。
# - 否定子句（`_META_NEGATIVE_CLAUSE`）：主詞＋地點（在／於＋摘要…＋中，在否定詞前後都可以）＋
#   否定（未、沒有、沒，前面可接並、均、皆、也、亦、尚）＋程度（明確、具體、清楚、詳細、特別、
#   直接，最多兩個）＋後設動詞（一定要有）＋研究要素（可省略；兩個以上時一定要用與、或、及、和、
#   以及…或頓號並列，第二輪修正；前面可接其、任何、相關、具體、所使用的…；「使用、採用」一定要帶
#   「所」或「的」，第一輪修正；動詞後面最多一個「的」，第二輪修正）。
# - 「僅／只＋提出、提及…＋研究要素」子句（`_META_ONLY_CLAUSE`，「摘要僅提出解釋框架」）：只能接
#   研究要素清單裡的名詞。
# - 主詞沿用（第一輪修正，Codex X1、審查 A2、A7）：每一欄從「還不知道主詞」開始。子句裡明寫了
#   摘要、原文、文中（當主詞或當地點「在／於摘要中」）→ 這個子句與之後沒寫主詞的子句是「摘要類」；
#   明寫了本文、本研究、研究、作者、文章、論文（當主詞，或當地點「在／於本文、文章、論文中」）→
#   「論文類」；兩類都寫了算論文類。之後又明寫另一類就換過去。
#   - 否定子句的動詞：說明類（`_META_STATE`：說明、提及、描述、指出…，講「寫了什麼」）三種狀態都
#     收；「提出、提供、報告、揭露、涉及、呈現」只有摘要類才收——「未提出假設」「作者未提出假設」
#     「在本文中未提出假設」可能是在講研究本身（探索性研究、概念性論文），照修改前的規則處理。
#   - 「僅／只…」子句只有摘要類才收（開頭沒寫主詞的「僅提出概念框架」、接在論文類後面的都不收）。
# - 至少要有一個否定子句；子句之間可以直接相連或隔一串子句邊界，新的子句開頭可以接而、但、且、
#   卻；最後可以接「填 null」「故填 null」（「填」與「null」之間可以有一個空白）。
# 只比對整欄：前面有內容、後面多一句說明的（「以問卷調查分析（摘要未說明樣本數）」）不符合，照樣由
# `_EMBEDDED_PLACEHOLDER` 拒收。
# 做法（`_whole_field_meta`）：從頭一個子句一個子句往後讀（小的狀態機：狀態是目前的主詞類別），每個
# 子句用固定開頭的正規式在目前位置比一次（子句前面有邊界就先跳過那串邊界）。每一段都用佔有式量詞或
# 在固定位置分岔（不回頭重試），詞表裡有前綴關係的詞長的排前面；子句比不上時只會在動詞之前的固定
# 幾個字內失敗，所以整欄是線性時間。
_META_TEXT_WORDS: Final = r"(?:摘要|原文|文中)"
_META_PAPER_WORDS: Final = r"(?:本文|本研究|研究|作者|文章|論文)"
_META_PAPER_PLACES: Final = r"(?:本文|文章|論文)"
_META_IN: Final = r"(?:中|裡|內)?+"
_META_SUBJECT: Final = (
    rf"(?:(?P<text_subject>{_META_TEXT_WORDS})|(?P<paper_subject>{_META_PAPER_WORDS})){_META_IN}"
)
_META_NEGATION: Final = r"(?:並|均|皆|也|亦|尚)?+(?:未|沒有|沒)"
_META_DEGREE: Final = r"(?:(?:明確|具體|清楚|詳細|特別|直接)地?+){0,2}+"
_META_STATE: Final = r"(?:提及|提到|說明|描述|指出|指明|交代|載明|詳述|陳述|闡述|列出|寫出|寫明)"
# 只有摘要類才收的動詞。
_META_SAY: Final = r"(?:提出|提供|報告|揭露|涉及|呈現)"
_META_ONLY: Final = r"(?:僅有|只有|僅|只)(?:提出|提及|提到|說明|描述|指出|呈現)"
_META_ELEMENT: Final = (
    r"(?:研究方法|研究設計|研究對象|研究問題|研究發現|研究結果|研究模型|研究樣本|研究資料|研究架構"
    r"|研究假設|分析方法|分析框架|計量方法|統計方法|實證資料|實證數據|實證分析|理論框架|理論基礎"
    r"|理論|解釋框架|概念框架|資料來源|資料|數據來源|數據|樣本數|樣本大小|樣本|變數|假設|方法論"
    r"|方法|發現|結果)"
)
# 「使用、採用」一定要帶「所」或「的」（第一輪修正，審查 A1：單獨的「，採用實證分析」是內容）；
# 動詞後面最多一個「的」（第二輪修正，審查 L3：「採用的的方法」不收）。
_META_MODIFIER: Final = (
    r"其?+(?:任何|相關|具體|明確|詳細|特定)?+的?+(?:所使用|所採用|所用|(?:使用|採用)(?=的))?+的?+"
)
# 研究要素之間一定要有連接詞或頓號（第二輪修正，Codex Y1）。
_META_ITEMS: Final = (
    rf"{_META_MODIFIER}{_META_ELEMENT}"
    rf"(?:(?:以及|或是|與|或|及|和|跟|暨|、){_META_MODIFIER}{_META_ELEMENT})*+"
)
_META_CLAUSE_JOIN: Final = r"(?:但是|而|但|且|卻)?+"


def _meta_place(name: str) -> str:
    return (
        rf"(?:(?:在|於)(?:(?P<text_{name}>{_META_TEXT_WORDS})"
        rf"|(?P<paper_{name}>{_META_PAPER_PLACES})){_META_IN})?+"
    )


_META_NEGATIVE_CLAUSE: Final = re.compile(
    rf"{_META_CLAUSE_JOIN}(?:{_META_SUBJECT})?+{_meta_place('before')}{_META_NEGATION}"
    rf"{_meta_place('after')}{_META_DEGREE}(?:(?P<state>{_META_STATE})|{_META_SAY})"
    rf"了?+(?:{_META_ITEMS})?+"
)
_META_ONLY_CLAUSE: Final = re.compile(
    rf"{_META_CLAUSE_JOIN}(?:(?P<text_subject>{_META_TEXT_WORDS}){_META_IN})?+{_META_ONLY}了?+"
    rf"{_META_ITEMS}"
)
_META_TAIL: Final = re.compile(r"(?:故|因此|所以|因而)?+(?:填入|填寫|填) ?(?:null|none|空值)")
# 子句邊界（第二輪修正）：逗號、句號、分號、冒號、驚嘆號、問號、括號、空白（全形的在檢查用副本裡
# 已經是半形；句號另收「。」）。頓號不是子句邊界，只能接在研究要素之間。
_META_BOUNDARY: Final = re.compile(r"[,.。;:!?() ]++")
# 頭尾的空白與標點（整欄外層的括號、句尾標點）。
_META_EDGE: Final = re.compile(r"[\W_]")
_TEXT_GROUPS: Final = ("text_subject", "text_before", "text_after")
_PAPER_GROUPS: Final = ("paper_subject", "paper_before", "paper_after")


def _subject_kind(match: re.Match[str], current: str | None) -> str | None:
    """子句明寫的主詞類別（`"paper"`、`"text"`），沒寫就沿用 `current`。"""
    groups = match.groupdict()
    if any(groups.get(name) for name in _PAPER_GROUPS):
        return "paper"
    if any(groups.get(name) for name in _TEXT_GROUPS):
        return "text"
    return current


def _meta_text(copy: str) -> str:
    """整欄說明比對用的文字：`copy`（已拿掉開頭欄位名稱的檢查用副本）去掉頭尾的空白與標點、
    不分大小寫；中間的標點保留。用迴圈找頭尾，線性時間。"""
    start, end = 0, len(copy)
    while start < end and _META_EDGE.match(copy[start]):
        start += 1
    while end > start and _META_EDGE.match(copy[end - 1]):
        end -= 1
    return copy[start:end].casefold()


def _whole_field_meta(text: str) -> bool:
    """`text`（`_meta_text` 的結果）整欄只由說明子句組成（規則見上面的註解）。子句與子句之間可以
    直接相連或隔一串邊界；邊界之後一定要是新的子句或結尾的「填 null」。"""
    if _FIELD_LABEL_INSIDE.search(text) is not None:
        return False
    position = 0
    kind: str | None = None
    negatives = 0
    while position < len(text):
        boundary = _META_BOUNDARY.match(text, position)
        if boundary is not None:
            position = boundary.end()
        negative = _META_NEGATIVE_CLAUSE.match(text, position)
        if negative is not None:
            kind = _subject_kind(negative, kind)
            if negative.group("state") is None and kind != "text":
                return False
            negatives += 1
            position = negative.end()
            continue
        only = _META_ONLY_CLAUSE.match(text, position)
        if only is not None:
            kind = _subject_kind(only, kind)
            if kind != "text":
                return False
            position = only.end()
            continue
        return negatives > 0 and _META_TAIL.fullmatch(text, position) is not None
    return negatives > 0


def is_placeholder(text: str) -> bool:
    """整欄只是「摘要未提及／未提及／無／N/A」這類字，或整欄只是講「摘要沒寫什麼」的說明句
    （`_whole_field_meta`，例如「摘要未明確說明具體研究方法與資料來源」）。前兩種在檢查用副本上
    拿掉空白與標點、不分大小寫後比對，拿掉之後什麼都不剩（例如只有「—」或零寬空白）也算；整欄說明
    保留中間的標點當子句邊界比對（第二輪修正）。全形逗號先換成半形逗號再做檢查用副本，才不會被
    當成頓號。"""
    copy = _FIELD_LABEL.sub("", _check_copy(text), count=1)
    key = _NOT_WORD.sub("", copy).casefold()
    if not key or _PLACEHOLDER_ZH.fullmatch(key) is not None:
        return True
    if _PLACEHOLDER_EN.fullmatch(key) is not None:
        return True
    meta_copy = _FIELD_LABEL.sub("", _check_copy(text.replace("，", ",")), count=1)
    return _whole_field_meta(_meta_text(meta_copy))


def _normalize_field(value: str | None) -> str | None:
    if value is None:
        return None
    text = normalize_text(value)
    if not text or is_placeholder(text):
        return None
    return text


def normalize_zh_summary(output: ZhSummaryOutput) -> ZhSummaryContent:
    """四欄正規化成要存的內容：沒提到（null、空白、只有佔位字）的欄位是 None。

    不做拒收判斷；要存的輸出一定先經過 `validate_zh_summary`。

    Raises:
        TypeError: `output` 不是 `ZhSummaryOutput`。
        ValueError: 有欄位存不進資料庫（先經 `validate_zh_summary` 就不會發生）。
    """
    if not isinstance(output, ZhSummaryOutput):
        raise TypeError("normalize_zh_summary 只收 ZhSummaryOutput")
    return ZhSummaryContent(
        research_question=_normalize_field(output.research_question),
        method=_normalize_field(output.method),
        theory=_normalize_field(output.theory),
        findings=_normalize_field(output.findings),
    )


# --- 判準 ---------------------------------------------------------------------------------

# 欄位裡夾的「講摘要本身」的句子：
# - 「摘要」後面 8 個字內接否定詞、再 4 個字內接「提及、說明…」；「未在摘要中…」；「未在原文
#   （文中）中提及…」（「姓名未在原文中出現」不算）。
# - 原文、文中、本文、本研究、研究、作者、文章、論文：否定詞要緊接在後面（只能隔「中、並、也、
#   明確」這類字），接著「提及、說明…」，再接研究要素（樣本數、方法、理論…）或直接斷句。所以
#   「原文未揭露姓名時仍可推得作者身分」「作者未揭露身分的評論」「本文探討使用者為何不揭露個人
#   資訊」都不算。
# - 英文 `not mentioned`、`n/a`。
# 只寫「未提及」不算（「受訪者未提及隱私疑慮」可能是真的發現）。
_RESEARCH_ELEMENTS: Final = (
    r"(?:樣本數|樣本大小|樣本|受試者人數|研究方法|分析方法|方法|理論基礎|理論框架|理論"
    r"|研究問題|問題|研究發現|發現|研究結果|結果|資料來源|數據|變數|假設|研究模型|模型"
    r"|研究設計|研究對象|細節)"
)
_EMBEDDED_PLACEHOLDER: Final = re.compile(
    rf"摘要[^。！？!?；;]{{0,8}}?(?:未|沒有|沒|無|並未|不)[^。！？!?；;]{{0,4}}?{_META_VERBS}"
    rf"|(?:原文|文中|{_PAPER_SUBJECTS}){_NEGATION}{_META_VERBS}(?:任何|相關|具體)?(?:所使用|所採用|所用|使用|採用)?(?:的)?"
    rf"(?:{_RESEARCH_ELEMENTS}|(?=[、,.。；;！？!?)]|$))"
    r"|(?:未|沒有|沒|並未)(?:在|於)摘要"
    rf"|(?:未|沒有|沒|並未)(?:在|於)(?:原文|文中)(?:中|裡|內)?{_META_VERBS}"
    r"|(?<![A-Za-z])not\s+(?:mentioned|specified|reported|stated)(?![A-Za-z])"
    r"|(?<![A-Za-z])n/a(?![A-Za-z])",
    re.IGNORECASE,
)
# 摘要的句子結尾：一串 `.!?` 後面接空白。開頭不能接在 `.!?` 後面、整串用佔有式量詞，所以一長串
# 句點後面沒有空白時只掃一次（原本的 `[.!?]+\s+` 會平方級回溯）。
_ABSTRACT_SENTENCE_END: Final = re.compile(r"(?<![.!?])[.!?]++\s+")
_OUTPUT_SENTENCE_END: Final = re.compile(r"[。！？!?]+")
# 括號（全形括號在檢查用副本裡已經是半形）。
_PARENTHESIS: Final = re.compile(r"[()]")
# 每欄「中文名稱（英文）」括號裡的英文，合計最多幾個詞不算進漢字比例（「整合型科技接受模型（unified
# theory of acceptance and use of technology）」是 8 個）；超過的部分照算（審查 C7、A-3、B-6）。
_MAX_PARENTHESIZED_WORDS: Final = 8
_HAS_WORD: Final = re.compile(r"\w")


def _input_texts(llm_input: LlmInput) -> tuple[str, str | None]:
    """（標題＋摘要合在一起的檢查用副本, 摘要的檢查用副本；沒有摘要時是 None），已拿掉
    `<document>` 包裝。"""
    parts: list[str] = []
    abstract: str | None = None
    for block in llm_input.blocks:
        text = _check_copy(unwrap_document(block))
        parts.append(text)
        if block.document_type is DocumentType.ABSTRACT:
            abstract = text
    return "\n".join(parts), abstract


def _reject(code: ErrorCode, detail: str) -> OutputRejectedError:
    return OutputRejectedError(code, detail)


def _check_not_zh_hant(name: str, text: str, rules: ZhSummaryRules) -> None:
    """`text` 是檢查用副本。漢字比例不算「中文名稱（英文）」括號裡的英文
    （`_english_word_count`）。"""
    han = sum(1 for char in text if _is_han(char))
    words = _english_word_count(text)
    if han == 0:
        raise _reject(ErrorCode.ZH_SUMMARY_NOT_ZH_HANT, f"{name} 沒有漢字")
    if han * 100 < rules.min_cjk_percent * (han + words):
        raise _reject(ErrorCode.ZH_SUMMARY_NOT_ZH_HANT, f"{name} 的漢字比例太低")
    if any(char in SIMPLIFIED_ONLY for char in text):
        raise _reject(ErrorCode.ZH_SUMMARY_NOT_ZH_HANT, f"{name} 有簡體專用字")


def _english_word_count(text: str) -> int:
    """算漢字比例用的英文詞數。「中文名稱（英文）」括號（prompt 規則 3）裡的英文，整欄合計最多
    `_MAX_PARENTHESIZED_WORDS` 個詞不算，超過的部分照算。算數的括號要：緊接在漢字（或漢字＋
    一個空白）後面、有閉合、是最外層（不在別的括號裡）、裡面沒有別的括號（審查 C5）。其他括號
    （沒閉合、巢狀、前面不是漢字）裡的英文照算。只掃一次括號，線性時間。"""
    words = len(_ENGLISH_WORD.findall(text))
    exempt = 0
    depth = 0
    opened = 0
    nested = False
    for match in _PARENTHESIS.finditer(text):
        position = match.start()
        if match.group(0) == "(":
            if depth == 0:
                opened, nested = position, False
            else:
                nested = True
            depth += 1
            continue
        if depth == 0:
            continue  # 沒有對應開括號的 `)`
        depth -= 1
        if depth == 0 and not nested and _after_han(text, opened):
            exempt += len(_ENGLISH_WORD.findall(text, opened + 1, position))
    return words - min(exempt, _MAX_PARENTHESIZED_WORDS)


def _after_han(text: str, position: int) -> bool:
    """`position` 前面緊接漢字，或漢字＋一個空白。"""
    before = text[max(0, position - 2) : position]
    before = before[:-1] if before.endswith(" ") else before[-1:]
    return len(before) == 1 and _is_han(before)


def count_abstract_sentences(abstract: str) -> int:
    """摘要的句數：以 `.`、`!`、`?` 後面接空白切開，數非空的段落。"""
    return sum(1 for part in _ABSTRACT_SENTENCE_END.split(abstract.strip()) if part.strip())


def count_output_sentences(texts: Iterable[str]) -> int:
    """輸出的句數：每欄以 `。！？!?` 切開、數有字的段落；每個非空欄位至少算一句。"""
    total = 0
    for text in texts:
        parts = [part for part in _OUTPUT_SENTENCE_END.split(text) if _HAS_WORD.search(part)]
        total += max(1, len(parts))
    return total


def validate_zh_summary(
    output: BaseModel | str, llm_input: LlmInput, rules: ZhSummaryRules
) -> ZhSummaryContent:
    """驗證中文重點（規則與順序見模組說明），通過時回正規化後要存的內容。

    Raises:
        OutputRejectedError: 不通過；`code` 是原因代碼，訊息只有欄位名稱與規則，不含輸出內容。
        TypeError: `output` 不是 `ZhSummaryOutput`（prompt 的 schema 設錯，是程式的錯）。
    """
    if not isinstance(output, ZhSummaryOutput):
        raise TypeError("中文重點的驗證只收 ZhSummaryOutput")
    check_output_storable(output.model_dump(mode="json"))

    input_text, abstract = _input_texts(llm_input)
    for name in FIELDS:
        value = getattr(output, name)
        if value is None:
            continue
        if _BIDI_OR_TAG.search(value) or _HTML_ENTITY.search(value):
            raise _reject(ErrorCode.ZH_SUMMARY_UNSAFE_TEXT, f"{name} 含雙向控制字元或 HTML 實體")
        if _has_markup(value, input_text):
            raise _reject(ErrorCode.ZH_SUMMARY_UNSAFE_TEXT, f"{name} 含標記或連結")

    content = normalize_zh_summary(output)
    present = {name: value for name, value in content.fields().items() if value is not None}
    checked = {name: _check_copy(value) for name, value in present.items()}

    for name, text in checked.items():
        if _EMBEDDED_PLACEHOLDER.search(text):
            raise _reject(ErrorCode.ZH_SUMMARY_PLACEHOLDER_TEXT, f"{name} 夾了講摘要本身的句子")
    for name, text in present.items():
        if len(text) > rules.max_chars_per_field:
            raise _reject(
                ErrorCode.ZH_SUMMARY_FIELD_TOO_LONG,
                f"{name} 有 {len(text)} 字，上限 {rules.max_chars_per_field}",
            )
    for name, text in checked.items():
        _check_not_zh_hant(name, text, rules)

    if abstract is not None:
        abstract_sentences = count_abstract_sentences(abstract)
        if (
            abstract_sentences >= rules.translation_min_abstract_sentences
            and count_output_sentences(checked.values()) >= abstract_sentences
        ):
            raise _reject(
                ErrorCode.ZH_SUMMARY_SENTENCE_TRANSLATION,
                f"摘要 {abstract_sentences} 句，輸出句數不少於摘要",
            )

    vocabulary = _ExtendedVocabulary.build(input_text, rules.acronym_full_names())
    for name, text in checked.items():
        if _has_unsupported_number(present[name], text):
            raise _reject(ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT, f"{name} 有不支援的數字寫法")
        number_text, unknown_alnum = vocabulary.number_text(text)
        if unknown_alnum is not None:
            raise _reject(ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT, f"{name} 有輸入沒有的英數詞")
        numbers, unreadable = _numbers(number_text)
        # 「讀不懂」（科學記號、`^`）看替換前與替換後兩份取聯集：替換不能把讀不懂變成讀得懂。
        unreadable = unreadable or _numbers(text)[1]
        if unreadable or not all(vocabulary.knows_number(number) for number in numbers):
            raise _reject(ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT, f"{name} 有輸入沒有的數字")
        for match in _ENGLISH_TOKEN.finditer(text):
            if not vocabulary.knows_token(match.group(0)):
                raise _reject(
                    ErrorCode.ZH_SUMMARY_UNSUPPORTED_CONTENT, f"{name} 有輸入沒有的英文詞"
                )
    return content


@dataclass(frozen=True)
class ZhSummaryValidator:
    """交給閘門 `LlmJob(validate_output=...)` 的驗證函式（符合 `OutputValidator` 的簽名）。"""

    rules: ZhSummaryRules

    def __call__(self, output: BaseModel | str, llm_input: LlmInput) -> None:
        validate_zh_summary(output, llm_input, self.rules)
