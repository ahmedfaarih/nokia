"""Step 4 - Score, classify (List / Timing / RAI) and build the action plan."""
from __future__ import annotations

import numpy as np
import pandas as pd


def _merged_owners(rules, pack):
    owners = dict(rules.get("owners", {}))
    owners.update(pack.get("owners", {}) or {})
    return owners


def score(df: pd.DataFrame, rules: dict) -> pd.DataFrame:
    rk, flags = rules["risk"], rules["flags"]
    # materiality: percentile of |EUR| inside the working group (0..1)
    pct = df.groupby("work_group")["abs_eur"].rank(pct=True)
    df["score_materiality"] = (pct * rk["materiality_max"]).round(1)
    allowed = df["allowed_days"].fillna(365).clip(lower=30)
    df["score_age"] = (np.clip(df["age_days"].clip(lower=0) / allowed, 0, 3) / 3 * rk["age_max"]).round(1)
    fs = pd.Series(0.0, index=df.index)
    for name, meta in flags.items():
        col = f"flag_{name}"
        if col in df:
            fs += df[col].astype(float) * meta["weight"]
    df["score_flags"] = fs.clip(upper=rk["flags_cap"])
    # explained items (net-zero pairs) carry no flag risk
    df.loc[df["explained_by"].notna(), "score_flags"] = 0
    df["risk_score"] = (df["score_materiality"] + df["score_age"] + df["score_flags"]).round(0).astype(int)
    df.loc[df["explained_by"].notna(), "risk_score"] = (df["score_materiality"] * 0.3).round(0).astype(int)
    df["risk_level"] = np.select([df["risk_score"] >= rk["high_threshold"], df["risk_score"] >= rk["medium_threshold"]],
                                 ["High", "Medium"], default="Low")
    return df


def explain(df: pd.DataFrame, rules: dict, pack: dict) -> pd.DataFrame:
    flags = rules["flags"]
    owners = _merged_owners(rules, pack)

    def render(reasons):
        out = []
        for r in reasons:
            name, _, det = r.partition("::")
            label = flags.get(name, {}).get("label", name)
            out.append(f"{label} ({det})" if det else label)
        return out

    df["reason_text"] = df["reasons"].apply(lambda r: "; ".join(render(r)))

    def primary(reasons):
        best, w = None, -1
        for r in reasons:
            name = r.split("::")[0]
            meta = flags.get(name, {})
            if meta.get("weight", 0) > w:
                best, w = meta.get("issue"), meta.get("weight", 0)
        return best or "No issue"

    df["issue_type"] = df["reasons"].apply(primary)
    df.loc[df["explained_by"].notna(), "issue_type"] = "No issue"

    rai_flags = [n for n, m in flags.items() if m.get("rai")]
    is_rai = pd.Series(False, index=df.index)
    for n in rai_flags:
        if f"flag_{n}" in df:
            is_rai |= df[f"flag_{n}"]
    list_c = set(pack.get("list_categories", []))
    timing_c = set(pack.get("timing_categories", []))
    df["recon_class"] = np.select(
        [df["explained_by"].notna(), is_rai, df["category"].isin(list_c), df["category"].isin(timing_c)],
        ["Explained (nets to zero)", "Required Adjustment Item", "List component", "Timing item"],
        default="Needs review")

    df["owner_team"] = df["issue_type"].map(lambda i: owners.get(i, {}).get("team", "Preparer"))
    df["action"] = df["issue_type"].map(lambda i: owners.get(i, {}).get("action", "Review"))
    rd = df["recon_date"]
    days = np.select([df["risk_level"] == "High", df["risk_level"] == "Medium"], [14, 30], default=60)
    df["due_date"] = (rd + pd.to_timedelta(days, unit="D")).dt.date
    df.loc[df["recon_class"].isin(["Explained (nets to zero)", "List component", "Timing item"]) &
           (df["issue_type"] == "No issue"), "due_date"] = pd.NaT
    df["suggested_comment"] = df.apply(_comment, axis=1)
    df["new_this_week"] = (df["recon_date"] - df["posting_date"]).dt.days.between(0, 7)
    return df


def _comment(r) -> str:
    """Standard, reviewer-ready comment (template; the LLM version can replace it)."""
    amt = f"{r['amount_lc']:,.2f} {r['curr_lc']}"
    base = f"{r['category']} posted {r['posting_date']:%d.%m.%Y} (doc {r['doc_no']}), {amt}, {int(r['age_days'])} days old."
    if r["recon_class"] == "Explained (nets to zero)":
        return f"{base} {r['explained_by']}; no balance impact."
    if r["recon_class"] == "Required Adjustment Item":
        return f"{base} RAI - {r['issue_type']}: {r['action']} Owner: {r['owner_team']}, due {r['due_date']}."
    if r["recon_class"] == "Timing item":
        return f"{base} Timing item - expected to clear in the normal cycle."
    if r["recon_class"] == "List component":
        return f"{base} Long-term balance supported by sub-ledger / calculation."
    return f"{base} Not explained automatically - preparer to review."


