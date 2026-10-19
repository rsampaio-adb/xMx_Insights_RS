# xMX Insights Dashboard

Self-contained interactive report recreating six views from `MP&A Insights.pdf` (Base &
Marketing ARR Contribution, GNARR Contribution Mix, Marketing Spend & RoMS Trend,
Contribution Breakdown, Channel Spend & Performance, Contribution Change), backed by
`dpaas_uccatalog_prd.gmi_ml.xmx_insights`.

## Regenerate the report

```bash
python build.py            # render index.html from the local cache (no Databricks call)
python build.py --refresh  # re-pull from Databricks into the cache, then render
```

`index.html` is the deliverable — open it directly (double-click) or serve it locally:

```bash
python -m http.server 8124
```

## Data

- Cached at `cache/xmx_facts.json` (git-ignored) — quarter-grain aggregate of `arr_contribution`,
  `unit_contribution`, and `spend`, refreshed on demand via `--refresh`.
- Only quarters through **2 quarters behind the current fiscal quarter** are pulled — the most
  recent 1–2 quarters of model output are still settling. "Current fiscal quarter" is resolved
  from `aa_target.hana_ccmusage_dim_date`, not a hardcoded calendar mapping.
- `.env` holds `DATABRICKS_HOST` / `DATABRICKS_WAREHOUSE_ID` (not secret). The access token comes
  from the `DATABRICKS_PAT` or `DATABRICKS_TOKEN` environment variable — never stored on disk.

## Model QA

```bash
python qa.py    # checks the latest model_versions against the tables xmx_insights joins,
                 # writes cache/qa_results.json (feeds the "Model QA" tab) and
                 # "xMX Insights QA Report.xlsx" (full detail)
python build.py # pick up cache/qa_results.json into the Model QA tab
```

For every model_key that is the latest `model_version` per `(model_category, region,
product_category)` in `gmi_ml.model_manager` — the same set `xmx_insights` surfaces — `qa.py`
checks:

1. **Model coverage** — the model_key is present in `gmi_ml.channel_contribution` (MMM output),
   `gmi_ml.raw_data_xmx_models` (spend/raw data), and `gmi_ml.variable_lineage` (variable dim).
   Missing from channel_contribution/raw_data means that whole fact source is silently absent from
   `xmx_insights` for the model; missing from variable_lineage means every row for the model gets
   a `NULL` category/channel.
2. **Variable coverage** — every variable channel_contribution uses for a latest model also has a
   variable_lineage row for that model_key. The view joins channel_contribution to
   variable_lineage with a `LEFT JOIN`, so an unmatched variable survives with a `NULL` category →
   lands in the dashboard's "5. Other Marketing – Other" bucket instead of its real category —
   this is flagged as an issue. raw_data_xmx_models variables missing from variable_lineage (joined
   with an `INNER JOIN`, so unmatched ones are silently dropped) are reported as an informational
   note only, never an issue — raw_data_xmx_models collects more series than any one model
   actually uses, so a variable being absent from a given model's variable_lineage is normal, not
   a data-quality defect.
3. **Channel population** — every variable_lineage row for a latest model has a non-blank
   `channel`, else it renders as "\<Category\> – Other".

Missing arr/unit-contribution variables from channel_contribution are treated as issues (they
change what the six dashboard views show); missing raw_data_xmx_models variables (spend,
impressions, clicks, etc.) are always informational notes only, per the reasoning above.

## Caveat

Not all 10 models in `xmx_insights` refresh on the same cadence (the NA Creative Cloud models are
tagged with a `q2` refresh cycle; ANZ/JPN Acrobat are tagged `q3`). At an "All Products" scope the
most recent kept quarter can look thin relative to prior quarters until the laggard models catch
up — that's a source-data characteristic, not a bug. Use the quarter picker to check an earlier
quarter if a view looks unexpectedly light.
