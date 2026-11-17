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
  ——空白千分位、月份英文名、`10-40%`、`10% points`、年代、英數混寫的詞（`B2B`）、輸出正數對輸入
  負數、英文詞的詞形變化與黏字、`vs`。研究模型不用它。

都是純函式；上面幾項的行為和 2.4 原本在 `zh_summary.py` 的一樣（只改成公開名稱），加
`ExtendedVocabulary` 時也沒改。判準的細節與抓不到的情況寫在 ADR-0032。
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

# A1 空白當千分位（`131 978`、`387, 000`、LaTeX 的 `150$\,$000`；不換行空白、窄空白在檢查用副本裡
# 已經是一般空白）：1～3 位數字後面接一組以上「分隔＋剛好 3 位數字」才合併。開頭不能接在字母、
# 數字、`.`、`,`、「英數＋連字號」後面（`in 2019 300 firms` 的 2019 是 4 位，不合併；`COVID-19 300`
# 不合併）。每一組後面都不能再接數字，量詞用佔有式（不退回重試，不會平方級回溯）。
_SPACE_GROUPED: Final = re.compile(
    r"(?<![0-9A-Za-z.,])(?<![0-9A-Za-z]-)[0-9]{1,3}(?:(?: |, |\$\\,\$)[0-9]{3}(?![0-9]))++"
)
_NOT_DIGIT: Final = re.compile(r"[^0-9]")
# A3 `2.84%-38.94%`：`%` 後面的 `-` 是範圍，不是負號；另讀一次時換成空白。
_PERCENT_DASH: Final = re.compile(r"%-(?=[0-9.])")
# A3 `10-40%` 的前半也是百分比（只認 `-`，`10 to 40%` 不算）。
_PERCENT_RANGE: Final = re.compile(
    r"(?<![0-9A-Za-z.,])(?P<low>[0-9]++(?:\.[0-9]++)?+)\s?-\s?[0-9]++(?:\.[0-9]++)?+\s?%"
)
# A3 `10% points`、`10 percent points`：讀成不是百分比的 10，對得上輸出的「10 個百分點」。
# （`10 percentage points` 本來就讀成不是百分比的 10。）
_PERCENT_POINTS: Final = re.compile(
    r"(?<![0-9A-Za-z.,])(?P<value>[0-9]++(?:\.[0-9]++)?+)\s?(?:%|(?i:percent|per cent))\s?"
    r"(?i:points?)(?![A-Za-z])"
)
# A2 月份英文名：全名與常見縮寫。首字大寫（其餘小寫）或全大寫才算；`May` 也是助動詞，另外要緊鄰
# 數字（`May 2019`、`May 5`、`5 May`）才算。
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
_MAY_THEN_NUMBER: Final = re.compile(r"\.? ?[0-9]")
# A5 年代：`1960s`、`1960's`、`60s`、`'60s`（最後一位是 0）。四位數的另外留後兩位（60）。
_DECADE: Final = re.compile(r"(?<![0-9A-Za-z.])'?(?P<year>[0-9]{3}0|[0-9]0)'?s(?![A-Za-z])")
# 輸出裡後面接「月」「年代」的數字（含範圍：`7-8月`、`7 至 8 月`、`9、10 月`）：只有這種位置的數字
# 才拿月份、年代比對；其他位置的 9 不因為輸入有 September 而放行。
_RANGE_JOIN: Final = r"\s?(?:-|至|到|~|、)\s?"
_MONTH_CONTEXT: Final = re.compile(
    rf"(?<![0-9A-Za-z.])(?P<a>[0-9]{{1,2}})(?:{_RANGE_JOIN}(?P<b>[0-9]{{1,2}}))?\s?月"
)
_DECADE_CONTEXT: Final = re.compile(
    rf"(?<![0-9A-Za-z.])(?P<a>[0-9]{{4}}|[0-9]{{2}})(?:{_RANGE_JOIN}(?P<b>[0-9]{{4}}|[0-9]{{2}}))?"
    r"\s?年代"
)
# A6 英數混寫的詞（`B2B`、`EC2`、`3D`、`H2b`、`Web2.0`、`N120`）：字母與數字連在一起、前後不接字母、
# 數字、`.`；`.數字` 可以接在後面（`Web2.0`）。佔有式量詞：讀不成一個完整的詞（`2.5D`、`1.5e3`）就
# 不算英數詞，照原本的方式讀數字。輸入這一側另外把用一個空白或連字號隔開的 2～3 段串起來
# （`Web 2.0` → web2.0、`3-D` → 3d、`COVID-19` → covid19、`SARS-CoV-2`）。
_ALNUM_PIECE: Final = re.compile(r"(?<![0-9A-Za-z.])[0-9A-Za-z]++(?:\.[0-9]++)*+(?![0-9A-Za-z])")
_HAS_LETTER: Final = re.compile(r"[A-Za-z]")
_HAS_DIGIT: Final = re.compile(r"[0-9]")
# 英數詞裡仍然當成數字讀的：指數（`2E5`；`1e-3` 讀到的是 `1e`）。
_EXPONENT_TOKEN: Final = re.compile(r"[0-9]+[eE][0-9]*")
# 輸入沒有同一個英數詞時，照原本的方式讀（數字照樣要在輸入裡）：一個字母＋一位數字（`H1`、`R2`、
# `T2`）、數字開頭後面只有字母（`3D`、`5G`、`12M`、`1960s`、`20th`、`24h`）。其他的（`B2B`、`P2P`、
# `EC2`、`H2b`、`N120`、`COVID19`）輸入沒有同一個詞就拒收。
_OLD_READING_TOKEN: Final = re.compile(r"[A-Za-z][0-9]|[0-9]+[A-Za-z]+")
# B1 詞形變化（ADR-0032 2026-11-18 更新）：把一個詞拆成「字根＋字尾」，兩個詞的字根相同、而且字尾的
# 組合是下面三種之一才算同一個詞：
# 1. 一邊是原形（沒有字尾，或只去掉結尾的 e、y），另一邊是原形加一個字尾：字根至少 5 個字母
#    （`paradox`→`paradoxical`、`theory`→`theoretical`、`calibrate`→`calibration`）；字尾是 -ing、
#    -ed、-set 時至少 4 個字母（`hack`→`hacking`、`data`→`dataset`）。所以 `form`／`formal` 擋。
# 2. 兩邊都有字尾（同一個字根的兩種變化）：字根至少 6 個字母（`abduction`／`abductive`、
#    `mediation`／`mediating`）；所以 `relation`／`relative`（relat 5 個）、`general`／`generation`
#    （gener）擋。例外：-ism／-ist、兩邊都是 -ing／-ed／-set 時字根至少 4 個（`realism`／
#    `realist`、`hacking`／`hacked`）。
# 3. 兩邊都只差 e、y（`policy`／`police`）不算。
# 字尾前面的子音重複時也試去掉一個（`diagramming` → diagram）。複數（s、es、ies）先照 `_variants`
# 去掉。只看字尾，不查字典：`universe`／`university` 這種字尾剛好對得上的不同詞擋不住（已知漏網）。
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
_VOWELS: Final = frozenset("aeiou")
# B2 黏字：輸出的英文詞至少幾個字母，才拿去輸入的詞裡找子字串。
_GLUED_MIN_LETTERS: Final = 6
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
        return len(root) >= (4 if suffix in _INFLECTIONAL_SUFFIXES else 5)
    if {first, second} == {"ism", "ist"}:
        return len(root) >= 4
    if first in _INFLECTIONAL_SUFFIXES and second in _INFLECTIONAL_SUFFIXES:
        return len(root) >= 4
    return len(root) >= 6


