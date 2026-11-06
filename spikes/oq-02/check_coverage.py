"""OQ-02 spike：OpenAlex 對 14 本追蹤期刊的摘要覆蓋率。

一次性驗證用，不屬於正式程式架構。只用 Python 標準函式庫。

用法（在 repo 根目錄）：
    python3 spikes/oq-02/check_coverage.py

流程：
  1. 用 ISSN 查 OpenAlex source ID，名稱對不上或查到多筆的標記「待確認」
  2. 每本期刊用 group_by=publication_year 各查一次「全部」與「有摘要」的篇數
     （只拿統計數字，不下載任何論文內容）
  3. 抽樣檢查 has_abstract 篩選器與 abstract_inverted_index 是否一致
  4. 原始數字寫到 spikes/oq-02/output/results.json，
     報告寫到 docs/research/oq-02-abstract-coverage.md
"""

from __future__ import annotations

import json
import os
import re
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
REPORT_MD = REPO_ROOT / "docs" / "research" / "oq-02-abstract-coverage.md"

API = "https://api.openalex.org"
YEAR_FROM, YEAR_TO = 2016, 2025
# 「期刊論文」：OpenAlex 的 article 與 review（不含 editorial、erratum、letter 等）
WORK_TYPES = "article|review"
MIN_INTERVAL = 0.25  # 秒；OpenAlex 上限 10 次/秒，這裡保守用 4 次/秒

# (簡稱, 期刊全名, [ISSN...], 來源)
JOURNALS = [
    ("MISQ", "MIS Quarterly", ["0276-7783", "2162-9730"], "AIS"),
    ("ISR", "Information Systems Research", ["1047-7047", "1526-5536"], "AIS"),
    ("JMIS", "Journal of Management Information Systems", ["0742-1222", "1557-928X"], "AIS"),
    ("JAIS", "Journal of the Association for Information Systems", ["1536-9323"], "AIS"),
    ("EJIS", "European Journal of Information Systems", ["0960-085X", "1476-9344"], "AIS"),
    ("ISJ", "Information Systems Journal", ["1350-1917", "1365-2575"], "AIS"),
    ("JIT", "Journal of Information Technology", ["0268-3962", "1466-4437"], "AIS"),
    ("JSIS", "Journal of Strategic Information Systems", ["0963-8687", "1873-1198"], "AIS"),
    ("DSS", "Decision Support Systems", ["0167-9236", "1873-5797"], "AIS"),
    ("I&M", "Information & Management", ["0378-7206", "1872-7530"], "AIS"),
    ("I&O", "Information and Organization", ["1471-7727", "1873-7919"], "AIS"),
    ("PMJ", "Project Management Journal", ["8756-9728", "1938-9507"], "王老師"),
    ("TEM", "IEEE Transactions on Engineering Management", ["0018-9391", "1558-0040"], "王老師"),
    ("DATABASE", "The DATA BASE for Advances in Information Systems", ["0095-0033", "1532-0936"], "王老師"),
]


# ---------------------------------------------------------------- 基本工具

def load_api_key() -> str:
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("OPENALEX_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    value = os.environ.get("OPENALEX_API_KEY", "").strip()
    if value:
        return value
    sys.exit("找不到 OPENALEX_API_KEY：請在 repo 根目錄的 .env 加上 OPENALEX_API_KEY=你的金鑰")


_last_call = 0.0


def get(path: str, params: dict, api_key: str) -> dict:
    """呼叫 OpenAlex，固定間隔＋遇到 429/5xx 退避重試。"""
    global _last_call
    query = urllib.parse.urlencode({**params, "api_key": api_key}, safe=":|,")
    url = f"{API}{path}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "research-radar-oq02-spike"})
    for attempt in range(5):
        wait = MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                backoff = int(e.headers.get("Retry-After") or 2 ** (attempt + 1))
                print(f"  HTTP {e.code}，{backoff} 秒後重試", file=sys.stderr)
                time.sleep(backoff)
                continue
            # 錯誤訊息裡不要印出含金鑰的網址
            raise RuntimeError(f"OpenAlex {path} 回傳 HTTP {e.code}: {e.read()[:300]!r}") from None
    raise RuntimeError("重試次數用完")


def norm(name: str) -> set[str]:
    stop = {"the", "of", "for", "and", "in", "on", "journal"}
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("&", " and ")).split()
    return {w for w in words if w not in stop}


def name_match(expected: str, actual: str) -> bool:
    a, b = norm(expected), norm(actual)
    return bool(a) and len(a & b) / len(a | b) >= 0.6


