# Intercompany Reconciliation Assistant

A small tool for balance-sheet reconciliation: upload a line-item export
(`.xlsx`), and it automatically finds duplicate/clearing pairs that net to
zero, categorizes everything else (aged, awaiting counterparty, timing
difference), drafts a comment per open item, and lets you export the whole
thing as a working Excel file.

Built for the Nokia x Corvinus Finance Hackathon.

## How it works

- **Matching**: finds line pairs/groups that cancel out, using whichever key
  field (`Assignment` or `Reference`) isn't dominated by a placeholder value,
  grouped by account + posting date. Non-OIM accounts (e.g. Fixed Assets) use
  a separate transfer-matching rule instead of clearing-matching, since they
  don't have a "clearing" concept.
- **Categorization**: items that don't match get bucketed as aged (>365 days,
  using the file's own aging flag where present, else a computed fallback),
  awaiting counterparty (no comment on file), or a timing difference.
- **Draft comments**: a short, data-grounded comment per item (real computed
  age, real matched document numbers) - no invented details. Optionally
  re-drafted via the Claude API if `ANTHROPIC_API_KEY` is set.
- **Excel export**: a `Summary` tab, a `Resolved - Matched` tab (grouped
  pairs), and a `Needs Decision - Open` tab with blank columns (Decision
  dropdown, Final comment, Reviewed by) so it works as an actual working
  document, not just a read-only dump.

Reconciliation is entirely input-driven - there's no hardcoded demo data; you
upload a file and it builds the sheet list from whatever's inside it.

## Running locally

```
cd recon_tool/backend
pip install -r requirements.txt
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000`, upload an `.xlsx`, pick a sheet, run it.

Set `ANTHROPIC_API_KEY` in your environment to enable the AI-drafted comment
endpoint (`/api/draft-comment-ai`); without it, draft comments are
template-based only (no API calls made).

## Project structure

```
recon_tool/
  backend/
    main.py        FastAPI app: upload, reconcile, export, AI draft-comment endpoints
    matching.py     Core matching/categorization/draft-comment logic
    export.py       Builds the working Excel export
    requirements.txt
  frontend/
    index.html      Single-page UI (Tabulator grid, no build step)
```

## Deploying

See [DEPLOYMENT.md](DEPLOYMENT.md) for deploying to your own server via
GitHub Actions (SSH + systemd + nginx).
