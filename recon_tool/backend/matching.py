"""
Core reconciliation logic: load a sheet, find duplicate/clearing pairs that net
to zero, categorize everything else, and build a draft comment per open item.

Matching logic (clearing-match / transfer-match key selection, placeholder-
domination check) is carried over unchanged from the validated eda_v2.py
analysis - see /private/tmp/.../scratchpad/eda_v2.py and eda_report_v2.md.
"""
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

DATA_DIR = Path("/Users/ahmedfaarih/Code/Nokia")

SOURCES = {
    "fixed_assets-BPP": ("Fixed Assets", "BPP", "Hackathon_Fixed_Assets_Nokia.xlsx", "non_oim"),
    "fixed_assets-P20": ("Fixed Assets", "P20", "Hackathon_Fixed_Assets_Nokia.xlsx", "non_oim"),
    "intercompany-P20": ("Intercompany", "P20", "Hackathon_Intercompany_Nokia.xlsx", "clearing"),
    "intercompany-BPP": ("Intercompany", "BPP", "Hackathon_Intercompany_Nokia.xlsx", "clearing"),
    "intercompany-P10": ("Intercompany", "P10", "Hackathon_Intercompany_Nokia.xlsx", "clearing"),
    "payroll-P20": ("Payroll", "P20", "Hackathon_Payroll_Nokia.xlsx", "clearing"),
    "payroll-BPP": ("Payroll", "BPP", "Hackathon_Payroll_Nokia.xlsx", "clearing"),
}


