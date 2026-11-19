"""送雲端的放行（AD-9）：什麼文字可以送到 Anthropic／Cloudflare，只在這裡判定。

- `CloudSafeText` 是「已經放行、可以送雲端的文字」。它不是 `str` 的子類別，建構子要一個只有
  這個模組拿得到的私有記號（`_CLEARANCE`），所以只有 `clear_for_cloud` 做得出來；
  `LlmProvider`、`EmbeddingProvider` 只收它（mypy 擋一次、adapter 入口執行期再擋一次）。
- `clear_for_cloud(文字, 來源, 授權, 設定)` 依來源與授權決定放不放行；不放行丟
  `CloudClearanceError`（帶來源與 `domain/errors.py` 的原因代碼，不帶文字內容）。
- 放行表（ADR-0029）：
  - OpenAlex／Crossref 的摘要、標題、使用者的問題：放行。
  - prompt 模板：只能經 `clear_prompt_file` 放行（整個檔的雜湊要登記在 `domain/prompt_hashes.py`）；
    `clear_for_cloud` 拒收 `PROMPT_TEMPLATE`，呼叫端不能自己把文字標成模板（ADR-0030「依此更正」）。
  - OA 全文：授權是 CC BY、CC BY-SA、CC0 才放行；其他（含 NC、ND 類 CC、`other-oa`、
    付費、不明、沒有授權資訊）要 `RADAR_PAYWALLED_FULLTEXT_LLM` 開著才放行。
  - 上傳的 PDF 內容：不看授權，一律當付費，開關開著才放行。
  - Semantic Scholar 的摘要：還沒取得書面授權，一律擋下（開關也不放行）。
  - 空字串或只有空白：一律擋下；編不成 UTF-8 的文字（例如單獨的代理字元）也擋下。
- 新增來源時，`clear_for_cloud` 的 `match` 不寫規則就過不了 mypy（`assert_never`）。
- `join_cloud_safe_texts`：把幾段各自放行過的文字用空白接成一段（向量化的「標題＋空行＋摘要」）。

示範站可用（ADR-0007 第 5 點、ADR-0052）也在這裡判定：`is_demo_eligible` 是全專案唯一的判定，
`openalex_license` 把 OpenAlex 的授權字串對到 `ContentLicense`。
"""

import itertools
import unicodedata
import weakref
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, NoReturn, TypeIs, assert_never, final

from pydantic import BaseModel, ConfigDict, Field

from radar.domain import prompt_hashes
from radar.domain.errors import ErrorCode
from radar.domain.fingerprints import prompt_sha256
from radar.domain.identifiers import InvalidIdentifierError, normalize_openalex_id
from radar.domain.prompt_format import split_prompt_file


class ContentSource(StrEnum):
    """要送雲端的文字是從哪裡來的。

    和 `AbstractSource`（資料庫裡摘要的來源）不是同一份清單：呼叫的一方把自己資料的來源對應到
    這裡的值。2.2 的閘門必須從資料列（例如 `paper_abstract.source`）推出來源，不能讓自由輸入
    （請求參數、模型輸出）決定。
    """

    OPENALEX_ABSTRACT = "openalex_abstract"
    CROSSREF_ABSTRACT = "crossref_abstract"
    SEMANTIC_SCHOLAR_ABSTRACT = "semantic_scholar_abstract"  # 要等書面授權
    TITLE = "title"
    USER_QUERY = "user_query"  # 使用者在問答或搜尋打的字
    PROMPT_TEMPLATE = "prompt_template"  # repo 裡的 prompt 模板（`prompts/`）
    OA_FULLTEXT = "oa_fulltext"  # 開放取用的全文
    UPLOADED_PDF = "uploaded_pdf"  # 使用者上傳的 PDF 內容


