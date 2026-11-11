"""OQ-10 spike：關係排行表可行性的早期訊號（PRD 8.5、審稿 #3）。

從 14 本追蹤期刊 2016–2025、有摘要的論文中，每本隨機抽 N 篇（OpenAlex sample＋seed，依期刊分層），
用 Claude Haiku 依 PRD FR-3 的規則擷取研究模型，量：
  - 每篇平均關係數（通過「原句必須出現在摘要裡」檢查的關係）
  - 可判定支持與否的關係比例
  - 情境欄位（國家、產業）有值的比例
  - 實際花費（依 usage 與標準價計算）

一次性驗證用，不屬於正式程式架構。用法（在 repo 根目錄）：
    uv run --with anthropic python3 spikes/oq-10/extract_trial.py             # 正式試擷取（Haiku）
    uv run --with anthropic python3 spikes/oq-10/extract_trial.py --compare   # 同一批論文 Haiku vs. Opus
    （.env 需要 OPENALEX_API_KEY 與 ANTHROPIC_API_KEY）

repo 只存每篇的計數（spikes/oq-10/output/extraction.json），不存摘要、原句、構念名稱。
LLM 的輸出一律當作使用者輸入驗證：檢查型別、列舉值，原句比對不過就拒收。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import anthropic

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "output" / "extraction.json"

MODEL = "claude-haiku-5-5"
EFFORT = "medium"  # Haiku 5.5 的預設值，明寫出來
PRICE = {"input": 0.10, "output": 0.50}  # 美元／百萬 tokens（Haiku 5.5，提示 ≤100K tokens；Batch 再打五折）
PER_JOURNAL = 3
OUT_COMPARE = OUT.parent / "extraction_compare.json"
# 模型比較（--compare）：價格為美元／百萬 tokens 的標準價
COMPARE_MODELS = ["claude-haiku-5-5", "claude-opus-5-5"]
COMPARE_PRICE = {"claude-haiku-5-5": {"input": 0.10, "output": 0.50},
                 "claude-opus-5-5": {"input": 4.00, "output": 20.00}}
SEED = 20261110

JOURNALS14 = [
    ("MISQ", "S57293258"), ("ISR", "S202812398"), ("JMIS", "S9954729"), ("JAIS", "S30879505"),
    ("EJIS", "S116712389"), ("ISJ", "S92260147"), ("JIT", "S135086714"), ("JSIS", "S6144335"),
    ("DSS", "S11479521"), ("I&M", "S38883057"), ("I&O", "S35363033"), ("PMJ", "S196205230"),
    ("TEM", "S154533451"), ("DATABASE", "S4210234096"),
]

PAPER_TYPES = ["empirical_quantitative", "empirical_qualitative", "mixed_methods", "design_science",
               "analytical_modeling", "conceptual", "literature_review", "other"]
SUPPORT = ["supported", "not_supported", "partially_supported", "mixed", "not_stated"]
DECIDABLE = {"supported", "not_supported", "partially_supported", "mixed"}

NULLABLE_STR = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paper_type", "context", "limitations", "future_research", "constructs", "relations"],
    "properties": {
        "paper_type": {"type": "string", "enum": PAPER_TYPES},
        "context": {
            "type": "object",
            "additionalProperties": False,
            "required": ["country", "industry", "sample_size", "unit_of_analysis", "research_design"],
            "properties": {
                "country": NULLABLE_STR,
                "industry": NULLABLE_STR,
                "sample_size": {"type": ["integer", "null"]},
                "unit_of_analysis": NULLABLE_STR,
                "research_design": NULLABLE_STR,
            },
        },
        "limitations": NULLABLE_STR,
        "future_research": NULLABLE_STR,
        "constructs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name_en", "label_zh_tw"],
                "properties": {"name_en": {"type": "string"}, "label_zh_tw": {"type": "string"}},
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["from_construct", "to_construct", "quote", "support",
                             "beta", "p_value", "n", "effect_type"],
                "properties": {
                    "from_construct": {"type": "string"},
                    "to_construct": {"type": "string"},
                    "quote": {"type": "string"},
                    "support": {"type": "string", "enum": SUPPORT},
                    "beta": {"type": ["number", "null"]},
                    "p_value": NULLABLE_STR,
                    "n": {"type": ["integer", "null"]},
                    "effect_type": NULLABLE_STR,
                },
            },
        },
    },
}

SYSTEM = """You extract the research model of an academic paper from its title and abstract, for a literature database used by information-systems researchers.

