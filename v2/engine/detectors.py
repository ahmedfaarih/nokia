"""Step 3 - Detect: shared, config-driven checks that run on any working group.

Every detector adds a boolean column flag_<name> and a human-readable reason to
the item's 'reasons' list. Nothing here is specific to one file - rule packs
decide which detectors run and with which parameters.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

BASE = ["erp", "cocd", "account", "curr_dc"]


def init(df: pd.DataFrame) -> pd.DataFrame:
    df["reasons"] = [[] for _ in range(len(df))]
    df["explained_by"] = pd.Series(pd.NA, index=df.index, dtype="string")
    df["match_group"] = pd.Series(pd.NA, index=df.index, dtype="string")
    return df


def add_flag(df, mask, name, detail=None):
    mask = mask.fillna(False) if hasattr(mask, "fillna") else mask
    col = f"flag_{name}"
    if col not in df:
        df[col] = False
    df.loc[mask, col] = True
    idx = df.index[mask]
    if detail is None:
        for i in idx:
            df.at[i, "reasons"].append(name)
    else:
        det = detail if isinstance(detail, pd.Series) else pd.Series(detail, index=df.index)
        for i in idx:
            df.at[i, "reasons"].append(f"{name}::{det.at[i]}")
    return df


def _valid_key(s: pd.Series, empty_vals) -> pd.Series:
    low = s.astype("string").str.strip().str.lower()
    return low.notna() & ~low.isin([e.lower() for e in empty_vals])


# --------------------------------------------------------------------------
def offsetting_pairs(df, cfg, empty_vals):
    """Groups of open items sharing a key whose amounts net to zero."""
    treat = cfg.get("treat_as", "issue")
    matched = df["match_group"].notna()
    for key in cfg.get("keys", []):
        ok = _valid_key(df[key], empty_vals) & ~matched & (df["amount_dc"] != 0)
        sub = df[ok]
        if sub.empty:
            continue
        tmp = sub.assign(_abs=sub["amount_dc"].abs(), _pos=sub["amount_dc"] > 0, _neg=sub["amount_dc"] < 0)
        stats = tmp.groupby(BASE + [key], dropna=False).agg(
            n=("amount_dc", "size"), s=("amount_dc", "sum"), gross=("_abs", "sum"),
            pos=("_pos", "any"), neg=("_neg", "any"))
        good = stats[(stats.n >= 2) & (stats.n <= 60) & stats.pos & stats.neg &
                     (stats.s.abs() <= np.maximum(0.011, stats.gross * 0.0005))]
        if good.empty:
            continue
        keys = sub.set_index(BASE + [key]).index
        hit = keys.isin(good.index)
        rows = sub.index[hit]
        gid = (key + ":" + sub.loc[rows, key].astype(str))
        df.loc[rows, "match_group"] = gid
        matched = df["match_group"].notna()
        if treat == "explained":
            df.loc[rows, "explained_by"] = f"Nets to zero with other items (same {key})"
        else:
            add_flag(df, df.index.isin(rows), "offset_uncleared", pd.Series(f"key = {key}", index=df.index))

    if cfg.get("amount_only"):
        # vectorised: pair the k-th debit with the k-th credit of the same absolute amount
        rem = df[df["match_group"].isna() & (df["amount_dc"] != 0)].copy()
        rem["absamt"] = rem["amount_dc"].abs().round(2)
        rem["sign"] = (rem["amount_dc"] > 0).astype(int)
        rem = rem.sort_values("posting_date")
        g = BASE + ["absamt"]
        rem["gid"] = rem.groupby(g, dropna=False).ngroup()
        rem["rank"] = rem.groupby(["gid", "sign"]).cumcount()
        n_pos = rem.groupby("gid")["sign"].transform("sum")
        n_neg = rem.groupby("gid")["sign"].transform("size") - n_pos
        hit = rem["rank"] < pd.concat([n_pos, n_neg], axis=1).min(axis=1)
        rows = rem.index[hit]
        if len(rows):
            df.loc[rows, "match_group"] = ("amount:" + rem.loc[rows, "gid"].astype(str) + "-"
                                           + rem.loc[rows, "rank"].astype(str))
            add_flag(df, df.index.isin(rows), "offset_amount_only")
    return df


def reversal_pairs(df, cfg):
    treat = cfg.get("treat_as", "issue")
    key = df["cocd"].astype(str) + "|" + df["doc_no"].astype(str)
    open_docs = set(key)
    rkey = df["cocd"].astype(str) + "|" + df["reversal"].astype(str)
    has = df["reversal"].notna() & (df["reversal"] != df["doc_no"]) & rkey.isin(open_docs)
    reversed_docs = set(rkey[has])
    both = has | key.isin(reversed_docs)
    if treat == "explained":
        df.loc[both & df["explained_by"].isna(), "explained_by"] = "Document and its reversal both open - net zero"
        grp_key = rkey.where(has, key)          # reversing doc points to original
        df.loc[both & df["match_group"].isna(), "match_group"] = "reversal:" + grp_key[both]
    else:
        add_flag(df, both, "reversed_still_open")
    return df


def overdue(df, cfg):
    allowed = pd.Series(float(cfg.get("default_allowed_days", 365)), index=df.index)
    if cfg.get("use_allowed_ageing_column"):
        allowed = df["allowed_days_col"].fillna(allowed)
    df["allowed_days"] = allowed.where(~df["no_overdue_rule"], np.nan)
    m = df["allowed_days"].notna() & (df["age_days"] > df["allowed_days"]) & df["explained_by"].isna()
    det = df["age_days"].astype("Int64").astype(str) + " days vs " + df["allowed_days"].astype("Int64").astype(str) + " allowed"
    return add_flag(df, m, "overdue", det)


def stale_temporary(df, cfg, temp_categories):
    max_days = cfg.get("max_days", 365)
    df["allowed_days"] = df.get("allowed_days", pd.Series(np.nan, index=df.index))
    df.loc[df["category"].isin(temp_categories), "allowed_days"] = max_days
    m = df["category"].isin(temp_categories) & (df["age_days"] > max_days) & df["explained_by"].isna()
    det = df["category"] + ", " + df["age_days"].astype("Int64").astype(str) + " days old"
    return add_flag(df, m, "stale_temporary", det)


def partner_checks(df, cfg):
    """Missing partner + reciprocal balance check between entities in the extract."""
    add_flag(df, df["partner"].isna() & df["explained_by"].isna(), "missing_partner")
    rx = re.compile(cfg.get("partner_entity_pattern", r"^N?(C\d{3})$"))
    df["partner_entity"] = df["partner"].astype("string").str.extract(rx.pattern)[0]
    entities = set(df["cocd"].dropna())
    bal = (df[df["partner_entity"].notna()]
           .groupby(["cocd", "partner_entity"])["amount_eur"].agg(["sum", "size"]).reset_index())
    bal.columns = ["entity", "partner_entity", "entity_balance_eur", "entity_items"]
    rec = bal.rename(columns={"entity": "partner_entity", "partner_entity": "entity",
                              "entity_balance_eur": "partner_balance_eur", "entity_items": "partner_items"})
    pairs = bal.merge(rec, on=["entity", "partner_entity"], how="left")
    pairs["partner_in_extract"] = pairs["partner_entity"].isin(entities)
    pairs["difference_eur"] = pairs["entity_balance_eur"] + pairs["partner_balance_eur"].fillna(0)
    tol = cfg.get("tolerance_eur", 100)
    pairs["status"] = np.select(
        [~pairs["partner_in_extract"], pairs["partner_balance_eur"].isna(),
         pairs["difference_eur"].abs() <= tol],
        ["Partner entity not in extract", "One-sided in extract (partner side not found)", "Agrees"],
        default="Mismatch")
    bad = pairs[pairs["status"] == "Mismatch"]
    if not bad.empty:
        k = df["cocd"].astype(str) + "|" + df["partner_entity"].astype(str)
        badk = set(bad["entity"] + "|" + bad["partner_entity"])
        diff = dict(zip(bad["entity"] + "|" + bad["partner_entity"], bad["difference_eur"]))
        det = k.map(lambda x: f"difference EUR {diff.get(x, 0):,.0f}")
        add_flag(df, k.isin(badk) & (df["abs_eur"] > 0), "ic_difference", det)
    return df, pairs


def duplicates(df):
    # same amount, date, text AND reference, but booked on different documents
    cols = ["erp", "cocd", "account", "curr_dc", "amount_dc", "posting_date", "full_text", "reference"]
    sub = df[(df["amount_dc"] != 0)]
    g = sub.groupby(cols, dropna=False)["doc_no"].transform("nunique")
    m = pd.Series(False, index=df.index)
    m.loc[sub.index] = g > 1
    return add_flag(df, m & df["explained_by"].isna(), "duplicate")


def sign_anomaly(df, min_eur=1000):
    grp = df.groupby(["erp", "cocd", "account"])
    credit_share = grp["amount_eur"].transform(lambda v: v.clip(upper=0).abs().sum() / max(v.abs().sum(), 1e-9))
    dominant_credit = credit_share >= 0.9
    dominant_debit = credit_share <= 0.1
    odd = ((dominant_credit & (df["amount_eur"] > 0)) | (dominant_debit & (df["amount_eur"] < 0)))
    m = odd & (df["abs_eur"] >= min_eur) & df["match_group"].isna() & (df["category"] != "Reversal")
    det = np.where(dominant_credit, "debit on a credit-balance account", "credit on a debit-balance account")
    return add_flag(df, m, "sign_anomaly", pd.Series(det, index=df.index))


def monthly_spike(df, cfg):
    factor, min_eur = cfg.get("factor", 3.0), cfg.get("min_eur", 5000)
    ym = df["posting_date"].dt.to_period("M")
    tot = df.assign(ym=ym).groupby(["erp", "cocd", "account", "ym"])["abs_eur"].sum().rename("month_total").reset_index()
    med = tot.groupby(["erp", "cocd", "account"])["month_total"].agg(["median", "size"]).reset_index()
    tot = tot.merge(med, on=["erp", "cocd", "account"])
    spikes = tot[(tot["size"] >= 6) & (tot["month_total"] > factor * tot["median"]) & (tot["month_total"] > min_eur)]
    if spikes.empty:
        return add_flag(df, pd.Series(False, index=df.index), "monthly_spike")
    tmp = df.assign(ym=ym).reset_index().merge(spikes, on=["erp", "cocd", "account", "ym"], how="inner")
    tmp = tmp[tmp["abs_eur"] >= 0.1 * tmp["month_total"]]
    det = pd.Series("", index=df.index)
    for _, r in tmp.iterrows():
        det.at[r["index"]] = f"{r['ym']} total EUR {r['month_total']:,.0f} vs typical {r['median']:,.0f}"
    return add_flag(df, df.index.isin(tmp["index"]), "monthly_spike", det)


def ml_anomaly(df, cfg):
    """Isolation Forest on behavioural features; flags the most unusual lines."""
    f = pd.DataFrame(index=df.index)
    f["log_amt"] = np.log1p(df["abs_eur"])
    acc_med = df.groupby(["erp", "cocd", "account"])["abs_eur"].transform("median")
    f["vs_account"] = np.log1p(df["abs_eur"]) - np.log1p(acc_med)
    f["credit"] = (df["amount_eur"] < 0).astype(int)
    f["manual"] = df["is_manual"].astype(int)
    f["dom"] = df["posting_date"].dt.day.fillna(15)
    f["weekday"] = df["entry_date"].dt.weekday.fillna(2)
    f["lag"] = (df["entry_date"] - df["posting_date"]).dt.days.fillna(0).clip(-30, 365)
    f["log_age"] = np.log1p(df["age_days"].clip(lower=0).fillna(0))
    freq = df["doc_type"].map(df["doc_type"].value_counts(normalize=True)).fillna(0)
    f["doctype_rarity"] = -np.log(freq + 1e-6)
    ufreq = df["user"].map(df["user"].value_counts(normalize=True)).fillna(0)
    f["user_rarity"] = -np.log(ufreq + 1e-6)
    model = IsolationForest(n_estimators=200, contamination=cfg.get("contamination", 0.02), random_state=42)
    pred = model.fit_predict(f.values)
    df["ml_anomaly_score"] = -model.score_samples(f.values)
    # explain: which features are most extreme for this row
    z = (f - f.mean()) / f.std(ddof=0).replace(0, 1)
    names = {"log_amt": "large amount", "vs_account": "amount unusual for this account", "credit": "sign",
             "manual": "manual user", "dom": "day of month", "weekday": "weekday of entry",
             "lag": "entry lag", "log_age": "age", "doctype_rarity": "rare document type",
             "user_rarity": "rare user"}
    top = z.abs().idxmax(axis=1).map(names)
    m = pd.Series(pred == -1, index=df.index) & (df["abs_eur"] > 0)
    return add_flag(df, m, "ml_anomaly", "driver: " + top)


def simple_flags(df, dets, rules):
    if dets.get("round_amount", {}).get("enabled"):
        mn = dets["round_amount"].get("min_eur", 10000)
        m = (df["abs_eur"] >= mn) & (df["amount_dc"] % 1000 == 0) & (df["amount_dc"] != 0)
        add_flag(df, m, "round_amount")
    if dets.get("manual_posting", {}).get("enabled"):
        add_flag(df, df["is_manual"] & (df["abs_eur"] >= 1000), "manual_posting")
    if dets.get("backdated", {}).get("enabled"):
        d = dets["backdated"].get("days", 30)
        lag = (df["entry_date"] - df["posting_date"]).dt.days
        add_flag(df, lag > d, "backdated", lag.astype("Int64").astype(str) + " days late")
    if dets.get("weekend", {}).get("enabled"):
        add_flag(df, df["is_manual"] & (df["entry_date"].dt.weekday >= 5), "weekend")
    if dets.get("text_quality", {}).get("enabled"):
        rx = rules.get("problem_text_pattern")
        t = df["full_text"].fillna("")
        add_flag(df, t.str.contains(rx, case=False, regex=True), "text_investigation")
        poor = (t.str.len() <= 3) | t.str.fullmatch(r"[\d\s|./-]*") | t.str.lower().isin(["text", "accrual", "acc"])
        add_flag(df, poor, "text_poor")
    if dets.get("zero_amount", {}).get("enabled"):
        add_flag(df, df["amount_lc"] == 0, "zero_amount")
    return df


def roll_forward(df, pack):
    """Fixed-asset style roll-forward per entity: opening + movements = closing."""
    buckets = pack.get("roll_forward_buckets", {})
    cat2b = {c: b for b, cats in buckets.items() for c in cats}
    rd = df["recon_date"].iloc[0]
    fy_start = pd.Timestamp(year=rd.year, month=1, day=1)
    d = df.copy()
    d["bucket"] = np.where(d["posting_date"] < fy_start, "Opening balance",
                           d["category"].map(cat2b).fillna("Unclassified movements"))
    d.loc[d["explained_by"].notna() & (d["posting_date"] >= fy_start), "bucket"] = "Net-zero pairs"
    order = ["Opening balance"] + list(buckets.keys()) + ["Net-zero pairs", "Unclassified movements"]
    rf = d.pivot_table(index=["erp", "cocd", "curr_lc"], columns="bucket", values="amount_lc",
                       aggfunc="sum", fill_value=0.0)
    rf = rf.reindex(columns=[c for c in order if c in rf.columns])
    rf["Closing balance"] = rf.sum(axis=1)
    rf = rf.reset_index()
    rf.insert(3, "fiscal_year", rd.year)
    return rf
