"""`prompt_template` 這個來源不能由一般呼叫端自己標（ADR-0029、Opus 第三次確認審查第 2 點）。

`clear_for_cloud` 遇到 `ContentSource.PROMPT_TEMPLATE` 不管開關一律放行，而 `LlmRequest.system`
又規定要是這個來源；呼叫端只要把「模板 `.format(fulltext=...)`」的結果標成 `prompt_template`，
付費全文就送得出去。2.2 會由專用的 prompt loader 從 `prompts/<工作>/v<N>.md` 讀檔放行模板，
`clear_for_cloud` 改成拒收外部傳入的 `prompt_template`（spec 的 deferred）。在那之前先守住：

- `radar/` 裡 `ContentSource.PROMPT_TEMPLATE`（名字、屬性、`from ... import`）與字串
  `"prompt_template"` 只能出現在 `domain/policy.py`、`domain/llm.py`，以及預留給 2.2 的
  `services/prompt_loader.py`（現在還不存在）。

掃描函式另外用 tmp 裡的假檔證明抓得到。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "radar"
# 拆開寫，避免這個檔的寫法和被掃的寫法混在一起（這個檔不在掃描範圍）。
MEMBER = "PROMPT" + "_TEMPLATE"
VALUE = "prompt" + "_template"
ALLOWED = frozenset(
    {
        RADAR / "domain" / "policy.py",
        RADAR / "domain" / "llm.py",
        # 2.2 的 prompt loader：只讀 prompts/ 底下的檔案放行模板。名稱先預留，2.2 若換名字要改這裡。
        RADAR / "services" / "prompt_loader.py",
    }
)


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


def test_only_policy_llm_and_the_prompt_loader_use_prompt_template() -> None:
    offenders = [
        hit
        for path in sorted(RADAR.rglob("*.py"))
        if path not in ALLOWED
        for hit in prompt_template_violations(path, base=ROOT)
    ]

    assert offenders == []


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