Rules (the database depends on them):
1. Extract only what the text explicitly states. If a field is not mentioned, return null (or an empty list). Never infer, guess, or use outside knowledge about the paper.
2. constructs: the theoretical constructs (variables/concepts) the study models or measures. name_en as written in the text; label_zh_tw is a short Traditional Chinese label.
3. relations: each directional relationship between two constructs that the text states or tests (construct A -> construct B), including moderating effects (write the moderator as from_construct and "A -> B" as to_construct). One entry per relationship.
   - quote: copy ONE sentence (or contiguous clause) from the abstract VERBATIM that states this relationship. Do not paraphrase, shorten with ellipses, or merge sentences. Entries whose quote is not found verbatim in the abstract are discarded.
   - support: "supported" / "not_supported" / "partially_supported" / "mixed" only when the text reports the finding for this relationship (e.g. "X significantly increases Y", "we find no effect of X on Y"). Use "not_stated" when the text only proposes, hypothesizes or examines the relationship without reporting whether it held.
   - beta, p_value, n, effect_type: only if stated for this relationship; otherwise null.
   - Do not create relations for qualitative themes, design artifacts or literature-review categories unless the text states a directional relationship between constructs.
4. context: country and industry of the empirical setting only if named (e.g. "Chinese manufacturing firms" -> country "China", industry "manufacturing"); sample_size only if a number is given; research_design such as "survey", "experiment", "case study", "panel data", "design science".
5. limitations / future_research: only if the abstract states them; otherwise null.
6. paper_type: your best classification of the paper from the abstract."""

QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                        "–": "-", "—": "-", "−": "-", " ": " "})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(QUOTES).lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip().rstrip(".;:, ")


def env_value(name: str) -> str:
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"找不到 {name}：請在 repo 根目錄的 .env 設定")
    return value


def openalex(params: dict, key: str) -> dict:
    query = urllib.parse.urlencode({**params, "api_key": key}, safe=":|,")
    req = urllib.request.Request(f"https://api.openalex.org/works?{query}",
                                 headers={"User-Agent": "research-radar-oq10-spike"})
    for attempt in range(5):
        time.sleep(0.25)  # OpenAlex 上限 10 次/秒
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** (attempt + 1))
                continue
            raise RuntimeError(f"OpenAlex HTTP {e.code}") from None
    raise RuntimeError("重試次數用完")


def rebuild_abstract(inv: dict) -> str:
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def validate(data: dict, abstract: str) -> dict:
    """把 LLM 輸出當使用者輸入檢查；回傳計數（不含任何文字）。"""
    if not isinstance(data, dict):
        raise ValueError("輸出不是物件")
    ptype = data.get("paper_type")
    if ptype not in PAPER_TYPES:
        raise ValueError("paper_type 不合法")
    ctx = data.get("context") or {}
    constructs = data.get("constructs")
    relations = data.get("relations")
    if not isinstance(constructs, list) or not isinstance(relations, list):
        raise ValueError("constructs／relations 不是陣列")
    norm_abs = normalize(abstract)
    accepted, rejected = [], 0
    for rel in relations:
        if not isinstance(rel, dict) or rel.get("support") not in SUPPORT:
            rejected += 1
            continue
        quote = rel.get("quote")
        if (not isinstance(quote, str) or len(normalize(quote)) < 15 or "..." in quote
                or normalize(quote) not in norm_abs):
            rejected += 1
            continue
        if not str(rel.get("from_construct") or "").strip() or not str(rel.get("to_construct") or "").strip():
            rejected += 1
            continue
        accepted.append(rel)

    def present(v) -> bool:
        return isinstance(v, str) and v.strip().lower() not in ("", "null", "none", "n/a", "not stated")

    names = {normalize(str(c.get("name_en", ""))) for c in constructs if isinstance(c, dict)}
    names.discard("")
    return {
        "paper_type": ptype,
        "relations": len(accepted),
        "rejected": rejected,
        "decidable": sum(1 for r in accepted if r["support"] in DECIDABLE),
        "support": dict(Counter(r["support"] for r in accepted)),
        "with_beta_or_p": sum(1 for r in accepted if r.get("beta") is not None or present(r.get("p_value"))),
        "constructs": len(names),
        "country": present(ctx.get("country")),
        "industry": present(ctx.get("industry")),
        "sample_size": isinstance(ctx.get("sample_size"), int),
        "research_design": present(ctx.get("research_design")),
        "limitations": present(data.get("limitations")),
        "future_research": present(data.get("future_research")),
        # 同一句話常被拆成多條關係；「不同原句數」是比較保守的計法
        "distinct_quotes": len({normalize(r["quote"]) for r in accepted}),
        "decidable_quotes": len({normalize(r["quote"]) for r in accepted if r["support"] in DECIDABLE}),
        "_quotes": {normalize(r["quote"]) for r in accepted},  # 只在記憶體裡比對用，寫檔前移除
    }


def extract_one(client, model: str, title: str, abstract: str) -> tuple[dict, set]:
    """擷取一篇；回傳（計數, 原句集合）。原句集合只在記憶體裡用。"""
    row: dict = {}
    quotes: set = set()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": f"<title>{title}</title>\n<abstract>{abstract}</abstract>"}],
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        row["input_tokens"] = resp.usage.input_tokens
        row["output_tokens"] = resp.usage.output_tokens
        if resp.stop_reason != "end_turn":  # 含 refusal、max_tokens：算失敗
            raise ValueError(f"stop_reason={resp.stop_reason}")
        text = next(b.text for b in resp.content if b.type == "text")
        counts = validate(json.loads(text), abstract)
        quotes = counts.pop("_quotes")
        row.update(counts)
        row["failed"] = False
    except anthropic.RateLimitError:
        print("  遇到速率限制，30 秒後跳過這篇", file=sys.stderr)
        time.sleep(30)
        row.update(failed=True, error="rate_limit")
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        row.update(failed=True, error=type(e).__name__)
    except (ValueError, StopIteration, json.JSONDecodeError) as e:
        row.update(failed=True, error=f"invalid_output: {e}"[:80])
    return row, quotes


def summarize(papers: list[dict]) -> dict:
    ok = [p for p in papers if not p["failed"]]
    by_journal = defaultdict(lambda: Counter())
    by_type = defaultdict(lambda: Counter())
    for p in ok:
        for agg, key in ((by_journal, p["journal"]), (by_type, p["paper_type"])):
            agg[key]["papers"] += 1
            agg[key]["relations"] += p["relations"]
            agg[key]["decidable"] += p["decidable"]
            agg[key]["country"] += p["country"]
            agg[key]["industry"] += p["industry"]
    support = Counter()
    for p in ok:
        support.update(p["support"])
    n = len(ok)
    return {
        "papers": n,
        "failed": len(papers) - n,
        "relations": sum(p["relations"] for p in ok),
        "decidable": sum(p["decidable"] for p in ok),
        "rejected": sum(p["rejected"] for p in ok),
        "relations_per_paper": sum(p["relations"] for p in ok) / n if n else 0,
        "papers_with_relation": sum(1 for p in ok if p["relations"] > 0),
        "constructs_per_paper": sum(p["constructs"] for p in ok) / n if n else 0,
        "distinct_quotes": sum(p.get("distinct_quotes", 0) for p in ok),
        "decidable_quotes": sum(p.get("decidable_quotes", 0) for p in ok),
        "country": sum(p["country"] for p in ok),
        "industry": sum(p["industry"] for p in ok),
        "sample_size": sum(p["sample_size"] for p in ok),
        "research_design": sum(p["research_design"] for p in ok),
        "limitations": sum(p["limitations"] for p in ok),
        "future_research": sum(p["future_research"] for p in ok),
        "with_beta_or_p": sum(p["with_beta_or_p"] for p in ok),
        "support_dist": dict(support.most_common()),
        "by_journal": {k: dict(v) for k, v in by_journal.items()},
        "by_paper_type": {k: dict(v) for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]["papers"])},
    }


def usage_of(papers: list[dict], price: dict) -> dict:
    u = {"input_tokens": sum(p.get("input_tokens", 0) for p in papers),
         "output_tokens": sum(p.get("output_tokens", 0) for p in papers)}
    u["cost_usd"] = (u["input_tokens"] * price["input"] + u["output_tokens"] * price["output"]) / 1e6
    return u


def log(model: str, short: str, row: dict) -> None:
    print(f"  [{model}][{short}] {row['openalex_id']}：" +
          ("失敗 " + row.get("error", "") if row["failed"] else
           f"關係 {row['relations']}（可判定 {row['decidable']}，拒收 {row['rejected']}）"))


def main() -> None:
    """正式試擷取（v1 可行性判斷用）：Haiku，每本期刊抽 PER_JOURNAL 篇。"""
    oa_key = env_value("OPENALEX_API_KEY")
    client = anthropic.Anthropic(api_key=env_value("ANTHROPIC_API_KEY"))
    papers = []
    for short, sid in JOURNALS14:
        flt = (f"primary_location.source.id:{sid},publication_year:2016-2025,"
               f"type:article|review,has_doi:true,has_abstract:true")
        res = openalex({"filter": flt, "sample": PER_JOURNAL, "seed": SEED,
                        "select": "id,title,abstract_inverted_index", "per-page": PER_JOURNAL}, oa_key)
        for w in res.get("results", []):
            abstract = rebuild_abstract(w.get("abstract_inverted_index") or {})
            row = {"journal": short, "openalex_id": w["id"].rsplit("/", 1)[-1],
                   "abstract_words": len(abstract.split())}
            counts, _ = extract_one(client, MODEL, w.get("title") or "", abstract)
            row.update(counts)
            papers.append(row)
            log(MODEL, short, row)
    summary = summarize(papers)
    usage = usage_of(papers, PRICE)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "run_date": date.today().isoformat(), "model": MODEL, "effort": EFFORT, "price": PRICE,
        "per_journal": PER_JOURNAL, "seed": SEED, "usage": usage, "summary": summary, "papers": papers,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完成：{summary['papers']} 篇，平均關係 {summary['relations_per_paper']:.2f}，"
          f"可判定 {summary['decidable']}/{summary['relations']}，花費 {usage['cost_usd']:.4f} 美元")


def compare() -> None:
    """模型比較：同一批論文（讀 extraction.json 的 ID）用 Haiku 與 Opus 各擷取一次。

    Haiku 在這裡是第二次執行，用來估計「同一模型跑兩次」的差異；v1 可行性判斷仍以 extraction.json 為準。
    兩個模型用同一份 prompt、schema 與 effort。Opus 不開 server-side fallback：
    遭拒（refusal）時直接記為失敗，避免換成別的模型而混淆比較。
    """
    oa_key = env_value("OPENALEX_API_KEY")
    client = anthropic.Anthropic(api_key=env_value("ANTHROPIC_API_KEY"))
    base = json.loads(OUT.read_text(encoding="utf-8"))
    rows = {m: [] for m in COMPARE_MODELS}
    overlap = []  # 每篇：兩模型用到的原句，交集／聯集（只存數字）
    for p in base["papers"]:
        w = openalex({"filter": f"openalex:{p['openalex_id']}",
                      "select": "id,title,abstract_inverted_index"}, oa_key)["results"][0]
        abstract = rebuild_abstract(w.get("abstract_inverted_index") or {})
        quotes = {}
        for m in COMPARE_MODELS:
            row = {"journal": p["journal"], "openalex_id": p["openalex_id"]}
            counts, quotes[m] = extract_one(client, m, w.get("title") or "", abstract)
            row.update(counts)
            rows[m].append(row)
            log(m, p["journal"], row)
        a, b = (quotes[m] for m in COMPARE_MODELS)
        overlap.append({"openalex_id": p["openalex_id"], "both": len(a & b), "union": len(a | b),
                        "only_" + COMPARE_MODELS[0]: len(a - b), "only_" + COMPARE_MODELS[1]: len(b - a)})
    OUT_COMPARE.write_text(json.dumps({
        "run_date": date.today().isoformat(), "effort": EFFORT, "models": COMPARE_MODELS,
        "price": COMPARE_PRICE,
        "results": {m: {"summary": summarize(rows[m]), "usage": usage_of(rows[m], COMPARE_PRICE[m]),
                        "papers": rows[m]} for m in COMPARE_MODELS},
        "quote_overlap": overlap,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    for m in COMPARE_MODELS:
        s, u = summarize(rows[m]), usage_of(rows[m], COMPARE_PRICE[m])
        print(f"{m}：平均關係 {s['relations_per_paper']:.2f}，可判定 {s['decidable']}/{s['relations']}，"
              f"國家 {s['country']}，產業 {s['industry']}，花費 {u['cost_usd']:.4f} 美元")


if __name__ == "__main__":
    compare() if "--compare" in sys.argv else main()