# ---------------------------------------------------------------- 步驟

def resolve_sources(api_key: str) -> list[dict]:
    rows = []
    for short, name, issns, origin in JOURNALS:
        data = get("/sources", {
            "filter": "issn:" + "|".join(issns),
            "select": "id,display_name,issn_l,issn,host_organization_name,type,works_count",
        }, api_key)
        results = data.get("results", [])
        row = {"short": short, "expected": name, "issns": issns, "origin": origin,
               "candidates": results, "source": None, "status": ""}
        if not results:
            row["status"] = "找不到"
        elif len(results) > 1:
            row["status"] = "多筆候選"
            # 先挑名稱對得上、且論文數最多的，當作暫定，但仍標記待確認
            ok = [r for r in results if name_match(name, r["display_name"])]
            if ok:
                row["source"] = max(ok, key=lambda r: r.get("works_count") or 0)
        else:
            row["source"] = results[0]
            row["status"] = "OK" if name_match(name, results[0]["display_name"]) else "名稱不符"
        shown = row["source"]["display_name"] if row["source"] else "-"
        print(f"[{short}] {row['status']}: {shown}")
        rows.append(row)
    return rows


def year_counts(source_id: str, extra_filter: str, api_key: str) -> dict[int, int]:
    sid = source_id.rsplit("/", 1)[-1]
    flt = (f"primary_location.source.id:{sid},"
           f"publication_year:{YEAR_FROM}-{YEAR_TO},type:{WORK_TYPES}{extra_filter}")
    data = get("/works", {"filter": flt, "group_by": "publication_year"}, api_key)
    return {int(g["key"]): g["count"] for g in data.get("group_by", [])}


def sanity_check(source_id: str, api_key: str) -> dict:
    """抽樣確認 has_abstract 篩選器 = abstract_inverted_index 不為空。"""
    sid = source_id.rsplit("/", 1)[-1]
    base = f"primary_location.source.id:{sid},publication_year:{YEAR_FROM}-{YEAR_TO},type:{WORK_TYPES}"
    out = {}
    for flag in ("true", "false"):
        data = get("/works", {"filter": f"{base},has_abstract:{flag}",
                              "select": "id,abstract_inverted_index", "per-page": 25}, api_key)
        res = data.get("results", [])
        out[flag] = {"sampled": len(res),
                     "non_empty": sum(1 for r in res if r.get("abstract_inverted_index"))}
    return out


# ---------------------------------------------------------------- 報告

def pct(n: int, d: int) -> str:
    return f"{n / d * 100:.0f}%" if d else "-"


