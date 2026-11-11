"""OQ-10 spike：資料擴充評估（OpenAlex 統計部分）。

一次性驗證用，不屬於正式程式架構。只用 Python 標準函式庫。
試擷取（Haiku）在另一支程式 extract_trial.py；報告由本程式組合產生。

用法（在 repo 根目錄）：
    python3 spikes/oq-10/check_expansion.py              # 抓資料＋產生報告（約 15–25 分鐘）
    python3 spikes/oq-10/check_expansion.py --report-only

量的東西（只拿統計數字，不下載、不儲存任何摘要或全文）：
  1. 14 本追蹤期刊 2016–2025：OA 類型、CC 授權、OpenAlex 已解析全文（TEI）比例
  2. 引用擴展：這些論文引用的「14 本以外」論文中，被引用 ≥5／≥10／≥20 次的篇數與摘要比例
  3. 拉長年份：14 本 2006–2015 的篇數與摘要比例
  4. 13 本候選期刊 2016–2025：篇數、摘要、OA／CC
  5. 研討會：ICIS、PACIS、ECIS、AMCIS、HICSS 2016–2025

輸出：spikes/oq-10/output/results.json（只有統計數字）與 docs/research/oq-10-data-expansion.md
中間資料（被引用次數表等）放在 spikes/oq-10/.cache/（不進 repo）。
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SPIKE_DIR = Path(__file__).resolve().parent
OUTPUT_JSON = SPIKE_DIR / "output" / "results.json"
EXTRACTION_JSON = SPIKE_DIR / "output" / "extraction.json"
CACHE_DIR = SPIKE_DIR / ".cache"
CONCLUSION_MD = SPIKE_DIR / "conclusion.md"
REPORT_MD = REPO_ROOT / "docs" / "research" / "oq-10-data-expansion.md"

API = "https://api.openalex.org"
RECENT = (2016, 2025)
EARLY = (2006, 2015)
JOURNAL_TYPES = "article|review"                  # 與 OQ-02 相同
CONF_TYPES = "article|review|conference-paper"     # 研討會論文在 OpenAlex 多標成 conference-paper
MIN_INTERVAL = 0.25  # 秒；OpenAlex 上限 10 次/秒，保守用 4 次/秒

# PRD 名詞表：OA 全文＝任何 CC 變體（含 CC0）；示範站可用論文＝CC BY、CC BY-SA、CC0
# OpenAlex 的 public-domain 標籤顯示名稱為「public domain (CC0)」
CC_ANY = {"cc-by", "cc-by-sa", "cc-by-nc", "cc-by-nd", "cc-by-nc-sa", "cc-by-nc-nd", "public-domain"}
CC_DEMO = {"cc-by", "cc-by-sa", "public-domain"}
OA_STATUSES = ["gold", "diamond", "hybrid", "green", "bronze", "closed"]

# OQ-02 已用 ISSN 驗證過的 source ID
JOURNALS14 = [
    ("MISQ", "S57293258"), ("ISR", "S202812398"), ("JMIS", "S9954729"), ("JAIS", "S30879505"),
    ("EJIS", "S116712389"), ("ISJ", "S92260147"), ("JIT", "S135086714"), ("JSIS", "S6144335"),
    ("DSS", "S11479521"), ("I&M", "S38883057"), ("I&O", "S35363033"), ("PMJ", "S196205230"),
    ("TEM", "S154533451"), ("DATABASE", "S4210234096"),
]
ELSEVIER = {"JSIS", "DSS", "I&M", "I&O"}
IDS14 = {sid for _, sid in JOURNALS14}

# 候選期刊：(簡稱, 全名, [ISSN...], 老師領域)
CANDIDATES = [
    ("IJEC", "International Journal of Electronic Commerce", ["1086-4415", "1557-9301"], "電子商務"),
    ("ECRA", "Electronic Commerce Research and Applications", ["1567-4223", "1873-7846"], "電子商務"),
    ("EM", "Electronic Markets", ["1019-6781", "1422-8890"], "電子商務"),
    ("ISF", "Information Systems Frontiers", ["1387-3326", "1572-9419"], "資管綜合"),
    ("IJIM", "International Journal of Information Management", ["0268-4012", "1873-4707"], "資管綜合"),
    ("INTR", "Internet Research", ["1066-2243", "2054-5657"], "電子商務"),
    ("CAIS", "Communications of the Association for Information Systems", ["1529-3181"], "資管綜合（AIS）"),
    ("PAJAIS", "Pacific Asia Journal of the Association for Information Systems", ["1943-7536", "1943-7544"], "資管綜合（AIS）"),
    ("JOM", "Journal of Operations Management", ["0272-6963", "1873-1317"], "供應鏈／作業管理"),
    ("IJPE", "International Journal of Production Economics", ["0925-5273", "1873-7579"], "供應鏈／作業管理"),
    ("SCM", "Supply Chain Management: An International Journal", ["1359-8546", "1758-6852"], "供應鏈"),
    ("IEP", "Information Economics and Policy", ["0167-6245", "1873-5975"], "資訊經濟"),
    ("IJPM", "International Journal of Project Management", ["0263-7863", "1873-4634"], "IT 專案管理"),
]

# 研討會在 OpenAlex 的專屬 source（2026-11-10 以名稱搜尋確認）
HICSS_SOURCE = "S4210195248"
AIS_CONF_SOURCES = {"ICIS": "S4306419566", "PACIS": "S4306420657",
                    "ECIS": "S4306418324", "AMCIS": "S4306417665"}
AIS_CONFS = ["ICIS", "PACIS", "ECIS", "AMCIS"]


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
STATS = {"requests": 0, "cost_usd": 0.0}
API_KEY = ""


def get(path: str, params: dict) -> dict:
    """呼叫 OpenAlex，固定間隔＋遇到 429/5xx 退避重試；記錄請求數與花費。"""
    global _last_call
    query = urllib.parse.urlencode({**params, "api_key": API_KEY}, safe=":|,*")
    url = f"{API}{path}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "research-radar-oq10-spike"})
    for attempt in range(6):
        wait = MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                STATS["requests"] += 1
                try:
                    STATS["cost_usd"] += float(resp.headers.get("x-ratelimit-cost-usd") or 0)
                except ValueError:
                    pass
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                backoff = int(e.headers.get("Retry-After") or 2 ** (attempt + 1))
                print(f"  HTTP {e.code}，{backoff} 秒後重試", file=sys.stderr)
                time.sleep(backoff)
                continue
            # 錯誤訊息裡不要印出含金鑰的網址
            raise RuntimeError(f"OpenAlex {path} 回傳 HTTP {e.code}: {e.read()[:300]!r}") from None
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < 5:
                print(f"  網路錯誤（{type(e).__name__}），{2 ** (attempt + 1)} 秒後重試", file=sys.stderr)
                time.sleep(2 ** (attempt + 1))
                continue
            raise
    raise RuntimeError("重試次數用完")


def group(flt: str, by: str) -> tuple[int, dict[str, int]]:
    """回傳（符合條件的總數, {分組 key: 篇數}）。key 去掉網址前綴。"""
    data = get("/works", {"filter": flt, "group_by": by})
    out = {}
    for g in data.get("group_by", []):
        out[str(g["key"]).rsplit("/", 1)[-1]] = g["count"]
    return data["meta"]["count"], out


def paginate(flt: str, select: str):
    cursor = "*"
    while cursor:
        data = get("/works", {"filter": flt, "select": select, "per-page": 200, "cursor": cursor})
        yield from data.get("results", [])
        cursor = data["meta"].get("next_cursor")


def sid_filter(sid: str) -> str:
    return f"primary_location.source.id:{sid}"


def journal_filter(sid: str, years: tuple[int, int]) -> str:
    return f"{sid_filter(sid)},publication_year:{years[0]}-{years[1]},type:{JOURNAL_TYPES},has_doi:true"


def norm_name(name: str) -> set[str]:
    stop = {"the", "of", "for", "and", "in", "on", "journal", "an"}
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("&", " and ")).split()
    return {w for w in words if w not in stop}


def name_match(expected: str, actual: str) -> bool:
    a, b = norm_name(expected), norm_name(actual)
    return bool(a) and len(a & b) / len(a | b) >= 0.6


def lic_sum(lic: dict[str, int], allowed: set[str]) -> int:
    return sum(v for k, v in lic.items() if k in allowed)


# ---------------------------------------------------------------- 量測單元

def profile(flt: str) -> dict:
    """一個母體的規模、摘要、OA 類型、授權、TEI（4 次請求）。"""
    total, oa = group(flt, "open_access.oa_status")
    _, abs_ = group(flt, "has_abstract")
    _, lic = group(flt, "best_oa_location.license")
    tei_total, tei_lic = group(f"{flt},has_content.grobid_xml:true", "best_oa_location.license")
    return {"total": total, "abstract": abs_.get("true", 0), "oa_status": oa, "license": lic,
            "tei_total": tei_total, "tei_license": tei_lic}


def by_year(flt: str) -> dict:
    _, n = group(flt, "publication_year")
    _, a = group(f"{flt},has_abstract:true", "publication_year")
    return {"counts": n, "with_abstract": a}


def journal_extras(flt: str) -> dict:
    """14 本專用：各 OA 類型內的授權分布，以及「沒摘要但有 CC TEI」的篇數。"""
    per_status = {}
    for st in ["gold", "diamond", "hybrid", "green", "bronze"]:
        _, lic = group(f"{flt},open_access.oa_status:{st}", "best_oa_location.license")
        per_status[st] = lic
    noabs_n, noabs_oa = group(f"{flt},has_abstract:false", "open_access.oa_status")
    _, noabs_lic = group(f"{flt},has_abstract:false", "best_oa_location.license")
    _, noabs_tei_lic = group(f"{flt},has_abstract:false,has_content.grobid_xml:true", "best_oa_location.license")
    return {"license_by_status": per_status, "noabs_total": noabs_n, "noabs_oa_status": noabs_oa,
            "noabs_license": noabs_lic, "noabs_tei_license": noabs_tei_lic}


# ---------------------------------------------------------------- 步驟

def step1_current() -> list[dict]:
    rows = []
    for short, sid in JOURNALS14:
        flt = journal_filter(sid, RECENT)
        r = {"short": short, "source_id": sid, **profile(flt), **journal_extras(flt)}
        print(f"  [{short}] {r['total']} 篇，CC {lic_sum(r['license'], CC_ANY)}，TEI {r['tei_total']}")
        rows.append(r)
    return rows


def step3_early() -> list[dict]:
    rows = []
    for short, sid in JOURNALS14:
        flt = journal_filter(sid, EARLY)
        r = {"short": short, "source_id": sid, **profile(flt), "years": by_year(flt)}
        print(f"  [{short}] {r['total']} 篇，有摘要 {r['abstract']}")
        rows.append(r)
    return rows


def step4_candidates() -> list[dict]:
    rows = []
    for short, name, issns, field in CANDIDATES:
        data = get("/sources", {"filter": "issn:" + "|".join(issns),
                                "select": "id,display_name,issn,host_organization_name,type,works_count"})
        res = data.get("results", [])
        row = {"short": short, "expected": name, "issns": issns, "field": field,
               "candidates": res, "source": None, "status": ""}
        ok = [s for s in res if name_match(name, s["display_name"])]
        if not res:
            row["status"] = "找不到"
        elif len(res) == 1:
            row["source"] = res[0]
            row["status"] = "OK" if ok else "名稱不符"
        else:
            row["status"] = "多筆候選"
            if ok:
                row["source"] = max(ok, key=lambda s: s.get("works_count") or 0)
        if row["source"]:
            sid = row["source"]["id"].rsplit("/", 1)[-1]
            flt = journal_filter(sid, RECENT)
            row.update(profile(flt))
            row["years"] = by_year(flt)
            # 不要求 DOI 時的篇數（看有沒有像 JAIS 那樣的錯掛雜訊）
            row["total_without_doi_rule"], _ = group(
                f"{sid_filter(sid)},publication_year:{RECENT[0]}-{RECENT[1]},type:{JOURNAL_TYPES}", "has_doi")
            print(f"  [{short}] {row['status']} {row['source']['display_name']}：{row['total']} 篇，"
                  f"有摘要 {row['abstract']}")
        else:
            print(f"  [{short}] {row['status']}")
        rows.append(row)
    return rows


AIS_URL_RE = re.compile(r"(?:aisel\.aisnet\.org/|context=)(icis|pacis|ecis|amcis)(\d{4})", re.I)
AIS_RAW_RE = re.compile(r"\b(ICIS|PACIS|ECIS|AMCIS)\b", re.I)


def classify_ais(rec: dict) -> str:
    loc = rec.get("primary_location") or {}
    for text in (loc.get("landing_page_url") or "", loc.get("pdf_url") or ""):
        m = AIS_URL_RE.search(text)
        if m:
            return m.group(1).upper()
    m = AIS_RAW_RE.search(loc.get("raw_source_name") or "")
    if m:
        return m.group(1).upper()
    return "OTHER"


def other_label(rec: dict) -> str:
    loc = rec.get("primary_location") or {}
    url = loc.get("landing_page_url") or ""
    m = re.search(r"aisel\.aisnet\.org/(?:cgi/viewcontent\.cgi\?.*context=)?([a-z_]+?)(?:\d{4})?(?:[/_]|$)", url, re.I)
    return (m.group(1).lower() if m else "（非 AISeL 網址）")


def step5_conferences() -> dict:
    out = {}
    # HICSS
    flt = f"{sid_filter(HICSS_SOURCE)},publication_year:{RECENT[0]}-{RECENT[1]},type:{CONF_TYPES}"
    out["HICSS"] = {**profile(flt), "years": by_year(flt)}
    _, out["HICSS"]["doi"] = group(flt, "has_doi")
    print(f"  [HICSS] {out['HICSS']['total']} 篇，有摘要 {out['HICSS']['abstract']}")

    # AIS 研討會的專屬 source（大多是 2016 年、來自 DBLP）
    out["ais_dedicated"] = {}
    for conf, sid in AIS_CONF_SOURCES.items():
        flt = f"{sid_filter(sid)},publication_year:{RECENT[0]}-{RECENT[1]},type:{CONF_TYPES}"
        out["ais_dedicated"][conf] = {**profile(flt), "years": by_year(flt)}
        print(f"  [{conf} 專屬 source] {out['ais_dedicated'][conf]['total']} 篇")

    # 錯掛到 JAIS、沒有 DOI 的 AISeL 條目：逐筆依網址分類
    base = f"{sid_filter('S30879505')},publication_year:{RECENT[0]}-{RECENT[1]},has_doi:false"
    with_abs = {r["id"] for r in paginate(f"{base},has_abstract:true", "id")}
    agg = defaultdict(lambda: {"total": 0, "abstract": 0, "is_oa": 0, "cc_any": 0,
                               "years": Counter(), "years_abs": Counter(), "types": Counter()})
    others = Counter()
    for rec in paginate(base, "id,publication_year,type,primary_location,open_access,best_oa_location"):
        conf = classify_ais(rec)
        if conf == "OTHER":
            others[other_label(rec)] += 1
        a = agg[conf]
        a["total"] += 1
        a["types"][rec.get("type")] += 1
        y = rec.get("publication_year")
        a["years"][y] += 1
        if rec["id"] in with_abs:
            a["abstract"] += 1
            a["years_abs"][y] += 1
        if (rec.get("open_access") or {}).get("is_oa"):
            a["is_oa"] += 1
        if ((rec.get("best_oa_location") or {}).get("license") or "") in CC_ANY:
            a["cc_any"] += 1
    out["ais_in_jais"] = {k: {**v, "years": dict(v["years"]), "years_abs": dict(v["years_abs"]),
                              "types": dict(v["types"])} for k, v in agg.items()}
    out["ais_in_jais_other_top"] = others.most_common(15)
    print("  [JAIS 錯掛] " + "、".join(f"{k} {v['total']}" for k, v in sorted(agg.items())))
    return out


def step2_citations() -> dict:
    """引用擴展。被引用次數表放 .cache，不進 repo。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    counts_path = CACHE_DIR / "ref_counts.json"
    meta_path = CACHE_DIR / "ref_meta.json"
    coverage = {}
    if counts_path.exists():
        cached = json.loads(counts_path.read_text(encoding="utf-8"))
        ref_counts = Counter(cached["counts"])
        coverage, totals = cached["coverage"], cached["totals"]
        print("  （使用 .cache 的被引用次數表）")
    else:
        ref_counts = Counter()
        totals = {"citing": 0, "ref_links": 0}
        for short, sid in JOURNALS14:
            n = with_refs = 0
            for rec in paginate(journal_filter(sid, RECENT), "id,referenced_works"):
                refs = set(rec.get("referenced_works") or [])
                n += 1
                if refs:
                    with_refs += 1
                totals["ref_links"] += len(refs)
                ref_counts.update(r.rsplit("/", 1)[-1] for r in refs)
            totals["citing"] += n
            coverage[short] = {"works": n, "with_refs": with_refs}
            print(f"  [{short}] {n} 篇，{with_refs} 篇有參考文獻資料")
        totals["unique_refs"] = len(ref_counts)
        keep = {k: v for k, v in ref_counts.items() if v >= 5}
        counts_path.write_text(json.dumps({"counts": keep, "coverage": coverage, "totals": totals}),
                               encoding="utf-8")
        ref_counts = Counter(keep)

    # 被引用 ≥5 次的作品：查 metadata 與有沒有摘要（只拿 ID，不拿摘要內容）
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    todo = [w for w, c in ref_counts.items() if c >= 5 and w not in meta]
    print(f"  被引用 ≥5 次：{sum(1 for c in ref_counts.values() if c >= 5)} 篇，需查 metadata {len(todo)} 篇")
    sel = "id,type,publication_year,primary_location,open_access,best_oa_location,has_content"
    for i in range(0, len(todo), 100):
        batch = todo[i:i + 100]
        flt = "openalex:" + "|".join(batch)
        got = get("/works", {"filter": flt, "select": sel, "per-page": 100}).get("results", [])
        has_abs = {r["id"].rsplit("/", 1)[-1] for r in
                   get("/works", {"filter": f"{flt},has_abstract:true", "select": "id",
                                  "per-page": 100}).get("results", [])}
        found = set()
        for r in got:
            wid = r["id"].rsplit("/", 1)[-1]
            found.add(wid)
            src = ((r.get("primary_location") or {}).get("source") or {})
            meta[wid] = {
                "type": r.get("type"), "year": r.get("publication_year"),
                "source_id": (src.get("id") or "").rsplit("/", 1)[-1] or None,
                "source_name": src.get("display_name"), "source_type": src.get("type"),
                "oa_status": (r.get("open_access") or {}).get("oa_status"),
                "license": ((r.get("best_oa_location") or {}).get("license")),
                "tei": bool((r.get("has_content") or {}).get("grobid_xml")),
                "abstract": wid in has_abs,
            }
        for wid in batch:  # 被合併或刪除的 ID：查不到
            if wid not in found:
                meta[wid] = {"missing": True}
        if (i // 100) % 10 == 0:
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            print(f"    metadata {min(i + 100, len(todo))}/{len(todo)}")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    def summarize(ids: list[str]) -> dict:
        ms = [meta[w] for w in ids]
        n = len(ms)
        dec = Counter()
        for m in ms:
            y = m.get("year")
            dec["<1980" if not y or y < 1980 else f"{y // 10 * 10}s"] += 1
        return {
            "n": n,
            "abstract": sum(m["abstract"] for m in ms),
            "types": dict(Counter(m["type"] for m in ms).most_common()),
            "oa": sum(1 for m in ms if m.get("oa_status") not in (None, "closed")),
            "cc_any": sum(1 for m in ms if (m.get("license") or "") in CC_ANY),
            "cc_demo": sum(1 for m in ms if (m.get("license") or "") in CC_DEMO),
            "tei_cc": sum(1 for m in ms if m.get("tei") and (m.get("license") or "") in CC_ANY),
            "decades": dict(sorted(dec.items())),
            "abstract_article": sum(m["abstract"] for m in ms if m["type"] in ("article", "review")),
            "n_article": sum(1 for m in ms if m["type"] in ("article", "review")),
            "top_sources": Counter(m.get("source_name") or "（無來源）" for m in ms).most_common(20),
        }

    result = {"coverage": coverage, "totals": totals, "thresholds": {}}
    valid = {w: c for w, c in ref_counts.items() if c >= 5 and not meta.get(w, {}).get("missing")}
    result["missing_ids"] = sum(1 for w, c in ref_counts.items() if c >= 5 and meta.get(w, {}).get("missing"))
    for t in (5, 10, 20):
        ext = [w for w, c in valid.items() if c >= t and meta[w]["source_id"] not in IDS14]
        in14_old = [w for w, c in valid.items() if c >= t and meta[w]["source_id"] in IDS14
                    and (meta[w]["year"] or 0) < RECENT[0]]
        in14_recent = [w for w, c in valid.items() if c >= t and meta[w]["source_id"] in IDS14
                       and (meta[w]["year"] or 0) >= RECENT[0]]
        result["thresholds"][str(t)] = {
            "external": summarize(ext),
            "in14_before_2016": summarize(in14_old),
            "in14_since_2016": len(in14_recent),
        }
        print(f"  ≥{t}：14 本以外 {len(ext)} 篇；14 本 2016 年前 {len(in14_old)} 篇")
    return result


# ---------------------------------------------------------------- 報告

def pct(n: float, d: float, digits: int = 0) -> str:
    return f"{n / d * 100:.{digits}f}%" if d else "-"


def fmt(n: int) -> str:
    return f"{n:,}"


def cc_counts(p: dict) -> tuple[int, int, int, int]:
    """(CC 任何變體, 示範站可用, TEI∧CC 任何, TEI∧示範站可用)"""
    return (lic_sum(p["license"], CC_ANY), lic_sum(p["license"], CC_DEMO),
            lic_sum(p["tei_license"], CC_ANY), lic_sum(p["tei_license"], CC_DEMO))


def sum_profiles(ps: list[dict]) -> dict:
    out = {"total": 0, "abstract": 0, "oa_status": Counter(), "license": Counter(),
           "tei_total": 0, "tei_license": Counter()}
    for p in ps:
        out["total"] += p["total"]
        out["abstract"] += p["abstract"]
        out["tei_total"] += p["tei_total"]
        out["oa_status"].update(p["oa_status"])
        out["license"].update(p["license"])
        out["tei_license"].update(p["tei_license"])
    return out


def load_extraction() -> dict | None:
    if EXTRACTION_JSON.exists():
        return json.loads(EXTRACTION_JSON.read_text(encoding="utf-8"))
    return None


def build_report(res: dict) -> str:
    L: list[str] = []
    ext = load_extraction()
    cur = res["current"]
    cur_all = sum_profiles(cur)
    L.append("# OQ-10 資料擴充評估：各擴充選項的規模、摘要、CC 全文與試擷取\n")
    L.append(f"- 資料抓取日期：{res['fetched']}（OpenAlex 資料會更新，數字只代表當天）")
    L.append("- 範圍與篩選條件沿用 OQ-02：期刊論文＝OpenAlex type 為 `article` 或 `review`、有 DOI、"
             "主要出處（primary_location）是該期刊；研討會另加 `conference-paper` 類型")
    L.append("- **CC 授權**依 PRD 名詞表：「OA 全文」＝OpenAlex 最佳開放版本（`best_oa_location`）的授權是任何 CC 變體"
             "（CC BY、BY-SA、BY-NC、BY-ND、BY-NC-SA、BY-NC-ND、CC0）；「示範站可用」＝CC BY、CC BY-SA、CC0。"
             "OpenAlex 把 CC0 標成 `public-domain`（顯示名稱「public domain (CC0)」）")
    L.append("- **TEI**＝OpenAlex 已解析的全文（`has_content.grobid_xml:true`）；「TEI＋CC」才是 PRD 規則下能自動抓、"
             "能送雲端的全文")
    L.append("- 程式：`spikes/oq-10/check_expansion.py`（OpenAlex 統計）、`spikes/oq-10/extract_trial.py`（試擷取）；"
             "原始數字：`spikes/oq-10/output/results.json`、`spikes/oq-10/output/extraction.json`、`spikes/oq-10/output/extraction_compare.json`")
    L.append(f"- OpenAlex 用量：{fmt(res['api']['requests'])} 次請求，約 {res['api']['cost_usd']:.4f} 美元"
             "（OpenAlex 每日免費額度 1 美元）\n")

    # ---------- 總表
    L.append("## 1. 總表：各擴充選項一覽\n")
    L.append("「CC 全文」欄是 TEI＋CC（可自動抓、可送雲端的全文篇數）。費用估算見第 8 節。\n")
    L.append("| 選項 | 篇數 | 有摘要 | 摘要比例 | OA 比例 | CC 比例 | CC 全文（TEI＋CC） | 示範站可用（CC BY/BY-SA/CC0） |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")

    def total_row(label: str, p: dict) -> None:
        cc, demo, tei_cc, _ = cc_counts(p)
        oa = p["total"] - p["oa_status"].get("closed", 0)
        L.append(f"| {label} | {fmt(p['total'])} | {fmt(p['abstract'])} | {pct(p['abstract'], p['total'])} | "
                 f"{pct(oa, p['total'])} | {pct(cc, p['total'])} | {fmt(tei_cc)}（{pct(tei_cc, p['total'])}） | "
                 f"{fmt(demo)}（{pct(demo, p['total'])}） |")

    total_row("現況：14 本 2016–2025", cur_all)
    cit = res["citations"]["thresholds"]
    for t in ("5", "10", "20"):
        e = cit[t]["external"]
        L.append(f"| ① 引用擴展：14 本以外、被引用 ≥{t} 次 | {fmt(e['n'])} | {fmt(e['abstract'])} | "
                 f"{pct(e['abstract'], e['n'])} | {pct(e['oa'], e['n'])} | {pct(e['cc_any'], e['n'])} | "
                 f"{fmt(e['tei_cc'])}（{pct(e['tei_cc'], e['n'])}） | {fmt(e['cc_demo'])}（{pct(e['cc_demo'], e['n'])}） |")
    cands = [c for c in res["candidates"] if c.get("source")]
    total_row(f"② 候選期刊 {len(cands)} 本 2016–2025（合計）", sum_profiles(cands))
    total_row("③ 拉長年份：14 本 2006–2015", sum_profiles(res["early"]))
    conf = res["conferences"]
    total_row("④ HICSS 2016–2025（OpenAlex）", conf["HICSS"])
    ais_total = sum(v["total"] for k, v in conf["ais_in_jais"].items() if k != "OTHER") + \
        sum(v["total"] for v in conf["ais_dedicated"].values())
    ais_abs = sum(v["abstract"] for k, v in conf["ais_in_jais"].items() if k != "OTHER") + \
        sum(v["abstract"] for v in conf["ais_dedicated"].values())
    L.append(f"| ④ ICIS／PACIS／ECIS／AMCIS 2016–2025（OpenAlex 找得到的部分，見第 6 節） | {fmt(ais_total)} | "
             f"{fmt(ais_abs)} | {pct(ais_abs, ais_total)} | 見第 6 節 | 見第 6 節 | 見第 6 節 | 見第 6 節 |")

    # ---------- 1. 現況
    L.append("\n## 2. 現有 14 本期刊（2016–2025）：OA 類型、CC 授權、TEI\n")
    L.append("### 2.1 OA 類型\n")
    L.append("OA 類型是 OpenAlex 的 `open_access.oa_status`：gold＝OA 期刊、diamond＝OA 期刊且不收作者費、"
             "hybrid＝訂閱期刊裡付費開放的單篇、green＝只有典藏庫（預印本、作者稿）的免費版本、"
             "bronze＝出版社網站可免費讀但沒有授權。\n")
    L.append("| 簡稱 | 論文數 | OA 比例 | gold | diamond | hybrid | green | bronze | closed |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in cur + [dict(short="**合計**", **cur_all)]:
        oa = r["oa_status"]
        n = r["total"]
        L.append(f"| {r['short']} | {fmt(n)} | {pct(n - oa.get('closed', 0), n)} | " +
                 " | ".join(f"{oa.get(s, 0)}（{pct(oa.get(s, 0), n)}）" for s in OA_STATUSES[:-1]) +
                 f" | {oa.get('closed', 0)}（{pct(oa.get('closed', 0), n)}） |")

    L.append("\n### 2.2 CC 授權與 TEI\n")
    L.append("| 簡稱 | 論文數 | CC（任何） | 示範站可用 | 其他 OA 授權* | 有 TEI | TEI＋CC | TEI＋示範站可用 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in cur + [dict(short="**合計**", **cur_all)]:
        n = r["total"]
        cc, demo, tei_cc, tei_demo = cc_counts(r)
        other = sum(r["license"].values()) - cc
        L.append(f"| {r['short']} | {fmt(n)} | {cc}（{pct(cc, n)}） | {demo}（{pct(demo, n)}） | {other} | "
                 f"{r['tei_total']}（{pct(r['tei_total'], n)}） | {tei_cc}（{pct(tei_cc, n)}） | "
                 f"{tei_demo}（{pct(tei_demo, n)}） |")
    L.append("\n\\* 其他 OA 授權＝OpenAlex 標成 `other-oa`、`publisher-specific-oa` 等非 CC 授權，依 PRD 不算 OA 全文。\n")

    L.append("### 2.3 授權組合（全部 14 本合計）\n")
    lic_all = cur_all["license"]
    L.append("| 授權 | 篇數 | 占全部 | 其中有 TEI |")
    L.append("|---|---:|---:|---:|")
    for k, v in sorted(lic_all.items(), key=lambda kv: -kv[1]):
        mark = "（示範站可用）" if k in CC_DEMO else ("（CC）" if k in CC_ANY else "（非 CC）")
        L.append(f"| {k} {mark} | {v} | {pct(v, cur_all['total'], 1)} | {cur_all['tei_license'].get(k, 0)} |")
    none = cur_all["total"] - sum(lic_all.values())
    L.append(f"| 無授權資訊（含 closed 與 bronze 大多數） | {none} | {pct(none, cur_all['total'], 1)} | "
             f"{cur_all['tei_total'] - sum(cur_all['tei_license'].values())} |")

    L.append("\n### 2.4 各 OA 類型裡的 CC 比例（14 本合計）\n")
    L.append("| OA 類型 | 篇數 | CC（任何） | 示範站可用 |")
    L.append("|---|---:|---:|---:|")
    for st in ["gold", "diamond", "hybrid", "green", "bronze"]:
        n = cur_all["oa_status"].get(st, 0)
        lic = Counter()
        for r in cur:
            lic.update(r["license_by_status"][st])
        L.append(f"| {st} | {n} | {lic_sum(lic, CC_ANY)}（{pct(lic_sum(lic, CC_ANY), n)}） | "
                 f"{lic_sum(lic, CC_DEMO)}（{pct(lic_sum(lic, CC_DEMO), n)}） |")

    L.append("\n### 2.5 CC 全文能補多少「沒摘要」的論文\n")
    L.append("| 簡稱 | 沒摘要 | 其中 OA | 其中 CC | 其中 TEI＋CC |")
    L.append("|---|---:|---:|---:|---:|")
    nt = no_oa = no_cc = no_tei = 0
    for r in cur:
        n = r["noabs_total"]
        oa = n - r["noabs_oa_status"].get("closed", 0)
        cc = lic_sum(r["noabs_license"], CC_ANY)
        tc = lic_sum(r["noabs_tei_license"], CC_ANY)
        nt += n; no_oa += oa; no_cc += cc; no_tei += tc
        L.append(f"| {r['short']} | {n} | {oa} | {cc} | {tc} |")
    L.append(f"| **合計** | **{nt}** | **{no_oa}** | **{no_cc}** | **{no_tei}** |")

    # ---------- 2. 引用擴展
    c = res["citations"]
    L.append("\n## 3. ① 引用擴展\n")
    tot = c["totals"]
    L.append(f"- 14 本 2016–2025 共 {fmt(tot['citing'])} 篇，OpenAlex 記錄的參考文獻連結 {fmt(tot['ref_links'])} 條，"
             f"指向 {fmt(tot.get('unique_refs', 0))} 篇不同的作品")
    L.append(f"- 被引用 ≥5 次的作品中，有 {c['missing_ids']} 個 ID 在 OpenAlex 查不到（已合併或刪除），不計入")
    L.append("- 「14 本以外」＝被引用作品的主要出處不是 14 本追蹤期刊（含書籍、沒有出處的作品）\n")
    L.append("### 3.1 參考文獻資料的完整度（OpenAlex 有沒有這篇論文的參考文獻清單）\n")
    L.append("| 簡稱 | 論文數 | 有參考文獻資料 | 比例 |")
    L.append("|---|---:|---:|---:|")
    for short, _ in JOURNALS14:
        cv = c["coverage"].get(short, {})
        L.append(f"| {short} | {cv.get('works', 0)} | {cv.get('with_refs', 0)} | "
                 f"{pct(cv.get('with_refs', 0), cv.get('works', 0))} |")

    L.append("\n### 3.2 依被引用次數門檻\n")
    L.append("| 門檻 | 14 本以外 | 有摘要 | 摘要比例 | 其中期刊論文（article/review） | 期刊論文摘要比例 | OA | CC | TEI＋CC | "
             "14 本、2016 年以前（對照：拉長年份會收到） |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for t in ("5", "10", "20"):
        e = c["thresholds"][t]["external"]
        old = c["thresholds"][t]["in14_before_2016"]
        L.append(f"| ≥{t} 次 | {fmt(e['n'])} | {fmt(e['abstract'])} | {pct(e['abstract'], e['n'])} | "
                 f"{fmt(e['n_article'])} | {pct(e['abstract_article'], e['n_article'])} | {pct(e['oa'], e['n'])} | "
                 f"{pct(e['cc_any'], e['n'])} | {e['tei_cc']} | {fmt(old['n'])}（摘要 {pct(old['abstract'], old['n'])}） |")
    L.append("\n### 3.3 14 本以外、被引用 ≥5 次的作品類型與年代\n")
    e5 = c["thresholds"]["5"]["external"]
    L.append("| 類型 | 篇數 |")
    L.append("|---|---:|")
    for k, v in e5["types"].items():
        L.append(f"| {k} | {v} |")
    L.append("\n| 年代 | 篇數 |")
    L.append("|---|---:|")
    for k, v in e5["decades"].items():
        L.append(f"| {k} | {v} |")
    L.append("\n### 3.4 14 本以外、被引用 ≥10 次的作品最常來自哪些出處（前 20）\n")
    L.append("| 出處 | 篇數 |")
    L.append("|---|---:|")
    for name, v in c["thresholds"]["10"]["external"]["top_sources"]:
        L.append(f"| {name} | {v} |")

    # ---------- 3. 拉長年份
    early = res["early"]
    L.append("\n## 4. ③ 拉長年份：14 本 2006–2015\n")
    L.append("| 簡稱 | 論文數 | 有摘要 | 摘要比例 | OA 比例 | CC | TEI＋CC |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for r in early + [dict(short="**合計**", **sum_profiles(early))]:
        n = r["total"]
        cc, _, tei_cc, _ = cc_counts(r)
        L.append(f"| {r['short']} | {fmt(n)} | {fmt(r['abstract'])} | {pct(r['abstract'], n)} | "
                 f"{pct(n - r['oa_status'].get('closed', 0), n)} | {cc}（{pct(cc, n)}） | {tei_cc} |")
    years = list(range(EARLY[0], EARLY[1] + 1))
    L.append("\n每年的摘要比例：\n")
    L.append("| 簡稱 | " + " | ".join(map(str, years)) + " |")
    L.append("|---|" + "---:|" * len(years))
    for r in early:
        cn, ca = r["years"]["counts"], r["years"]["with_abstract"]
        L.append(f"| {r['short']} | " + " | ".join(
            f"{pct(ca.get(str(y), 0), cn.get(str(y), 0))}（{cn.get(str(y), 0)}）" for y in years) + " |")
    el = sum_profiles([r for r in early if r["short"] in ELSEVIER])
    ne = sum_profiles([r for r in early if r["short"] not in ELSEVIER])
    L.append(f"\n- Elsevier 4 本：{fmt(el['total'])} 篇，摘要 {pct(el['abstract'], el['total'])}；"
             f"其他 10 本：{fmt(ne['total'])} 篇，摘要 {pct(ne['abstract'], ne['total'])}")

    # ---------- 4. 候選期刊
    L.append("\n## 5. ② 候選期刊（2016–2025）\n")
    L.append("### 5.1 期刊對應（ISSN → OpenAlex source）\n")
    L.append("| 簡稱 | 期刊 | 領域 | ISSN | OpenAlex 名稱 | source ID | 出版者 | 狀態 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in res["candidates"]:
        s = r.get("source") or {}
        L.append(f"| {r['short']} | {r['expected']} | {r['field']} | {', '.join(r['issns'])} | "
                 f"{s.get('display_name', '-')} | {(s.get('id') or '-').rsplit('/', 1)[-1]} | "
                 f"{s.get('host_organization_name') or '-'} | {r['status']} |")
    L.append("\n### 5.2 規模、摘要、OA／CC\n")
    L.append("| 簡稱 | 論文數 | 不要求 DOI 時 | 有摘要 | 摘要比例 | OA 比例 | gold | hybrid | green | bronze | CC | 示範站可用 | TEI＋CC |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in cands + [dict(short="**合計**", total_without_doi_rule=sum(x["total_without_doi_rule"] for x in cands),
                           **sum_profiles(cands))]:
        n = r["total"]
        oa = r["oa_status"]
        cc, demo, tei_cc, _ = cc_counts(r)
        L.append(f"| {r['short']} | {fmt(n)} | {fmt(r['total_without_doi_rule'])} | {fmt(r['abstract'])} | "
                 f"{pct(r['abstract'], n)} | {pct(n - oa.get('closed', 0), n)} | "
                 + " | ".join(f"{pct(oa.get(s, 0) + (oa.get('diamond', 0) if s == 'gold' else 0), n)}"
                              for s in ("gold", "hybrid", "green", "bronze")) +
                 f" | {pct(cc, n)} | {pct(demo, n)} | {tei_cc} |")
    L.append("\n（gold 欄含 diamond。）\n")
    years = list(range(RECENT[0], RECENT[1] + 1))
    L.append("每年的論文數（摘要比例）：\n")
    L.append("| 簡稱 | " + " | ".join(map(str, years)) + " |")
    L.append("|---|" + "---:|" * len(years))
    for r in cands:
        cn, ca = r["years"]["counts"], r["years"]["with_abstract"]
        L.append(f"| {r['short']} | " + " | ".join(
            f"{cn.get(str(y), 0)}（{pct(ca.get(str(y), 0), cn.get(str(y), 0))}）" for y in years) + " |")

    # ---------- 5. 研討會
    L.append("\n## 6. ④ 研討會（2016–2025）\n")
    h = conf["HICSS"]
    cc, demo, tei_cc, _ = cc_counts(h)
    L.append(f"### 6.1 HICSS（OpenAlex source {HICSS_SOURCE}）\n")
    L.append(f"- {fmt(h['total'])} 篇（有 DOI {h['doi'].get('true', 0)} 篇），有摘要 {fmt(h['abstract'])}（{pct(h['abstract'], h['total'])}），"
             f"OA {pct(h['total'] - h['oa_status'].get('closed', 0), h['total'])}，CC {pct(cc, h['total'])}"
             f"（示範站可用 {pct(demo, h['total'])}），TEI＋CC {tei_cc} 篇")
    L.append("- OA 類型：" + "、".join(f"{k} {v}" for k, v in sorted(h["oa_status"].items(), key=lambda kv: -kv[1])))
    L.append("- 授權：" + "、".join(f"{k} {v}" for k, v in sorted(h["license"].items(), key=lambda kv: -kv[1])))
    L.append("\n| 年 | " + " | ".join(map(str, years)) + " |")
    L.append("|---|" + "---:|" * len(years))
    L.append("| 篇數 | " + " | ".join(str(h["years"]["counts"].get(str(y), 0)) for y in years) + " |")
    L.append("| 摘要比例 | " + " | ".join(pct(h["years"]["with_abstract"].get(str(y), 0),
                                              h["years"]["counts"].get(str(y), 0)) for y in years) + " |")

    L.append("\n### 6.2 ICIS、PACIS、ECIS、AMCIS\n")
    L.append("OpenAlex 裡這四個研討會分散在兩處：(a) 各自的研討會 source（大多是 2016 年、從 DBLP 匯入的條目）；"
             "(b) 錯掛在 JAIS 底下、沒有 DOI 的 AISeL 條目（OQ-02 發現的那批），依 AISeL 網址判斷屬於哪個研討會。"
             "兩處可能有重複，這裡分開列、不去重。\n")
    L.append("| 研討會 | (a) 專屬 source 篇數 | (a) 摘要比例 | (b) 錯掛 JAIS 篇數 | (b) 摘要比例 | (b) OA 比例 | (b) CC 比例 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for k in AIS_CONFS:
        a = conf["ais_dedicated"][k]
        b = conf["ais_in_jais"].get(k, {"total": 0, "abstract": 0, "is_oa": 0, "cc_any": 0})
        L.append(f"| {k} | {a['total']} | {pct(a['abstract'], a['total'])} | {b['total']} | "
                 f"{pct(b['abstract'], b['total'])} | {pct(b['is_oa'], b['total'])} | {pct(b['cc_any'], b['total'])} |")
    oth = conf["ais_in_jais"].get("OTHER", {"total": 0})
    L.append(f"\n錯掛 JAIS 的條目中，不屬於這四個研討會的有 {oth['total']} 篇（例如 MIS Quarterly Executive、"
             "其他區域研討會）。前幾名：" + "、".join(f"{k} {v}" for k, v in conf["ais_in_jais_other_top"][:10]) + "\n")
    L.append("(b) 每年篇數：\n")
    L.append("| 研討會 | " + " | ".join(map(str, years)) + " |")
    L.append("|---|" + "---:|" * len(years))
    for k in AIS_CONFS:
        b = conf["ais_in_jais"].get(k, {"years": {}})
        L.append(f"| {k} | " + " | ".join(str(b["years"].get(str(y), b["years"].get(y, 0))) for y in years) + " |")
    L.append("\n(a) 專屬 source 每年篇數：\n")
    L.append("| 研討會 | " + " | ".join(map(str, years)) + " |")
    L.append("|---|" + "---:|" * len(years))
    for k in AIS_CONFS:
        a = conf["ais_dedicated"][k]["years"]["counts"]
        L.append(f"| {k} | " + " | ".join(str(a.get(str(y), 0)) for y in years) + " |")

    # ---------- 6. 試擷取
    L.append("\n## 7. 試擷取（PRD 8.5 早期訊號）\n")
    if ext:
        s = ext["summary"]
        L.append(f"- 日期：{ext['run_date']}；模型 `{ext['model']}`（effort `{ext['effort']}`）；"
                 f"抽樣：14 本期刊 2016–2025、有摘要的論文，每本 {ext['per_journal']} 篇（OpenAlex `sample`，seed {ext['seed']}），"
                 f"共 {s['papers']} 篇")
        L.append("- 規則依 PRD FR-3：只擷取摘要裡寫得出的部分、沒提到的欄位留空不推測；"
                 "每條關係附摘要原句，原句正規化後必須出現在摘要裡，否則拒收")
        L.append("- repo 只存每篇的計數，不存摘要、原句或構念名稱\n")
        L.append("| 指標 | 數值 | PRD 8.5 門檻 | 判定 |")
        L.append("|---|---:|---:|---|")
        L.append(f"| 每篇平均關係數（通過原句檢查） | {s['relations_per_paper']:.2f} | ≥1 | "
                 f"{'達到' if s['relations_per_paper'] >= 1 else '**未達**'} |")
        L.append(f"| 可判定支持與否的關係比例 | {pct(s['decidable'], s['relations'], 1)}（{s['decidable']}/{s['relations']}） | ≥20% | "
                 f"{'達到' if s['relations'] and s['decidable'] / s['relations'] >= 0.2 else '**未達**'} |")
        L.append(f"| 國家有值的論文比例 | {pct(s['country'], s['papers'])}（{s['country']}/{s['papers']}） | — | — |")
        L.append(f"| 產業有值的論文比例 | {pct(s['industry'], s['papers'])}（{s['industry']}/{s['papers']}） | — | — |")
        L.append(f"| 至少有 1 條關係的論文比例 | {pct(s['papers_with_relation'], s['papers'])} | — | — |")
        L.append(f"| 每篇平均構念數 | {s['constructs_per_paper']:.2f} | — | 供 FR-18 估算用 |")
        L.append(f"| 原句檢查被拒收的關係 | {s['rejected']}（占模型提出的 {pct(s['rejected'], s['relations'] + s['rejected'])}） | — | — |")
        L.append(f"| 呼叫失敗或輸出不合格式的論文 | {s['failed']} | — | — |")
        L.append("\n支持與否的分布：" + "、".join(f"{k} {v}" for k, v in s["support_dist"].items()))
        L.append("\n論文類型（模型判斷）與每篇關係數：\n")
        L.append("| 論文類型 | 篇數 | 平均關係數 | 可判定比例 |")
        L.append("|---|---:|---:|---:|")
        for k, v in s["by_paper_type"].items():
            L.append(f"| {k} | {v['papers']} | {v['relations'] / v['papers']:.2f} | {pct(v['decidable'], v['relations'])} |")
        L.append("\n依期刊：\n")
        L.append("| 簡稱 | 篇數 | 平均關係數 | 可判定比例 | 國家有值 | 產業有值 |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for k, v in s["by_journal"].items():
            L.append(f"| {k} | {v['papers']} | {v['relations'] / v['papers']:.2f} | {pct(v['decidable'], v['relations'])} | "
                     f"{v['country']}/{v['papers']} | {v['industry']}/{v['papers']} |")
        u = ext["usage"]
        L.append(f"\n**實際花費**：輸入 {fmt(u['input_tokens'])} tokens、輸出 {fmt(u['output_tokens'])} tokens（含思考），"
                 f"依 {ext['model']} 標準價（輸入 ${ext['price']['input']}/百萬、輸出 ${ext['price']['output']}/百萬）"
                 f"共 **{u['cost_usd']:.4f} 美元**，每篇 {u['cost_usd'] / s['papers']:.5f} 美元；"
                 f"改走 Batch API（五折）約每篇 {u['cost_usd'] / s['papers'] / 2:.5f} 美元。")
    else:
        L.append("（尚未執行：`uv run --with anthropic python3 spikes/oq-10/extract_trial.py`）")

    cmp_path = EXTRACTION_JSON.parent / "extraction_compare.json"
    if cmp_path.exists():
        cm = json.loads(cmp_path.read_text(encoding="utf-8"))
        models = cm["models"]
        L.append("\n### 7.1 模型比較：同一批摘要，Haiku 與 Opus 各擷取一次\n")
        L.append(f"- 日期：{cm['run_date']}；同一份 prompt、schema、effort（`{cm['effort']}`），論文與上表相同（42 篇）")
        L.append("- 這裡的 Haiku 是**第二次執行**，和上表（第一次）比，可以看出同一模型跑兩次的差異有多大；"
                 "**v1 可行性判斷仍以上表（第一次 Haiku）為準**")
        L.append("- Opus 沒有開 server-side fallback：遭拒時記為失敗，避免換成別的模型而混淆比較")
        L.append("- 「不同原句數」：同一句摘要常被拆成多條關係，改數「用到幾句不同的原句」是比較保守的計法\n")
        cols = [("Haiku 第一次（上表）", ext["summary"], ext["usage"])] if ext else []
        for m in models:
            r = cm["results"][m]
            cols.append((f"{m}（比較）", r["summary"], r["usage"]))
        L.append("| 指標 | " + " | ".join(c[0] for c in cols) + " |")
        L.append("|---|" + "---:|" * len(cols))

        def line(label, fn):
            L.append(f"| {label} | " + " | ".join(fn(s, u) for _, s, u in cols) + " |")

        line("成功篇數", lambda s, u: f"{s['papers']}（失敗 {s['failed']}）")
        line("每篇平均關係數", lambda s, u: f"{s['relations_per_paper']:.2f}")
        line("至少 1 條關係的論文", lambda s, u: f"{s['papers_with_relation']}/{s['papers']}")
        line("可判定支持與否的比例", lambda s, u: f"{pct(s['decidable'], s['relations'])}（{s['decidable']}/{s['relations']}）")
        line("每篇平均不同原句數", lambda s, u: f"{s['distinct_quotes'] / s['papers']:.2f}" if "distinct_quotes" in s else "-")
        line("每篇可判定的不同原句數", lambda s, u: f"{s['decidable_quotes'] / s['papers']:.2f}" if "decidable_quotes" in s else "-")
        line("國家有值", lambda s, u: f"{s['country']}/{s['papers']}")
        line("產業有值", lambda s, u: f"{s['industry']}/{s['papers']}")
        line("樣本數有值", lambda s, u: f"{s['sample_size']}/{s['papers']}")
        line("每篇平均構念數", lambda s, u: f"{s['constructs_per_paper']:.2f}")
        line("原句檢查拒收", lambda s, u: str(s["rejected"]))
        line("花費（標準價）", lambda s, u: f"${u['cost_usd']:.4f}")
        line("每篇花費", lambda s, u: f"${u['cost_usd'] / max(s['papers'], 1):.5f}")
        ov = cm["quote_overlap"]
        both = sum(o["both"] for o in ov)
        union = sum(o["union"] for o in ov)
        a, b = models
        L.append(f"\n兩個模型用到的證據原句：共 {union} 句，兩者都用到 {both} 句（{pct(both, union)}），"
                 f"只有 {a} 用到 {sum(o['only_' + a] for o in ov)} 句，只有 {b} 用到 {sum(o['only_' + b] for o in ov)} 句。")
        zero_both = sum(1 for pa, pb in zip(cm["results"][a]["papers"], cm["results"][b]["papers"])
                        if not pa["failed"] and not pb["failed"] and pa["relations"] == 0 and pb["relations"] == 0)
        L.append(f"兩個模型都擷取不到任何關係的論文：{zero_both} 篇。")
        L.append("\n依論文類型（比較執行）：\n")
        L.append("| 論文類型 | " + " | ".join(f"{m} 篇數／平均關係／可判定" for m in models) + " |")
        L.append("|---|" + "---|" * len(models))
        types = sorted({t for m in models for t in cm["results"][m]["summary"]["by_paper_type"]})
        for t in types:
            cells = []
            for m in models:
                v = cm["results"][m]["summary"]["by_paper_type"].get(t)
                cells.append(f"{v['papers']}／{v['relations'] / v['papers']:.2f}／{pct(v['decidable'], v['relations'])}"
                             if v else "-")
            L.append(f"| {t} | " + " | ".join(cells) + " |")

    # ---------- 7. 費用
    L.append("\n## 8. LLM 處理費用估算\n")
    if ext:
        u = ext["usage"]
        per_std = u["cost_usd"] / ext["summary"]["papers"]
        per_batch = per_std / 2
        per_in = u["input_tokens"] / ext["summary"]["papers"]
        per_out = u["output_tokens"] / ext["summary"]["papers"]
        L.append(f"以試擷取實測：每篇摘要平均輸入 {per_in:,.0f} tokens、輸出 {per_out:,.0f} tokens，"
                 f"研究模型擷取每篇 {per_batch:.5f} 美元（Batch）。"
                 "「摘要全套」假設中文結構化摘要＋分類＋擷取三項合計約為擷取的 3 倍 `[ASSUMPTION]`（只實測了擷取）。"
                 "CC 全文另加 OpenAlex TEI 每篇約 0.01 美元（PRD 數字）與全文送 Haiku 的費用；"
                 "全文 token 數這次沒有量（不下載全文），假設每篇 20,000 輸入 tokens、輸出為摘要擷取的 3 倍 `[ASSUMPTION]`。\n")
        ft_llm = (20000 * ext["price"]["input"] + per_out * 3 * ext["price"]["output"]) / 1e6 / 2
        L.append(f"全文每篇估計：TEI 0.01 ＋ Haiku（Batch）{ft_llm:.5f} ≈ {0.01 + ft_llm:.4f} 美元。\n")
        L.append("| 選項 | 有摘要篇數 | 摘要擷取（Batch） | 摘要全套 ×3（Batch） | CC 全文篇數（TEI＋CC） | CC 全文管線 |")
        L.append("|---|---:|---:|---:|---:|---:|")

        def cost_row(label: str, n_abs: int, n_ft: int) -> None:
            L.append(f"| {label} | {fmt(n_abs)} | ${n_abs * per_batch:.2f} | ${n_abs * per_batch * 3:.2f} | "
                     f"{fmt(n_ft)} | ${n_ft * (0.01 + ft_llm):.2f} |")

        cost_row("現況：14 本 2016–2025", cur_all["abstract"], cc_counts(cur_all)[2])
        for t in ("5", "10", "20"):
            e = cit[t]["external"]
            cost_row(f"① 引用擴展 ≥{t}", e["abstract"], e["tei_cc"])
        cs = sum_profiles(cands)
        cost_row("② 候選期刊合計", cs["abstract"], cc_counts(cs)[2])
        es = sum_profiles(early)
        cost_row("③ 拉長年份 2006–2015", es["abstract"], cc_counts(es)[2])
        cost_row("④ HICSS", conf["HICSS"]["abstract"], cc_counts(conf["HICSS"])[2])
        L.append(f"| ④ AIS 四研討會（OpenAlex 找得到的部分） | {fmt(ais_abs)} | ${ais_abs * per_batch:.2f} | "
                 f"${ais_abs * per_batch * 3:.2f} | 不適用（AISeL 條款禁止自動下載與 AI 處理，見第 9 節） | — |")
        L.append("\n（費用是一次性的第一次處理；之後每週增量只處理新論文。月預算上限見 PRD §6：Research Radar 70 美元、熔斷 50 美元。）")

    L.append("")
    if CONCLUSION_MD.exists():
        L.append(CONCLUSION_MD.read_text(encoding="utf-8").strip())
    else:
        L.append("## 9. 結論與建議\n\n（待補：寫在 spikes/oq-10/conclusion.md 後用 --report-only 重新產生報告）")
    return "\n".join(L) + "\n"


def main() -> None:
    global API_KEY
    if "--report-only" in sys.argv:
        res = json.loads(OUTPUT_JSON.read_text(encoding="utf-8"))
    else:
        API_KEY = load_api_key()
        OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        partial = json.loads(OUTPUT_JSON.read_text(encoding="utf-8")) if OUTPUT_JSON.exists() else {}
        res = {"fetched": date.today().isoformat()}
        steps = [("current", "步驟 1：14 本 2016–2025 的 OA／CC／TEI", step1_current),
                 ("early", "步驟 3：14 本 2006–2015", step3_early),
                 ("candidates", "步驟 4：候選期刊", step4_candidates),
                 ("conferences", "步驟 5：研討會", step5_conferences),
                 ("citations", "步驟 2：引用擴展（最久）", step2_citations)]
        only = [a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")]
        for key, label, fn in steps:
            if only and key not in only[0].split(","):
                res[key] = partial.get(key)
                continue
            print(label)
            res[key] = fn()
            # 每步存一次，中斷時不用重跑
            OUTPUT_JSON.write_text(json.dumps({**partial, **res}, ensure_ascii=False, indent=1), encoding="utf-8")
        prev_api = (partial.get("api") or {}) if only else {}
        res["api"] = {"requests": STATS["requests"] + prev_api.get("requests", 0),
                      "cost_usd": STATS["cost_usd"] + prev_api.get("cost_usd", 0.0)}
        res = {**partial, **res}
        OUTPUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    REPORT_MD.parent.mkdir(parents=True, exist_ok=True)
    REPORT_MD.write_text(build_report(res), encoding="utf-8")
    print(f"報告已寫入 {REPORT_MD.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
