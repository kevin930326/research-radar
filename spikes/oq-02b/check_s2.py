"""OQ-02b spike：Semantic Scholar 能補回多少 Elsevier 4 本期刊缺的摘要。

一次性驗證用，不屬於正式程式架構。只用 Python 標準函式庫。

用法（在 repo 根目錄）：
    python3 spikes/oq-02b/check_s2.py            # 抓資料＋產生報告
    python3 spikes/oq-02b/check_s2.py --report-only

流程：
  1. 從 OpenAlex 取出 JSIS、DSS、I&M、I&O 在 2016–2025 年「有 DOI、沒摘要」的論文 DOI
     （條件與 OQ-02 相同）
  2. 用 Semantic Scholar 批次 API（每次最多 500 個 DOI）只查 abstract 欄位，
     只記錄「找不找得到、有沒有摘要」，不儲存摘要內容
  3. 對照組：抽 MISQ 100 篇 OpenAlex 有摘要的論文，確認 Semantic Scholar 一般情況下會回摘要
  4. 統計數字寫到 spikes/oq-02b/output/results.json，報告寫到
     docs/research/oq-02b-semantic-scholar.md
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SPIKE_DIR = Path(__file__).resolve().parent
OUTPUT_JSON = SPIKE_DIR / "output" / "results.json"
CONCLUSION_MD = SPIKE_DIR / "conclusion.md"
REPORT_MD = REPO_ROOT / "docs" / "research" / "oq-02b-semantic-scholar.md"

OPENALEX = "https://api.openalex.org"
S2_BATCH = "https://api.semanticscholar.org/graph/v1/paper/batch"
YEAR_FROM, YEAR_TO = 2016, 2025
BASE_FILTER = f"publication_year:{YEAR_FROM}-{YEAR_TO},type:article|review,has_doi:true"

ELSEVIER = {  # OQ-02 用 ISSN 對到的 source ID
    "JSIS": "S6144335",
    "DSS": "S11479521",
    "I&M": "S38883057",
    "I&O": "S35363033",
}
CONTROL = ("MISQ", "S57293258")
CONTROL_SIZE = 100

OPENALEX_INTERVAL = 0.25  # OpenAlex 上限 10 次/秒
S2_INTERVAL_NO_KEY = 3.0  # 無金鑰時共用公用額度，保守放慢
S2_INTERVAL_KEY = 1.1     # 有金鑰時官方上限 1 次/秒
S2_BATCH_SIZE = 500       # 批次 API 單次上限


def env_value(name: str) -> str:
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    return os.environ.get(name, "").strip()


_last: dict[str, float] = {}


def request(kind: str, interval: float, req: urllib.request.Request):
    """固定間隔＋遇到 429/5xx/網路錯誤時退避重試。錯誤訊息不印出網址（可能含金鑰）。"""
    for attempt in range(6):
        wait = interval - (time.monotonic() - _last.get(kind, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last[kind] = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                backoff = int(e.headers.get("Retry-After") or 2 ** (attempt + 2))
                print(f"  {kind} HTTP {e.code}，{backoff} 秒後重試", file=sys.stderr)
                time.sleep(backoff)
                continue
            raise RuntimeError(f"{kind} 回傳 HTTP {e.code}: {e.read()[:300]!r}") from None
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError) as e:
            if attempt < 5:
                print(f"  {kind} 網路錯誤（{type(e).__name__}），重試", file=sys.stderr)
                time.sleep(2 ** (attempt + 2))
                continue
            raise
    raise RuntimeError("重試次數用完")


def openalex_dois(source_id: str, extra: str, api_key: str, limit: int | None = None) -> list[dict]:
    """用 cursor 分頁取出符合條件論文的 DOI、年份、是否開放取用。"""
    out, cursor = [], "*"
    while cursor:
        params = {
            "filter": f"primary_location.source.id:{source_id},{BASE_FILTER}{extra}",
            "select": "doi,publication_year,open_access",
            "per-page": 200, "cursor": cursor, "api_key": api_key,
        }
        url = f"{OPENALEX}/works?" + urllib.parse.urlencode(params, safe=":|,*")
        req = urllib.request.Request(url, headers={"User-Agent": "research-radar-oq02b-spike"})
        data = request("OpenAlex", OPENALEX_INTERVAL, req)
        for w in data.get("results", []):
            doi = (w.get("doi") or "").replace("https://doi.org/", "")
            if doi:
                out.append({"doi": doi, "year": w["publication_year"],
                            "is_oa": bool((w.get("open_access") or {}).get("is_oa"))})
        if limit and len(out) >= limit:
            return out[:limit]
        cursor = data.get("meta", {}).get("next_cursor")
        if not data.get("results"):
            break
    return out


def s2_lookup(dois: list[str], s2_key: str) -> list[str]:
    """回傳每個 DOI 的狀態：not_found / no_abstract / abstract。不保留摘要內容。"""
    headers = {"Content-Type": "application/json", "User-Agent": "research-radar-oq02b-spike"}
    if s2_key:
        headers["x-api-key"] = s2_key
    interval = S2_INTERVAL_KEY if s2_key else S2_INTERVAL_NO_KEY
    status = []
    for i in range(0, len(dois), S2_BATCH_SIZE):
        chunk = dois[i:i + S2_BATCH_SIZE]
        body = json.dumps({"ids": [f"DOI:{d}" for d in chunk]}).encode()
        req = urllib.request.Request(f"{S2_BATCH}?fields=abstract", data=body,
                                     headers=headers, method="POST")
        data = request("SemanticScholar", interval, req)
        if len(data) != len(chunk):
            raise RuntimeError("Semantic Scholar 回傳筆數與送出的不一致")
        for paper in data:
            if paper is None:
                status.append("not_found")
            elif (paper.get("abstract") or "").strip():
                status.append("abstract")
            else:
                status.append("no_abstract")
        print(f"  Semantic Scholar 已查 {min(i + S2_BATCH_SIZE, len(dois))}/{len(dois)}")
    return status


# ---------------------------------------------------------------- 報告

def tally(items: list[dict]) -> dict:
    t = {"total": len(items), "abstract": 0, "no_abstract": 0, "not_found": 0}
    for it in items:
        t[it["s2"]] += 1
    return t


def pct(n: int, d: int) -> str:
    return f"{n / d * 100:.0f}%" if d else "-"


def build_report(res: dict) -> str:
    papers, control = res["papers"], res["control"]
    years = list(range(YEAR_FROM, YEAR_TO + 1))
    L = ["# OQ-02b 驗證：Semantic Scholar 能補回多少 Elsevier 期刊的摘要\n"]
    L.append(f"- 資料抓取日期：{res['date']}")
    L.append(f"- 對象：OQ-02 中 OpenAlex 沒有摘要的 Elsevier 4 本期刊論文（JSIS、DSS、I&M、I&O；"
             f"{YEAR_FROM}–{YEAR_TO}、article/review、有 DOI），共 {len(papers)} 篇")
    L.append("- 方法：用 DOI 呼叫 Semantic Scholar 批次 API，只取 `abstract` 欄位，"
             "記錄「找不到／找到但沒摘要／有摘要」，不儲存摘要內容")
    L.append(f"- Semantic Scholar 金鑰：{'有' if res['s2_key_used'] else '無（使用公用額度）'}")
    L.append("- **不測 Crossref**：Elsevier 沒有把摘要提交給 Crossref，從 Crossref 補不回這 4 本的摘要")
    L.append("- 程式：`spikes/oq-02b/check_s2.py`；統計數字：`spikes/oq-02b/output/results.json`\n")

    L.append("## 1. 各期刊補回比例\n")
    L.append("| 簡稱 | OpenAlex 缺摘要 | S2 有摘要 | 補回比例 | S2 找到但沒摘要 | S2 找不到 |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for short in list(ELSEVIER) + ["合計"]:
        items = papers if short == "合計" else [p for p in papers if p["journal"] == short]
        t = tally(items)
        b = "**" if short == "合計" else ""
        L.append(f"| {b}{short}{b} | {b}{t['total']}{b} | {b}{t['abstract']}{b} | "
                 f"{b}{pct(t['abstract'], t['total'])}{b} | {t['no_abstract']} | {t['not_found']} |")

    L.append("\n## 2. 依開放取用狀態\n")
    L.append("| OpenAlex 開放取用 | 篇數 | S2 有摘要 | 補回比例 |")
    L.append("|---|---:|---:|---:|")
    for label, flag in (("是", True), ("否", False)):
        t = tally([p for p in papers if p["is_oa"] == flag])
        L.append(f"| {label} | {t['total']} | {t['abstract']} | {pct(t['abstract'], t['total'])} |")

    L.append("\n## 3. 各期刊、各年份補回比例（S2 有摘要／OpenAlex 缺摘要）\n")
    L.append("| 簡稱 | " + " | ".join(map(str, years)) + " |")
    L.append("|---|" + "---:|" * len(years))
    for short in ELSEVIER:
        cells = []
        for y in years:
            t = tally([p for p in papers if p["journal"] == short and p["year"] == y])
            cells.append(f"{t['abstract']}/{t['total']}" if t["total"] else "-")
        L.append(f"| {short} | " + " | ".join(cells) + " |")

    L.append("\n## 4. 對照組\n")
    t = tally(control)
    found = t["abstract"] + t["no_abstract"]
    L.append(f"抽 {CONTROL[0]} {t['total']} 篇 OpenAlex **有**摘要的論文丟給 Semantic Scholar："
             f"找到 {found}（{pct(found, t['total'])}），其中有摘要 {t['abstract']}"
             f"（{pct(t['abstract'], t['total'])}）、找到但沒摘要 {t['no_abstract']}、找不到 {t['not_found']}。\n")
    L.append("- 找到率高，代表用 DOI 查詢的方式本身沒問題。")
    L.append("- 但即使 OpenAlex 有摘要，Semantic Scholar API 也只回一部分摘要，"
             "表示它的摘要欄位本來就不完整，**不是 Elsevier 特有的現象**。"
             "原因（出版社授權限制或其他）這次沒有查明；有沒有 API 金鑰會不會影響結果，也沒有驗證。\n")

    if CONCLUSION_MD.exists():
        L.append(CONCLUSION_MD.read_text(encoding="utf-8").strip())
    else:
        L.append("## 5. 結論與對 PRD 範圍的建議\n\n（待補：寫在 spikes/oq-02b/conclusion.md 後用 --report-only 重新產生）")
    return "\n".join(L) + "\n"


def main() -> None:
    if "--report-only" in sys.argv:
        res = json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
    else:
        oa_key = env_value("OPENALEX_API_KEY")
        if not oa_key:
            sys.exit("找不到 OPENALEX_API_KEY：請在 repo 根目錄的 .env 設定")
        s2_key = env_value("SEMANTIC_SCHOLAR_API_KEY")

        print("步驟 1：從 OpenAlex 取出缺摘要論文的 DOI")
        papers = []
        for short, sid in ELSEVIER.items():
            got = openalex_dois(sid, ",has_abstract:false", oa_key)
            print(f"  [{short}] {len(got)} 篇")
            papers += [{**p, "journal": short} for p in got]
        control = openalex_dois(CONTROL[1], ",has_abstract:true", oa_key, limit=CONTROL_SIZE)

        print("步驟 2–3：查 Semantic Scholar")
        for p, s in zip(papers, s2_lookup([p["doi"] for p in papers], s2_key)):
            p["s2"] = s
        for p, s in zip(control, s2_lookup([p["doi"] for p in control], s2_key)):
            p["s2"] = s

        # 只保留統計需要的欄位（DOI 屬公開書目資料，留著方便日後複查）
        res = {"date": date.today().isoformat(), "s2_key_used": bool(s2_key),
               "papers": papers, "control": control}
        OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        t = tally(papers)
        print(f"補回 {t['abstract']}/{t['total']}（{pct(t['abstract'], t['total'])}）；"
              f"對照組 {tally(control)['abstract']}/{len(control)}")

    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text(build_report(res), encoding="utf-8")
    print(f"報告已寫入 {REPORT_MD.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
