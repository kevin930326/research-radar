"""模型輸出的共用文字檢查（Story 2.4 寫在 `zh_summary.py`，Story 2.6 搬到這裡共用）。

中文重點（`zh_summary.py`）與研究模型（`research_model.py`）都用：

- `normalize_text`：存進資料庫前的正規化（NFC、空白縮成一個、去頭尾）。
- `check_copy`：「檢查用副本」，只拿來檢查不存：HTML 實體解碼、拿掉零寬等格式字元、NFKC、
  各種文字的十進位數字換成 0～9、破折號類換成 `-`、空白縮成一個。
- `BIDI_OR_TAG`、`HTML_ENTITY`：輸出有就直接拒收的字（顯示和檢查看到的會不一樣）。
- `has_markup`：標記或連結（標籤樣的 `<`、反引號、Markdown 連結、網址、裸網域…）。
- `numbers`、`number_word_values`、`has_unsupported_number`、`InputVocabulary`：編造判準用的數字
  與英文詞比對（輸出的數字與英文詞都要在輸入出現）。
- `is_han`、`SIMPLIFIED_ONLY`：漢字與簡體專用字。

都是純函式，行為和 2.4 原本在 `zh_summary.py` 的一樣（只改成公開名稱）；判準的細節與抓不到的
情況寫在 ADR-0032。
"""

import html
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, DecimalException
from typing import Final

_WHITESPACE: Final = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """NFC、連續空白（含全形空白、換行）縮成一個半形空白、去頭尾。"""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFC", value)).strip()


# 雙向控制字元（強制從右到左等）與 tag 字元：檢查時看不出來、顯示時卻會把字的順序倒過來或藏字，
# 所以輸出有就直接拒收，不在檢查用副本裡拿掉。
BIDI_OR_TAG: Final = re.compile(
    "[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\U000e0000-\U000e007f]"
)
# HTML 實體（`&lt;`、`&#96;`、`&#x3c;`）：純文字 JSON 不該有，輸出有就直接拒收，存的才等於檢查的。
HTML_ENTITY: Final = re.compile(
    r"&(?:[A-Za-z][A-Za-z0-9]{1,31}|#[0-9]{1,8}|#[xX][0-9A-Fa-f]{1,8});"
)
# 長得像負號的字（各種破折號：Unicode 分類 Pd，加上減號 `−`、粗減號 `➖`）在檢查用副本裡一律換成
# `-`，所以「‒0.35」「—0.35」和「-0.35」一樣是負數。
_MINUS_LIKE: Final = frozenset({"−", "➖"})


def strip_invisible(text: str) -> str:
    """HTML 實體解碼（最多三層，`&amp;lt;` 也解得開）並拿掉零寬空白等格式字元（Unicode Cf）。"""
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return "".join(char for char in text if unicodedata.category(char) != "Cf")


def check_copy(text: str) -> str:
    """檢查用副本：`strip_invisible` 之後全形逗號換成頓號、NFKC（全形變體換成半形）、各種文字
    的十進位數字（例如 `٩`）換成 0～9、各種破折號與減號換成 `-`、連續空白縮成一個、去頭尾。
    只拿來檢查，不存。"""
    # NFKC 會把全形逗號「，」變成「,」，「120，350 名」就被當成千分位的 120350；先換成頓號。
    folded = unicodedata.normalize("NFKC", strip_invisible(text).replace("，", "、"))
    folded = "".join(_fold_char(char) for char in folded)
    return _WHITESPACE.sub(" ", folded).strip()


def _fold_char(char: str) -> str:
    if char.isascii():
        return char
    if char.isdecimal():
        return str(unicodedata.decimal(char))
    if char in _MINUS_LIKE or unicodedata.category(char) == "Pd":
        return "-"
    return char


# --- 標記與連結 ---------------------------------------------------------------------------

