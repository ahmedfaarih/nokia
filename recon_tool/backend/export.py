"""
Build a working Excel export of a reconciliation result: not just a dump of
the grid, but a document shaped for how a preparer actually works from it -
a "Resolved" tab (matched pairs, grouped) they can sign off on at a glance,
and a "Needs Decision" tab (open items) with blank columns to record the
actual decision, not just the AI's draft read of the item.
"""
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

HEADER_FILL = PatternFill(start_color="142850", end_color="142850", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True)
GROUP_FILL_A = PatternFill(start_color="EAFAF0", end_color="EAFAF0", fill_type="solid")
GROUP_FILL_B = PatternFill(start_color="D4F3E0", end_color="D4F3E0", fill_type="solid")
CAT_FILLS = {
    "Aged": PatternFill(start_color="F9D6D2", end_color="F9D6D2", fill_type="solid"),
    "Awaiting": PatternFill(start_color="FCE7CE", end_color="FCE7CE", fill_type="solid"),
    "Timing": PatternFill(start_color="D6E6F5", end_color="D6E6F5", fill_type="solid"),
}

DECISION_OPTIONS = [
    "Confirmed - no action needed",
    "Escalate to counterparty",
    "Write-off / adjustment",
    "Follow-up next cycle",
    "Other (see notes)",
]


def _style_header(ws, row, n_cols):
    for col in range(1, n_cols + 1):
        cell = ws.cell(row=row, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")


def _autosize(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _cat_fill(category: str):
    for key, fill in CAT_FILLS.items():
        if category.startswith(key):
            return fill
    return None


def build_excel_report(result: dict) -> io.BytesIO:
    rows = result["rows"]
    matched = [r for r in rows if r["status"] == "matched"]
    open_items = [r for r in rows if r["status"] == "open"]

    wb = Workbook()

    # --- Summary tab ---
    ws = wb.active
    ws.title = "Summary"
    ws.append([f"{result['working_group']} / {result['erp']} - Reconciliation Summary"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    ws.append(["Total lines", result["total_rows"]])
    ws.append(["Auto-matched (net to zero)", result["matched_rows"], f"{result['matched_pct']}%"])
    ws.append(["Open items to review", result["open_rows"]])
    ws.append(["Matched groups found", result["matched_groups"], f"of {result['candidate_groups']} candidates"])
    ws.append([])
    ws.append(["Open items by category"])
    ws["A8"].font = Font(bold=True)
    for cat, n in result["category_counts"].items():
        ws.append([cat, n])
    _autosize(ws, [32, 16, 20])

    # --- Resolved (matched) tab ---
    ws2 = wb.create_sheet("Resolved - Matched")
    cols = ["Group", "Account", "Amount", "Posting Date", "Reference", "Auto-match comment"]
    ws2.append(cols)
    _style_header(ws2, 1, len(cols))
    ws2.freeze_panes = "A2"
    ws2.auto_filter.ref = f"A1:{get_column_letter(len(cols))}1"

    last_group = None
    toggle = False
    for r in matched:
        if r["group_id"] != last_group:
            toggle = not toggle
            last_group = r["group_id"]
        ws2.append([r["group_id"], r["account"], r["amount"], r["posting_date"], r["reference"], r["draft_comment"]])
        fill = GROUP_FILL_A if toggle else GROUP_FILL_B
        for col in range(1, len(cols) + 1):
            ws2.cell(row=ws2.max_row, column=col).fill = fill
    _autosize(ws2, [20, 14, 14, 14, 20, 60])

    # --- Needs Decision (open) tab ---
    ws3 = wb.create_sheet("Needs Decision - Open")
    cols3 = [
        "Account", "Amount", "Posting Date", "Age (days)", "Reference", "Category",
        "Suggested comment (AI draft)", "Decision", "Final comment", "Reviewed by", "Date reviewed",
    ]
    ws3.append(cols3)
    _style_header(ws3, 1, len(cols3))
    ws3.freeze_panes = "A2"
    ws3.auto_filter.ref = f"A1:{get_column_letter(len(cols3))}1"

    for r in open_items:
        ws3.append([
            r["account"], r["amount"], r["posting_date"], r["age_days"], r["reference"],
            r["category"], r["draft_comment"], "", "", "", "",
        ])
        fill = _cat_fill(r["category"])
        if fill:
            ws3.cell(row=ws3.max_row, column=6).fill = fill
    _autosize(ws3, [14, 14, 14, 12, 20, 24, 55, 26, 40, 16, 16])

    dv = DataValidation(type="list", formula1=f'"{",".join(DECISION_OPTIONS)}"', allow_blank=True)
    ws3.add_data_validation(dv)
    dv.add(f"H2:H{max(ws3.max_row, 2)}")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
