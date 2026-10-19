#!/usr/bin/env python3
"""Regenerate index.html for the xMX Insights Dashboard.

    python build.py            - render index.html from the local cache (no Databricks call)
    python build.py --refresh  - re-pull from Databricks into the cache, then render

Inputs:
    cache/xmx_facts.json  - cached quarter-grain aggregate of dpaas_uccatalog_prd.gmi_ml.xmx_insights
                            (created/updated by fetch_from_databricks(), git-ignored)
    report_template.html  - HTML template (has /*__DATA__*/ + __DATE__ placeholders)
    .env                   - DATABRICKS_HOST, DATABRICKS_WAREHOUSE_ID (the access token itself is
                             read from the DATABRICKS_PAT or DATABRICKS_TOKEN environment variable,
                             never stored in .env)

Output:
    index.html - self-contained interactive report (the committed deliverable)

Requires: requests.

Data-recency rule: the most recent 2 fiscal quarters are always dropped (model output for the
current and prior quarter is still settling) - see fetch_from_databricks(). "Current fiscal
quarter" is resolved from aa_target.hana_ccmusage_dim_date, not a hardcoded calendar mapping,
since Adobe's fiscal year does not align to the calendar year.
"""
import datetime
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache", "xmx_facts.json")
QA_CACHE = os.path.join(HERE, "cache", "qa_results.json")
TEMPLATE = os.path.join(HERE, "report_template.html")
OUTPUT = os.path.join(HERE, "index.html")
ENV_FILE = os.path.join(HERE, ".env")

CATALOG = "dpaas_uccatalog_prd"
METRICS = ["arr_contribution", "unit_contribution", "spend"]

# (marketing_flag, demand_category) -> (rank, label, color). Matches the PDF's 5-category legend:
# "halo" rides along with "base" (marketing_flag='base' either way); everything else is keyed by
# demand_category. Any unrecognized combo falls back to Base (if marketing_flag='base') or
# Other Marketing (5) below, in category_of().
CATEGORY_META = {
    ("base", "base"): (1, "1. Base", "#5B6470"),
    ("base", "halo"): (1, "1. Base", "#5B6470"),
    ("marketing", "conversion"): (2, "2. Demand Conversion", "#4FC3E8"),
    ("marketing", "progression"): (3, "3. Demand Progression", "#F06292"),
    ("marketing", "creation"): (4, "4. Demand Creation", "#9575CD"),
    ("marketing", "owned"): (5, "5. Other Marketing", "#F2B84B"),
}
MARKETING_LINE_COLOR = "#6A3FA0"
ROMS_LINE_COLOR = "#B8860B"

CLOUD_LABELS = {"CC": "Creative Cloud", "DC": "Document Cloud"}

ACRONYMS = {"sem": "SEM", "ctv": "CTV", "roi": "ROI", "ste": "STE", "ax": "AX",
            "kpi": "KPI", "ar1": "AR1", "yt": "YT", "olv": "OLV", "agi": "AGI",
            "cc": "CC", "ai": "AI", "tv": "TV"}


def _qadd(q, n):
    """Shift a 'YYYY-Qn' fiscal quarter string by n quarters."""
    y, qq = int(q[:4]), int(q[6])
    i = y * 4 + (qq - 1) + n
    return f"{i // 4}-Q{i % 4 + 1}"


def humanize_channel(channel):
    if not channel:
        return "Other"
    words = []
    for w in channel.replace("_", " ").strip().split(" "):
        if not w:
            continue
        words.append(ACRONYMS.get(w.lower(), w.capitalize()))
    return " ".join(words) if words else "Other"


def category_of(marketing_flag, demand_category):
    key = (marketing_flag, demand_category)
    if key in CATEGORY_META:
        return CATEGORY_META[key]
    if marketing_flag == "base":
        return CATEGORY_META[("base", "base")]
    return (5, "5. Other Marketing", CATEGORY_META[("marketing", "owned")][2])


# ---------------------------------------------------------------------------
# Databricks SQL Statement API (see .claude/skills/databricks-query for the
# reference this mirrors) - plain REST, no databricks-sql-connector dependency.
# ---------------------------------------------------------------------------

def load_env():
    env = dict(os.environ)
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip())
    return env


