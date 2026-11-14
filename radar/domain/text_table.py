"""給終端機看的文字表格：依顯示寬度對齊（匯入報表、覆蓋率共用）。"""

import unicodedata
from collections.abc import Sequence


def display_width(text: str) -> int:
    """終端機上的顯示寬度：全形字（East Asian Width 是 F 或 W）算 2，其他算 1。"""
    return sum(2 if unicodedata.east_asian_width(char) in "FW" else 1 for char in text)


def render_table(header: Sequence[str], body: Sequence[Sequence[str]], *, align: str) -> list[str]:
    """依顯示寬度對齊的表格；`align` 每欄一個字元（`l` 靠左、`r` 靠右），欄與欄之間兩個空白。"""
    table = [list(header), *(list(row) for row in body)]
    widths = [max(display_width(row[index]) for row in table) for index in range(len(header))]
    lines: list[str] = []
    for row in table:
        cells: list[str] = []
        for index, cell in enumerate(row):
            padding = " " * (widths[index] - display_width(cell))
            cells.append(cell + padding if align[index] == "l" else padding + cell)
        lines.append("  ".join(cells).rstrip())
    return lines
