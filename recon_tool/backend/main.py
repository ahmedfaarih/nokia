import io
import os
import urllib.parse

import pandas as pd
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from export import build_excel_report
from matching import SOURCES, detect_mode, get_columns, run_reconciliation, run_reconciliation_on_df

app = FastAPI(title="Nokia Intercompany Reconciliation Assistant")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_cache: dict[str, dict] = {}


@app.get("/api/sheets")
def list_sheets():
    """Only sheets from files the user has uploaded this session - no
    hardcoded demo list. Reconciliation is input-driven: upload a file first."""
    return [
        {"sheet_id": sid, "working_group": r["working_group"], "erp": r["erp"]}
        for sid, r in _cache.items()
        if sid.startswith("upload::")
    ]


@app.get("/api/reconcile/{sheet_id:path}")
def reconcile(sheet_id: str, refresh: bool = False):
    if sheet_id.startswith("upload::"):
        if sheet_id not in _cache:
            raise HTTPException(404, "Unknown uploaded sheet_id - upload the file again.")
        return _cache[sheet_id]
    if sheet_id not in SOURCES:
        raise HTTPException(404, f"Unknown sheet_id. Options: {list(SOURCES)}")
    if refresh or sheet_id not in _cache:
        _cache[sheet_id] = run_reconciliation(sheet_id)
    return _cache[sheet_id]


@app.post("/api/upload")
async def upload(file: UploadFile):
    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Only .xlsx/.xls files are supported.")
    body = await file.read()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large (max 25MB).")

    try:
        xls = pd.ExcelFile(io.BytesIO(body), engine="openpyxl")
    except Exception as e:
        raise HTTPException(400, f"Could not read Excel file: {e}")

    results = []
    for sheet_name in xls.sheet_names:
        df = xls.parse(sheet_name)
        if df.empty:
            continue
        df.insert(0, "_row_id", df.index.astype(str) + f"-upload-{sheet_name}")
        c = get_columns(df)
        if c["amt_col"] is None or c["date_col"] is None:
            continue  # sheet doesn't look like a reconciliation line-item sheet, skip it
        mode = detect_mode(df, c)
        sheet_id = f"upload::{file.filename}::{sheet_name}"
        result = run_reconciliation_on_df(df, mode, file.filename, sheet_name, sheet_id)
        _cache[sheet_id] = result
        results.append({"sheet_id": sheet_id, "working_group": file.filename, "erp": sheet_name})

    if not results:
        raise HTTPException(400, "No usable reconciliation sheets found in this file.")
    return results


@app.get("/api/export/{sheet_id:path}")
def export_excel(sheet_id: str):
    result = _cache.get(sheet_id)
    if not result:
        raise HTTPException(404, "Run /api/reconcile for this sheet first.")

    buf = build_excel_report(result)
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in f"{result['working_group']}_{result['erp']}")
    filename = f"reconciliation_{safe_name}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{urllib.parse.quote(filename)}"},
    )


class DraftRequest(BaseModel):
    sheet_id: str
    row_id: str


@app.post("/api/draft-comment-ai")
def draft_comment_ai(req: DraftRequest):
    """Re-draft a single item's comment using Claude instead of the template.
    Falls back with a clear error if no API key is configured."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(400, "ANTHROPIC_API_KEY not set - using template drafts only.")

    result = _cache.get(req.sheet_id)
    if not result:
        raise HTTPException(404, "Run /api/reconcile for this sheet first.")
    item = next((r for r in result["rows"] if r["row_id"] == req.row_id and r["status"] == "open"), None)
    if not item:
        raise HTTPException(404, "row_id not found among this sheet's open items.")

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-sonnet-5-5",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": (
                "Write a one-sentence, professional reconciliation comment for this "
                "open balance-sheet item, explaining why it's still open and what the "
                "next action is. Use only the facts given, do not invent details.\n\n"
                f"Account: {item['account']}\nAmount: {item['amount']}\n"
                f"Posting date: {item['posting_date']}\nReference: {item['reference']}\n"
                f"Category: {item['category']}"
            ),
        }],
    )
    return {"row_id": req.row_id, "ai_comment": msg.content[0].text}


app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
