"""Step 1 - Normalize: read any ERP extract and map it to one standard table."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_rulepacks() -> list[dict]:
    packs = []
    for p in sorted((CONFIG_DIR / "rulepacks").glob("*.yaml")):
        with open(p, encoding="utf-8") as fh:
            pack = yaml.safe_load(fh)
        pack["_file"] = p.name
        packs.append(pack)
    return packs


def pick_rulepack(file_name: str, title: str, packs: list[dict]) -> dict:
    hay = f" {file_name} {title} ".lower().replace("_", " ")
    for pack in packs:
        if any(m.lower().replace("_", " ") in hay for m in pack["match"]):
            return pack
    raise ValueError(f"No rule pack matches '{file_name}'. Add a pack in config/rulepacks/.")


def _clean_code(s: pd.Series) -> pd.Series:
    """Codes read from Excel often arrive as floats ('239001.0')."""
    out = s.astype("string").str.strip()
    out = out.str.replace(r"\.0$", "", regex=True)
    return out.replace({"nan": pd.NA, "None": pd.NA, "": pd.NA})


def _fx_rates_from_data(df: pd.DataFrame) -> dict[str, float]:
    """Derive local-currency-per-EUR rates from rows posted in EUR."""
    rates = {}
    m = (df["curr_dc"] == "EUR") & (df["amount_dc"].abs() > 1) & (df["amount_lc"].abs() > 0)
    if m.any():
        r = (df.loc[m, "amount_lc"] / df.loc[m, "amount_dc"]).abs()
        for cur, grp in r.groupby(df.loc[m, "curr_lc"]):
            rates[cur] = float(grp.median())
    return rates


def normalize_sheet(raw: pd.DataFrame, erp: str, work_group: str, source_file: str,
                    mapping: dict, fx_fallback: dict) -> pd.DataFrame:
    raw = raw.copy()
    raw.columns = [str(c).strip() for c in raw.columns]
    out = pd.DataFrame(index=raw.index)
    for field, aliases in mapping["fields"].items():
        col = next((a for a in aliases if a in raw.columns and raw[a].notna().any()), None)
        out[field] = raw[col] if col else pd.NA

    out = out[out["cocd"].notna()].copy()           # drop empty trailing rows

    for c in ["cocd", "account", "doc_no", "item", "doc_type", "posting_key", "reversal",
              "clearing_doc", "partner", "assignment", "reference", "profit_center", "wbs",
              "curr_dc", "curr_lc", "user", "account_mgmt"]:
        out[c] = _clean_code(out[c])
    for c in ["year", "period"]:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("Int64")
    for c in ["amount_dc", "amount_lc", "amount_eur", "age_days"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    for c in ["posting_date", "doc_date", "entry_date", "net_due_date"]:
        out[c] = pd.to_datetime(out[c], errors="coerce")
    for c in ["text", "header_text", "account_desc", "vendor_name", "customer_name"]:
        out[c] = out[c].astype("string").str.strip().replace({"nan": pd.NA, "": pd.NA})

    out["amount_lc"] = out["amount_lc"].fillna(0.0)
    out["amount_dc"] = out["amount_dc"].fillna(out["amount_lc"])

    # --- EUR amount: from file, else derived rate, else fallback table -------
    if out["amount_eur"].isna().all():
        rates = {**{k: float(v) for k, v in fx_fallback.items()}, **_fx_rates_from_data(out)}
        rate = out["curr_lc"].map(rates)
        out["fx_source"] = np.where(out["curr_lc"].isin(_fx_rates_from_data(out).keys()),
                                    "derived from file", "fallback table")
        out["amount_eur"] = out["amount_lc"] / rate
        missing = out["amount_eur"].isna()
        out.loc[missing, "fx_source"] = "unknown currency"
        out.loc[missing, "amount_eur"] = 0.0
    else:
        out["fx_source"] = "file"
        out["amount_eur"] = out["amount_eur"].fillna(0.0)
    out["abs_eur"] = out["amount_eur"].abs()

    # --- Account management type -----------------------------------------
    am = out["account_mgmt"].astype("string").str.upper().str.replace(" ", "")
    out["account_mgmt"] = np.select(
        [am.str.contains("SUB", na=False), am.str.contains("NON", na=False), am.str.contains("OIM", na=False)],
        ["Sub-ledger", "Non-OIM", "OIM"], default="Unknown")

    # --- Reconciliation date and ageing -----------------------------------
    if out["age_days"].notna().any():
        recon = (out["posting_date"] + pd.to_timedelta(out["age_days"], unit="D")).mode()
        recon_date = recon.iloc[0] if len(recon) else out["posting_date"].max()
    else:
        recon_date = out["posting_date"].max()
    out["recon_date"] = recon_date
    out["age_days"] = out["age_days"].fillna((recon_date - out["posting_date"]).dt.days)

    # allowed ageing from column ("365" / "Actual ageing ( No Overdue )")
    aa = out["allowed_ageing"].astype("string")
    out["allowed_days_col"] = pd.to_numeric(aa.str.extract(r"(\d+)")[0], errors="coerce")
    out["no_overdue_rule"] = aa.str.contains("No Overdue", case=False, na=False)

    bots = tuple(mapping.get("bot_user_prefixes", ["BOT_"]))
    out["is_manual"] = ~out["user"].fillna("").str.upper().str.startswith(bots) & out["user"].notna()

    out["erp"] = erp
    out["work_group"] = work_group
    out["source_file"] = source_file
    out["full_text"] = (out["text"].fillna("") + " | " + out["header_text"].fillna("")).str.strip(" |")
    out["item_id"] = (work_group[:2].upper() + "-" + erp + "-" + out.index.astype(str))
    return out.reset_index(drop=True)


def load_file(path: str | Path, packs: list[dict], mapping: dict, fx_fallback: dict):
    """Read one workbook: every sheet except 'Summary' is one ERP extract."""
    path = Path(path)
    try:                                    # calamine is ~10x faster than openpyxl
        sheets = pd.read_excel(path, sheet_name=None, dtype=object, engine="calamine")
    except Exception:
        sheets = pd.read_excel(path, sheet_name=None, dtype=object)
    title = ""
    if "Summary" in sheets:
        first = sheets["Summary"].columns[0]
        title = str(first)
    pack = pick_rulepack(path.name, title, packs)
    frames = []
    for name, raw in sheets.items():
        if name.lower() == "summary" or raw.empty:
            continue
        frames.append(normalize_sheet(raw, name, pack["name"], path.name, mapping, fx_fallback))
    return pd.concat(frames, ignore_index=True), pack
