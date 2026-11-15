"""程式裡沒有下載付費全文的路徑（AD-9、ADR-0029、epic 2「付費全文不送雲端」）。

AST 掃 `radar/`：
1. 能連網路的套件只能在允許清單的模組 import（清單每項寫用途）。
2. OpenAlex adapter 對 HTTP client 送出的請求只有 `get`，網址以 `/works` 結尾；
   `config/openalex.yaml` 的 `base_url` 是 `https://api.openalex.org`。
3. 識別字與字串常數（docstring 除外）不出現全文下載相關的字（不分大小寫）。

掃描函式另外用 tmp 裡的假檔證明抓得到。
"""

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "radar"
OPENALEX_ADAPTER = PACKAGE / "adapters" / "openalex" / "__init__.py"

NETWORK_PACKAGES = frozenset(
    {
        "httpx2",
        "httpx",
        "requests",
        "urllib.request",
        "urllib3",
        "aiohttp",
        "http.client",
        "socket",
        "anthropic",
        "boto3",
        "botocore",
        # 開外部程式（curl、wget）、FTP、libcurl、WebSocket 也都能下載檔案。
        "subprocess",
        "ftplib",
        "pycurl",
        "websockets",
    }
)
# 可以 import 能連網路的套件的模組（含子模組）與用途。
NETWORK_ALLOWED: dict[str, str] = {
    "radar.adapters.openalex": "OpenAlex 書目與摘要（只用 GET /works）",
    "radar.adapters.heartbeat": "排程成功後對監看服務送心跳 GET",
    "radar.adapters.object_store": "自己的物件儲存（R2／RustFS，存原始資料）",
    "radar.adapters.anthropic": "Anthropic Messages API（只送已放行的 CloudSafeText）",
}
# 會送出請求的 HTTP client 方法。
REQUEST_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request", "send", "stream"}
)
FORBIDDEN_WORDS = (
    "pdf_url",
    "content.openalex.org",
    "oa_url",
    "best_oa_location",
    "unpaywall",
    "sci-hub",
    "libgen",
    "ezproxy",
    "openaccesspdf",
    "landing_page_url",
    "text-mining",
    ".pdf",
)
# 現有程式裡合理的命中（相對路徑 → 允許的字）；目前沒有。
FORBIDDEN_WORDS_ALLOWED: dict[str, frozenset[str]] = {}