def col(df, *candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None


def dominant_placeholder_ratio(series: pd.Series) -> float:
    s = series.dropna().astype(str)
    s = s[s.str.strip() != ""]
    if s.empty:
        return 1.0
    return s.value_counts(normalize=True).iloc[0]


def choose_match_key(df) -> Optional[str]:
    cands = []
    for name in ["Assignment", "Reference"]:
        if name in df.columns:
            ratio = dominant_placeholder_ratio(df[name])
            cands.append((ratio, name))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    best_ratio, best_name = cands[0]
    return best_name if best_ratio < 0.8 else None


def load_sheet(sheet_id: str) -> pd.DataFrame:
    wg, erp, filename, mode = SOURCES[sheet_id]
    raw = pd.read_excel(DATA_DIR / filename, sheet_name=erp, engine="openpyxl")
    raw.insert(0, "_row_id", raw.index.astype(str) + "-" + sheet_id)
    return raw


def get_columns(df: pd.DataFrame) -> dict:
    return dict(
        acct_col=col(df, "Account"),
        cocd_col=col(df, "CoCd"),
        amt_col=col(df, "Amount in Local Currency", "Amount in LC"),
        oim_col=col(df, "OIM/Non-OIM/Subledger", "GL Account Categories"),
        due_col=col(df, "Due/Not Due", "Due/Not Due Aged/Not Aged"),
        ref_col=col(df, "Reference"),
        assign_col=col(df, "Assignment"),
        date_col=col(df, "Pstng Date"),
        doc_col=col(df, "DocumentNo"),
        text_col=col(df, "Text"),
        user_col=col(df, "User Name"),
        trprt_col=col(df, "Tr.Prt", "Reprt. Customer"),
    )


def find_matched_pairs(df: pd.DataFrame, mode: str, c: dict):
    """Returns (matched_row_ids: set[str], groups_info: list[dict])."""
    matched_ids: set[str] = set()
    groups_info = []

    amt_col = c["amt_col"]
    date_col = c["date_col"]
    if amt_col is None or date_col is None:
        return matched_ids, groups_info
    df = df.copy()
    df[amt_col] = pd.to_numeric(df[amt_col], errors="coerce")

    if mode == "non_oim":
        ref_col, cocd_col = c["ref_col"], c["cocd_col"]
        if not (ref_col and cocd_col):
            return matched_ids, groups_info
        base = df.dropna(subset=[ref_col, amt_col, date_col])
        base = base[base[ref_col].astype(str).str.strip() != ""]
        for key, g in base.groupby([ref_col, date_col]):
            if len(g) < 2:
                continue
            is_match = g[cocd_col].nunique() >= 2 and abs(g[amt_col].sum()) < 1.0
            groups_info.append({
                "key": f"{key[0]} / {key[1]}",
                "matched": bool(is_match),
                "row_ids": g["_row_id"].tolist(),
            })
            if is_match:
                matched_ids.update(g["_row_id"].tolist())
    else:  # clearing
        key_name = choose_match_key(df)
        acct_col = c["acct_col"]
        if not (key_name and acct_col):
            return matched_ids, groups_info
        base = df.dropna(subset=[key_name, amt_col, date_col])
        base = base[base[key_name].astype(str).str.strip() != ""]
        for key, g in base.groupby([acct_col, key_name, date_col]):
            if len(g) < 2:
                continue
            is_match = abs(g[amt_col].sum()) < 1.0
            groups_info.append({
                "key": f"{key[0]} / {key[1]} / {key[2]}",
                "matched": bool(is_match),
                "row_ids": g["_row_id"].tolist(),
            })
            if is_match:
                matched_ids.update(g["_row_id"].tolist())

    return matched_ids, groups_info


def _fmt_id(val) -> str:
    """Account/doc numbers come in as floats from Excel (e.g. 2340000000.0);
    render whole numbers without the trailing .0."""
    if pd.isna(val):
        return "N/A"
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def age_in_days(posting_date, as_of=None) -> Optional[int]:
    if pd.isna(posting_date):
        return None
    as_of = as_of or pd.Timestamp.now()
    try:
        return (as_of - pd.Timestamp(posting_date)).days
    except (ValueError, TypeError):
        return None


def categorize_row(row: pd.Series, c: dict) -> str:
    due_col, text_col, date_col = c["due_col"], c["text_col"], c["date_col"]
    has_text = text_col and str(row.get(text_col, "")).strip() != ""

    is_aged = None  # None = unknown, use the age fallback below
    if due_col and pd.notna(row.get(due_col)):
        val = str(row[due_col]).lower()
        is_aged = "aged" in val and "not aged" not in val

    if is_aged is None and date_col:
        days = age_in_days(row.get(date_col))
        if days is not None:
            is_aged = days > 365

    if is_aged:
        return "Aged >365 days - needs escalation"
    if not has_text:
        return "Awaiting counterparty / no comment on file"
    return "Timing difference - in progress"


def draft_comment_template(row: pd.Series, c: dict, category: str) -> str:
    amt = row.get(c["amt_col"])
    date = row.get(c["date_col"])
    ref = row.get(c["ref_col"]) if c["ref_col"] else row.get(c["assign_col"])
    acct = _fmt_id(row.get(c["acct_col"]))
    doc = _fmt_id(row.get(c["doc_col"]))

    amt_str = f"{amt:,.2f}" if pd.notna(amt) else "N/A"
    date_str = str(date)[:10] if pd.notna(date) else "N/A"
    days = age_in_days(date)
    age_str = f"{days:,} days old" if days is not None else "age unknown"

    if category.startswith("Aged"):
        return (
            f"Open item of {amt_str} on account {acct} (doc {doc}, posted {date_str}, "
            f"ref {ref}) is {age_str}, beyond the 365-day threshold. Needs escalation to "
            f"counterparty / review for write-off or adjustment."
        )
    if category.startswith("Awaiting"):
        return (
            f"Item of {amt_str} on account {acct} (doc {doc}, posted {date_str}, "
            f"ref {ref}, {age_str}) has no counterparty confirmation or comment on "
            f"file yet. Follow up with counterparty to confirm status."
        )
    return (
        f"Item of {amt_str} on account {acct} (doc {doc}, posted {date_str}, "
        f"ref {ref}) is {age_str}, within the normal timing-difference window. "
        f"No action needed yet; re-check next cycle."
    )


def _row_base(row: pd.Series, c: dict) -> dict:
    return {
        "account": _fmt_id(row.get(c["acct_col"])) if c["acct_col"] else None,
        "amount": None if c["amt_col"] is None or pd.isna(row.get(c["amt_col"])) else float(row.get(c["amt_col"])),
        "posting_date": str(row.get(c["date_col"]))[:10] if c["date_col"] and pd.notna(row.get(c["date_col"])) else None,
        "reference": str(row.get(c["ref_col"])) if c["ref_col"] and pd.notna(row.get(c["ref_col"])) else None,
        "age_days": age_in_days(row.get(c["date_col"])) if c["date_col"] else None,
    }


def draft_matched_comment(row: pd.Series, c: dict, df_by_id: pd.DataFrame, group_row_ids: list[str]) -> str:
    amt = row.get(c["amt_col"])
    amt_str = f"{amt:,.2f}" if pd.notna(amt) else "N/A"
    others = [rid for rid in group_row_ids if rid != row["_row_id"]]
    other_docs = []
    for rid in others:
        if rid in df_by_id.index:
            other_docs.append(_fmt_id(df_by_id.loc[rid, c["doc_col"]]) if c["doc_col"] else rid)
    docs_str = ", ".join(other_docs) if other_docs else "its counterpart"
    return (
        f"Auto-matched: nets to zero with doc {docs_str} ({len(group_row_ids)} lines in group). "
        f"This line's amount: {amt_str}. No action needed - auto-resolved."
    )


def detect_mode(df: pd.DataFrame, c: dict) -> str:
    """For uploaded files where the mode isn't known in advance: Non-OIM
    accounts (e.g. Fixed Assets) don't have a clearing concept, so they use
    transfer-matching instead of clearing-matching. Same rule as eda_v2.py."""
    oim_col = c["oim_col"]
    if oim_col and df[oim_col].astype(str).str.contains("Non-OIM", case=False, na=False).all():
        return "non_oim"
    return "clearing"


def run_reconciliation_on_df(df: pd.DataFrame, mode: str, wg: str, erp: str, sheet_id: str) -> dict:
    c = get_columns(df)

    matched_ids, groups_info = find_matched_pairs(df, mode, c)
    df_by_id = df.set_index("_row_id", drop=False)

    # assign a stable group_id to each matched group, and map row_id -> group
    matched_groups = [g for g in groups_info if g["matched"]]
    row_to_group: dict[str, dict] = {}
    for i, g in enumerate(matched_groups):
        group_id = f"{sheet_id}-grp-{i}"
        for rid in g["row_ids"]:
            row_to_group[rid] = {"group_id": group_id, "row_ids": g["row_ids"]}

    matched_rows = []
    open_rows = []
    for _, row in df.iterrows():
        rid = row["_row_id"]
        base = _row_base(row, c)
        if rid in row_to_group:
            grp = row_to_group[rid]
            matched_rows.append({
                "row_id": rid,
                "status": "matched",
                "group_id": grp["group_id"],
                **base,
                "category": "Matched - net to zero",
                "draft_comment": draft_matched_comment(row, c, df_by_id, grp["row_ids"]),
            })
        else:
            category = categorize_row(row, c)
            open_rows.append({
                "row_id": rid,
                "status": "open",
                "group_id": None,
                **base,
                "category": category,
                "draft_comment": draft_comment_template(row, c, category),
            })

    # matched groups first (grouped together), then open items
    matched_rows.sort(key=lambda r: r["group_id"])
    rows = matched_rows + open_rows

    total_rows = len(df)
    n_matched = len(matched_rows)
    from collections import Counter
    cat_counts = Counter(r["category"] for r in open_rows)

    return {
        "sheet_id": sheet_id,
        "working_group": wg,
        "erp": erp,
        "total_rows": total_rows,
        "matched_rows": n_matched,
        "matched_pct": round(100 * n_matched / total_rows, 1) if total_rows else 0.0,
        "open_rows": total_rows - n_matched,
        "category_counts": dict(cat_counts),
        "candidate_groups": len(groups_info),
        "matched_groups": len(matched_groups),
        "rows": rows,
    }


def run_reconciliation(sheet_id: str) -> dict:
    wg, erp, filename, mode = SOURCES[sheet_id]
    df = load_sheet(sheet_id)
    return run_reconciliation_on_df(df, mode, wg, erp, sheet_id)