def _dbx_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def dbx_query(host, warehouse_id, token, statement, timeout_s=180):
    """Run one SQL statement via the Databricks SQL Statement API; return list of row-dicts."""
    body = {
        "statement": statement,
        "warehouse_id": warehouse_id,
        "wait_timeout": "30s",
        "on_wait_timeout": "CONTINUE",
        "format": "JSON_ARRAY",
    }
    r = requests.post(f"{host}/api/2.0/sql/statements", headers=_dbx_headers(token), json=body, timeout=60)
    r.raise_for_status()
    payload = r.json()
    stmt_id = payload["statement_id"]

    deadline = time.time() + timeout_s
    while payload["status"]["state"] in ("PENDING", "RUNNING"):
        if time.time() > deadline:
            raise TimeoutError(f"Databricks query timed out after {timeout_s}s: {statement[:120]}")
        time.sleep(1.5)
        r = requests.get(f"{host}/api/2.0/sql/statements/{stmt_id}", headers=_dbx_headers(token), timeout=30)
        r.raise_for_status()
        payload = r.json()

    state = payload["status"]["state"]
    if state != "SUCCEEDED":
        err = payload["status"].get("error", {}).get("message", "unknown error")
        raise RuntimeError(f"Databricks query failed ({state}): {err}\n  statement: {statement[:200]}")

    columns = [c["name"] for c in payload["manifest"]["schema"]["columns"]]
    rows = list(payload.get("result", {}).get("data_array", []))
    total_chunks = payload["manifest"].get("total_chunk_count", 1)
    for i in range(1, total_chunks):
        cr = requests.get(f"{host}/api/2.0/sql/statements/{stmt_id}/result/chunks/{i}",
                           headers=_dbx_headers(token), timeout=60)
        cr.raise_for_status()
        rows.extend(cr.json().get("data_array", []))
    return [dict(zip(columns, row)) for row in rows]


def fetch_from_databricks():
    env = load_env()
    host = env.get("DATABRICKS_HOST")
    warehouse_id = env.get("DATABRICKS_WAREHOUSE_ID")
    token = env.get("DATABRICKS_PAT") or env.get("DATABRICKS_TOKEN")
    missing = [n for n, v in [("DATABRICKS_HOST", host), ("DATABRICKS_WAREHOUSE_ID", warehouse_id),
                               ("DATABRICKS_PAT or DATABRICKS_TOKEN", token)] if not v]
    if missing:
        raise SystemExit(f"Missing Databricks config: {', '.join(missing)}. "
                          f"Set host/warehouse in .env and export a token in your shell.")
    host = host.rstrip("/")

    print("Resolving current fiscal quarter ...")
    today_rows = dbx_query(host, warehouse_id, token,
        f"SELECT fiscal_yr_and_qtr_desc FROM {CATALOG}.aa_target.hana_ccmusage_dim_date "
        f"WHERE date_date = current_date()")
    current_q = today_rows[0]["fiscal_yr_and_qtr_desc"]
    cutoff_q = _qadd(current_q, -2)
    print(f"  current fiscal quarter = {current_q}  ->  cutoff = {cutoff_q} "
          f"(dropping the most recent 2 quarters, per the data-recency rule)")

    print("Pulling model dimension ...")
    model_dim_rows = dbx_query(host, warehouse_id, token, f"""
        SELECT model_key, geo, region, product_cloud, customer_group, product_category,
               model_category, response_variable
        FROM {CATALOG}.gmi_ml.xmx_insights
        GROUP BY ALL
    """)

    print("Pulling quarter-grain facts (aggregated server-side; can take a few seconds) ...")
    fact_rows = dbx_query(host, warehouse_id, token, f"""
        SELECT model_key, fiscal_yr_and_qtr_desc, marketing_flag, demand_category,
               channel, metric, sum(value) as value
        FROM {CATALOG}.gmi_ml.xmx_insights
        WHERE metric IN ({",".join("'%s'" % m for m in METRICS)})
          AND fiscal_yr_and_qtr_desc <= '{cutoff_q}'
        GROUP BY ALL
    """)

    cache = {
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "current_fiscal_quarter": current_q,
        "cutoff_quarter": cutoff_q,
        "model_dim": model_dim_rows,
        "facts": fact_rows,
    }
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    print(f"  wrote {os.path.relpath(CACHE, HERE)}: {len(model_dim_rows)} models, "
          f"{len(fact_rows):,} fact rows, quarters through {cutoff_q}")
    return cache


def load_cache():
    if not os.path.exists(CACHE):
        return None
    with open(CACHE, encoding="utf-8") as f:
        return json.load(f)


