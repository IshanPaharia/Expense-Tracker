import os
import logging
from datetime import datetime
from typing import Optional, Dict, Union
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

# ─── Load Credentials ──────────────────────────────────────────────────────────
load_dotenv()
CREDS = os.getenv("GOOGLE_KEY_PATH")
if not CREDS or not os.path.exists(CREDS):
    raise RuntimeError("Missing or invalid GOOGLE_KEY_PATH in .env")

_SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

try:
    _creds = ServiceAccountCredentials.from_json_keyfile_name(CREDS, _SCOPE)
    _client = gspread.authorize(_creds)
    _SPREADSHEET = _client.open("Expenses")
except Exception as e:
    raise RuntimeError(f"Failed to initialize Google Sheets client: {e}")

# ─── Logger ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ─── Constants ─────────────────────────────────────────────────────────────────
_MAX_ROWS = 1000
_MAX_DESCRIPTION_LENGTH = 200
_MAX_CATEGORY_LENGTH = 50
_HEADERS = ["Timestamp", "Amount", "Description", "Category"]

# ─── Utility Functions ─────────────────────────────────────────────────────────
def _get_monthly_ws(month: Optional[str] = None) -> gspread.Worksheet:
    try:
        if not month:
            month = datetime.now().strftime("%Y-%m")

        # Validate and format title
        dt = datetime.strptime(month, "%Y-%m")
        title = dt.strftime("%B %Y")

        try:
            ws = _SPREADSHEET.worksheet(title)
            headers = ws.row_values(1)
            if headers != _HEADERS:
                ws.clear()
                ws.append_row(_HEADERS)
            return ws
        except gspread.WorksheetNotFound:
            ws = _SPREADSHEET.add_worksheet(title=title, rows=str(_MAX_ROWS), cols="4")
            ws.append_row(_HEADERS)
            return ws
    except ValueError as e:
        logger.error(f"Invalid month format: {month} - {e}")
        raise ValueError(f"Invalid month format. Expected YYYY-MM, got {month}")
    except Exception as e:
        logger.error(f"Error accessing worksheet: {e}")
        raise RuntimeError("Failed to access worksheet. Please try again later.")

def log_expense_to_sheet(
    amount: Union[float, str],
    description: str,
    category: str,
    month: Optional[str] = None
) -> bool:
    try:
        amount = float(amount)
        if amount <= 0 or amount > 1_000_000:
            raise ValueError("Amount must be between 0.01 and 1,000,000")

        description = description.strip()
        if not description or len(description) > _MAX_DESCRIPTION_LENGTH:
            raise ValueError(f"Description must be 1-{_MAX_DESCRIPTION_LENGTH} characters")

        category = category.strip()
        if not category or len(category) > _MAX_CATEGORY_LENGTH:
            raise ValueError(f"Category must be 1-{_MAX_CATEGORY_LENGTH} characters")

        ws = _get_monthly_ws(month)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if len(ws.get_all_values()) >= _MAX_ROWS - 10:
            logger.warning(f"Worksheet approaching row limit: {_MAX_ROWS}")

        ws.append_row([timestamp, amount, description, category])
        return True
    except ValueError as e:
        logger.error(f"Validation error: {e}")
        raise
    except gspread.exceptions.APIError as e:
        logger.error(f"Google API error: {e}")
        raise RuntimeError("Google Sheets API error. Please try again later.")
    except Exception as e:
        logger.error(f"Unexpected error logging expense: {e}")
        raise RuntimeError("Failed to log expense. Please try again later.")

def delete_last_expense_from_sheet(month: Optional[str] = None) -> Optional[Dict[str, Union[str, float]]]:
    try:
        ws = _get_monthly_ws(month)
        records = ws.get_all_records()

        if not records:
            return None

        last_row = len(records) + 1  # +1 for header
        last_record = records[-1]

        if not all(k in last_record for k in _HEADERS):
            logger.error("Malformed record detected")
            raise ValueError("Malformed expense record")

        ws.delete_rows(last_row)
        return {
            "Timestamp": last_record["Timestamp"],
            "Amount": float(last_record["Amount"]),
            "Description": last_record["Description"],
            "Category": last_record["Category"]
        }
    except gspread.exceptions.APIError as e:
        logger.error(f"Google API error deleting expense: {e}")
        raise RuntimeError("Google Sheets API error. Please try again later.")
    except Exception as e:
        logger.error(f"Error deleting expense: {e}")
        raise RuntimeError("Failed to delete expense. Please try again later.")

# ─── Budget Functions ──────────────────────────────────────────────────────────
def _get_info_ws() -> gspread.Worksheet:
    try:
        try:
            ws = _SPREADSHEET.worksheet("Info")
            if ws.acell("A1").value != "Monthly Budget":
                ws.clear()
                ws.append_row(["Monthly Budget", ""])
            return ws
        except gspread.WorksheetNotFound:
            ws = _SPREADSHEET.add_worksheet(title="Info", rows="2", cols="2")
            ws.append_row(["Monthly Budget", ""])
            return ws
    except Exception as e:
        logger.error(f"Error accessing Info worksheet: {e}")
        raise RuntimeError("Failed to access budget information. Please try again later.")

def set_monthly_budget(amount: Union[float, str]) -> None:
    try:
        amount = float(amount)
        if amount <= 0 or amount > 10_000_000:
            raise ValueError("Budget must be between 0.01 and 10,000,000")

        ws = _get_info_ws()
        ws.update_acell("B1", str(amount))
    except ValueError as e:
        logger.error(f"Invalid budget amount: {e}")
        raise
    except gspread.exceptions.APIError as e:
        logger.error(f"Google API error setting budget: {e}")
        raise RuntimeError("Google Sheets API error. Please try again later.")
    except Exception as e:
        logger.error(f"Error setting budget: {e}")
        raise RuntimeError("Failed to set budget. Please try again later.")

def get_monthly_budget() -> Optional[float]:
    try:
        ws = _get_info_ws()
        budget = ws.acell("B1").value
        return float(budget) if budget else None
    except ValueError:
        logger.error("Invalid budget value in sheet")
        return None
    except gspread.exceptions.APIError as e:
        logger.error(f"Google API error getting budget: {e}")
        raise RuntimeError("Google Sheets API error. Please try again later.")
    except Exception as e:
        logger.error(f"Error getting budget: {e}")
        raise RuntimeError("Failed to get budget. Please try again later.")

def clear_monthly_budget() -> None:
    try:
        ws = _get_info_ws()
        ws.update_acell("B1", "")
    except gspread.exceptions.APIError as e:
        logger.error(f"Google API error clearing budget: {e}")
        raise RuntimeError("Google Sheets API error. Please try again later.")
    except Exception as e:
        logger.error(f"Error clearing budget: {e}")
        raise RuntimeError("Failed to clear budget. Please try again later.")
