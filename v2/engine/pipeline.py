"""Runs the whole flow for one or many workbooks."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import ai, classify, detectors as det, risk
from .load import load_file, load_rulepacks, load_yaml


def run_one(path, packs, mapping, fx, rules, use_llm=True):
    df, pack = load_file(path, packs, mapping, fx)
    d = pack["detectors"]
    empty_vals = mapping.get("empty_key_values", [])
    temp_cats = pack.get("temporary_categories", rules.get("temporary_categories", []))

    # 2. understand text
    df = classify.rule_classify(df, pack, rules)
    df = classify.ml_propagate(df)
    df = classify.finish(df)
    if use_llm:
        cats = [c for c, _ in pack.get("categories", []) + rules.get("categories", [])]
        df = ai.classify_texts(df, sorted(set(cats)))

    # 3. detect
    df = det.init(df)
    df["allowed_days"] = float("nan")
    if d.get("offsetting_pairs", {}).get("enabled"):
        df = det.offsetting_pairs(df, d["offsetting_pairs"], empty_vals)
    if d.get("reversal_pairs", {}).get("enabled"):
        df = det.reversal_pairs(df, d["reversal_pairs"])
    if d.get("overdue", {}).get("enabled"):
        df = det.overdue(df, d["overdue"])
    if d.get("stale_temporary", {}).get("enabled"):
        df = det.stale_temporary(df, d["stale_temporary"], temp_cats)
    partners = pd.DataFrame()
    if d.get("partner", {}).get("enabled"):
        df, partners = det.partner_checks(df, d["partner"])
        partners.insert(0, "work_group", pack["name"])
    if d.get("duplicates", {}).get("enabled"):
        df = det.duplicates(df)
    if d.get("sign_anomaly", {}).get("enabled"):
        df = det.sign_anomaly(df)
    if d.get("monthly_spike", {}).get("enabled"):
        df = det.monthly_spike(df, d["monthly_spike"])
    if d.get("ml_anomaly", {}).get("enabled"):
        df = det.ml_anomaly(df, d["ml_anomaly"])
    df = det.simple_flags(df, d, rules)

    rollf = pd.DataFrame()
    if d.get("roll_forward", {}).get("enabled"):
        rollf = det.roll_forward(df, pack)
        rollf.insert(0, "work_group", pack["name"])

    # 4. score + explain + plan
    df = risk.score(df, rules)
    df = risk.explain(df, rules, pack)
    return df, partners, rollf


def run(paths, use_llm=True):
    mapping, fx, rules = load_yaml("mappings.yaml"), load_yaml("fx_fallback.yaml"), load_yaml("rules.yaml")
    packs = load_rulepacks()
    items, partners, rolls = [], [], []
    for p in paths:
        df, pa, rf = run_one(p, packs, mapping, fx, rules, use_llm)
        items.append(df)
        partners.append(pa)
        rolls.append(rf)
    items = pd.concat(items, ignore_index=True)
    summary = risk.account_summary(items)
    summary["summary_source"] = "template"
    if use_llm:
        summary = ai.draft_summaries(summary)
    plan = risk.action_plan(items)
    return {
        "items": items,
        "accounts": summary,
        "action_plan": plan,
        "partners": pd.concat([p for p in partners if not p.empty], ignore_index=True) if any(not p.empty for p in partners) else pd.DataFrame(),
        "roll_forward": pd.concat([r for r in rolls if not r.empty], ignore_index=True) if any(not r.empty for r in rolls) else pd.DataFrame(),
    }


ITEM_EXPORT_COLS = [
    "item_id", "work_group", "erp", "cocd", "account", "account_desc", "doc_no", "item", "doc_type",
    "posting_date", "age_days", "allowed_days", "amount_lc", "curr_lc", "amount_eur", "user", "is_manual",
    "partner", "text", "header_text", "assignment", "reference", "category", "category_source",
    "recon_class", "issue_type", "risk_score", "risk_level", "reason_text", "explained_by", "match_group",
    "owner_team", "action", "due_date", "suggested_comment", "new_this_week",
]


def save(res: dict, out_dir: str | Path):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    items = res["items"].copy()
    items["reasons"] = items["reasons"].apply(lambda r: " || ".join(r))
    items.to_parquet(out / "items.parquet", index=False)
    for k in ["accounts", "action_plan", "partners", "roll_forward"]:
        if not res[k].empty:
            res[k].to_parquet(out / f"{k}.parquet", index=False)
    write_excel(res, out / "Reconciliation_Results.xlsx")


def write_excel(res: dict, path):
    items = res["items"]
    kpi = kpis(items)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(list(kpi.items()), columns=["KPI", "Value"]).to_excel(xw, sheet_name="KPIs", index=False)
        res["action_plan"].to_excel(xw, sheet_name="Action plan", index=False)
        res["accounts"].to_excel(xw, sheet_name="Account summary", index=False)
        top = items.sort_values("risk_score", ascending=False)
        top[[c for c in ITEM_EXPORT_COLS if c in top]].to_excel(xw, sheet_name="All items (scored)", index=False)
        if not res["partners"].empty:
            res["partners"].to_excel(xw, sheet_name="IC partner check", index=False)
        if not res["roll_forward"].empty:
            res["roll_forward"].to_excel(xw, sheet_name="FA roll-forward", index=False)
        for ws in xw.book.worksheets:
            ws.freeze_panes = "A2"
            for col in ws.columns:
                width = min(60, max(10, max(len(str(c.value or "")) for c in col[:200]) + 2))
                ws.column_dimensions[col[0].column_letter].width = width


def kpis(items: pd.DataFrame) -> dict:
    auto = items["recon_class"].isin(["Explained (nets to zero)", "List component", "Timing item"])
    rai = items["recon_class"] == "Required Adjustment Item"
    return {
        "Open items": len(items),
        "Gross open value (EUR)": round(items["abs_eur"].sum()),
        "Items explained automatically": int(auto.sum()),
        "Items explained automatically (%)": round(auto.mean() * 100, 1),
        "Required Adjustment Items": int(rai.sum()),
        "Value needing correction (EUR)": round(items.loc[rai, "abs_eur"].sum()),
        "High-risk items": int((items["risk_level"] == "High").sum()),
        "Items needing human review": int((items["recon_class"] == "Needs review").sum()),
        "Lines classified by rules": int((items["category_source"] == "rule").sum()),
        "Lines classified by ML": int(items["category_source"].astype(str).str.startswith("ML").sum()),
        "Lines classified by LLM": int((items["category_source"] == "LLM").sum()),
    }