def load_qa_cache():
    """Optional: cache/qa_results.json, produced by `python qa.py`. Feeds the Model QA tab."""
    if not os.path.exists(QA_CACHE):
        return None
    with open(QA_CACHE, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Transform: pure precompute only. Anything tied to a UI control (quarter
# range, product filter, region filter, begin/end quarter, top-N) stays in
# the browser - see report_template.html.
# ---------------------------------------------------------------------------

def transform(cache):
    model_dim = {r["model_key"]: r for r in cache["model_dim"]}
    facts_in = [r for r in cache["facts"] if r["model_key"] in model_dim]

    quarters = sorted({r["fiscal_yr_and_qtr_desc"] for r in facts_in})
    q_index = {q: i for i, q in enumerate(quarters)}

    model_keys = sorted(model_dim.keys())
    m_index = {mk: i for i, mk in enumerate(model_keys)}

    metric_index = {m: i for i, m in enumerate(METRICS)}

    chan_label_index = {}
    chan_labels = []

    def chan_label_idx(label):
        i = chan_label_index.get(label)
        if i is None:
            i = len(chan_labels)
            chan_label_index[label] = i
            chan_labels.append(label)
        return i

    facts = []
    for r in facts_in:
        rank, cat_label, _color = category_of(r["marketing_flag"], r["demand_category"])
        label = f"{cat_label.split('. ', 1)[1]} - {humanize_channel(r['channel'])}"
        value = float(r["value"] or 0)
        facts.append([
            m_index[r["model_key"]],
            q_index[r["fiscal_yr_and_qtr_desc"]],
            rank,
            chan_label_idx(label),
            metric_index[r["metric"]],
            round(value, 4),
        ])

    models = []
    for mk in model_keys:
        d = model_dim[mk]
        models.append({
            "key": mk,
            "geo": d["geo"], "region": d["region"],
            "product_cloud": d["product_cloud"], "customer_group": d["customer_group"],
            "product_category": d["product_category"],
        })

    by_rank = {}
    for rank, label, color in CATEGORY_META.values():
        by_rank[rank] = (label, color)
    category_meta = [{"rank": r, "label": by_rank[r][0], "color": by_rank[r][1]} for r in sorted(by_rank)]

    cloud_product_pairs = sorted({(d["product_cloud"], d["product_category"]) for d in models})
    clouds = sorted({c for c, _p in cloud_product_pairs})
    product_options = [{"value": "*|*", "label": "All Products"}]
    for cloud in clouds:
        cloud_label = CLOUD_LABELS.get(cloud, cloud)
        product_options.append({"value": f"{cloud}|*", "label": f"All {cloud_label}"})
        for c, p in cloud_product_pairs:
            if c == cloud:
                product_options.append({"value": f"{c}|{p}", "label": f"{cloud_label} – {p}"})

    # NOTE: in this source data, `geo` is the coarse bucket (AMER/APAC/JPN) and
    # `region` is the finer one (NA/ANZ/JPN) - opposite of the PDF's own "Region"
    # (coarse) / "Geo" (fine) naming. The display label below reads fine (coarse),
    # e.g. "NA (AMER)", regardless of which column holds which.
    region_geo_pairs = sorted({(d["region"], d["geo"]) for d in models})
    region_options = [{"value": "*|*", "label": "All Regions"}]
    for region, geo in region_geo_pairs:
        label = f"{region} ({geo})" if region != geo else region
        region_options.append({"value": f"{region}|{geo}", "label": label})

    n_q = len(quarters)
    meta = {
        "fetched_at": cache["fetched_at"],
        "current_fiscal_quarter": cache["current_fiscal_quarter"],
        "cutoff_quarter": cache["cutoff_quarter"],
        "quarters": quarters,
        "models": models,
        "chan_labels": chan_labels,
        "metrics": METRICS,
        "categories": category_meta,
        "marketing_line_color": MARKETING_LINE_COLOR,
        "roms_line_color": ROMS_LINE_COLOR,
        "product_options": product_options,
        "region_options": region_options,
        "default_range": [max(0, n_q - 13), n_q - 1] if n_q else [0, 0],
        "default_snapshot_quarter": n_q - 1 if n_q else 0,
        "default_change": [max(0, n_q - 5), n_q - 1] if n_q else [0, 0],
    }
    return {"meta": meta, "facts": facts}


def main():
    refresh = "--refresh" in sys.argv
    cache = None
    if not refresh:
        cache = load_cache()
        if cache is None:
            print("No cache found - pulling from Databricks for the first time ...")
            refresh = True
    if refresh:
        cache = fetch_from_databricks()
    else:
        fetched = datetime.datetime.fromisoformat(cache["fetched_at"])
        age = datetime.datetime.now() - fetched
        print(f"Using cached data from {cache['fetched_at']} ({age.days}d {age.seconds // 3600}h old), "
              f"cutoff quarter {cache.get('cutoff_quarter', '?')}. Run `python build.py --refresh` to re-pull.")

    if not os.path.exists(TEMPLATE):
        raise SystemExit(f"Missing template: {TEMPLATE}")

    data = transform(cache)
    qa_cache = load_qa_cache()
    if qa_cache:
        data["qa"] = qa_cache
        print(f"  including Model QA tab data from cache/qa_results.json "
              f"({qa_cache['n_ok']}/{qa_cache['n_models']} models clean, generated {qa_cache['generated_at']})")
    else:
        print("  no cache/qa_results.json found - Model QA tab will show a placeholder. "
              "Run `python qa.py` to generate it.")

    tpl = open(TEMPLATE, encoding="utf-8").read()
    date_str = datetime.date.today().strftime("%b %d, %Y").replace(" 0", " ")
    blob = json.dumps(data, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    html = tpl.replace("/*__DATA__*/", blob).replace("__DATE__", date_str)
    if "/*__DATA__*/" in html or "__DATE__" in html:
        raise SystemExit("Template placeholder was not replaced - check report_template.html")
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\nWrote {os.path.basename(OUTPUT)} ({len(html):,} chars, generated {date_str})")
    print(f"  {len(data['meta']['quarters'])} quarters, {len(data['meta']['models'])} models, "
          f"{len(data['facts']):,} fact rows")


if __name__ == "__main__":
    main()
