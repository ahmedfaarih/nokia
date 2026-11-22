"""ReconRadar - dashboard for the back office.   Run:  streamlit run app.py"""
from __future__ import annotations

import html
import io
import os
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from engine import ai
from engine.pipeline import kpis, run, write_excel

st.set_page_config(page_title="ReconRadar", layout="wide", initial_sidebar_state="collapsed")

# Palette inspired by Nokia's 2023 identity and the Nokia Pure design language:
# white space, a deep "classic" blue, a bright blue accent, and vivid secondary hues for data.
NAVY, BLUE, PURPLE = "#001135", "#005AFF", "#6E3CFF"
GREEN, RED, AMBER, GREY = "#00A884", "#E8304F", "#F5A400", "#5B6478"
LINE, SOFT = "#E3E8F1", "#F3F5F9"
GRAD = f"linear-gradient(110deg, {NAVY} 0%, #0B2E8C 42%, {BLUE} 78%, {PURPLE} 112%)"
PLAIN = {
    "Overdue vs allowed ageing": "Open longer than allowed",
    "Uncleared offsetting items": "Matching items not cleared",
    "Stale temporary item": "Old accrual never closed",
    "Possible duplicate": "Possible duplicate posting",
    "Missing trading partner": "Trading partner missing",
    "IC partner mismatch": "Partner balance doesn't agree",
    "Flagged in text as problem": "Poster wrote it needs checking",
    "Not explained automatically": "Needs a human explanation",
}
PRIO = {"P1": "High", "P2": "Medium", "P3": "Low"}
PRIO_COL = {"High": RED, "Medium": AMBER, "Low": GREEN}

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html, body, .stApp, .stMarkdown, p, li, label, input, textarea, button, h1, h2, h3, h4 {{
  font-family: "Nokia Pure Text", "Nokia Pure Headline", "Inter", "Segoe UI", system-ui, sans-serif;
}}
#MainMenu, footer, header[data-testid="stHeader"], [data-testid="stDecoration"] {{display:none}}
.stApp {{background:{SOFT}}}
.block-container {{padding-top:1.6rem; padding-bottom:3rem; max-width:1360px}}
h1, h2, h3 {{color:{NAVY}; letter-spacing:-.015em}}
/* cards: generous radius, hairline borders, no shadows */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]) {{
  background:white; border-radius:16px !important; border:1px solid {LINE} !important; padding:6px 6px}}
/* pill buttons */
.stButton button, .stDownloadButton button, [data-testid="stPopover"] button {{
  border-radius:999px !important; font-weight:600; border:1px solid {LINE}}}
