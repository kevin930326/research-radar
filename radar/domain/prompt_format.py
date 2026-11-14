"""prompt 檔的切法：設定區與本文（AD-8、ADR-0009 第 3 步、ADR-0030）。

prompt 檔放在 `prompts/<工作代碼>/v<N>.md`，第一行是 `---`，接著設定區的 `key: value` 行，
再一行 `---`，之後是本文（系統提示，沒有任何變數）。

這裡只做字串處理、不 import 其他 `radar` 模組：`domain/policy.py` 核對過整個檔的雜湊之後用它
拿掉設定區、放行本文；`domain/prompts.py` 用同一個函式解析設定區，兩邊切出來的一定一樣。
"""

from typing import Final

FENCE: Final = "---"


class PromptFormatError(ValueError):
    """prompt 檔的格式不對。訊息只說哪裡不對，不帶模板內容。"""


def split_prompt_file(text: str) -> tuple[list[tuple[int, str]], str]:
    """把 prompt 檔切成設定區與本文。

    回（設定區的每一行與它的行號，本文）。換行一律當 `\\n`；本文去掉頭尾空白後不能是空的。

    Raises:
        TypeError: `text` 不是 `str`。
        PromptFormatError: 第一行不是 `---`、設定區沒有結尾的 `---`，或本文是空的。
    """
    if type(text) is not str:
        raise TypeError(f"prompt 檔的內容必須是 str（收到 {type(text).__name__}）")
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[0].strip() != FENCE:
        raise PromptFormatError("prompt 檔第一行要是 ---（設定區開頭）")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == FENCE)
    except StopIteration:
        raise PromptFormatError("設定區沒有結尾的 ---") from None
    settings = [(number, lines[number - 1]) for number in range(2, end + 1)]
    body = "\n".join(lines[end + 1 :]).strip()
    if not body:
        raise PromptFormatError("prompt 的本文是空的")
    return settings, body