class ContentLicense(StrEnum):
    """全文的授權。值照 OpenAlex 的寫法；`paywalled`、`unknown` 是本專案補的。"""

    CC_BY = "cc-by"
    CC_BY_SA = "cc-by-sa"
    CC0 = "cc0"
    CC_BY_NC = "cc-by-nc"
    CC_BY_ND = "cc-by-nd"
    CC_BY_NC_SA = "cc-by-nc-sa"
    CC_BY_NC_ND = "cc-by-nc-nd"
    OTHER_OA = "other-oa"
    PAYWALLED = "paywalled"
    UNKNOWN = "unknown"


# 算「CC 授權」、OA 全文不用開關就能送雲端的授權（ADR-0029 的保守選擇：NC、ND 類不算）。
CLOUD_CLEARED_LICENSES: Final[frozenset[ContentLicense]] = frozenset(
    {ContentLicense.CC_BY, ContentLicense.CC_BY_SA, ContentLicense.CC0}
)

# --- 示範站可用（ADR-0007 第 5 點、ADR-0052） -------------------------------------------------

# 示範站可以放完整內容的授權。和 `CLOUD_CLEARED_LICENSES` 現在是同一組值，但是兩個不同的決定
# （送雲端 vs. 公開轉載），之後可能分開改，所以各寫一份。
DEMO_ELIGIBLE_LICENSES: Final[frozenset[ContentLicense]] = frozenset(
    {ContentLicense.CC_BY, ContentLicense.CC_BY_SA, ContentLicense.CC0}
)

# OpenAlex 的授權字串和 `ContentLicense` 寫法不同的：OpenAlex 把 CC0 與公眾領域併成一個
# `public-domain`（官方授權清單的顯示名稱是「public domain (CC0)」，ADR-0052 有出處）。
_OPENALEX_LICENSE_ALIASES: Final[dict[str, ContentLicense]] = {
    "public-domain": ContentLicense.CC0,
}
# OpenAlex 不會給、本專案自己補的值：OpenAlex 字串剛好是這兩個也當不明。
_PROJECT_ONLY_LICENSES: Final[frozenset[ContentLicense]] = frozenset(
    {ContentLicense.PAYWALLED, ContentLicense.UNKNOWN}
)


def openalex_license(value: str | None) -> ContentLicense:
    """OpenAlex 的授權原字串（`primary_location.license`）→ `ContentLicense`。

    完全相同的寫法才算（不改大小寫、不去空白）；`public-domain` 當 CC0；沒有、看不懂的
    （例如 `mit`、`publisher-specific-oa`）一律 `UNKNOWN`。
    """
    if value is None:
        return ContentLicense.UNKNOWN
    alias = _OPENALEX_LICENSE_ALIASES.get(value)
    if alias is not None:
        return alias
    try:
        license = ContentLicense(value)
    except ValueError:
        return ContentLicense.UNKNOWN
    return ContentLicense.UNKNOWN if license in _PROJECT_ONLY_LICENSES else license


def normalize_publisher_name(value: str) -> str:
    """出版社名稱比對前的正規化：NFKC、`casefold`、連續空白縮成一個、去頭尾空白。"""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


@dataclass(frozen=True)
class PublisherExclusions:
    """示範站不放完整內容的出版社（`config/demo_publishers.yaml`）。

    `ids` 是正規化的 OpenAlex ID（例如 `P4310320990`），`names` 是 `normalize_publisher_name`
    過的名稱。由 `DemoPublishersConfig.exclusions()` 建立。
    """

    ids: frozenset[str]
    names: frozenset[str]

    def matches(self, publisher_ids: Iterable[str], publisher_names: Iterable[str]) -> bool:
        """任一個 ID 或名稱對上就算（排除方向，多對上只會更保守）。

        `publisher_ids` 經 `normalize_openalex_id` 比；看不懂的 ID 照原字串（轉大寫）比。
        """
        for value in publisher_ids:
            try:
                normalized = normalize_openalex_id(value)
            except InvalidIdentifierError:
                normalized = value.strip().upper()
            if normalized in self.ids:
                return True
        return any(normalize_publisher_name(name) in self.names for name in publisher_names)


