"""Step 2 - Understand the text: put every line into a standard category.

Three layers, cheapest first:
  1. Keyword rules from the rule pack + shared rules (transparent, auditable).
  2. ML: a text model trained on the rule-labelled lines suggests a category for
     lines the rules could not label (only when it is confident).
  3. Optional LLM (Claude) for what is still unlabelled - see engine/ai.py.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression


def _compile(pairs):
    return [(cat, re.compile(pat, re.IGNORECASE)) for cat, pat in pairs]


def rule_classify(df: pd.DataFrame, pack: dict, rules: dict) -> pd.DataFrame:
    patterns = _compile(pack.get("categories", []) + rules.get("categories", []))
    # classify on line text + header text + reference (reference often holds the real meaning)
    texts = (df["full_text"].fillna("") + " | " + df["reference"].fillna("")).str.strip(" |")
    df["class_text"] = texts
    uniq = texts.unique()
    lookup = {}
    for t in uniq:
        lookup[t] = next((cat for cat, rx in patterns if rx.search(t)), None)
    df["category"] = texts.map(lookup)
    df["category_source"] = np.where(df["category"].notna(), "rule", None)
    return df


def ml_propagate(df: pd.DataFrame, min_conf: float = 0.7) -> pd.DataFrame:
    """Train on rule-labelled rows, predict for unlabelled rows with text."""
    has_text = df["class_text"].fillna("").str.len() > 2
    train = df[df["category"].notna() & has_text]
    todo = df[df["category"].isna() & has_text]
    if train["category"].nunique() < 2 or todo.empty:
        return df
    # context features: text + document type + account description
    def feat(x):
        return (x["class_text"].fillna("") + " doctype_" + x["doc_type"].fillna("")
                + " " + x["account_desc"].fillna(""))
    # train / predict on distinct texts only (much faster, same result)
    tr = pd.DataFrame({"x": feat(train), "y": train["category"]}).drop_duplicates()
    td_x = feat(todo)
    uniq = td_x.drop_duplicates()
    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=2, max_features=30000)
    xtr = vec.fit_transform(tr["x"])
    clf = LogisticRegression(max_iter=1000, C=3.0)
    clf.fit(xtr, tr["y"])
    up = clf.predict_proba(vec.transform(uniq))
    pos = pd.Series(range(len(uniq)), index=uniq.values)
    proba = up[pos.loc[td_x.values].values]
    best = proba.argmax(axis=1)
    conf = proba.max(axis=1)
    keep = conf >= min_conf
    idx = todo.index[keep]
    df.loc[idx, "category"] = clf.classes_[best[keep]]
    df.loc[idx, "category_source"] = "ML (" + pd.Series(conf[keep], index=idx).round(2).astype(str) + ")"
    return df


def finish(df: pd.DataFrame) -> pd.DataFrame:
    no_text = df["class_text"].fillna("").str.len() <= 2
    df.loc[df["category"].isna() & no_text, "category"] = "No description"
    df.loc[df["category"].isna(), "category"] = "Unclassified"
    df["category_source"] = df["category_source"].fillna("none")
    return df
