# ReconRadar: one reconciliation engine, three rule packs

ReconRadar is a prototype for the Nokia Finance Hackathon (Budapest, Nov 2026). It runs the
same engine on the Payroll, Intercompany and Fixed Assets extracts. Every difference between
ERPs and working groups lives in config files, not in code.

## Run it

Mac (in Terminal):
```bash
cd ~/Desktop/recon_engine
python3 -m pip install -r requirements.txt
python3 -m streamlit run app.py
```
Windows: double-click `start_windows.bat`.

The dashboard opens at http://localhost:8501 with an empty upload screen. Upload the Excel
extracts (the hackathon files are in `data/`) and click **Analyse files**, which takes about 30 seconds.
**Reset** removes the files and returns to the empty screen. **Download Excel** exports the full
results.

**AI comments (LLM):** click **AI settings** (top right) and choose a provider:

| Provider | Cost | Setup |
|---|---|---|
| **Groq (recommended)** | Free, very fast | Key from console.groq.com → API Keys |
| Gemini (Google) | Free tier (limited requests per day, often busy) | Key from aistudio.google.com → Get API key |
| OpenRouter | Free models (about 50 requests/day) | Key from openrouter.ai → Keys |
| Mistral | Free tier | Key from console.mistral.ai → API Keys |
| Claude (Anthropic) | Paid, about 1 cent per comment | Key from console.anthropic.com |
| Local model (Ollama) | Free; data never leaves the laptop | Install ollama.com, then run `ollama pull llama3.1` in Terminal |

Click **Test connection**, then open any to-do row and click **Write comment with AI**. AI output
is labelled "AI draft" together with the provider and model. You can also set keys once, before
starting the app, with `export GROQ_API_KEY=...`, `export GEMINI_API_KEY=...` or `export ANTHROPIC_API_KEY=...`.
If a model is busy or rate-limited, the app retries once and then switches to another model
from the same provider automatically.

For a batch or weekly scheduled run without the dashboard, use `python3 run.py --no-llm`, which
writes its results to `output/`.

## How it works

```
 SAP extracts (P20 / BPP / P10)
        │
 1. NORMALIZE    config/mappings.yaml     every column name → one standard table, EUR amounts
        │
 2. UNDERSTAND   rules → ML → LLM         each line gets a standard category
        │
 3. DETECT       config/rulepacks/*.yaml  shared detectors, switched on/off per working group
        │
 4. SCORE        config/rules.yaml        explainable risk score 0-100 + reasons
        │
 5. CLASSIFY     List component / Timing item / Required Adjustment Item / Needs review
        │
 6. ACT          owner team, action, due date, SAP document numbers → action plan
        │
 Dashboard (app.py) + Excel export (output/Reconciliation_Results.xlsx)
```

| Folder / file | What it does |
|---|---|
| `config/mappings.yaml` | Column aliases per standard field. To add a new ERP, add its column names here. |
| `config/rules.yaml` | Flag weights, risk thresholds, shared categories, default owners and actions. |
| `config/rulepacks/*.yaml` | One file per working group: which detectors run, ageing rules, categories, owners. To add a new working group (e.g. Tax), add a new pack. |
| `engine/load.py` | Reads any workbook, picks the rule pack, normalizes the data, converts amounts to EUR. |
| `engine/classify.py` | Keyword rules, then a TF-IDF + logistic-regression model trained on the rule labels. |
| `engine/ai.py` | Optional Claude layer: classifies texts the rules couldn't read and drafts reviewer comments. |
| `engine/detectors.py` | All detectors (see below), plus the Fixed Assets roll-forward. |
| `engine/risk.py` | Risk score, reasons, reconciliation class, action plan, account summaries. |
| `run.py` | Command-line run. A weekly scheduled job (RPA or cron) would call this. |
| `app.py` | Streamlit dashboard. |

## Detectors (shared by all working groups)

| Detector | What it finds |
|---|---|
| Offsetting pairs | Open items with the same key (assignment, reference, ...) that net to zero but were never cleared. |
| Amount-only match | An open item with exactly the opposite amount but no shared key; suggested for review. |
| Reversal pairs | A document and its reversal that are both still open. |
| Overdue | Items older than their allowed ageing (taken from the file, or from the pack). |
| Stale temporary item | Accruals or goods-in-transit that should have moved on (Fixed Assets). |
| Partner checks | Missing trading partner; for each entity pair, whether the partner entity's balance agrees (Intercompany). |
| Duplicates | Same entity, account, amount, date, text and reference, but on different documents. |
| Sign anomaly | A debit on an account that is almost always in credit, or the other way round. |
| Monthly spike | A month far above the account's normal level (Payroll). |
| ML anomaly | Isolation Forest on amount, timing, user, document type and age; reports the main driver. |
| Simple flags | Round amounts, manual postings, back-dated entries, weekend entries, poor text, zero lines. |

On Non-OIM accounts (Fixed Assets), items that net to zero count as **explained**, not as an
issue, because those accounts are never cleared. This is set by `treat_as: explained` in the pack.

## Results on the hackathon data

| | Fixed Assets | Intercompany | Payroll | Total |
|---|---|---|---|---|
| Open items | 10,198 | 14,320 | 13,967 | 38,485 |
| Explained automatically | 99.7% | 57.2% | 47.6% | 65.0% |
| Required Adjustment Items | 17 | 5,959 | 5,980 | 11,956 |
| Need a human | 18 | 165 | 1,333 | 1,516 |

* **Fixed Assets:** the high share comes mostly from pairs that net to zero (accrual plus its
  reversal, or the same reference posted plus and minus). The flagged items are old asset
  accruals and goods in transit that should have been capitalised or reversed.
* **Compared with Nokia's own rule-based "Clearable Item" flag** (Intercompany P10): ReconRadar
  matches all 10 items the rule flags, and finds **438 more** offsetting items that the rule misses.
* Amounts are in the anonymised, scaled units of the dataset. EUR comes from the file where it
  exists; otherwise it is derived from the file's own EUR postings, and only as a last resort
  taken from `config/fx_fallback.yaml`.

## Governance

* **Explainable:** every flag adds a plain-language reason, and the risk score is a visible
  weighted sum (materiality + age + flags).
* **Auditable:** every category records its source (`rule`, `ML (confidence)` or `LLM`).
  Reviewers edit and accept the suggested comments; nothing is posted to SAP automatically.
* **Secure:** the LLM layer is optional. It sends only distinct text strings for
  classification, and can point to an enterprise or private endpoint.