def is_demo_eligible(
    license: str | None,
    publisher_ids: Collection[str],
    publisher_names: Collection[str],
    excluded: PublisherExclusions | None,
) -> bool:
    """這篇論文能不能在示範站放完整內容。全專案只有這個函式做這個判定（ADR-0007 第 5 點）。

    - `license`：出版社自己那一版（OpenAlex `primary_location.license`）的授權原字串。
    - `publisher_ids`／`publisher_names`：`primary_location.source` 的出版社 ID 與名稱（含
      lineage）。
    - `excluded`：排除清單；None＝清單沒有載入（例如測試沒帶），一律 False。

    只有授權對到 `DEMO_ELIGIBLE_LICENSES`、至少一個出版社**名稱**正規化後不是空的、而且沒有任何
    ID 或名稱在排除清單裡，才是 True。授權沒有或看不懂、沒有可用的出版社名稱都是 False。

    名稱是必要條件，只有 ID 不夠：排除清單目前只靠名稱（`config/demo_publishers.yaml` 的 `id` 都是
    null），只有 ID（看得懂或看不懂都一樣）時無法確認不是被排除的出版社。之後排除清單補上 ID 時再
    檢討這個條件（ADR-0052 第 4 點）。
    """
    if excluded is None:
        return False
    if openalex_license(license) not in DEMO_ELIGIBLE_LICENSES:
        return False
    if not any(normalize_publisher_name(name) for name in publisher_names):
        return False
    return not excluded.matches(publisher_ids, publisher_names)


