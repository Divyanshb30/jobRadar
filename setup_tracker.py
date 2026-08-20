"""One-time setup — create the 'JobRadar Tracker' Google Sheet.

Creates the spreadsheet, writes the column headers, freezes the header row,
hides the dedup-hash column (Q), applies the conditional formatting from §4 of
the plan, and (optionally) shares it with your Google account.

Run once after configuring GOOGLE_SHEETS_CREDENTIALS:

    python setup_tracker.py --share you@gmail.com

Then copy the printed spreadsheet id into JOBRADAR_SHEET_ID (env / secret).
Safe to re-run: it reuses an existing sheet of the same name.
"""

from __future__ import annotations

import argparse
import logging

from common import logging_setup
from output.sheets_writer import (HEADERS, WORKSHEETS, open_spreadsheet,
                                  open_worksheet, service_account_email)

log = logging.getLogger("jobradar")

# Background/text colors as normalized RGB.
GREEN = {"red": 0.85, "green": 0.94, "blue": 0.83}
YELLOW = {"red": 1.0, "green": 0.97, "blue": 0.80}
RED_BG = {"red": 0.98, "green": 0.85, "blue": 0.85}
BLUE_BG = {"red": 0.80, "green": 0.89, "blue": 0.99}
PURPLE_BG = {"red": 0.90, "green": 0.82, "blue": 0.98}
RED_TEXT = {"red": 0.80, "green": 0.0, "blue": 0.0}

LAST_ROW = 2000
N_COLS = len(HEADERS)  # 17 (A..Q)


def _bg_rule(sheet_id: int, formula: str, color: dict, cols=(0, N_COLS)) -> dict:
    return {
        "addConditionalFormatRule": {
            "index": 0,
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id, "startRowIndex": 1,
                    "endRowIndex": LAST_ROW,
                    "startColumnIndex": cols[0], "endColumnIndex": cols[1],
                }],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                  "values": [{"userEnteredValue": formula}]},
                    "format": {"backgroundColor": color},
                },
            },
        }
    }


def _text_rule(sheet_id: int, formula: str, color: dict, cols) -> dict:
    return {
        "addConditionalFormatRule": {
            "index": 0,
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id, "startRowIndex": 1,
                    "endRowIndex": LAST_ROW,
                    "startColumnIndex": cols[0], "endColumnIndex": cols[1],
                }],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA",
                                  "values": [{"userEnteredValue": formula}]},
                    "format": {"textFormat": {"foregroundColor": color,
                                              "bold": True}},
                },
            },
        }
    }


def build_requests(sheet_id: int) -> list[dict]:
    """Rules are pushed at index 0, so LAST appended = HIGHEST priority.

    We want status/visa to win over the score row-color, so append the score
    rules first and the status/visa rules last.
    """
    reqs: list[dict] = []
    # Score row colors (lowest priority).
    reqs.append(_bg_rule(sheet_id, '=AND($G2<>"",$G2<40)', RED_BG))
    reqs.append(_bg_rule(sheet_id, "=AND($G2>=40,$G2<60)", YELLOW))
    reqs.append(_bg_rule(sheet_id, "=$G2>=60", GREEN))
    # Visa = NO -> red text on column K only.
    reqs.append(_text_rule(sheet_id, '=$K2="NO"', RED_TEXT, (10, 11)))
    # Status highlights (highest priority — appended last).
    reqs.append(_bg_rule(sheet_id, '=$O2="Applied"', BLUE_BG))
    reqs.append(_bg_rule(sheet_id, '=$O2="Interview"', PURPLE_BG))

    # Freeze the header row.
    reqs.append({
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }
    })
    # Hide the dedup-hash column (Q = index 16).
    reqs.append({
        "updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": 16, "endIndex": 17},
            "properties": {"hiddenByUser": True},
            "fields": "hiddenByUser",
        }
    })
    return reqs


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the JobRadar tracker")
    parser.add_argument("--share", metavar="EMAIL",
                        help="share the sheet with this Google account (writer)")
    args = parser.parse_args()

    logging_setup.setup()

    sa = service_account_email()
    if sa:
        print(f"Service account: {sa}")
        print("(A sheet referenced by JOBRADAR_SHEET_ID must be shared with it "
              "as Editor.)\n")

    sheet = open_spreadsheet(create=True)

    # Build both pipeline tabs with identical schema + formatting.
    for pipeline, title in WORKSHEETS.items():
        ws = open_worksheet(sheet, title, create=True)
        ws.update([HEADERS], "A1")
        ws.format("A1:Q1", {
            "textFormat": {"bold": True},
            "backgroundColor": {"red": 0.12, "green": 0.16, "blue": 0.22},
            "horizontalAlignment": "LEFT",
        })
        # Header text is dark-on-dark by default; make it white.
        ws.format("A1:Q1", {"textFormat": {"bold": True,
                  "foregroundColor": {"red": 1, "green": 1, "blue": 1}}})
        sheet.batch_update({"requests": build_requests(ws.id)})
        log.info("Prepared '%s' tab (headers, formatting, freeze, hidden Q)",
                 title)

    # Remove the default empty "Sheet1"/"Sheet" tab if it's still around.
    for junk in ("Sheet1", "Sheet"):
        try:
            sheet.del_worksheet(sheet.worksheet(junk))
            log.info("Removed default '%s' tab", junk)
        except Exception:  # noqa: BLE001 - not present, or can't delete last sheet
            pass

    if args.share:
        sheet.share(args.share, perm_type="user", role="writer")
        log.info("Shared with %s", args.share)

    print("\n=== JobRadar Tracker ready ===")
    print(f"Spreadsheet ID : {sheet.id}")
    print(f"URL            : {sheet.url}")
    print("\nSet this in your env / GitHub secrets:")
    print(f"  JOBRADAR_SHEET_ID={sheet.id}\n")


if __name__ == "__main__":
    main()
