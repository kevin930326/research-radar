"""`prompt_template` 這個來源不能由一般呼叫端自己標（ADR-0029、Opus 第三次確認審查第 2 點）。

`clear_for_cloud` 遇到 `ContentSource.PROMPT_TEMPLATE` 不管開關一律放行，而 `LlmRequest.system`
又規定要是這個來源；呼叫端只要把「模板 `.format(fulltext=...)`」的結果標成 `prompt_template`，
付費全文就送得出去。2.2 會由專用的 prompt loader 從 `prompts/<工作>/v<N>.md` 讀檔放行模板，
`clear_for_cloud` 改成拒收外部傳入的 `prompt_template`（spec 的 deferred）。在那之前先守住：

- `radar/` 裡 `ContentSource.PROMPT_TEMPLATE`（名字、屬性、`from ... import`）與字串
  `"prompt_template"` 只能出現在 `domain/policy.py`；`domain/llm.py` 只能拿它來**比對**
  （`is`／`==`），不能拿來放行。
- 預留給 2.2 的 `services/prompt_loader.py`（現在還不存在；2.2 若放在別處，要把下面的
  `PROMPT_LOADER` 一起改掉）**沒有**特別的許可：模板的來源由 policy 的專用入口自己標，loader
  不需要也不准提到 `PROMPT_TEMPLATE`。這個檔存在時另外要守：不呼叫 `clear_for_cloud`（模板只經
  policy 的專用入口放行）、不用 `.format`／`.format_map`／`string.Template`／`%` 把變數填進模板
  （變數用分開的 `CloudSafeText` 區塊）。

這條架構測試擋不到的寫法（ADR-0029 取捨）：`clear_for_cloud(text, cloud_source(既有的模板), ...)`
沿用別的文字的來源、`"prompt" + "_template"` 這種組字串、走訪 `ContentSource` 挑出成員、
`ContentSource(變數)`。2.2 讓 `clear_for_cloud` 拒收 `PROMPT_TEMPLATE` 之後，這些都會在執行期被擋。

掃描函式另外用 tmp 裡的假檔證明抓得到。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
# 拆開寫，避免這個檔的寫法和被掃的寫法混在一起（這個檔不在掃描範圍）。
MEMBER = "PROMPT" + "_TEMPLATE"
VALUE = "prompt" + "_template"
POLICY = RADAR / "domain" / "policy.py"
LLM_MODULE = RADAR / "domain" / "llm.py"
# 2.2 的 prompt loader 預留位置（架構文件還沒寫；2.2 若放在別處，改這裡並同步改 ADR-0029）。
PROMPT_LOADER = RADAR / "services" / "prompt_loader.py"
TEMPLATE_FILLERS = frozenset({"format", "format_map", "Template", "substitute", "safe_substitute"})


def prompt_template_violations(path: Path, *, base: Path) -> list[str]:
    """這個檔裡用到 `prompt_template` 來源的地方（「相對路徑:行號 說明」）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    name = path.relative_to(base).as_posix()
    found: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == MEMBER) or (
            isinstance(node, ast.Name) and node.id == MEMBER
        ):
            found.append(f"{name}:{node.lineno} 用 {MEMBER}")
        elif isinstance(node, ast.ImportFrom) and any(a.name == MEMBER for a in node.names):
            found.append(f"{name}:{node.lineno} import {MEMBER}")
        elif isinstance(node, ast.Constant) and node.value in (MEMBER, VALUE):
            found.append(f"{name}:{node.lineno} 用字串 {node.value!r}")
    return found


def outside_comparison_uses(path: Path, *, base: Path) -> list[str]:
    """這個檔裡不是拿來比對（`is`／`==` 等）的 `PROMPT_TEMPLATE` 用法，以及字串用法。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    name = path.relative_to(base).as_posix()
    in_comparison = {
        id(sub)
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        for sub in ast.walk(node)
    }
    return [
        hit
        for node in ast.walk(tree)
        if id(node) not in in_comparison
        for hit in prompt_template_violations_of(node, name)
    ]


def prompt_template_violations_of(node: ast.AST, name: str) -> list[str]:
    if (isinstance(node, ast.Attribute) and node.attr == MEMBER) or (
        isinstance(node, ast.Name) and node.id == MEMBER
    ):
        return [f"{name}:{node.lineno} 不是比對卻用 {MEMBER}"]
    if isinstance(node, ast.Constant) and node.value in (MEMBER, VALUE):
        return [f"{name}:{node.lineno} 用字串 {node.value!r}"]
    return []


def prompt_loader_violations(path: Path, *, base: Path) -> list[str]:
    """prompt loader 裡自己放行模板或把變數填進模板的地方。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    name = path.relative_to(base).as_posix()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else None
            if isinstance(func, ast.Attribute):
                called = func.attr
            if called == "clear_for_cloud":
                found.append(f"{name}:{node.lineno} 呼叫 clear_for_cloud")
            elif called in TEMPLATE_FILLERS:
                found.append(f"{name}:{node.lineno} 用 {called} 填模板")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            found.append(f"{name}:{node.lineno} 用 % 填模板")
    return found