def account_summary(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby(["work_group", "erp", "cocd", "account", "account_desc", "curr_lc"], dropna=False)
    s = g.agg(items=("item_id", "size"),
              balance_lc=("amount_lc", "sum"),
              balance_eur=("amount_eur", "sum"),
              gross_eur=("abs_eur", "sum"),
              rai_items=("recon_class", lambda v: (v == "Required Adjustment Item").sum()),
              needs_review=("recon_class", lambda v: (v == "Needs review").sum()),
              high_risk=("risk_level", lambda v: (v == "High").sum()),
              max_risk=("risk_score", "max"),
              oldest_days=("age_days", "max")).reset_index()
    rai_eur = df[df["recon_class"] == "Required Adjustment Item"].groupby(
        ["work_group", "erp", "cocd", "account"])["abs_eur"].sum().rename("rai_eur")
    auto = df[df["recon_class"].isin(["Explained (nets to zero)", "List component", "Timing item"])].groupby(
        ["work_group", "erp", "cocd", "account"])["abs_eur"].sum().rename("auto_explained_eur")
    top_issue = (df[df["issue_type"] != "No issue"].groupby(["work_group", "erp", "cocd", "account", "issue_type"])["abs_eur"]
                 .sum().reset_index().sort_values("abs_eur", ascending=False)
                 .drop_duplicates(["work_group", "erp", "cocd", "account"])
                 .set_index(["work_group", "erp", "cocd", "account"])["issue_type"].rename("top_issue"))
    s = s.join(rai_eur, on=["work_group", "erp", "cocd", "account"]).join(
        auto, on=["work_group", "erp", "cocd", "account"]).join(top_issue, on=["work_group", "erp", "cocd", "account"])
    s[["rai_eur", "auto_explained_eur"]] = s[["rai_eur", "auto_explained_eur"]].fillna(0)
    s["auto_explained_pct"] = (s["auto_explained_eur"] / s["gross_eur"].replace(0, np.nan) * 100).round(0).fillna(0)
    s["top_issue"] = s["top_issue"].fillna("None")
    s["account_risk"] = np.select([s["high_risk"] > 0, s["rai_items"] > 0], ["High", "Medium"], default="Low")
    s["reviewer_summary"] = s.apply(_acct_comment, axis=1)
    return s.sort_values(["rai_eur", "max_risk"], ascending=False).reset_index(drop=True)


def _acct_comment(r) -> str:
    txt = (f"{r['cocd']} / {r['account']} {r['account_desc']} ({r['erp']}): balance {r['balance_lc']:,.0f} {r['curr_lc']} "
           f"(EUR {r['balance_eur']:,.0f}) over {r['items']} open items. "
           f"{r['auto_explained_pct']:.0f}% of gross value explained automatically.")
    if r["rai_items"]:
        txt += (f" {r['rai_items']} items (EUR {r['rai_eur']:,.0f}) need correction - mainly '{r['top_issue']}'. "
                f"Oldest item {int(r['oldest_days'])} days.")
    else:
        txt += " No adjustment needed."
    if r["needs_review"]:
        txt += f" {r['needs_review']} items could not be explained automatically."
    return txt


def action_plan(df: pd.DataFrame) -> pd.DataFrame:
    """One line per issue cluster (entity + account + issue type)."""
    a = df[df["recon_class"].isin(["Required Adjustment Item", "Needs review"])].copy()
    if a.empty:
        return pd.DataFrame()
    rev = a["recon_class"] == "Needs review"
    a.loc[rev, "issue_type"] = "Not explained automatically"
    a.loc[rev, "owner_team"] = "GL Accounting (preparer)"
    a.loc[rev, "action"] = "Check the items and write a reconciliation comment."
    a.loc[rev, "due_date"] = (a.loc[rev, "recon_date"] + pd.Timedelta(days=30)).dt.date
    keys = ["work_group", "erp", "cocd", "account", "account_desc", "issue_type", "owner_team", "action"]
    p = a.groupby(keys, dropna=False).agg(
        items=("item_id", "size"), value_eur=("abs_eur", "sum"), net_eur=("amount_eur", "sum"),
        max_risk=("risk_score", "max"), oldest_days=("age_days", "max"),
        due_date=("due_date", "min"),
        sap_documents=("doc_no", lambda v: ", ".join(sorted(set(map(str, v)))[:15]) + (" ..." if v.nunique() > 15 else ""))
    ).reset_index()
    p["priority"] = np.select([p["max_risk"] >= 60, p["max_risk"] >= 35], ["P1", "P2"], default="P3")
    p["status"] = "Open"
    return p.sort_values(["priority", "value_eur"], ascending=[True, False]).reset_index(drop=True)