def _consume_context(text: str, pattern: re.Pattern[str], allowed: frozenset[int]) -> str:
    """`pattern` 的 a、b 兩組數字在 `allowed` 裡時換成同樣長度的 `Q`（不再當成數字讀；換成字母，
    範圍的 `-` 才不會變成負號）。"""
    if not allowed:
        return text

    def replace(match: re.Match[str]) -> str:
        chars = list(match.group(0))
        for group in ("a", "b"):
            digits = match[group]
            if digits is not None and int(digits) in allowed:
                start = match.start(group) - match.start()
                chars[start : start + len(digits)] = "Q" * len(digits)
        return "".join(chars)

    return pattern.sub(replace, text)


def _month_numbers(text: str) -> frozenset[int]:
    found: set[int] = set()
    for match in ENGLISH_WORD.finditer(text):
        word = match.group(0)
        month = _MONTHS.get(word.lower())
        if month is None or not (word.isupper() or (word[0].isupper() and word[1:].islower())):
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


def _decade_numbers(text: str) -> frozenset[int]:
    found: set[int] = set()
    for match in _DECADE.finditer(text):
        year = match["year"]
        found.add(int(year))
        if len(year) == 4:
            found.add(int(year[2:]))
    return frozenset(found)


def _alnum_tokens(text: str) -> frozenset[str]:
    """輸入裡的英數詞（小寫），加上用一個空白或連字號隔開的 2～3 段串起來的寫法。"""
    pieces = [(match.start(), match.end(), match.group(0)) for match in _ALNUM_PIECE.finditer(text)]
    found: set[str] = set()
    for index, (_, end, piece) in enumerate(pieces):
        joined = piece
        candidates = [piece]
        last_end = end
        for next_start, next_end, next_piece in pieces[index + 1 : index + 3]:
            if text[last_end:next_start] not in {" ", "-"}:
                break
            joined += next_piece
            candidates.append(joined)
            last_end = next_end
        found.update(
            candidate.lower()
            for candidate in candidates
            if _HAS_LETTER.search(candidate) and _HAS_DIGIT.search(candidate)
        )
    return frozenset(found)