def _module_name(path: Path, base: Path) -> str:
    parts = list(path.relative_to(base).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_network(module: str) -> bool:
    return any(module == pkg or module.startswith(pkg + ".") for pkg in NETWORK_PACKAGES)


def network_imports(path: Path) -> list[str]:
    """這個檔 import 的能連網路的套件（「行號 模組」）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                f"{node.lineno} {alias.name}" for alias in node.names if _is_network(alias.name)
            )
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _is_network(node.module):
                found.append(f"{node.lineno} {node.module}")
            else:
                # `from urllib import request`、`from http import client`
                found.extend(
                    f"{node.lineno} {node.module}.{alias.name}"
                    for alias in node.names
                    if _is_network(f"{node.module}.{alias.name}")
                )
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called in {"import_module", "__import__"} and node.args:
                first = node.args[0]
                if (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and _is_network(first.value)
                ):
                    found.append(f"{node.lineno} {first.value}")
    return found


def _allowed_module(module: str) -> bool:
    return any(module == owner or module.startswith(owner + ".") for owner in NETWORK_ALLOWED)


def test_only_allowed_modules_import_network_packages() -> None:
    offenders = {
        _module_name(path, ROOT): hits
        for path in sorted(PACKAGE.rglob("*.py"))
        if not _allowed_module(_module_name(path, ROOT)) and (hits := network_imports(path))
    }

    assert offenders == {}


def test_every_allowed_module_exists_and_has_a_purpose() -> None:
    for module, purpose in NETWORK_ALLOWED.items():
        path = ROOT / Path(*module.split("."))
        assert (path / "__init__.py").is_file() or path.with_suffix(".py").is_file(), module
        assert purpose.strip(), module


def test_network_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import httpx2\n"
        "import urllib.request\n"
        "from urllib import request\n"
        "from http import client\n"
        "from botocore.config import Config\n"
        "import importlib\n"
        "s = importlib.import_module('socket')\n"
        "from urllib.parse import urlsplit\n"
        "import json\n"
        "import subprocess\n"
        "from ftplib import FTP\n"
        "import pycurl\n"
        "from websockets.sync.client import connect\n",
        encoding="utf-8",
    )

    assert sorted(network_imports(sample)) == sorted(
        [
            "1 httpx2",
            "2 urllib.request",
            "3 urllib.request",
            "4 http.client",
            "5 botocore.config",
            "7 socket",
            "10 subprocess",
            "11 ftplib",
            "12 pycurl",
            "13 websockets.sync.client",
        ]
    )


# --- OpenAlex 只用 GET /works ---------------------------------------------------------------


def openalex_requests(path: Path) -> list[tuple[str, str | None]]:
    """這個檔對 HTTP client 送出的請求：（方法, 網址字面的結尾）。

    算的是 `self._client.<方法>(...)`、`<任何東西>.client.<方法>(...)` 與 `httpx2.<方法>(...)`。
    網址是 f-string 或字串時取最後一段字面，否則是 None。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        method = node.func.attr
        owner = node.func.value
        is_client = (isinstance(owner, ast.Attribute) and owner.attr in {"_client", "client"}) or (
            isinstance(owner, ast.Name) and owner.id in {"client", "httpx2", "httpx"}
        )
        if not is_client or method not in REQUEST_METHODS:
            continue
        found.append((method, _url_tail(node.args[0]) if node.args else None))
    return found


def _url_tail(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        last = node.values[-1]
        if isinstance(last, ast.Constant) and isinstance(last.value, str):
            return last.value
    return None


def test_openalex_only_sends_get_works() -> None:
    requests = openalex_requests(OPENALEX_ADAPTER)

    assert requests, "OpenAlex adapter 找不到任何請求，掃描方式可能對不上"
    assert all(method == "get" for method, _ in requests), requests
    assert all(tail is not None and tail.endswith("/works") for _, tail in requests), requests


def test_openalex_config_points_at_the_official_api() -> None:
    config = yaml.safe_load((ROOT / "config" / "openalex.yaml").read_text(encoding="utf-8"))

    assert config["base_url"] == "https://api.openalex.org"


def test_openalex_request_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "self._client.get(f'{base}/works', params=p)\n"
        "self._client.get(f'{base}/works/{oid}/fulltext')\n"
        "self._client.post(f'{base}/works')\n"
        "httpx2.get('https://example.org/x.pdf')\n"
        "client.stream('GET', url)\n"
        "self._client.close()\n",
        encoding="utf-8",
    )

    assert sorted(openalex_requests(sample), key=str) == sorted(
        [
            ("get", "/works"),
            ("get", "/fulltext"),
            ("post", "/works"),
            ("get", "https://example.org/x.pdf"),
            ("stream", "GET"),
        ],
        key=str,
    )


# --- 全文下載相關的字 -------------------------------------------------------------------------


def _docstring_nodes(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def forbidden_words(path: Path) -> list[str]:
    """識別字與字串常數（docstring 除外）裡出現的全文下載相關字（「行號 字」）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = _docstring_nodes(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        texts: list[str] = []
        if isinstance(node, ast.Name):
            texts.append(node.id)
        elif isinstance(node, ast.Attribute):
            texts.append(node.attr)
        elif isinstance(node, ast.arg) or (isinstance(node, ast.keyword) and node.arg):
            texts.append(node.arg or "")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            texts.append(node.name)
        elif isinstance(node, ast.alias):
            texts.extend(filter(None, [node.name, node.asname]))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            texts.append(getattr(node, "module", None) or "")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            texts.append(node.value)
        for text in texts:
            lowered = text.lower()
            found.extend(
                f"{getattr(node, 'lineno', 0)} {word}"
                for word in FORBIDDEN_WORDS
                if word in lowered
            )
    return found


def test_no_fulltext_download_words_in_code() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        name = path.relative_to(ROOT).as_posix()
        allowed = FORBIDDEN_WORDS_ALLOWED.get(name, frozenset())
        hits = [hit for hit in forbidden_words(path) if hit.split(" ", 1)[1] not in allowed]
        if hits:
            offenders[name] = hits

    assert offenders == {}


def test_forbidden_word_scanner_catches_each_form(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        '"""模組說明可以提到 Unpaywall 與 pdf_url。"""\n'
        "def get_PDF_URL(work):\n"
        '    """函式說明也可以提 best_oa_location。"""\n'
        "    return work['best_oa_location']\n"
        "oa_url = 1\n"
        "x = 'https://content.openalex.org/works/W1.pdf'\n"
        "f(via='EZproxy')\n"
        "client.unpaywall_lookup()\n"
        "def g(libgen_mirror): pass\n"
        "y = 'sci-hub'\n"
        "z = work['openAccessPdf']\n"
        "u = work['landing_page_url']\n"
        "h = {'link-type': 'text-mining'}\n"
        "name = 'paper.PDF'\n"
        "q = Queue.PDF\n",
        encoding="utf-8",
    )

    assert sorted(forbidden_words(sample)) == sorted(
        [
            "2 pdf_url",
            "4 best_oa_location",
            "5 oa_url",
            "6 content.openalex.org",
            "6 .pdf",
            "7 ezproxy",
            "8 unpaywall",
            "9 libgen",
            "10 sci-hub",
            "11 openaccesspdf",
            "12 landing_page_url",
            "13 text-mining",
            "14 .pdf",
        ]
    )