# 完整的比較式（先認出來、換成空白，再找標記）：比較符號後面接數字（`p < .05`、`>50%`、`β > 0`），
# 或兩邊各是一個英文字母（`x<y`、`a > b`；右邊後面不能再接字母，到下一個漢字或頓號之前的 40 個
# 字內也不能有 `>`，所以 `x<b>`、`a<b class=x>` 不算）。
_COMPARISON: Final = re.compile(
    r"[<>]=?\s?[-+−]?(?:[0-9]|\.[0-9])"
    r"|(?<![A-Za-z])[A-Za-z]\s?[<>]=?\s?[A-Za-z](?![A-Za-z>])(?![^<>\u2e80-\u9fff、]{0,40}>)"
)
# 標記與連結（在拿掉比較式的檢查用副本上找）：`<` 後面接標籤樣的字元（`<b>`、`</div`、`<!--`、
# `<?`）；欄位開頭的 `>`（Markdown 引言）；反引號；粗體與刪除線（`**`、`__`、`~~`）；行內連結
# `](`、參照式連結 `][` 與 `[名稱]: 網址`；任何 `://`、`www.`、`mailto:`、`javascript:`。漢字在
# Python 的正規式裡也是 `\w`，所以「見https://…」這種寫法不能靠 `\b`，改用不接英文字母的前後文。
_UNSAFE: Final = re.compile(
    r"<(?=[/!?A-Za-z])|^>|`|\*\*|__|~~|\]\(|\]\[|\[[^\[\]]{1,80}\]:"
    r"|://|(?<![A-Za-z0-9])www\.|(?<![A-Za-z])(?:mailto|javascript):",
    re.IGNORECASE,
)
# 裸網域（`osf.io`、`example.org`、`data.gov.tw`），即使摘要裡也有：網域只認小寫的常見頂級網域，
# 所以 `ASP.NET`、`Node.js` 這類技術名稱不算。開頭不能接在字母、數字、`.`、`-` 後面，每個網域
# 只從開頭試一次（不會平方級回溯）。
_TLDS: Final = (
    "com|org|net|edu|gov|mil|int|info|biz|xyz|app|dev|site|online|tech|io|ai|co|me|tv|ly"
    "|tw|cn|hk|jp|kr|sg|uk|de|fr|nl|ch|se|it|es|eu|us|ca|au|br|ru|in"
)
_BARE_DOMAIN: Final = re.compile(
    rf"(?<![A-Za-z0-9.\-])(?:[A-Za-z0-9\-]++\.)+(?:{_TLDS})(?![A-Za-z0-9\-])"
)


def has_markup(value: str, input_text: str) -> bool:
    """標記或連結：先把完整的比較式（`p < .05`、`>50%`、`x<y`）換成空白，再找標記與裸網域。
    首字大寫、而且輸入（檢查用副本）裡有一模一樣字樣的裸網域（品牌名稱 `Amazon.com`）放行；
    小寫的（`osf.io`）即使輸入有也擋。"""
    text = _COMPARISON.sub(" ", check_copy(value))
    if _UNSAFE.search(text):
        return True
    return any(
        not (domain[0].isupper() and domain in input_text)
        for domain in (match.group(0) for match in _BARE_DOMAIN.finditer(text))
    )


# --- 漢字 ---------------------------------------------------------------------------------

# 只出現在簡體的常用字（每個都不在 Big5／CP950 裡，單元測試核對）。
SIMPLIFIED_ONLY: Final = frozenset(
    "这们过为发现说时会对进关个实动将应经结论数题问学业产务资统释验证响显级际观处设计认识变让还"
    "从样点间长无电脑条绩组织员参测试调术运营销费购买卖东车门见简语读写给该没决质较选择专环随态势"
    "历众虑"
)


def is_han(char: str) -> bool:
    """`char` 是漢字（CJK 統一表意文字與擴充區、相容表意文字）。"""
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x323AF
    )


# --- 數字與英文詞（編造判準） -------------------------------------------------------------