.stButton button[kind="primary"] {{background:{BLUE}; border-color:{BLUE}}}
.stButton button[kind="primary"]:hover {{background:#0047CC; border-color:#0047CC}}
.stButton button[kind="primary"]:disabled {{background:#C9D6F2; border-color:#C9D6F2; color:white; opacity:1}}
[data-testid="stButtonGroup"] button {{border-radius:999px !important}}
[data-baseweb="select"] > div, [data-testid="stFileUploaderDropzone"], textarea, input {{border-radius:12px !important}}
[data-testid="stFileUploaderDropzone"] {{background:{SOFT}; border:1.5px dashed #B9C6DD}}
[data-testid="stDataFrame"] {{border-radius:12px; overflow:hidden}}
/* brand */
.brand {{display:flex;align-items:center;gap:10px}}
.mark {{width:34px;height:34px;border-radius:10px;background:{GRAD};position:relative;flex:none}}
.mark:before {{content:"";position:absolute;inset:8px;border:2px solid white;border-radius:50%}}
.mark:after {{content:"";position:absolute;width:6px;height:6px;background:white;border-radius:50%;left:14px;top:14px}}
.brand .name {{font-size:1.25rem;font-weight:700;color:{NAVY};letter-spacing:-.02em}}
.brand .tag {{font-size:.72rem;font-weight:600;color:{BLUE};background:#E6EEFF;padding:3px 9px;border-radius:999px;margin-left:4px}}
/* hero band */
.hero {{background:{GRAD};border-radius:16px;padding:30px 34px;color:white;margin:6px 0 22px;position:relative;overflow:hidden}}
.hero:after {{content:"";position:absolute;right:-60px;top:-60px;width:260px;height:260px;border-radius:50%;
  border:36px solid rgba(255,255,255,.07)}}
.hero .eyebrow {{font-size:.75rem;font-weight:600;letter-spacing:.12em;text-transform:uppercase;opacity:.75}}
.hero .headline {{font-size:1.9rem;font-weight:700;line-height:1.2;margin:6px 0 8px;letter-spacing:-.02em;max-width:900px}}
.hero .meta {{font-size:.9rem;opacity:.85}}
.hero .pill {{display:inline-block;background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.25);
  padding:4px 12px;border-radius:999px;font-size:.8rem;margin:12px 8px 0 0}}
.sub {{color:{GREY};font-size:.88rem}}
/* KPI cards */
.kpi {{background:white;border:1px solid {LINE};border-radius:16px;padding:20px 22px;margin-bottom:22px;height:100%}}
.kpi .t {{display:flex;align-items:center;gap:8px;color:{GREY};font-size:.8rem;font-weight:600}}
.kpi .t i {{width:8px;height:8px;border-radius:50%;display:inline-block}}
.kpi .v {{font-size:2.2rem;font-weight:700;line-height:1.2;margin:8px 0 2px;color:{NAVY};letter-spacing:-.02em}}
.kpi .s {{color:{GREY};font-size:.82rem}}
.kpi.hi {{background:{GRAD};border:none}}
.kpi.hi .t, .kpi.hi .s {{color:rgba(255,255,255,.82)}}
.kpi.hi .v {{color:white}}
.h {{font-size:1.02rem;font-weight:650;color:{NAVY};margin:4px 0 2px}}
.hint {{color:{GREY};font-size:.84rem;margin-bottom:14px}}
/* bars */
.bar-row {{display:grid;grid-template-columns:220px 1fr 60px;align-items:center;gap:14px;margin:12px 0}}
.bar-row .lbl {{font-size:.88rem;color:{NAVY};text-align:right}}
.bar-track {{background:#EEF2F8;border-radius:999px;height:14px;overflow:hidden}}
.bar-fill {{height:100%;border-radius:999px}}
.bar-row .num {{font-size:.9rem;font-weight:700;color:{NAVY}}}
.stack-row {{display:grid;grid-template-columns:110px 1fr;align-items:center;gap:14px;margin:14px 0}}
.stack {{display:flex;height:28px;border-radius:999px;overflow:hidden;gap:2px;background:white}}
.stack div {{display:flex;align-items:center;justify-content:center;color:white;font-size:.76rem;font-weight:700}}
.legend {{display:flex;gap:18px;margin-top:16px;font-size:.82rem;color:{GREY}}}
.legend i {{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}}
.chip {{display:inline-block;padding:4px 12px;border-radius:999px;font-size:.78rem;font-weight:600;margin-right:6px}}
.facts div {{display:flex;justify-content:space-between;border-bottom:1px solid #EEF2F8;padding:8px 0;font-size:.88rem;color:{NAVY}}}
.facts div span:first-child {{color:{GREY}}}
.facts div span:last-child {{text-align:right;max-width:65%}}
/* start screen */
.step {{display:flex;gap:14px;align-items:flex-start;padding:14px 4px;border-bottom:1px solid #EEF2F8}}
.step:last-child {{border-bottom:none}}
.step .n {{flex:none;width:30px;height:30px;border-radius:50%;background:{GRAD};color:white;font-weight:700;
  display:flex;align-items:center;justify-content:center;font-size:.85rem}}
.step b {{display:block;font-size:.95rem;color:{NAVY};margin-bottom:2px}}
.step span {{color:{GREY};font-size:.86rem;line-height:1.45}}
.reason {{background:#FFF1F3;border-left:3px solid {RED};padding:9px 12px;border-radius:10px;margin:6px 0;font-size:.88rem;color:{NAVY}}}
.ai-tag {{display:inline-block;background:#E6EEFF;color:{BLUE};font-size:.7rem;font-weight:700;padding:3px 10px;border-radius:999px;letter-spacing:.05em}}
.foot {{color:#8A93A6;font-size:.75rem;text-align:center;margin-top:28px}}
</style>""", unsafe_allow_html=True)

ss = st.session_state
for k, v in {"res": None, "upload_key": 0, "view": None, "todo_key": 0, "ai_text": {}, "analysing": False,
             "ai_provider": next((p for p, e in [("groq", "GROQ_API_KEY"), ("gemini", "GEMINI_API_KEY"),
                                                 ("claude", "ANTHROPIC_API_KEY")] if os.environ.get(e)), "groq"),
             "ai_keys": {"claude": os.environ.get("ANTHROPIC_API_KEY", ""),
                         "gemini": os.environ.get("GEMINI_API_KEY", os.environ.get("GOOGLE_API_KEY", "")),
                         "groq": os.environ.get("GROQ_API_KEY", ""),
                         "openrouter": os.environ.get("OPENROUTER_API_KEY", ""),
                         "mistral": os.environ.get("MISTRAL_API_KEY", "")},
             "ai_model": {}, "ai_models_cache": {}}.items():
    ss.setdefault(k, v)


def reset():
    ss.keep_area, ss.keep_ent = "All", "All entities"
    for k in ["res", "files", "excel", "view"]:
        ss[k] = None
    ss.ai_text = {}
    ss.upload_key += 1


def brand(tag=""):
    t = f"<span class='tag'>{tag}</span>" if tag else ""
    return f"<div class='brand'><div class='mark'></div><div class='name'>ReconRadar</div>{t}</div>"


def hero(eyebrow, headline, meta, pills=()):
    p = "".join(f"<span class='pill'>{html.escape(x)}</span>" for x in pills)
    st.markdown(f"<div class='hero'><div class='eyebrow'>{eyebrow}</div><div class='headline'>{headline}</div>"
                f"<div class='meta'>{meta}</div>{p}</div>", unsafe_allow_html=True)


FOOT = ("<div class='foot'>Visual design inspired by the Nokia Pure principles: radically simple, human, dynamic. "
        "Not an official Nokia product.</div>")


def kpi(col, title, value, sub, color, highlight=False):
    col.markdown(f"<div class='kpi{' hi' if highlight else ''}'><div class='t'><i style='background:"
                 f"{'white' if highlight else color}'></i>{title}</div>"
                 f"<div class='v'>{value}</div><div class='s'>{sub}</div></div>", unsafe_allow_html=True)


def esc(x):
    try:
        missing = x is None or bool(pd.isna(x))
    except (TypeError, ValueError):
        missing = False
    return "-" if missing or str(x).strip() == "" else html.escape(str(x))


KEY_HELP = {"claude": "Get one at console.anthropic.com (paid, about 1 cent per comment).",
            "gemini": "Free: aistudio.google.com → Get API key (no card needed).",
            "groq": "Free: console.groq.com → API Keys → Create API Key (no card needed).",
            "openrouter": "Free: openrouter.ai → Keys → Create key (no card needed; free models only).",
            "mistral": "Free: console.mistral.ai → API Keys (no card needed)."}
TIPS = {"gemini": "Tip: pick a model with **flash** in the name - fast and in the free tier. 'preview', 'exp' "
                  "or code-named models can be slow or unavailable.",
        "groq": "Tip: **openai/gpt-oss-120b** or **llama-3.3-70b-versatile** give the best comments; answers take 1-3 seconds.",
        "openrouter": "Only free models (ending in ':free') are listed. Free models allow about 50 requests a day.",
        "mistral": "Tip: **mistral-small-latest** is fast and in the free tier."}


def ai_cfg():
    p = ss.ai_provider
    return {"provider": p, "api_key": ss.ai_keys.get(p, ""), "model": ss.ai_model.get(p)}


def ai_ready():
    c = ai_cfg()
    return bool(c["model"]) and (c["provider"] == "ollama" or bool(c["api_key"]))


def ai_label():
    c = ai_cfg()
    return f"{ai.PROVIDERS[c['provider']].split(' (')[0]} · {c['model']}"


def _models(provider, key):
    ck = (provider, key)
    if ck not in ss.ai_models_cache:
        ss.ai_models_cache[ck] = ai.list_models(provider, key)
    return ss.ai_models_cache[ck]


def ai_settings():
    with st.popover("AI settings  " + ("● on" if ai_ready() else "○ off"), width="stretch"):
        st.markdown("**LLM for writing comments**")
        provs = list(ai.PROVIDERS)
        ss.ai_provider = st.radio("Provider", provs, index=provs.index(ss.ai_provider),
                                  format_func=lambda x: ai.PROVIDERS[x])
        p = ss.ai_provider
        if p in TIPS:
            st.caption(TIPS[p])
        if p == "ollama":
            st.caption("Runs on this computer - no key, no cost, data never leaves the laptop. "
                       "Install from ollama.com, then in Terminal run e.g. `ollama pull llama3.1`.")
        else:
            ss.ai_keys[p] = st.text_input("API key", value=ss.ai_keys.get(p, ""), type="password",
                                          help=KEY_HELP[p] + " Kept only for this session.")
        key = ss.ai_keys.get(p, "")
        models = _models(p, key) if (p == "ollama" or key) else []
        if p == "ollama" and not models:
            st.warning("No local models found. Is Ollama running, and did you pull a model?")
        if models:
            cur = ss.ai_model.get(p)
            ss.ai_model[p] = st.selectbox("Model", models, index=models.index(cur) if cur in models else 0)
        c1, c2 = st.columns(2)
        if c1.button("Refresh models", width="stretch"):
            ss.ai_models_cache.pop((p, key), None)
            st.rerun()
        if c2.button("Test connection", width="stretch", disabled=not ai_ready()):
            try:
                with st.spinner("Testing - can take up to a minute ..."):
                    ans = ai._call("Reply with exactly: OK", key, ss.ai_model.get(p), 20, p)
                used = ai.LAST_MODEL.get("name")
                note = f" (used {used} - {ss.ai_model.get(p)} was busy)" if used and used != ss.ai_model.get(p) else ""
                st.success(f"Connected - model answered: {ans[:40]}{note}")
            except Exception as e:
                st.error(str(e))
        st.caption("Sends the selected items' SAP texts, dates and amounts to the chosen provider "
                   "(nothing leaves the laptop with Ollama). AI output is a draft - the preparer reviews it.")


# =========================================================== EMPTY STATE ===
if ss.res is None:
    st.markdown(brand("Balance sheet reconciliation"), unsafe_allow_html=True)
    hero("Continuous reconciliation",
         "Find what needs fixing in your balance sheet, before month-end close.",
         "Upload the SAP open-item extracts. ReconRadar checks every line, explains the normal items "
         "and gives you a prioritised to-do list.",
         ("Payroll", "Intercompany", "Fixed Assets", "P20 · BPP · P10"))
    left, right = st.columns([1.35, 1], gap="large")
    with left, st.container(border=True):
        st.markdown("<div class='h'>Upload SAP extracts</div><div class='hint'>Excel files, one sheet per ERP. "
                    "Several working groups can be uploaded together.</div>", unsafe_allow_html=True)
        files = st.file_uploader("SAP open-item extracts (.xlsx)", type="xlsx", accept_multiple_files=True,
                                 key=f"up_{ss.upload_key}", label_visibility="collapsed", disabled=ss.analysing)
        go = st.button("Analyse files", type="primary", disabled=not files or ss.analysing, width="stretch")
    with right, st.container(border=True):
        st.markdown("<div class='h'>How it works</div>", unsafe_allow_html=True)
        steps = [("Upload", "SAP extracts for any working group."),
                 ("Automatic checks", "Matching, ageing, duplicates, partner balances and unusual postings."),
                 ("To-do list", "Who fixes what and by when, with SAP document numbers and AI-drafted comments.")]
        st.markdown("".join(f"<div class='step'><div class='n'>{i + 1}</div><div><b>{t}</b><span>{d}</span>"
                            "</div></div>" for i, (t, d) in enumerate(steps)), unsafe_allow_html=True)
    with left:
        if go:
            # Flip the flag and rerun immediately so the button/uploader render
            # disabled *before* the (slow) analysis starts - otherwise a user
            # who clicks again while it's running can queue up repeat runs.
            ss.analysing = True
            st.rerun()

        if ss.analysing:
            with st.status("Analysing files ...", expanded=True) as status:
                tmp = Path(tempfile.mkdtemp())
                paths = []
                for u in files:
                    p = tmp / u.name
                    p.write_bytes(u.getbuffer())
                    paths.append(p)
                st.write(f"Checking every open item in {len(paths)} file(s) - about 30 seconds.")
                try:
                    res = run(paths, use_llm=False)
                except Exception as e:
                    ss.analysing = False
                    status.update(label="Could not analyse these files", state="error")
                    st.error(str(e))
                    st.stop()
                buf = io.BytesIO()
                write_excel(res, buf)
                ss.res, ss.files, ss.excel = res, [u.name for u in files], buf.getvalue()
                ss.analysing = False
                status.update(label="Done", state="complete")
            st.rerun()
    st.markdown(FOOT, unsafe_allow_html=True)
    st.stop()

# ============================================================ HEADER ===
res, items = ss.res, ss.res["items"]
plan = res["action_plan"].copy()
plan["Problem"] = plan["issue_type"].map(PLAIN).fillna(plan["issue_type"])
plan["Priority"] = plan["priority"].map(PRIO)
plan["Account"] = plan["account"].astype(str) + " " + plan["account_desc"].fillna("")

hl, hr = st.columns([2.4, 1.6], vertical_alignment="center")
rd = pd.to_datetime(items["recon_date"]).max()
hl.markdown(brand("Balance sheet reconciliation"), unsafe_allow_html=True)
with hr:
    a, b, c = st.columns([1.15, 1.2, 0.8])
    with a:
        ai_settings()
    b.download_button("Download Excel", ss.excel, "ReconRadar_results.xlsx", width="stretch")
    c.button("Reset", on_click=reset, width="stretch", help="Remove the files and start again")
st.write("")


# ======================================================= DRILL-IN VIEW ===
def group_items(g) -> pd.DataFrame:
    m = (items["work_group"] == g["work_group"]) & (items["cocd"] == g["cocd"]) & (items["account"] == g["account"])
    if g["issue_type"] == "Not explained automatically":
        m &= items["recon_class"] == "Needs review"
    else:
        m &= (items["recon_class"] == "Required Adjustment Item") & (items["issue_type"] == g["issue_type"])
    return items[m].sort_values("risk_score", ascending=False)


def ai_button(label, key, fn, disabled_reason=None):
    """Button that calls the LLM and stores the result in session."""
    if st.button(label, key=f"btn_{key}", type="primary", disabled=not ai_ready()):
        with st.spinner(f"{ai_label()} is writing - usually 5-30 seconds (retries automatically if the model is busy) ..."):
            try:
                ss.ai_text[key] = fn()
                used = ai.LAST_MODEL.get("name") or ai_cfg()["model"]
                ss.ai_text[key + "|by"] = f"{ai.PROVIDERS[ai_cfg()['provider']].split(' (')[0]} · {used}"
            except Exception as e:
                st.error(f"AI could not write the comment. {e}")
                return
        st.rerun()
    if not ai_ready():
        st.caption("Set up an LLM under **AI settings** (top right): Claude, Gemini (free) or a local model (free).")


if ss.view is not None:
    g = ss.view
    det = group_items(g)
    gkey = f"grp|{g['work_group']}|{g['cocd']}|{g['account']}|{g['issue_type']}"
    if st.button("← Back to dashboard"):
        ss.view = None
        st.rerun()
    pr = PRIO[g["priority"]]
    st.markdown(f"<div style='margin:8px 0 4px'><span class='chip' style='background:{PRIO_COL[pr]}1A;color:{PRIO_COL[pr]}'>"
                f"{pr} priority</span><span class='chip' style='background:#EEF1F6;color:#374151'>{esc(g['work_group'])} · "
                f"{esc(g['erp'])}</span></div><h2 style='margin:4px 0 2px'>{esc(g['cocd'])} · {esc(g['Account'])}</h2>"
                f"<div class='sub' style='font-size:1rem;margin-bottom:16px'>{esc(g['Problem'])} · {len(det)} item(s)</div>",
                unsafe_allow_html=True)

    left, right = st.columns([1.7, 1], gap="large")
    with left:
        with st.container(border=True):
            st.markdown("<div class='h'>Reconciliation comment</div>", unsafe_allow_html=True)
            by_cur = det.groupby("curr_lc")["amount_lc"].sum()
            template = (f"{len(det)} open items on {g['account']} {g['account_desc']} ({g['cocd']}), net "
                        + ", ".join(f"{v:,.2f} {k}" for k, v in by_cur.items())
                        + f". Problem: {g['Problem'].lower()}. Oldest item {int(det['age_days'].max())} days. "
                          f"Action: {g['action']} Owner: {g['owner_team']}, due {g['due_date']}.")
            if gkey in ss.ai_text:
                st.markdown(f"<span class='ai-tag'>AI DRAFT · {esc(ss.ai_text.get(gkey + '|by', '')).upper()} · "
                            "REVIEW BEFORE USE</span>", unsafe_allow_html=True)
                st.text_area("comment", ss.ai_text[gkey], height=230, label_visibility="collapsed", key=f"ta_{gkey}")
            else:
                st.markdown("<div class='hint' style='margin:4px 0 6px'>Standard template - use AI for a "
                            "reviewer-ready version that reads the SAP texts and explains the likely cause.</div>",
                            unsafe_allow_html=True)
                st.text_area("comment", template, height=120, label_visibility="collapsed", key=f"tt_{gkey}")
            ai_button("Write comment with AI" if gkey not in ss.ai_text else "Rewrite with AI", gkey,
                      lambda: ai.write_group_comment(det, g["Problem"], g["owner_team"], g["action"], g["due_date"],
                                                     **ai_cfg()))
    with right:
        with st.container(border=True):
            st.markdown("<div class='h'>Summary</div>", unsafe_allow_html=True)
            facts = {"Items": f"{len(det):,}",
                     "Net amount": "<br>".join(f"{v:,.2f} {esc(k)}" for k, v in by_cur.items()),
                     "Oldest item": f"{int(det['age_days'].max()):,} days",
                     "Posted between": f"{pd.to_datetime(det['posting_date']).min():%d.%m.%Y} - "
                                       f"{pd.to_datetime(det['posting_date']).max():%d.%m.%Y}",
                     "Who": esc(g["owner_team"]), "What to do": esc(g["action"]), "Due": esc(g["due_date"])}
            st.markdown("<div class='facts'>" + "".join(f"<div><span>{k}</span><span>{v}</span></div>"
                                                         for k, v in facts.items()) + "</div>", unsafe_allow_html=True)

    st.write("")
    with st.container(border=True):
        st.markdown("<div class='h'>Items</div><div class='hint'>Click an item to see everything about it</div>",
                    unsafe_allow_html=True)
        tbl = det[["risk_score", "doc_no", "posting_date", "age_days", "amount_lc", "curr_lc", "text", "category"]]
        ev = st.dataframe(tbl.head(500), hide_index=True, width="stretch", height=300, on_select="rerun",
                          selection_mode="single-row", key=f"items_{gkey}",
                          column_config={"risk_score": st.column_config.ProgressColumn("Risk", min_value=0, max_value=100, format="%d"),
                                         "doc_no": "SAP document", "posting_date": st.column_config.DateColumn("Posted", format="DD.MM.YYYY"),
                                         "age_days": st.column_config.NumberColumn("Age (days)", format="%d"),
                                         "amount_lc": st.column_config.NumberColumn("Amount", format="%.2f"),
                                         "curr_lc": "Curr.", "text": st.column_config.TextColumn("SAP text", width="large"),
                                         "category": "Category"})
        sel = ev.selection.rows if ev and ev.selection else []
    if sel:
        it = det.iloc[sel[0]]
        ikey = f"item|{it['item_id']}"
        st.write("")
        with st.container(border=True):
            st.markdown(f"<div class='h'>SAP document {esc(it['doc_no'])}</div>", unsafe_allow_html=True)
            l2, r2 = st.columns([1, 1.2], gap="large")
            with l2:
                f2 = {"Amount": f"{it['amount_lc']:,.2f} {esc(it['curr_lc'])} (EUR {it['amount_eur']:,.0f})",
                      "Posted": f"{pd.to_datetime(it['posting_date']):%d.%m.%Y} · {int(it['age_days']):,} days ago",
                      "Posted by": f"{esc(it['user'])} ({'person' if it['is_manual'] else 'robot / interface'})",
                      "Document type": esc(it["doc_type"]), "SAP text": esc(it["text"]),
                      "Header text": esc(it["header_text"]), "Reference": esc(it["reference"]),
                      "Assignment": esc(it["assignment"]), "Trading partner": esc(it["partner"]),
                      "Category": f"{esc(it['category'])} <span class='sub'>({esc(it['category_source'])})</span>",
                      "Risk score": f"{it['risk_score']} / 100"}
                st.markdown("<div class='facts'>" + "".join(f"<div><span>{k}</span><span>{v}</span></div>"
                                                             for k, v in f2.items()) + "</div>", unsafe_allow_html=True)
            with r2:
                st.markdown("**Why it was flagged**")
                reasons = [r for r in str(it["reason_text"]).split("; ") if r and r != "nan"]
                if reasons:
                    st.markdown("".join(f"<div class='reason'>{esc(r)}</div>" for r in reasons), unsafe_allow_html=True)
                else:
                    st.markdown("<div class='sub'>No rule fired - the text could not be matched to a known category.</div>",
                                unsafe_allow_html=True)
                st.markdown("**Comment for this item**")
                if ikey in ss.ai_text:
                    st.markdown(f"<span class='ai-tag'>AI DRAFT · {esc(ss.ai_text.get(ikey + '|by', '')).upper()} · "
                                "REVIEW BEFORE USE</span>", unsafe_allow_html=True)
                    st.text_area("ic", ss.ai_text[ikey], height=140, label_visibility="collapsed", key=f"ta_{ikey}")
                else:
                    st.text_area("ic", it["suggested_comment"], height=110, label_visibility="collapsed", key=f"tt_{ikey}")
                ai_button("Write item comment with AI", ikey,
                          lambda: ai.write_item_comment(it, **ai_cfg()))
    st.stop()

# ============================================================ DASHBOARD ===
kk = kpis(items)
hero(f"Reconciliation date {rd:%d %B %Y}",
     f"{kk['Items explained automatically (%)']:.0f}% of {kk['Open items']:,} open items explained automatically",
     f"{kk['Required Adjustment Items']:,} need fixing in SAP and {kk['Items needing human review']:,} need a human look. "
     f"Start with the High priority rows in the to-do list.",
     [f.replace('Hackathon_', '').replace('_Nokia', '').replace('.xlsx', '').replace('_', ' ') for f in ss.files])
areas = ["All"] + sorted(items["work_group"].unique())
f1, f2, _ = st.columns([2.2, 1.4, 2])
# remember filters while the user is in a drill-in view
keep_area = ss.get("keep_area", "All") if ss.get("keep_area", "All") in areas else "All"
area = f1.segmented_control("Area", areas, default=keep_area, label_visibility="collapsed") or "All"
pool = items if area == "All" else items[items["work_group"] == area]
ents = ["All entities"] + sorted(pool["cocd"].unique())
keep_ent = ss.get("keep_ent", "All entities")
ent = f2.selectbox("Entity", ents, index=ents.index(keep_ent) if keep_ent in ents else 0,
                   label_visibility="collapsed")
ss.keep_area, ss.keep_ent = area, ent
f = pool if ent == "All entities" else pool[pool["cocd"] == ent]
p = plan if area == "All" else plan[plan["work_group"] == area]
if ent != "All entities":
    p = p[p["cocd"] == ent]
p = p.reset_index(drop=True)

k = kpis(f)
c = st.columns(4, gap="medium")
kpi(c[0], "Open items checked", f"{k['Open items']:,}", "every line in the uploaded files", BLUE)
kpi(c[1], "Explained automatically", f"{k['Items explained automatically (%)']:.0f}%",
    f"{k['Items explained automatically']:,} items - no action needed", GREEN, highlight=True)
kpi(c[2], "Need fixing in SAP", f"{k['Required Adjustment Items']:,}", "errors to correct", RED)
kpi(c[3], "Need a human look", f"{k['Items needing human review']:,}", "could not be explained automatically", AMBER)

l, r = st.columns([1.35, 1], gap="medium")
with l, st.container(border=True):
    st.markdown("<div class='h'>What needs fixing</div><div class='hint'>Number of items per problem</div>",
                unsafe_allow_html=True)
    todo = f[f["recon_class"].isin(["Required Adjustment Item", "Needs review"])]
    prob = todo["issue_type"].where(todo["recon_class"] == "Required Adjustment Item", "Not explained automatically")
    cnt = prob.map(PLAIN).fillna(prob).value_counts()
    if cnt.empty:
        st.markdown("<div class='sub'>Nothing to fix for this selection.</div>", unsafe_allow_html=True)
    else:
        mx = cnt.max()
        rows = "".join(
            f"<div class='bar-row'><div class='lbl'>{esc(name)}</div><div class='bar-track'><div class='bar-fill' "
            f"style='width:{max(n / mx * 100, 1.2):.1f}%;background:{AMBER if name == 'Needs a human explanation' else f'linear-gradient(90deg, {BLUE}, {PURPLE})'}'>"
            f"</div></div><div class='num'>{n:,}</div></div>" for name, n in cnt.items())
        st.markdown(rows, unsafe_allow_html=True)
with r, st.container(border=True):
    st.markdown("<div class='h'>Status by area</div><div class='hint'>Share of open items</div>",
                unsafe_allow_html=True)
    stt = f["recon_class"].map({"Required Adjustment Item": "Need fixing", "Needs review": "Need a look"}).fillna("Explained")
    tab = pd.crosstab(f["work_group"], stt, normalize="index") * 100
    cols = [("Explained", GREEN), ("Need fixing", RED), ("Need a look", AMBER)]
    rows = ""
    for wg_, row in tab.iterrows():
        segs = "".join(f"<div style='width:{row.get(n, 0):.2f}%;background:{col}'>"
                       f"{f'{row.get(n, 0):.0f}%' if row.get(n, 0) >= 7 else ''}</div>" for n, col in cols)
        rows += f"<div class='stack-row'><div class='lbl' style='font-size:.88rem;color:{NAVY}'>{esc(wg_)}</div><div class='stack'>{segs}</div></div>"
    legend = "".join(f"<span><i style='background:{col}'></i>{n}</span>" for n, col in cols)
    st.markdown(rows + f"<div class='legend'>{legend}</div>", unsafe_allow_html=True)

st.write("")
with st.container(border=True):
    st.markdown("<div class='h'>To-do list</div><div class='hint'>Grouped by entity, account and problem · most "
                "urgent first · click a row to open it</div>", unsafe_allow_html=True)
    if p.empty:
        st.markdown("<div class='sub'>No open actions for this selection.</div>", unsafe_allow_html=True)
    else:
        view = p[["Priority", "work_group", "cocd", "Account", "Problem", "items", "oldest_days", "owner_team",
                  "due_date"]].rename(columns={"work_group": "Area", "cocd": "Entity", "items": "Items",
                                               "oldest_days": "Oldest (days)", "owner_team": "Who", "due_date": "Due"})
        ev = st.dataframe(view, hide_index=True, width="stretch", height=min(420, 40 + 35 * len(view)), on_select="rerun",
                          selection_mode="single-row", key=f"todo_{ss.todo_key}",
                          column_config={"Account": st.column_config.TextColumn(width="medium"),
                                         "Problem": st.column_config.TextColumn(width="medium"),
                                         "Oldest (days)": st.column_config.NumberColumn(format="%d"),
                                         "Due": st.column_config.DateColumn(format="DD.MM.YY")})
        sel = ev.selection.rows if ev and ev.selection else []
        if sel:
            ss.view = p.iloc[sel[0]].to_dict()
            ss.todo_key += 1          # clear the selection so "Back" returns to a clean list
            st.rerun()
st.markdown(FOOT, unsafe_allow_html=True)