def build_report(rows: list[dict]) -> str:
    years = list(range(YEAR_FROM, YEAR_TO + 1))
    L = []
    L.append("# OQ-02 驗證：OpenAlex 對 14 本追蹤期刊的摘要覆蓋率\n")
    L.append(f"- 資料抓取日期：{date.today().isoformat()}（OpenAlex 資料會更新，數字只代表當天）")
    L.append(f"- 範圍：{YEAR_FROM}–{YEAR_TO} 年、OpenAlex type 為 `article` 或 `review`、"
             "且主要出處（primary_location）是該期刊的論文")
    L.append("- 「有摘要」＝ `abstract_inverted_index` 不為空（用 OpenAlex 的 `has_abstract` 篩選器統計，"
             "並抽樣比對欄位內容）")
    L.append("- 程式：`spikes/oq-02/check_coverage.py`；原始數字：`spikes/oq-02/output/results.json`\n")

    L.append("## 1. 期刊對應（ISSN → OpenAlex source）\n")
    L.append("| 簡稱 | 期刊（預期） | ISSN | OpenAlex 名稱 | source ID | 出版者 | 狀態 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in rows:
        s = r["source"] or {}
        L.append(f"| {r['short']} | {r['expected']} | {', '.join(r['issns'])} | "
                 f"{s.get('display_name', '-')} | {(s.get('id') or '-').rsplit('/', 1)[-1]} | "
                 f"{s.get('host_organization_name') or '-'} | {r['status']} |")
    flagged = [r for r in rows if r["status"] != "OK"]
    if flagged:
        L.append("\n**需要確認的期刊：**\n")
        for r in flagged:
            L.append(f"- {r['short']}（{r['status']}）候選：")
            for c in r["candidates"] or []:
                L.append(f"  - {c['display_name']}｜{c['id'].rsplit('/', 1)[-1]}｜"
                         f"ISSN {', '.join(c.get('issn') or [])}｜works {c.get('works_count')}")
            if not r["candidates"]:
                L.append("  - （OpenAlex 沒有任何符合的 source）")
    else:
        L.append("\n14 本都以 ISSN 找到唯一一筆、且名稱相符的 source。")

    L.append("\n## 2. 總覽（10 年合計）\n")
    L.append("| 簡稱 | 論文數 | 有摘要 | 覆蓋率 | 最低年份覆蓋率 |")
    L.append("|---|---:|---:|---:|---:|")
    tot_n = tot_a = 0
    for r in rows:
        if not r.get("counts"):
            L.append(f"| {r['short']} | - | - | - | - |")
            continue
        n = sum(r["counts"].values()); a = sum(r["with_abstract"].values())
        tot_n += n; tot_a += a
        yearly = [(y, r["with_abstract"].get(y, 0), r["counts"].get(y, 0)) for y in years if r["counts"].get(y)]
        worst = min(yearly, key=lambda t: t[1] / t[2]) if yearly else None
        worst_s = f"{pct(worst[1], worst[2])}（{worst[0]}）" if worst else "-"
        L.append(f"| {r['short']} | {n} | {a} | {pct(a, n)} | {worst_s} |")
    L.append(f"| **合計** | **{tot_n}** | **{tot_a}** | **{pct(tot_a, tot_n)}** | |")

    L.append("\n## 3. 每本期刊、每一年的論文數\n")
    L.append("| 簡稱 | " + " | ".join(map(str, years)) + " |")
    L.append("|---|" + "---:|" * len(years))
    for r in rows:
        c = r.get("counts") or {}
        L.append(f"| {r['short']} | " + " | ".join(str(c.get(y, 0)) for y in years) + " |")

    L.append("\n## 4. 每本期刊、每一年的摘要覆蓋率\n")
    L.append("| 簡稱 | " + " | ".join(map(str, years)) + " |")
    L.append("|---|" + "---:|" * len(years))
    for r in rows:
        c = r.get("counts") or {}; a = r.get("with_abstract") or {}
        L.append(f"| {r['short']} | " + " | ".join(pct(a.get(y, 0), c.get(y, 0)) for y in years) + " |")

    L.append("\n## 5. 抽樣檢查（has_abstract 篩選器 vs. 欄位內容）\n")
    L.append("每本期刊各抽 25 篇 `has_abstract:true` 與 25 篇 `has_abstract:false`，"
             "數 `abstract_inverted_index` 實際不為空的篇數。\n")
    L.append("| 簡稱 | true 組：不為空／抽樣 | false 組：不為空／抽樣 | 一致 |")
    L.append("|---|---:|---:|---|")
    for r in rows:
        sc = r.get("sanity")
        if not sc:
            L.append(f"| {r['short']} | - | - | - |"); continue
        ok = sc["true"]["non_empty"] == sc["true"]["sampled"] and sc["false"]["non_empty"] == 0
        L.append(f"| {r['short']} | {sc['true']['non_empty']}/{sc['true']['sampled']} | "
                 f"{sc['false']['non_empty']}/{sc['false']['sampled']} | {'是' if ok else '**否**'} |")

    L.append("")
    if CONCLUSION_MD.exists():
        L.append(CONCLUSION_MD.read_text(encoding="utf-8").strip())
    else:
        L.append("## 6. 結論與對 PRD 範圍的建議\n\n（待補：寫在 spikes/oq-02/conclusion.md 後重新產生報告）")
    return "\n".join(L) + "\n"


def main() -> None:
    if "--report-only" in sys.argv:
        rows = json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
        for r in rows:  # JSON 的 key 是字串，轉回年份
            for k in ("counts", "with_abstract"):
                if r.get(k):
                    r[k] = {int(y): v for y, v in r[k].items()}
    else:
        api_key = load_api_key()
        print("步驟 1：用 ISSN 查 source ID")
        rows = resolve_sources(api_key)
        print("步驟 2–3：逐本統計")
        for r in rows:
            if not r["source"]:
                continue
            sid = r["source"]["id"]
            r["counts"] = year_counts(sid, "", api_key)
            r["with_abstract"] = year_counts(sid, ",has_abstract:true", api_key)
            r["sanity"] = sanity_check(sid, api_key)
            n, a = sum(r["counts"].values()), sum(r["with_abstract"].values())
            print(f"  [{r['short']}] {n} 篇，有摘要 {a}（{pct(a, n)}）")
        OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text(build_report(rows), encoding="utf-8")
    print(f"報告已寫入 {REPORT_MD.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