class CloudPolicy(BaseModel):
    """放行要看的設定。由 `Settings.cloud_policy` 產生（值來自環境變數）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # RADAR_PAYWALLED_FULLTEXT_LLM：付費全文與上傳的 PDF 內容可不可以送雲端（預設關）。
    paywalled_fulltext_llm: bool = Field(default=False, strict=True)


# 放行用的私有記號：只有這個模組拿得到，`CloudSafeText` 的建構子要它（架構測試守著）。
_CLEARANCE: Final = object()

# 放行登記處（Codex 審查 PR #54 第 1 點）：放行過的文字與來源只存在這裡，`CloudSafeText` 物件本身
# 只拿一個號碼（`_token`）。就算有人用 `object.__setattr__` 硬改物件，也只能把號碼換成另一筆
# 已經放行的內容，換不進沒放行的文字；號碼不在登記裡（亂填、物件已經回收、用 `object.__new__`
# 做出來的空殼）讀文字時就丟 `CloudSafeTextIntegrityError`。要繞過得直接改這個模組的私有狀態，
# 架構測試守著 `radar/` 裡沒有別處碰它們。號碼只增不減、不重複使用；物件被回收時登記跟著刪掉。
_REGISTRY: Final[dict[int, tuple[str, ContentSource]]] = {}
_TOKENS: Final = itertools.count(1)


class NotCloudSafeError(TypeError):
    """port 收到的不是有效的 `CloudSafeText`（例如直接傳 `str`）。在送出任何請求前丟出。

    訊息只有欄位名稱與收到的型別名稱，不含內容。`radar.domain.llm` 也匯出這個類別。
    """


class CloudSafeTextIntegrityError(NotCloudSafeError):
    """`CloudSafeText` 的號碼不在放行登記裡：被硬改過、物件已經回收後又被拿來用，或不是經
    `clear_for_cloud` 建立的空殼。原因代碼是 `cloud_text_not_registered`；訊息不含內容。
    """

    reason: Final = ErrorCode.CLOUD_TEXT_NOT_REGISTERED

    def __init__(self) -> None:
        super().__init__(
            "CloudSafeText 不在放行登記裡（被改過或不是經 clear_for_cloud 建立），不送出"
            f"（{ErrorCode.CLOUD_TEXT_NOT_REGISTERED.value}）"
        )


@final
class CloudSafeText:
    """已經放行、可以送雲端的文字。只能由 `clear_for_cloud` 建立。

    文字與來源存在本模組的放行登記處，物件只拿號碼；每次讀 `text`／`source` 都經模組函式
    `cloud_text`／`cloud_source` 回登記處查，查不到丟 `CloudSafeTextIntegrityError`。
    判斷「是不是 `CloudSafeText`」一律用 `is_cloud_safe_text`（`type(x) is CloudSafeText`），
    不用 `isinstance`：`isinstance` 會相信物件自己報的 `__class__`，偽裝的物件騙得過。
    `repr`／`str` 只顯示來源與長度，不顯示內容（避免寫進日誌或錯誤訊息）；
    建立後不能改、不能繼承、不能複製或序列化。
    """

    __slots__ = ("__weakref__", "_token")

    _token: int

    def __init__(self, text: str, source: ContentSource, *, clearance: object) -> None:
        if clearance is not _CLEARANCE:
            raise TypeError("CloudSafeText 只能由 domain/policy.py 的 clear_for_cloud 建立")
        token = next(_TOKENS)
        _REGISTRY[token] = (text, source)
        object.__setattr__(self, "_token", token)
        # 物件被回收時刪掉登記；號碼不重複使用，所以之後換成這個號碼也查不到。
        weakref.finalize(self, _REGISTRY.pop, token, None)

    def __init_subclass__(cls) -> None:
        raise TypeError("CloudSafeText 不能被繼承")

    @property
    def text(self) -> str:
        """要送出去的文字（經 `cloud_text` 從放行登記處取）。

        Raises:
            CloudSafeTextIntegrityError: 不在放行登記裡。
        """
        return cloud_text(self)

    @property
    def source(self) -> ContentSource:
        """放行時的來源（經 `cloud_source` 從放行登記處取）。

        Raises:
            CloudSafeTextIntegrityError: 不在放行登記裡。
        """
        return cloud_source(self)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("CloudSafeText 建立後不能改")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("CloudSafeText 建立後不能改")

    def __eq__(self, other: object) -> bool:
        if not is_cloud_safe_text(other):
            return NotImplemented
        return _registered(self) == _registered(other)

    def __hash__(self) -> int:
        return hash(_registered(self))

    def __repr__(self) -> str:
        try:
            text, source = _registered(self)
        except CloudSafeTextIntegrityError:
            return "CloudSafeText(不在放行登記裡)"
        return f"CloudSafeText(source={source.value!r}, length={len(text)})"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        # copy／pickle 會繞過建構子；不需要，直接擋。
        raise TypeError("CloudSafeText 不能被複製或序列化")


# 建好類別時就把號碼欄位的 slot descriptor 存下來：讀號碼直接經它，不經可被覆寫的屬性查找
# （`__getattribute__`、`__getattr__`、類別上的 `_token` 被換掉都影響不到）。
_TOKEN_SLOT: Final = CloudSafeText.__dict__["_token"]


def is_cloud_safe_text(value: object) -> TypeIs[CloudSafeText]:
    """`value` 的真實型別是不是 `CloudSafeText`。

    用 `type(value) is CloudSafeText`，不用 `isinstance`：`isinstance` 會相信物件自己的
    `__class__` 屬性，一般物件只要讓 `__class__` 回 `CloudSafeText` 就騙得過
    （Codex 複查 PR #54）。`CloudSafeText` 不能繼承，所以兩者沒有別的差別。
    """
    return type(value) is CloudSafeText


def _registered(value: object) -> tuple[str, ContentSource]:
    """核對真實型別、讀號碼、查放行登記處，回（文字, 來源）。不經物件上任何可覆寫的屬性。

    Raises:
        NotCloudSafeError: 真實型別不是 `CloudSafeText`。
        CloudSafeTextIntegrityError: 號碼不在放行登記裡（被改過、空殼、已回收）。
    """
    if not is_cloud_safe_text(value):
        raise NotCloudSafeError(
            f"要送雲端的文字必須是 CloudSafeText（收到 {type(value).__name__}），"
            "要先經過 clear_for_cloud"
        )
    try:
        token = _TOKEN_SLOT.__get__(value, CloudSafeText)
    except AttributeError:
        raise CloudSafeTextIntegrityError from None
    entry = _REGISTRY.get(token) if type(token) is int else None
    if entry is None:
        raise CloudSafeTextIntegrityError
    return entry


def cloud_text(value: object) -> str:
    """送出前取文字的唯一做法：確認真實型別是 `CloudSafeText`、號碼在放行登記裡，回放行時的文字。

    Raises:
        NotCloudSafeError: 真實型別不是 `CloudSafeText`（包含偽裝 `__class__` 的物件）。
        CloudSafeTextIntegrityError: 不在放行登記裡（被改過、空殼、已回收）。
    """
    return _registered(value)[0]


def cloud_source(value: object) -> ContentSource:
    """放行時的來源，核對方式同 `cloud_text`。

    Raises:
        NotCloudSafeError: 真實型別不是 `CloudSafeText`。
        CloudSafeTextIntegrityError: 不在放行登記裡。
    """
    return _registered(value)[1]


class CloudClearanceError(Exception):
    """放行被擋下。訊息、`args`、`repr` 只有來源與原因代碼，不含文字內容。"""

    def __init__(self, source: ContentSource, reason: ErrorCode) -> None:
        self.source = source
        self.reason = reason
        super().__init__(f"不可送雲端：來源 {source.value}（{reason.value}）")

    def __repr__(self) -> str:
        return f"CloudClearanceError(source={self.source.value!r}, reason={self.reason.value!r})"


def clear_for_cloud(
    text: str,
    source: ContentSource,
    license: ContentLicense | None,
    policy: CloudPolicy,
) -> CloudSafeText:
    """判定這段文字可不可以送雲端；可以就回 `CloudSafeText`。

    `license` 只對 OA 全文有意義；沒有授權資訊傳 None（當作不明）。

    Raises:
        TypeError: 參數型別不對（文字不是 `str`、來源不是 `ContentSource`、授權不是
            `ContentLicense`／None、設定不是 `CloudPolicy`）。
        CloudClearanceError: 擋下（`reason` 是 `cloud_blocked_paywalled`、`cloud_blocked_source`、
            `cloud_blocked_empty` 或 `cloud_blocked_invalid_text`）。來源是 `PROMPT_TEMPLATE` 時
            一律丟 `cloud_blocked_source`：模板只能經 `clear_prompt_file` 放行。
    """
    if not isinstance(text, str):
        raise TypeError(f"text 必須是 str（收到 {type(text).__name__}）")
    if not isinstance(source, ContentSource):
        raise TypeError(f"source 必須是 ContentSource（收到 {type(source).__name__}）")
    if license is not None and not isinstance(license, ContentLicense):
        raise TypeError(f"license 必須是 ContentLicense 或 None（收到 {type(license).__name__}）")
    if not isinstance(policy, CloudPolicy):
        raise TypeError(f"policy 必須是 CloudPolicy（收到 {type(policy).__name__}）")

    if not text.strip():
        raise CloudClearanceError(source, ErrorCode.CLOUD_BLOCKED_EMPTY)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # 送出時 SDK 編碼會失敗，丟的是不在 LlmError 範圍內的例外；在這裡就擋下。
        raise CloudClearanceError(source, ErrorCode.CLOUD_BLOCKED_INVALID_TEXT) from None

    match source:
        case (
            ContentSource.OPENALEX_ABSTRACT
            | ContentSource.CROSSREF_ABSTRACT
            | ContentSource.TITLE
            | ContentSource.USER_QUERY
        ):
            pass
        case ContentSource.PROMPT_TEMPLATE:
            # 模板只經 clear_prompt_file 放行（核對登記的雜湊）；這裡收了，任何文字標成模板
            # 都送得出去（例如把付費全文填進模板）。
            raise CloudClearanceError(source, ErrorCode.CLOUD_BLOCKED_SOURCE)
        case ContentSource.OA_FULLTEXT:
            if license not in CLOUD_CLEARED_LICENSES and not policy.paywalled_fulltext_llm:
                raise CloudClearanceError(source, ErrorCode.CLOUD_BLOCKED_PAYWALLED)
        case ContentSource.UPLOADED_PDF:
            # 上傳的 PDF 不看授權：使用者說的授權無從查證，一律當付費（ADR-0029）。
            if not policy.paywalled_fulltext_llm:
                raise CloudClearanceError(source, ErrorCode.CLOUD_BLOCKED_PAYWALLED)
        case ContentSource.SEMANTIC_SCHOLAR_ABSTRACT:
            raise CloudClearanceError(source, ErrorCode.CLOUD_BLOCKED_SOURCE)
        case _:
            assert_never(source)
    return CloudSafeText(text, source, clearance=_CLEARANCE)


def join_cloud_safe_texts(parts: object, separator: str) -> CloudSafeText:
    """把幾段已經放行的文字接成一段（向量化的「標題＋空行＋摘要」用，Story 2.8、ADR-0040）。

    每一段都要是放行登記裡的 `CloudSafeText`（各自經過 `clear_for_cloud`）；`separator` 只能是空白
    （空字串也可以），所以接起來的文字裡沒有任何沒放行的內容。接好的文字來源記最後一段的來源：
    只給 `repr` 看，放行早在每一段各自做完了，之後不會再拿它判斷。

    Raises:
        NotCloudSafeError: `parts` 是 `str`、不是有順序的序列、是空的，或有一段不是有效的
            `CloudSafeText`（被硬改過的丟子類別 `CloudSafeTextIntegrityError`）。
        TypeError: `separator` 不是 `str`。
        ValueError: `separator` 不是只有空白。
    """
    if type(separator) is not str:
        raise TypeError(f"separator 必須是 str（收到 {type(separator).__name__}）")
    if separator.strip():
        raise ValueError("separator 只能是空白，不能夾帶沒放行的文字")
    if isinstance(parts, (str, bytes, bytearray)) or not isinstance(parts, (list, tuple)):
        raise NotCloudSafeError(
            f"要接起來的文字必須是 CloudSafeText 的 list 或 tuple（收到 {type(parts).__name__}）"
        )
    if not parts:
        raise NotCloudSafeError("要接起來的文字至少要有一段")
    entries = [_registered(part) for part in parts]
    text = separator.join(entry[0] for entry in entries)
    return CloudSafeText(text, entries[-1][1], clearance=_CLEARANCE)


def clear_prompt_file(task: str, version: int, content: bytes) -> CloudSafeText:
    """放行 prompt 檔的本文（系統提示），來源是 `PROMPT_TEMPLATE`。模板只有這一個入口。

    `content` 是 `prompts/<task>/v<version>.md` 整個檔的原始位元組（由 `services/prompt_loader.py`
    讀檔，這裡不做 I/O）。先核對整個檔的 SHA-256 和 `domain/prompt_hashes.py` 寫死的登記一樣，
    核對過才拿掉設定區、放行本文；登記不從參數收，呼叫端造不出「自己登記的模板」。

    Raises:
        TypeError: 參數型別不對。
        CloudClearanceError: 沒登記、雜湊對不上（`cloud_prompt_unregistered`），或不是 UTF-8
            （`cloud_blocked_invalid_text`）。訊息不帶檔案內容。
        PromptFormatError: 登記過的檔格式不對（沒有設定區、本文是空的）。
    """
    if type(task) is not str:
        raise TypeError(f"task 必須是 str（收到 {type(task).__name__}）")
    if type(version) is not int:
        raise TypeError(f"version 必須是 int（收到 {type(version).__name__}）")
    if type(content) is not bytes:
        raise TypeError(f"content 必須是 bytes（收到 {type(content).__name__}）")
    source = ContentSource.PROMPT_TEMPLATE
    # 每次呼叫都從模組讀登記（不在 import 時複製一份）。
    registered = prompt_hashes.PROMPT_FILE_HASHES.get(task, {}).get(version)
    if registered is None or prompt_sha256(content) != registered[0]:
        raise CloudClearanceError(source, ErrorCode.CLOUD_PROMPT_UNREGISTERED)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise CloudClearanceError(source, ErrorCode.CLOUD_BLOCKED_INVALID_TEXT) from None
    _, body = split_prompt_file(text)
    return CloudSafeText(body, source, clearance=_CLEARANCE)
