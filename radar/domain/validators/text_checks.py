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
- `ExtendedVocabulary`（2026-11-18，只有中文重點用）：`InputVocabulary` 加上把輸入讀得更完整的部分
  ——空白千分位、月份英文名、`10-40%`、`10% points`、年代、英數詞（`B2B`）、輸出正數對輸入
  負數、英文詞的詞形變化與黏字、`vs`。數字的新規則一律以 `numbers` 讀到的「完整數字」為單位
  （`_whole_numbers`），不另外用正規式找數字。研究模型不用它。

都是純函式；上面幾項的行為和 2.4 原本在 `zh_summary.py` 的一樣（只改成公開名稱），加
`ExtendedVocabulary` 時也沒改。判準的細節與抓不到的情況寫在 ADR-0032。
"""

import html
import re
import string
import unicodedata
from bisect import bisect_left
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, DecimalException
from itertools import pairwise
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

# 科學記號（在檢查用副本上找）：「4.2 × 10^-2」「4.2 x 10-2」（上標的 10⁻² 經 NFKC 變成 10-2，所以
# 前面有「數字 ×」時 10 後面的 -2 是指數）、單獨的「10^-3」。整段一起讀；`numbers` 不取其中的碎片
# （4.2、10、2），`structured_numbers` 算出完整的值（0.042）。
_SCIENTIFIC: Final = re.compile(
    r"(?<![0-9A-Za-z.])(?:(?P<sign>[-+])\s?)?(?P<mantissa>[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\s?[×xX*·]\s?"
    r"10\s?(?:\^\s?(?P<power>[-+]?\s?[0-9]+)|(?P<signed_power>[-+][0-9]+))(?![0-9]|\.[0-9])"
    r"|(?<![0-9A-Za-z.])10\s?\^\s?(?P<lone_power>[-+]?\s?[0-9]+)(?![0-9]|\.[0-9])"
)
# 其他含 `^` 的寫法（讀不出完整的值）：整段不取數字。
# 只從一段不含空白的字的開頭試（不會平方級回溯）。
_CARET_TOKEN: Final = re.compile(r"(?<!\S)\S*\^\S*")
# 負號和數字之間隔了一個空白（「β = - 0.42」）：接在 `=`、`:`、`(`、比較符號後面時是負號，併起來
# 讀成 -0.42（不是 0.42）。
_SPACED_MINUS: Final = re.compile(r"(?<=[=:(<>≤≥])(\s?)-\s(?=[0-9]|\.[0-9])")


def _blank(match: re.Match[str]) -> str:
    return " " * len(match.group(0))


def _prepared_for_numbers(text: str) -> str:
    """讀數字前：科學記號與含 `^` 的整段換成空白（不取碎片），隔空白的負號併到數字上。"""
    text = _SCIENTIFIC.sub(_blank, text)
    if "^" in text:
        text = _CARET_TOKEN.sub(_blank, text)
    return _SPACED_MINUS.sub(lambda match: match.group(1) + "-", text)


def decimal_of(text: str) -> Decimal | None:
    """`.05`、`0.050`、`1e-3` 這種寫法的數值（正規化）；讀不懂是 None。"""
    try:
        return Decimal(text).normalize(NUMBER_CONTEXT)
    except DecimalException:
        return None


def numbers(text: str, *, keep_unscaled: bool = False) -> tuple[set[Number], bool]:
    """檢查用副本裡的數字 →（數值集合, 有沒有讀不懂的數字，例如指數大到算不出來）。

    單位（萬、million…）乘進數值；`keep_unscaled`（輸入用）時也留下沒乘的數字，所以摘要寫
    「12 million」時輸出寫 12 或 1200 萬都對得上，輸出寫「12 萬」只會是 120000。科學記號與含 `^`
    的寫法整段不取（不會把「4.2 × 10^-2」的 4.2 當成輸入裡的數字）；「= - 0.42」讀成 -0.42。

    輸出（沒有 `keep_unscaled`）裡有科學記號或 `^`（`9.9 x 10-7`、`9.9 * 10-7`、`9.9·10+3`）時
    回報讀不懂：整段不取數字後就沒東西可以核對，不能因此放行（確認審查）。`structured_numbers`
    讀的是輸入，不看這個旗標。"""
    found: set[Number] = set()
    unreadable = not keep_unscaled and ("^" in text or _SCIENTIFIC.search(text) is not None)
    for match in _NUMBER.finditer(_prepared_for_numbers(text)):
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


def _scientific_values(text: str) -> set[Number]:
    """`_SCIENTIFIC` 認得的科學記號的完整值（4.2 × 10^-2 → 0.042；10^-3 → 0.001）。"""
    found: set[Number] = set()
    for match in _SCIENTIFIC.finditer(text):
        power = match["power"] or match["signed_power"] or match["lone_power"]
        mantissa = match["mantissa"] or "1"
        try:
            exponent = int(power.replace(" ", ""))
            value = Decimal((match["sign"] or "") + mantissa).scaleb(exponent, NUMBER_CONTEXT)
            found.add((value.normalize(NUMBER_CONTEXT), False))
        except DecimalException, ValueError:
            continue
    return found


# 用字母縮寫寫量級的數字（2.6 決定 13 選 B，Kevin 2026-11-15）：`12M`、`10k`、`3.5K`、`2B`、
# `1,200K`、`.5M`、`12 m`、`5bn`，以及常見的變體 `12MM`、`1.2 Mn`、`12 mln`、`12 mil`、`12 mio`、
# `2 bln`、`2 bil`、`12-M`（Codex 主審查第 5 條；不分大小寫，數字和字母之間最多一個空白或一個
# 連字號）。讀不出量級（M 是 million 還是別的單位、k 是 thousand 還是 kilo、mm 也可能是公釐），照
# 科學記號的做法整段換成空白、不取數字：`12M` 不會變成 12，也不會變成 12000000。數字前面不能接著
# 字母、數字、`.`、`,`（`H2b`、`B2B` 的 2 不算）。字母後面：
# - 不能再接字母、數字、連字號、`&`、`/`，或「`.`＋字母」（`12 months`、`12-month`、`5 MB`、
#   `5-MB`、`2 B2B`、`312 K-12 teachers`、`200 M-Turk`、`12 mmHg`，以及 Opus 確認審查 M4 的
#   `412 M&A deals`、`120 M.B.A. students`、`20 M.D.`、`120 M/F` 不算）；句尾的 `12M.` 照樣算；
# - 例外（Opus 確認審查 M3）：數字和字母**中間沒有空白也沒有連字號**時，後面接「連字號＋字母」的
#   複合形容詞也算（`a 2M-user platform`、`a 10k-respondent panel`、`2B-dollar`），接「連字號＋
#   數字」的範圍也算（Opus 第三輪 L2：`a 12M-15M user base` 的 12）；中間有空白或連字號的
#   （`200 M-Turk`、`312 K-12`）照上一條不算。
_MAGNITUDE_NUMBER: Final = r"(?:(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?|\.[0-9]+)"
_MAGNITUDE_LETTERS: Final = r"(?i:bln|bil|bn|mln|mil|mio|mm|mn|[kmb])"
_ABBREVIATED_MAGNITUDE: Final = re.compile(
    rf"(?<![0-9A-Za-z.,]){_MAGNITUDE_NUMBER}"
    rf"(?:(?:\s?|-){_MAGNITUDE_LETTERS}(?![0-9A-Za-z&/-]|\.[A-Za-z])"
    rf"|{_MAGNITUDE_LETTERS}(?=-[A-Za-z0-9]))"
)


def structured_numbers(text: str) -> frozenset[Number]:
    """結構化數值（研究模型的樣本數、題數、α、β、p…）可以對照的輸入數字：只有**完整量級**
    （「12 million」只有 12000000，不留 12；資料庫的數字沒有單位欄位）、科學記號算出完整的值、
    隔空白的負號讀成負數，加上英文數字詞（整個詞組的值）。`text` 是檢查用副本。

    用字母縮寫寫量級的數字（`12M`、`10k`、`3.5K`、`2B`，`_ABBREVIATED_MAGNITUDE`）讀不懂，整段
    不取（不留 12、也不猜成 12000000）：模型照縮寫填的數字對不上，整筆拒收（prompt v2 規則 5 要
    模型這時填 null）。只影響結構化數值；英文名、繁中標籤與中文重點用的 `numbers` 不變。"""
    values, _ = numbers(_ABBREVIATED_MAGNITUDE.sub(_blank, text))
    words = [word.lower() for word in ENGLISH_WORD.findall(text)]
    return frozenset(values | _scientific_values(text) | number_word_values(words))


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


# --- 中文重點專用：把輸入讀得更完整（ADR-0032 的 2026-11-18 更新）-------------------------------
#
# 只有 `zh_summary.py` 用 `ExtendedVocabulary`；研究模型照舊用上面的 `numbers`、
# `structured_numbers`、`InputVocabulary`，那些函式的行為一個字都沒變（單元測試固定）。原則不變：
# 輸出的數字與英文詞都要在標題或摘要裡有依據；這裡只是把輸入讀得更完整，每一項放行都有「仍然擋得住
# 編造」的反例測試。


# 完整數字（2026-11-18 審查 R1～R3，第二輪改成結構性的做法）：下面的數字規則一律以「既有讀法讀到的
# 完整數字」為單位——`_prepared_for_numbers` 之後（科學記號與含 `^` 的整段換成空白、隔空白的負號併上
# 去）`_NUMBER` 的每一個匹配，連同它的正負號、千分位、小數、指數、量詞、百分比。規則只看這些完整數字
# 與它們之間的文字，不另外用正規式找數字：
# - 輸入這一側只從完整數字取值，而且只取沒有指數、沒有量詞的（`_WholeNumber.simple`）。科學記號
#   （`1.2 × 10-3`、`10 ^ - 10`）根本不在序列裡，`3e-10` 是一個帶指數的完整數字，所以讀不到其中的
#   片段。
# - 輸出這一側的 `Q` 替換只能把完整數字整個換掉（`_covers_whole_numbers`、`_exact_integer`），換完
#   再用 `numbers` 核對讀到的數字只會變少（`_reads_no_new_numbers`）。
@dataclass(frozen=True, slots=True)
class _WholeNumber:
    """一個完整數字：位置（在原文裡的位置；`_prepared_in_place` 不改長度）與 `_NUMBER` 的各部分。"""

    start: int
    end: int
    sign: str
    mantissa: str
    exponent: str
    scale: str
    percent: str
    # 碰到既有讀法沒認出整段的科學記號（`_SCIENTIFIC_PIECE`）。
    in_scientific: bool = False

    @property
    def simple(self) -> bool:
        """沒有指數、沒有量詞（萬、million…），也不是科學記號的一部分。"""
        return not self.exponent and not self.scale and not self.in_scientific

    def value(self, *, signed: bool = True) -> Decimal | None:
        """不含量詞、指數的值（`simple` 的數字就是它的值）；讀不懂是 None。"""
        return decimal_of((self.sign if signed else "") + self.mantissa.replace(",", ""))


def _prepared_in_place(text: str) -> str:
    """和 `_prepared_for_numbers` 一樣，但長度不變：隔空白的負號（`= - 0.42`）換成 `=  -0.42`（不是
    `= -0.42`），所以完整數字在這裡的位置就是它在原文的位置。讀到的數字和 `_prepared_for_numbers`
    一樣（單元測試核對）。"""
    text = _SCIENTIFIC.sub(_blank, text)
    if "^" in text:
        text = _CARET_TOKEN.sub(_blank, text)
    return _SPACED_MINUS.sub(lambda match: match.group(1) + " -", text)


# 既有讀法沒認出整段的科學記號：`_SCIENTIFIC` 要求整段前面不接英數字，`_CARET_TOKEN` 只蓋到空白
# 為止，所以 `K10^ -3`（`-3` 照樣讀得到）、`K1.2 × 10-3`（10、3 照樣讀得到）裡的次方與 10 仍是
# 既有讀法的完整數字。這些完整數字照樣在序列裡（`numbers` 讀得到的不變），但標成 `in_scientific`，
# 新規則不用：`×`／`x`／`*`／`·` 後面的「10＋次方」，與 `^` 後面（可隔空白、正負號）的次方——和
# `_SCIENTIFIC` 的 power 同樣的寫法（`x` 前面不能是字母：`max 10-20%` 不算）。只從 `×`、`x`、`*`、
# `·`、`^` 開始試，線性時間。
_SCIENTIFIC_PIECE: Final = re.compile(
    r"(?:[×*·]|(?<![A-Za-z])[xX])\s?10\s?(?:\^\s?[-+]?\s?[0-9]+|[-+]\s?[0-9]+)"
    r"|\^\s?[-+]?\s?[0-9]+"
)


def _whole_numbers(text: str) -> tuple[str, list[_WholeNumber]]:
    """（長度不變的讀數字用文字, 依序的完整數字）。`text` 是檢查用副本；掃一次，線性時間。"""
    prepared = _prepared_in_place(text)
    pieces = bytearray(len(text))
    for match in _SCIENTIFIC_PIECE.finditer(text):
        pieces[match.start() : match.end()] = b"\x01" * (match.end() - match.start())
    found: list[_WholeNumber] = []
    for match in _NUMBER.finditer(prepared):
        start, end = match.span()
        # `_NUMBER` 的五個群組依序是 sign、mantissa、exponent、scale、percent。
        sign, mantissa, exponent, scale, percent = match.groups("")
        in_scientific = pieces.find(1, start, end) != -1
        found.append(
            _WholeNumber(start, end, sign, mantissa, exponent, scale, percent, in_scientific)
        )
    return prepared, found


# 完整數字前面緊接這些字元時，它是一個詞或別的數字寫法的一部分（`K12`、`v1.2.3` 的 3、`1,20` 的
# 20），輸入這一側的新規則不用它。
_ATTACHED_BEFORE: Final = frozenset(string.ascii_letters + string.digits + ".,^")


def _standalone(text: str, number: _WholeNumber) -> bool:
    """獨立的完整數字：前面不是英數字、`.`、`,`、`^`；沒有正負號時前面也不是 `-`、`+`——既有讀法沒把
    那個 `-` 當成負號，就是連字號或範圍（`COVID-19`、`K-12`、`10-40` 的 40）。"""
    before = text[number.start - 1 : number.start] if number.start else ""
    if before in _ATTACHED_BEFORE:
        return False
    return bool(number.sign) or before not in {"-", "+"}


# A1 空白當千分位（`131 978`、`387, 000`、LaTeX 的 `150$\,$000`；不換行空白、窄空白在檢查用副本裡
# 已經是一般空白）：一個獨立、1～3 位的整數（可以帶正負號），後面接一組以上「分隔＋剛好 3 位、沒有
# 正負號與指數的整數」才合併；只有最後一組可以帶量詞或百分比（併到合併的值上，和上一版一樣）。整串
# 後面如果還接著「分隔＋沒有正負號的數字」卻不是 3 位整數（`1 234 5678`、`1 234 567.89`、
# `1 000 000e3`），整串不合併（比上一版保守）。
_THOUSANDS_GAPS: Final = frozenset({" ", ", ", "$\\,$"})
_THOUSANDS_HEAD: Final = re.compile(r"[0-9]{1,3}")
_THOUSANDS_GROUP: Final = re.compile(r"[0-9]{3}")
# A3 `10-40%`：前一個數字和帶百分比的數字之間（連同後者的負號）只有 `-` 與最多各一個空白。
_RANGE_DASH: Final = re.compile(r"\s?-\s?")
# A3 `10% points`、`10 percent points`：帶百分比的完整數字後面接 points。
_POINTS_AFTER: Final = re.compile(r"\s?(?i:points?)(?![A-Za-z])")
# A2 月份英文名：全名與常見縮寫。全名首字大寫（其餘小寫）或全大寫才算；三、四個字母的縮寫
# （`_MONTH_ABBREVIATIONS`）只認首字大寫（`Mar`、`Sept.`），全大寫的 `MAR`（missing at random）、
# `APR`（年利率）不算（審查 A-5）。`May` 也是助動詞，另外要緊鄰數字（`May 2019`、`May 5`、`5 May`）
# 才算。
_MONTHS: Final[Mapping[str, int]] = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_MONTH_ABBREVIATIONS: Final = frozenset(
    {"jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec"}
)
_MAY_THEN_NUMBER: Final = re.compile(r"\.? ?[0-9]")
# A5 年代：完整數字是 2 位或 4 位、最後一位是 0、沒有正負號的整數，後面緊接 `s` 或 `'s`
# （`1960s`、`1960's`、`60s`、`'60s`）。四位數的另外留後兩位（60）。前面不能是英數字、`.`、`,`、
# `^`；前面是連字號照樣算（`pre-1960s`、`mid-1960s`、範圍的後半 `1950s-1960s`、`1950-60s`）。
# `3e-10s`、`2E+10s` 是一個帶指數的完整數字，`1.2 × 10-10s`、`10 ^ - 10s` 整段是科學記號，都不算。
_DECADE_YEAR: Final = re.compile(r"[0-9]{3}0|[0-9]0")
_DECADE_SUFFIX: Final = re.compile(r"'?s(?![A-Za-z])")
# 輸出裡後面接「月」「年代」的數字（含範圍：`7-8月`、`7 至 8 月`、`9、10 月`）：只有這種位置的數字
# 才拿月份、年代比對；其他位置的 9 不因為輸入有 September 而放行。每個數字都必須剛好是一個完整
# 數字、沒有正負號的整數（`_exact_integer`；`3e-1 月`、`-3 月`、`1.5 月` 不算），有一個不算時整段
# 不換。
# 正規式前面的條件只處理「完整數字黏在詞上」的情況（完整與否由 `_exact_integer` 判斷）：前面不能是
# 字母（`K3 月`）、`,`（`1,5 月` 可能是小數逗號）、沒被當成正負號的 `-`／`+`（連字號：`2014-9 月`、
# `K-3 月`），也不能是 `±`、`∓`（審查 F4：既有讀法把 `±3` 讀成 3）。
_RANGE_JOIN: Final = r"\s?(?:-|至|到|~|、)\s?"
_CONTEXT_START: Final = r"(?<![A-Za-z,+\-±∓])"
_MONTH_CONTEXT: Final = re.compile(
    rf"{_CONTEXT_START}(?P<a>[0-9]{{1,2}})(?:{_RANGE_JOIN}(?P<b>[0-9]{{1,2}}))?\s?月"
)
_DECADE_CONTEXT: Final = re.compile(
    rf"{_CONTEXT_START}(?P<a>[0-9]{{4}}|[0-9]{{2}})(?:{_RANGE_JOIN}(?P<b>[0-9]{{4}}|[0-9]{{2}}))?"
    r"\s?年代"
)
# A6 英數詞（字母和數字連在一起的詞：`B2B`、`EC2`、`3D`、`H2b`、`Web2.0`、`N120`）：開頭不接字母、
# 數字、`.`，後面不接字母、數字（可以接 `.`：句尾的 `B2B.` 是 `B2B`）；`.數字` 可以接在後面
# （`Web2.0`）。佔有式量詞：讀不成一個完整的詞（`2.5D`、`1.5e3`）就不算英數詞，照原本的方式讀數字。
_ALNUM_PIECE: Final = re.compile(r"(?<![0-9A-Za-z.])[0-9A-Za-z]++(?:\.[0-9]++)*+(?![0-9A-Za-z])")
_HAS_DIGIT: Final = re.compile(r"[0-9]")
# 輸出裡字母開頭、輸入沒有同一個詞時，照原本的方式讀的（數字照樣要在輸入裡）：一個字母＋一位數字
# （`H1`、`R2`、`T2`）。其他的（`B2B`、`P2P`、`EC2`、`H2b`、`N120`、`COVID19`）拒收。數字開頭的
# 英數詞（`3D`、`12M`、`2x2`、`2020Q1`、`1e5`）一律照原本的方式讀，不在這裡判斷。
_OLD_READING_TOKEN: Final = re.compile(r"[A-Za-z][0-9]")
# B1 詞形變化（ADR-0032 2026-11-18 更新）：把一個詞拆成「字根＋字尾」，兩個詞的字根相同、而且字尾的
# 組合是下面幾種之一才算同一個詞：
# 1. 一邊是原形（沒有字尾，或只去掉結尾的 e、y），另一邊是原形加一個字尾：字根至少 5 個字母
#    （`paradox`→`paradoxical`、`theory`→`theoretical`、`calibrate`→`calibration`）；字尾是 -ing、
#    -ed、-set 時至少 4 個字母（`hack`→`hacking`、`data`→`dataset`）。所以 `form`／`formal` 擋。
#    審查 A-1 收緊：-ism、-ist、-ive、-ively 不和原形配（`individual`／`individualism`、
#    `posit`／`positive`、`object`／`objective` 擋）；-ity 和原形配時字根至少 7 個字母
#    （`sociomaterial`／`sociomateriality` 過，`minor`／`minority`、`prior`／`priority`、
#    `morale`／`morality`、`author`／`authority` 擋）。
# 2. 兩邊都有字尾（同一個字根的兩種變化）：字根至少 6 個字母（`abduction`／`abductive`、
#    `mediation`／`mediating`）；所以 `relation`／`relative`（relat 5 個）、`general`／`generation`
#    （gener）擋。例外：-ism 和 -ist 只和彼此配，字根至少 4 個（`realism`／`realist`）；兩邊都是
#    -ing／-ed／-set 時字根至少 4 個（`hacking`／`hacked`）；其中一邊是 -etic／-etical 時字根至少
#    5 個（`theoretic`／`theorise`，審查 C6）。
# 3. 兩邊都只差 e、y（`policy`／`police`）不算。
# 字尾前面的子音重複時也試去掉一個（`diagramming` → diagram）。複數（s、es、ies）先照 `_variants`
# 去掉。只看字尾，不查字典：`universe`／`university`、`execution`／`executive` 這種字尾剛好對得上的
# 不同詞擋不住（已知漏網）。
_DERIVATIONAL_SUFFIXES: Final = frozenset(
    {
        "al",
        "ial",
        "ical",
        "ic",
        "etic",
        "etical",
        "ally",
        "ly",
        "ian",
        "ity",
        "ion",
        "ation",
        "ational",
        "ive",
        "ively",
        "ise",
        "ize",
        "ism",
        "ist",
    }
)
_INFLECTIONAL_SUFFIXES: Final = frozenset({"ing", "ed", "set"})
_SUFFIXES: Final = _DERIVATIONAL_SUFFIXES | _INFLECTIONAL_SUFFIXES
_BASE_ENDINGS: Final = frozenset({"", "e", "y"})
_ISM_IST: Final = frozenset({"ism", "ist"})
_NOT_WITH_BASE: Final = _ISM_IST | {"ive", "ively"}
_ETIC: Final = frozenset({"etic", "etical"})
_VOWELS: Final = frozenset("aeiou")
# B2 黏字：輸出的英文詞至少幾個字母，才拿去輸入的詞裡找子字串。
_GLUED_MIN_LETTERS: Final = 6
# 黏字的命中前面到詞首剛好是這些否定字首、命中之後就是詞尾時，那個命中不算（審查 A-2、B-5：
# `unemployment` 不讓 `employment` 認得）。
_NEGATION_PREFIXES: Final = frozenset({"un", "in", "im", "ir", "il", "dis", "non", "mis"})
# B3 不必在輸入出現的英文詞。
_ALWAYS_KNOWN_WORDS: Final = frozenset({"vs"})


def _root_splits(word: str) -> set[tuple[str, str]]:
    """（字根, 字尾）的所有拆法；`word` 是小寫。字根至少 4 個字母。"""
    found = {(word, "")}
    for ending in ("e", "y"):
        if word.endswith(ending) and len(word) - 1 >= 4:
            found.add((word[:-1], ending))
    for suffix in _SUFFIXES:
        root = word[: -len(suffix)]
        if not word.endswith(suffix) or len(root) < 4:
            continue
        found.add((root, suffix))
        if (
            suffix[0] in _VOWELS
            and len(root) >= 5
            and root[-1] == root[-2]
            and root[-1] not in _VOWELS
        ):
            found.add((root[:-1], suffix))
    return found


def _same_root(root: str, first: str, second: str) -> bool:
    """同一個字根的兩個字尾算不算同一個詞（規則見 `_DERIVATIONAL_SUFFIXES` 上面的註解）。"""
    if first == second:
        return True
    if first in _BASE_ENDINGS and second in _BASE_ENDINGS:
        return False
    if first in _BASE_ENDINGS or second in _BASE_ENDINGS:
        suffix = second if first in _BASE_ENDINGS else first
        if suffix in _NOT_WITH_BASE:
            return False
        if suffix == "ity":
            return len(root) >= 7
        return len(root) >= (4 if suffix in _INFLECTIONAL_SUFFIXES else 5)
    pair = {first, second}
    if pair & _ISM_IST:
        return pair == _ISM_IST and len(root) >= 4
    if first in _INFLECTIONAL_SUFFIXES and second in _INFLECTIONAL_SUFFIXES:
        return len(root) >= 4
    if pair & _ETIC:
        return len(root) >= 5
    return len(root) >= 6


def _covers_whole_numbers(
    prepared: str, text: str, wholes: list[_WholeNumber], starts: list[int], start: int, end: int
) -> bool:
    """`text[start:end]` 換成 `Q` 不會切開完整數字：碰到的每一個完整數字都整個在裡面（含它的正負號、
    指數、量詞、百分比、千分位）、不是科學記號的一部分，而且這段不在既有讀法換成空白的科學記號或
    含 `^` 的整段裡。`starts` 是 `wholes` 的開頭位置（依序）。"""
    if prepared[start:end] != text[start:end]:
        return False
    index = bisect_left(starts, end) - 1
    while index >= 0 and wholes[index].end > start:
        number = wholes[index]
        if number.start < start or number.end > end or number.in_scientific:
            return False
        index -= 1
    return True


def _exact_integer(by_start: Mapping[int, _WholeNumber], start: int, end: int) -> bool:
    """`[start, end)` 剛好是一個完整數字（所以沒有正負號、小數、指數、量詞、百分比），而且不是科學
    記號的一部分。"""
    number = by_start.get(start)
    return number is not None and number.end == end and not number.in_scientific


def _with_q(text: str, spans: list[tuple[int, int]]) -> str:
    """把每一段換成同樣長度的 `Q`（不再當成數字讀；換成字母，範圍的 `-` 才不會變成負號）。"""
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        chars[start:end] = "Q" * (end - start)
    return "".join(chars)


def _reads_no_new_numbers(before: str, after: str) -> bool:
    """保險：`after`（替換後）讀到的數字都是 `before` 讀得到的，而且 `before` 讀不懂時 `after` 也讀
    不懂。替換只能讓數字變少，不能讀出新的值，也不能把讀不懂變成讀得懂。"""
    old, old_unreadable = numbers(before)
    new, new_unreadable = numbers(after)
    return new <= old and (new_unreadable or not old_unreadable)


def _month_numbers(text: str) -> frozenset[int]:
    found: set[int] = set()
    for match in ENGLISH_WORD.finditer(text):
        word = match.group(0)
        lowered = word.lower()
        month = _MONTHS.get(lowered)
        title = word[0].isupper() and word[1:].islower()
        if month is None or not (title or (word.isupper() and lowered not in _MONTH_ABBREVIATIONS)):
            continue
        if month == 5 and not _may_next_to_number(text, match.start(), match.end()):
            continue
        found.add(month)
    return frozenset(found)


def _may_next_to_number(text: str, start: int, end: int) -> bool:
    if _MAY_THEN_NUMBER.match(text, end):
        return True
    before = text[max(0, start - 2) : start]
    return before[-1:].isdigit() or (before[-1:] == " " and before[:1].isdigit())


def _decade_numbers(prepared: str, wholes: list[_WholeNumber]) -> frozenset[int]:
    """輸入裡的年代（規則見 `_DECADE_YEAR` 上面的註解）；只從完整數字取值。"""
    found: set[int] = set()
    for number in wholes:
        year = number.mantissa
        if number.sign or not number.simple or number.percent or not _DECADE_YEAR.fullmatch(year):
            continue
        before = prepared[number.start - 1 : number.start] if number.start else ""
        if before in _ATTACHED_BEFORE or not _DECADE_SUFFIX.match(prepared, number.end):
            continue
        found.add(int(year))
        if len(year) == 4:
            found.add(int(year[2:]))
    return frozenset(found)


def _alnum_tokens(text: str) -> frozenset[str]:
    """輸入裡字母開頭的英數詞（小寫），加上用一個空白或連字號隔開的 2～3 段串起來的寫法
    （`Web 2.0` → web2.0、`COVID-19` → covid19、`SARS-CoV-2` → sarscov2）。串起來的各段最多只能有
    一段含數字（審查 C1：`top 5 10 firms` 不串成 top510、`Study 1 30` 不串成 study130）。數字開頭的
    英數詞在輸出那一側照原本的方式讀，這裡不收。"""
    pieces = [(match.start(), match.end(), match.group(0)) for match in _ALNUM_PIECE.finditer(text)]
    found: set[str] = set()
    for index, (_, end, piece) in enumerate(pieces):
        if not piece[0].isalpha():
            continue
        joined = piece
        candidates = [piece]
        last_end = end
        digit_pieces = 1 if _HAS_DIGIT.search(piece) else 0
        for next_start, next_end, next_piece in pieces[index + 1 : index + 3]:
            if text[last_end:next_start] not in {" ", "-"}:
                break
            if _HAS_DIGIT.search(next_piece):
                digit_pieces += 1
                if digit_pieces > 1:
                    break
            joined += next_piece
            candidates.append(joined)
            last_end = next_end
        found.update(candidate.lower() for candidate in candidates if _HAS_DIGIT.search(candidate))
    return frozenset(found)


def _thousands_follows(prepared: str, wholes: list[_WholeNumber], index: int) -> bool:
    """`wholes[index]` 後面接著「千分位的分隔＋沒有正負號的完整數字」（自己沒有量詞、百分比）。"""
    if index + 1 >= len(wholes):
        return False
    current, following = wholes[index], wholes[index + 1]
    return (
        not current.scale
        and not current.percent
        and not following.sign
        and prepared[current.end : following.start] in _THOUSANDS_GAPS
    )


def _thousands_group(number: _WholeNumber) -> bool:
    """千分位的一組：剛好 3 位整數、沒有指數、不在科學記號裡（量詞、百分比只有最後一組能帶，由
    `_thousands_follows` 管）。"""
    return (
        not number.exponent
        and not number.in_scientific
        and _THOUSANDS_GROUP.fullmatch(number.mantissa) is not None
    )


def _space_thousands(prepared: str, wholes: list[_WholeNumber]) -> set[Number]:
    """A1 空白千分位合併後的值（規則見 `_THOUSANDS_GAPS` 上面的註解）。每個完整數字只看一次。"""
    found: set[Number] = set()
    index = 0
    while index < len(wholes):
        head = wholes[index]
        if not (
            head.simple
            and not head.percent
            and _THOUSANDS_HEAD.fullmatch(head.mantissa)
            and _standalone(prepared, head)
        ):
            index += 1
            continue
        last = index
        while _thousands_follows(prepared, wholes, last) and _thousands_group(wholes[last + 1]):
            last += 1
        if last > index and not _thousands_follows(prepared, wholes, last):
            group = wholes[index : last + 1]
            merged = head.sign + "".join(number.mantissa for number in group)
            found |= numbers(merged + group[-1].scale + group[-1].percent, keep_unscaled=True)[0]
        index = last + 1
    return found


def _extra_input_numbers(prepared: str, wholes: list[_WholeNumber]) -> set[Number]:
    """`InputVocabulary` 之外多認得的輸入數字，只從完整數字取值、只取沒有指數與量詞的：
    - 空白千分位合併（`_space_thousands`）；
    - `2.84%-38.94%`：負號緊接在前一個數字的 `%` 後面時，另外加正的值（原本的負值照樣留著）；
    - `10-40%`：獨立、沒有百分比的數字後面緊接 `-`＋帶 `%` 的數字時，前者另外加百分比的值；
    - `10% points`：獨立、帶百分比的數字後面接 points 時，另外加不是百分比的值。"""
    found: list[tuple[Decimal | None, bool]] = []
    for previous, current in pairwise(wholes):
        gap = prepared[previous.end : current.start]
        if current.sign == "-" and not gap and previous.percent.endswith("%") and current.simple:
            found.append((current.value(signed=False), bool(current.percent)))
        if (
            previous.simple
            and not previous.percent
            and current.simple
            and current.percent.strip() == "%"
            and _RANGE_DASH.fullmatch(gap + current.sign)
            and _standalone(prepared, previous)
        ):
            found.append((previous.value(), True))
    for number in wholes:
        if (
            number.percent
            and number.simple
            and _POINTS_AFTER.match(prepared, number.end)
            and _standalone(prepared, number)
        ):
            found.append((number.value(), False))
    extra = {(value, percent) for value, percent in found if value is not None}
    return _space_thousands(prepared, wholes) | extra


@dataclass(frozen=True)
class ExtendedVocabulary:
    """中文重點用的輸入詞彙：`InputVocabulary` 加上把輸入讀得更完整的部分（ADR-0032 2026-11-18）。

    - 數字：空白千分位、`%-`、`10-40%`、`10% points`（`_extra_input_numbers`，只從既有讀法讀到的
      完整數字取值）；輸出的正數可以對輸入同值的負數（`knows_number`）。
    - 月份英文名、年代（`1960s`）：只給輸出裡後面接「月」「年代」的數字用（`number_text`）。
    - 字母開頭的英數詞（`B2B`、`EC2`）：整個當成一個詞比對（`number_text`、`knows_alnum`）；數字
      開頭的（`3D`、`2x2`）照原本的方式讀數字。輸出的 `Q` 替換只能把完整數字整個換掉。
    - 英文詞：詞形變化、黏字（否定字首＋輸出詞剛好是整個詞時不算）、`vs`（`knows_word`）。
    """

    base: InputVocabulary
    numbers: frozenset[Number]
    months: frozenset[int]
    decades: frozenset[int]
    alnum_tokens: frozenset[str]
    roots: Mapping[str, frozenset[str]]
    letters: str

    @classmethod
    def build(cls, text: str) -> ExtendedVocabulary:
        """`text` 是檢查用副本。"""
        base = InputVocabulary.build(text)
        roots: dict[str, set[str]] = {}
        for word in base.words:
            for root, suffix in _root_splits(word):
                roots.setdefault(root, set()).add(suffix)
        prepared, wholes = _whole_numbers(text)
        return cls(
            base=base,
            numbers=base.numbers | _extra_input_numbers(prepared, wholes),
            months=_month_numbers(text),
            decades=_decade_numbers(prepared, wholes),
            alnum_tokens=_alnum_tokens(text),
            roots={root: frozenset(suffixes) for root, suffixes in roots.items()},
            letters=" ".join(sorted({word.lower() for word in ENGLISH_WORD.findall(text)})),
        )

    def knows_number(self, number: Number) -> bool:
        """輸入有同一個數字；或輸出是正數、輸入有同值的負數（輸出寫「下跌 4.40%」、輸入是
        `-4.40%`）。反過來（輸出負數、輸入只有正數）不算。"""
        value, percent = number
        return number in self.numbers or (value > 0 and (-value, percent) in self.numbers)

    def knows_alnum(self, token: str) -> bool:
        """輸入有同一個英數詞（不分大小寫；輸入用一個空白或連字號隔開的也算）。PR 2 的允許清單
        （`peer-to-peer` → `P2P`）接在這裡。"""
        return token.lower() in self.alnum_tokens

    def number_text(self, text: str) -> tuple[str, str | None]:
        """輸出（檢查用副本）裡要拿去和輸入數字比對的文字 →（文字, 第一個輸入沒有的英數詞）。

        - 數字開頭的英數詞（`3D`、`12M`、`2x2`、`2020Q1`、`12million`、`1e5`）：照原本的方式讀
          （正負號、百分比、量詞都照樣檢查，數字要在輸入裡）。
        - 字母開頭、輸入有的英數詞換成 `Q`（不再當成數字讀）；但只在它碰到的每一個完整數字都整個在
          詞裡面時才換（`_covers_whole_numbers`）——`K1e-3` 的 `1e-3`、`Top10%` 的 `10%`、`K12 萬`
          的 `12 萬`、`K12,000` 的 `12,000` 只被蓋到一部分，照原本的方式讀。
        - 字母開頭、輸入沒有的英數詞：`H1` 這種「一個字母＋一位數字」照原本的方式讀，其他的回報
          出來（呼叫端拒收）。
        - 後面接「月」「年代」、剛好是一個完整數字、而且是輸入裡的月份或年代的整數，換成 `Q`。
        - 保險（`_reads_no_new_numbers`）：換完讀到的數字必須都是換之前讀得到的，換之前讀不懂的
          換完也要讀不懂；不是的話整欄不換（等於修改前的讀法）。"""
        replaced, unknown = self._replaced_number_text(text)
        if replaced != text and not _reads_no_new_numbers(text, replaced):
            return text, unknown
        return replaced, unknown

    def _replaced_number_text(self, text: str) -> tuple[str, str | None]:
        """`number_text` 的替換本身（還沒經過保險）。"""
        prepared, wholes = _whole_numbers(text)
        starts = [number.start for number in wholes]
        spans: list[tuple[int, int]] = []
        unknown: str | None = None
        for match in _ALNUM_PIECE.finditer(text):
            token = match.group(0)
            if token[0].isdigit() or not _HAS_DIGIT.search(token):
                continue
            if self.knows_alnum(token):
                if _covers_whole_numbers(prepared, text, wholes, starts, *match.span()):
                    spans.append(match.span())
            elif not _OLD_READING_TOKEN.fullmatch(token) and unknown is None:
                unknown = token
        by_start = {number.start: number for number in wholes}
        for pattern, allowed in ((_MONTH_CONTEXT, self.months), (_DECADE_CONTEXT, self.decades)):
            if not allowed:
                continue
            for match in pattern.finditer(text):
                groups = [match.span(name) for name in ("a", "b") if match[name] is not None]
                if all(_exact_integer(by_start, start, end) for start, end in groups):
                    spans.extend(
                        (start, end) for start, end in groups if int(text[start:end]) in allowed
                    )
        return _with_q(text, spans), unknown

    def knows_word(self, word: str) -> bool:
        """`InputVocabulary.knows_word`，加上 `vs`、詞形變化（`_same_root`）、黏字（`_glued`）。"""
        if self.base.knows_word(word):
            return True
        lowered = word.lower()
        if lowered in _ALWAYS_KNOWN_WORDS:
            return True
        variants = _variants(lowered)
        for variant in variants:
            for root, suffix in _root_splits(variant):
                if any(_same_root(root, suffix, other) for other in self.roots.get(root, ())):
                    return True
        return any(self._glued(variant) for variant in variants)

    def _glued(self, variant: str) -> bool:
        """黏字：`variant` 6 個字母以上、是輸入某個英文詞（`letters` 用空白隔開的詞）的子字串。命中
        前面到詞首剛好是一個否定字首（`_NEGATION_PREFIXES`）、命中之後就是詞尾（或只剩複數的 s、es）
        時，那個命中不算，找下一個命中。每個命中只看前後幾個字元；不算的命中一個詞最多一個，所以仍是
        線性時間。"""
        if len(variant) < _GLUED_MIN_LETTERS:
            return False
        letters = self.letters
        start = letters.find(variant)
        while start != -1:
            rest = letters[start + len(variant) : start + len(variant) + 3]
            if (
                rest[:1] not in {"", " "}
                and rest[:2] not in {"s", "s "}
                and rest not in {"es", "es "}
            ):
                return True
            before = letters[max(0, start - 4) : start]
            space = before.rfind(" ")
            if space == -1 and start > 3:
                return True  # 前面到詞首超過 3 個字母，不是否定字首
            if before[space + 1 :] not in _NEGATION_PREFIXES:
                return True
            start = letters.find(variant, start + 1)
        return False

    def knows_token(self, token: str) -> bool:
        """`InputVocabulary.knows_token`，每一段改用 `knows_word`。"""
        if self.base.knows_token(token):
            return True
        parts = [part for part in token.split("-") if len(part) >= 2]
        return all(self.knows_word(part) for part in parts)


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