ENGLISH_WORD: Final = re.compile(r"[A-Za-z]+")
ENGLISH_TOKEN: Final = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*")
# 數字（在檢查用副本上找，破折號類已換成 `-`）：正負號（前面不是字母、數字、`.`、`)`；`3-5` 的
# `-` 不是負號）、千分位（1,200）合併、APA 寫法的 .05 是 0.05、指數（1e-3）、單位（萬、億、兆、
# 千、百萬…與 thousand、million、billion，乘進去）、百分比（`%`、`percent`、`per cent`）。值是
# （Decimal, 是不是百分比），所以 -0.35 ≠ 0.35、0.35 ≠ 0.35%、1e-3 ≠ 1e3、12 萬 ≠ 12 million。
_NUMBER: Final = re.compile(
    r"(?P<sign>(?<![0-9A-Za-z.)\]])[-+])?"
    r"(?P<mantissa>[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?|(?<![0-9])\.[0-9]+)"
    r"(?P<exponent>[eE][-+]?[0-9]+)?"
    r"(?P<scale>\s?(?:[百千]?萬|[百千]?億|兆|千|百(?!分)|(?i:thousand|million|billion)(?![A-Za-z])))?"
    r"(?P<percent>\s?(?:%|(?i:percent|per cent)(?![A-Za-z])))?"
)
NUMBER_CONTEXT: Final = Context(prec=100, Emax=MAX_EMAX, Emin=MIN_EMIN)
_SCALES: Final[Mapping[str, int]] = {
    "萬": 10**4,
    "百萬": 10**6,
    "千萬": 10**7,
    "億": 10**8,
    "百億": 10**10,
    "千億": 10**11,
    "兆": 10**12,
    "千": 10**3,
    "百": 10**2,
    "thousand": 10**3,
    "million": 10**6,
    "billion": 10**9,
}
# 不支援、會讓數值意思走樣的寫法（輸出有就拒收）：上標與下標數字（但拉丁或希臘字母後面的
# ¹²³ 可以，`R²`、`χ²`、`η²` 是統計符號，NFKC 後當成 R2 比對）、分數字元與分數斜線（NFKC 前才
# 看得到）；`^`、千分比、`×10` 這種科學記號（NFKC 後）；不是十進位、也不是漢字的數字字元
# （例如衣索比亞數字 `፱`，見 `has_unsupported_number`）。
_UNSUPPORTED_NUMBER_RAW: Final = re.compile(
    "(?<![A-Za-z\u0370-\u03ff])[\u00b2\u00b3\u00b9]|[\u2070-\u209f\u00bc-\u00be\u2150-\u215f\u2044]"
)
_UNSUPPORTED_NUMBER: Final = re.compile(r"\^|‰|‱|×\s?10(?![0-9.,])")
# 首字母縮寫可以跳過的虛詞（例如 Theory of Planned Behavior → TPB）。
_FUNCTION_WORDS: Final = frozenset(
    {"of", "and", "the", "for", "in", "on", "to", "a", "an", "with", "by", "at", "from", "or"}
)
_UNIT_NAMES: Final = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen"
    " fifteen sixteen seventeen eighteen nineteen"
)
_UNIT_WORDS: Final[Mapping[str, int]] = {
    word: value for value, word in enumerate(_UNIT_NAMES.split())
}
_TENS_WORDS: Final[Mapping[str, int]] = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_ORDINAL_NAMES: Final = "first second third fourth fifth sixth seventh eighth ninth tenth"
_ORDINAL_WORDS: Final[Mapping[str, int]] = {
    word: value for value, word in enumerate(_ORDINAL_NAMES.split(), start=1)
}
_SCALE_WORDS: Final[Mapping[str, int]] = {"thousand": 10**3, "million": 10**6, "billion": 10**9}

# 數值：（Decimal, 是不是百分比）。
type Number = tuple[Decimal, bool]


def numbers(text: str, *, keep_unscaled: bool = False) -> tuple[set[Number], bool]:
    """檢查用副本裡的數字 →（數值集合, 有沒有讀不懂的數字，例如指數大到算不出來）。

    單位（萬、million…）乘進數值；`keep_unscaled`（輸入用）時也留下沒乘的數字，所以摘要寫
    「12 million」時輸出寫 12 或 1200 萬都對得上，輸出寫「12 萬」只會是 120000。"""
    found: set[Number] = set()
    unreadable = False
    for match in _NUMBER.finditer(text):
        exponent = match["exponent"] or ""
        percent = match["percent"] is not None
        scale_word = (match["scale"] or "").strip().lower()
        try:
            value = Decimal((match["sign"] or "") + match["mantissa"].replace(",", "") + exponent)
            if scale_word:
                if keep_unscaled:
                    found.add((value.normalize(NUMBER_CONTEXT), percent))
                value = value * _SCALES[scale_word]
            found.add((value.normalize(NUMBER_CONTEXT), percent))
        except DecimalException:
            unreadable = True
    return found, unreadable


def _number_word_kind(word: str) -> tuple[str | None, int]:
    if word in _UNIT_WORDS:
        value = _UNIT_WORDS[word]
        return ("teen" if value >= 10 else "unit"), value
    if word in _TENS_WORDS:
        return "tens", _TENS_WORDS[word]
    if word == "hundred":
        return "hundred", 100
    if word in _SCALE_WORDS:
        return "big", _SCALE_WORDS[word]
    if word == "and":
        return "and", 0
    return None, 0


# 英文數字詞組裡，每一種詞後面可以接哪幾種（None＝詞組的開頭）。
_NUMBER_WORD_FOLLOWS: Final[Mapping[str | None, frozenset[str]]] = {
    None: frozenset({"unit", "teen", "tens", "hundred", "big"}),
    "unit": frozenset({"hundred", "big"}),
    "teen": frozenset({"hundred", "big"}),
    "tens": frozenset({"unit", "big"}),
    "hundred": frozenset({"unit", "teen", "tens", "big", "and"}),
    "big": frozenset({"unit", "teen", "tens", "and"}),
    "and": frozenset({"unit", "teen", "tens"}),
}