def test_only_policy_uses_prompt_template() -> None:
    offenders = [
        hit
        for path in sorted(RADAR.rglob("*.py"))
        if path not in (POLICY, LLM_MODULE)
        for hit in prompt_template_violations(path, base=ROOT)
    ]

    assert offenders == []


def test_llm_module_only_compares_against_prompt_template() -> None:
    assert outside_comparison_uses(LLM_MODULE, base=ROOT) == []


def test_prompt_loader_does_not_clear_or_fill_templates() -> None:
    # 2.2 建立這個檔之前沒有東西可查；建立之後這條自動生效。
    if not PROMPT_LOADER.exists():
        return
    assert prompt_loader_violations(PROMPT_LOADER, base=ROOT) == []


def test_comparison_scanner(tmp_path: Path) -> None:
    sample = tmp_path / "llm.py"
    sample.write_text(
        "from radar.domain.policy import ContentSource\n"
        f"a = source is ContentSource.{MEMBER}\n"
        f"b = source == ContentSource.{MEMBER}\n"
        f"c = clear_for_cloud(text, ContentSource.{MEMBER}, None, p)\n"
        f"d = ContentSource('{VALUE}')\n",
        encoding="utf-8",
    )

    assert sorted(outside_comparison_uses(sample, base=tmp_path)) == [
        f"llm.py:4 不是比對卻用 {MEMBER}",
        f"llm.py:5 用字串 {VALUE!r}",
    ]


def test_prompt_loader_scanner(tmp_path: Path) -> None:
    sample = tmp_path / "prompt_loader.py"
    sample.write_text(
        "from string import Template\n"
        "a = clear_for_cloud(text, source, None, p)\n"
        "b = policy.clear_for_cloud(text, source, None, p)\n"
        "c = template.format(fulltext=text)\n"
        "d = template.format_map(values)\n"
        "e = Template(template).substitute(values)\n"
        "f = template % values\n"
        "g = policy.clear_prompt_file(path)\n",
        encoding="utf-8",
    )

    assert sorted(prompt_loader_violations(sample, base=tmp_path)) == sorted(
        [
            "prompt_loader.py:2 呼叫 clear_for_cloud",
            "prompt_loader.py:3 呼叫 clear_for_cloud",
            "prompt_loader.py:4 用 format 填模板",
            "prompt_loader.py:5 用 format_map 填模板",
            "prompt_loader.py:6 用 Template 填模板",
            "prompt_loader.py:6 用 substitute 填模板",
            "prompt_loader.py:7 用 % 填模板",
        ]
    )


def test_llm_module_really_uses_prompt_template() -> None:
    # llm.py 真的用到：代表掃描的名稱對得上實際寫法。
    assert prompt_template_violations(RADAR / "domain" / "llm.py", base=ROOT) != []


def test_prompt_template_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "service.py"
    sample.write_text(
        "from radar.domain import policy\n"
        f"from radar.domain.policy import ContentSource\n"
        f"a = clear_for_cloud(text, ContentSource.{MEMBER}, None, p)\n"
        f"b = policy.ContentSource('{VALUE}')\n"
        f"c = getattr(ContentSource, '{MEMBER}')\n"
        f"d = ContentSource['{MEMBER}']\n",
        encoding="utf-8",
    )

    hits = prompt_template_violations(sample, base=tmp_path)

    assert sorted(hits) == [
        f"service.py:3 用 {MEMBER}",
        f"service.py:4 用字串 {VALUE!r}",
        f"service.py:5 用字串 {MEMBER!r}",
        f"service.py:6 用字串 {MEMBER!r}",
    ]


def test_prompt_template_scanner_allows_other_sources(tmp_path: Path) -> None:
    sample = tmp_path / "ok.py"
    sample.write_text(
        "from radar.domain.policy import ContentSource\n"
        "a = clear_for_cloud(text, ContentSource.OPENALEX_ABSTRACT, None, p)\n"
        "b = 'prompt_templates_dir'\n",
        encoding="utf-8",
    )

    assert prompt_template_violations(sample, base=tmp_path) == []
