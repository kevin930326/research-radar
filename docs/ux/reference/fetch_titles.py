"""抓 14 本期刊 2016–2025 的論文標題與年份（OpenAlex 公開目錄，不帶金鑰）。
篩選條件與 spikes/oq-10/check_expansion.py 的 journal_filter 相同。"""
import json, sys, time, urllib.parse, urllib.request

OUT = sys.argv[1]
JOURNALS = [("MISQ", "S57293258"), ("ISR", "S202812398"), ("JMIS", "S9954729"), ("JAIS", "S30879505"),
            ("EJIS", "S116712389"), ("ISJ", "S92260147"), ("JIT", "S135086714"), ("JSIS", "S6144335"),
            ("DSS", "S11479521"), ("I&M", "S38883057"), ("I&O", "S35363033"), ("PMJ", "S196205230"),
            ("TEM", "S154533451"), ("DATABASE", "S4210234096")]
calls = 0

def get(path, params):
    global calls
    url = "https://api.openalex.org" + path + "?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "python-urllib"})
            with urllib.request.urlopen(req, timeout=40) as r:
                calls += 1
                time.sleep(0.3)
                return json.load(r)
        except Exception as e:  # 逾時或暫時性錯誤：等一下再試
            time.sleep(2 + attempt * 3)
            err = e
    raise err

names = {}
src = get("/sources", {"filter": "openalex:" + "|".join(s for _, s in JOURNALS), "select": "id,display_name", "per-page": 50})
for s in src["results"]:
    names[s["id"].rsplit("/", 1)[-1]] = s["display_name"]

out = {"fetched": time.strftime("%Y-%m-%d"), "journals": []}
for short, sid in JOURNALS:
    papers = []
    for has in ("true", "false"):
        page = 1
        while True:
            d = get("/works", {"filter": f"primary_location.source.id:{sid},publication_year:2016-2025,type:article|review,has_doi:true,has_abstract:{has}",
                               "select": "id,title,publication_year", "per-page": 200, "page": page, "sort": "publication_date:asc"})
            for w in d["results"]:
                papers.append([w["publication_year"], 1 if has == "true" else 0, (w.get("title") or "").strip()[:220]])
            if page * 200 >= d["meta"]["count"] or not d["results"]:
                break
            page += 1
    out["journals"].append({"short": short, "name": names.get(sid, short), "papers": papers})
    print(short, len(papers), sum(p[1] for p in papers), flush=True)

json.dump(out, open(OUT, "w"), ensure_ascii=False, separators=(",", ":"))
tot = sum(len(j["papers"]) for j in out["journals"])
print("total", tot, "abstract", sum(p[1] for j in out["journals"] for p in j["papers"]), "calls", calls)