def _extra_input_numbers(text: str) -> set[Number]:
    """`InputVocabulary` 之外多認得的輸入數字：空白千分位合併、`%-` 後面不是負號（另讀一次，原本的
    讀法照樣留著）、`10-40%` 的 10%、`10% points` 的 10。"""
    reread = _PERCENT_DASH.sub("% ", _SPACE_GROUPED.sub(lambda m: _NOT_DIGIT.sub("", m[0]), text))
    found, _ = numbers(reread, keep_unscaled=True)
    for pattern, group, percent in (
        (_PERCENT_RANGE, "low", True),
        (_PERCENT_POINTS, "value", False),
    ):
        for match in pattern.finditer(reread):
            value = decimal_of(match[group])
            if value is not None:
                found.add((value, percent))
    return found


@dataclass(frozen=True)
class ExtendedVocabulary:
    """中文重點用的輸入詞彙：`InputVocabulary` 加上把輸入讀得更完整的部分（ADR-0032 2026-11-18）。

    - 數字：空白千分位、`%-`、`10-40%`、`10% points`（`_extra_input_numbers`）；輸出的正數可以對
      輸入同值的負數（`knows_number`）。
    - 月份英文名、年代（`1960s`）：只給輸出裡後面接「月」「年代」的數字用（`number_text`）。
    - 英數詞（`B2B`、`EC2`）：整個當成一個詞比對（`number_text`、`knows_alnum`）。
    - 英文詞：詞形變化、黏字、`vs`（`knows_word`）。
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
        return cls(
            base=base,
            numbers=base.numbers | _extra_input_numbers(text),
            months=_month_numbers(text),
            decades=_decade_numbers(text),
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

        輸入有的英數詞、後面接「月」「年代」而且是輸入裡的月份或年代的數字，換成 `Q`（不再當成
        數字讀）。輸入沒有的英數詞：`H1`、`3D` 這種照原本的方式讀（數字照樣要在輸入裡），其他的
        回報出來（呼叫端拒收）。"""
        unknown: str | None = None

        def replace(match: re.Match[str]) -> str:
            nonlocal unknown
            token = match.group(0)
            if not (_HAS_LETTER.search(token) and _HAS_DIGIT.search(token)):
                return token
            if _EXPONENT_TOKEN.fullmatch(token):
                return token
            if self.knows_alnum(token):
                return "Q" * len(token)
            if not _OLD_READING_TOKEN.fullmatch(token) and unknown is None:
                unknown = token
            return token

        text = _ALNUM_PIECE.sub(replace, text)
        text = _consume_context(text, _MONTH_CONTEXT, self.months)
        return _consume_context(text, _DECADE_CONTEXT, self.decades), unknown

    def knows_word(self, word: str) -> bool:
        """`InputVocabulary.knows_word`，加上 `vs`、詞形變化（`_same_root`）、黏字（6 個字母以上、是
        輸入某個英文詞的子字串）。"""
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
        return any(
            len(variant) >= _GLUED_MIN_LETTERS and variant in self.letters for variant in variants
        )

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