def number_word_values(words: list[str]) -> set[Number]:
    """輸入裡的英文數字詞換成數字：一整個詞組只留它的值，組成的詞不另外算。
    「three hundred」只有 300（沒有 3、100）、「twenty five」只有 25、「two thousand」只有 2000、
    「three hundred and twenty」只有 320、「twelve million」只有 12000000；first～tenth 是 1～10；
    詞組後面接 `percent`／`per cent`
    時是百分比。"""
    values: set[Number] = set()
    total = current = 0
    last: str | None = None
    in_phrase = False

    def flush(index: int) -> None:
        nonlocal in_phrase
        if in_phrase:
            following = words[index : index + 2]
            percent = following[:1] == ["percent"] or following == ["per", "cent"]
            values.add((Decimal(total + current), percent))
        in_phrase = False

    for index, word in enumerate(words):
        kind, value = _number_word_kind(word)
        if in_phrase and kind in _NUMBER_WORD_FOLLOWS[last]:
            pass  # 接在目前的詞組後面
        elif kind in _NUMBER_WORD_FOLLOWS[None]:
            flush(index)  # 新的詞組（「three four」是兩個詞組）
            total = current = 0
            in_phrase = True
        else:
            flush(index)
            if word in _ORDINAL_WORDS:
                values.add((Decimal(_ORDINAL_WORDS[word]), False))
            continue
        if kind == "hundred":
            current = (current or 1) * value
        elif kind == "big":
            total += (current or 1) * value
            current = 0
        elif kind != "and":
            current += value
        last = kind
    flush(len(words))
    return values


def _variants(word: str) -> set[str]:
    """容許複數字尾的差異：teams ↔ team、boxes ↔ box、studies ↔ study。"""
    found = {word}
    if word.endswith("ies") and len(word) > 3:
        found.add(word[:-3] + "y")
    if word.endswith("es") and len(word) > 2:
        found.add(word[:-2])
    if word.endswith("s") and len(word) > 1:
        found.add(word[:-1])
    return found


@dataclass(frozen=True)
class InputVocabulary:
    """輸入（標題＋摘要）裡可以拿來比對的東西。"""

    numbers: frozenset[Number]
    words: frozenset[str]
    initials: tuple[str, ...]

    @classmethod
    def build(cls, text: str) -> InputVocabulary:
        """`text` 是檢查用副本。"""
        words = [word.lower() for word in ENGLISH_WORD.findall(text)]
        variants: set[str] = set()
        for word in words:
            variants |= _variants(word)
        content = [word for word in words if word not in _FUNCTION_WORDS]
        return cls(
            numbers=frozenset(numbers(text, keep_unscaled=True)[0] | number_word_values(words)),
            words=frozenset(variants),
            initials=(
                "".join(word[0] for word in words),
                "".join(word[0] for word in content),
            ),
        )

    def knows_word(self, word: str) -> bool:
        """這個英文詞（2 個字母以上）在輸入出現過，或是輸入裡連續幾個詞的首字母。"""
        lowered = word.lower()
        if _variants(lowered) & self.words:
            return True
        return any(lowered in initials for initials in self.initials)

    def knows_token(self, token: str) -> bool:
        """`PLS-SEM` 這種連字號串：每一段都認得，或整串去掉連字號是首字母縮寫。"""
        parts = [part for part in token.split("-") if len(part) >= 2]
        if not parts:
            return True
        if all(self.knows_word(part) for part in parts):
            return True
        joined = token.replace("-", "").lower()
        return any(joined in initials for initials in self.initials)


def has_unsupported_number(stored: str, checked: str) -> bool:
    """不支援的數字寫法：`_UNSUPPORTED_NUMBER_RAW`（看沒經 NFKC 的字）、`_UNSUPPORTED_NUMBER`，或
    檢查用副本裡還有不是 ASCII、不是漢字、卻有數值的字元（`፱`；十進位數字已換成 0～9）。"""
    if _UNSUPPORTED_NUMBER_RAW.search(strip_invisible(stored)) or _UNSUPPORTED_NUMBER.search(
        checked
    ):
        return True
    return any(
        not char.isascii()
        and not is_han(char)
        and char != "〇"
        and unicodedata.numeric(char, None) is not None
        for char in checked
    )
