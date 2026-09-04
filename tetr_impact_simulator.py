"""
Tetr UG / PG Activity Impact Simulator
======================================

Purpose
-------
For each core activity (Online Event, Masterclass, Competition, Hackathon, TIF),
measure its observed relationship with:
  1) payment / final admission,
  2) subsequent engagement,
  3) reactivation / catalyst behaviour,
  4) deadline-confounded conversions,
  5) overlap / replaceability,
then simulate the likely *risk* if that activity is removed.

Data source
-----------
Uses the same Streamlit Secrets + Google Sheets connection pattern as the
existing Tetr analytics app:

    GSHEET_SPREADSHEET_ID = "..."

    [GOOGLE_SERVICE_ACCOUNT]
    type = "service_account"
    project_id = "..."
    private_key_id = "..."
    private_key = "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n"
    client_email = "..."
    client_id = "..."
    auth_uri = "https://accounts.google.com/o/oauth2/auth"
    token_uri = "https://oauth2.googleapis.com/token"
    auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
    client_x509_cert_url = "..."

Optional TIF secrets:

    TIF_FILE = "Tetr Innovation Fund - Overall Data.csv"
    TIF_EVENT_DATE = "2026-06-16"

If TIF_FILE is not supplied, the app auto-discovers a local CSV/XLSX containing
"TIF" or "Innovation Fund" in its filename. This is intended for the TIF file
that you commit manually to GitHub alongside this app.

Important methodology note
--------------------------
The removal simulation is a behavioural risk estimate, not a causal experiment.
It deliberately reduces confidence when an event is close to a student's
payment deadline, when the student was already highly engaged, or when another
activity could plausibly replace the selected activity.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None
    Credentials = None


# -----------------------------------------------------------------------------
# PAGE / CONSTANTS
# -----------------------------------------------------------------------------

st.set_page_config(
    page_title="Tetr Activity Impact Simulator",
    page_icon="📈",
    layout="wide",
)

MASTER_SHEETS = ["Master UG", "Master PG"]
UG_BATCH_SHEETS = [
    "UG - B1 to B4", "UG B5", "UG B6", "UG B7", "UG B8", "UG B9",
    "UG B10", "UG B11", "UG B12", "UG B13", "UG B14", "UG B15", "UG B16",
]
PG_BATCH_SHEETS = [
    "PG - B1 & B2", "PG - B3 & B4", "PG B5", "PG B6", "PG B7", "PG B8",
]
TX_SHEETS = ["Tetr-X-UG", "Tetr-X-PG"]
DATES_SHEET = "Dates"

REQUIRED_SHEETS = MASTER_SHEETS + UG_BATCH_SHEETS + PG_BATCH_SHEETS + TX_SHEETS + [DATES_SHEET]

CORE_TYPES = ["Online Event", "Masterclass", "Competition", "Hackathon", "TIF"]

GSHEETS_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# The first pass groups online events by obvious speaker names. Unknown online
# events remain available individually, so nothing is lost because a speaker was
# not in this dictionary.
ONLINE_EVENT_GROUP_PATTERNS: Dict[str, Sequence[str]] = {
    "Pratham": [r"\bpratham\b"],
    "Tarun": [r"\btarun\b"],
    "Garima": [r"\bgarima\b"],
    "Mohammed": [r"\bmohammed\b", r"\bmohamed\b", r"\bmuhammad\b"],
    "Priyank": [r"\bpriyank\b"],
    "Kevin / Kevyn": [r"\bkevin\b", r"\bkevyn\b"],
    "Karim": [r"\bkarim\b"],
}

DEFAULT_TIF_DATE = "2026-06-16"
HARDCODED_SHEET_ID = "1By2Zb8vKQnTIQn72JRgyEuuRgO6ZZARCZ1JNklmf25U"

# Visual palette kept close to the existing Tetr dashboard.
GREEN = "#0b3d2e"
GREEN_2 = "#1f7a56"
GREEN_3 = "#56a77b"
LIGHT = "#f5faf7"
AMBER = "#d99a00"
RED = "#b83232"
DARK = "#17352b"


# -----------------------------------------------------------------------------
# CSS
# -----------------------------------------------------------------------------

def inject_css() -> None:
    st.markdown(
        f"""
        <style>
        .stApp {{ background: linear-gradient(180deg, #ffffff 0%, {LIGHT} 100%); }}
        section[data-testid="stSidebar"] {{ background: #f4faf6; border-right: 1px solid #dcebe1; }}
        div[data-testid="stMetric"] {{
            background: white;
            border: 1px solid #dfece4;
            padding: 12px 14px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.025);
        }}
        .impact-note {{
            background:#ffffff; border:1px solid #dfece4; border-left:4px solid {GREEN_2};
            padding:12px 14px; border-radius:8px; margin:8px 0 14px 0;
        }}
        .risk-high {{ color:{RED}; font-weight:700; }}
        .risk-med {{ color:{AMBER}; font-weight:700; }}
        .risk-low {{ color:{GREEN_2}; font-weight:700; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


inject_css()


# -----------------------------------------------------------------------------
# GENERIC HELPERS
# -----------------------------------------------------------------------------

def clean_text(x) -> str:
    if x is None:
        return ""
    try:
        if pd.isna(x):
            return ""
    except Exception:
        pass
    return str(x).replace("\n", " ").replace("\r", " ").replace("\xa0", " ").strip()


def normalize_name(x) -> str:
    s = clean_text(x).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_email(x) -> str:
    return clean_text(x).lower().strip()


def normalize_phone(x) -> str:
    s = re.sub(r"\D+", "", clean_text(x))
    return s[-8:] if len(s) >= 8 else s


def normalize_batch_token(x) -> str:
    s = clean_text(x).upper().replace("–", "-").replace("—", "-")
    s = re.sub(r"\s+", "", s)
    if not s:
        return ""
    # Resolve combined batches before the single trailing-number rule.
    m = re.search(r"B?(\d+)(?:TO|-|&)B?(\d+)", s)
    if m:
        return f"B{m.group(1)}-B{m.group(2)}"
    m = re.search(r"B?(\d+)$", s)
    if m:
        return f"B{m.group(1)}"
    return s


def parse_date(x) -> pd.Timestamp:
    if x is None or clean_text(x) == "":
        return pd.NaT

    # IMPORTANT: ISO strings must be parsed year-first. Pandas can otherwise
    # reinterpret a value such as 2026-09-04 as 09-Apr when dayfirst=True.
    text = clean_text(x)
    if re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T].*)?$", text):
        try:
            v = pd.to_datetime(text, errors="coerce", yearfirst=True, dayfirst=False)
            if pd.notna(v):
                return pd.Timestamp(v).normalize()
        except Exception:
            pass

    # The human-entered Dates/activity cells in the existing tracker are
    # predominantly day-first (e.g. 04/09/2026).
    try:
        v = pd.to_datetime(x, errors="coerce", dayfirst=True)
        if pd.notna(v):
            return pd.Timestamp(v).normalize()
    except Exception:
        pass
    try:
        v = pd.to_datetime(x, errors="coerce")
        return pd.Timestamp(v).normalize() if pd.notna(v) else pd.NaT
    except Exception:
        return pd.NaT


def is_valid_student_name(x) -> bool:
    s = clean_text(x)
    if not s:
        return False
    low = s.lower()
    if low in {"total", "totals", "average", "avg", "mean", "count", "percentage"}:
        return False
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?%?", s.replace(",", "")):
        return False
    return bool(re.search(r"[A-Za-z]", s))


def normalize_yes_no(x) -> int:
    return int(clean_text(x).lower() in {"yes", "y", "1", "true", "present", "attended", "done"})


def make_unique(cols: Iterable) -> List[str]:
    seen: Dict[str, int] = {}
    out: List[str] = []
    for c in cols:
        base = clean_text(c) or "Unnamed"
        n = seen.get(base, 0)
        out.append(base if n == 0 else f"{base}_{n}")
        seen[base] = n + 1
    return out


def best_matching_col(df: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    lowered = {c: clean_text(c).lower() for c in df.columns}
    for cand in candidates:
        cand = cand.lower()
        for col, low in lowered.items():
            if cand in low:
                return col
    return None


def exact_matching_col(df: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    lowered = {clean_text(c).lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def is_deferral_status(status_value, program_or_sheet: str = "") -> bool:
    status = clean_text(status_value).lower().strip()
    ctx = clean_text(program_or_sheet).lower().strip()
    if not status or "refund" in status:
        return False
    is_pg = ctx == "pg" or "pg" in ctx
    if is_pg:
        return bool(re.search(r"\badmitted\s*:?\s*deferral\b", status))
    return "deferral" in status


def is_final_admitted_status(status_value, program_or_sheet: str = "") -> bool:
    status = clean_text(status_value).lower().strip()
    if not status or "refund" in status:
        return False
    return status == "admitted" or is_deferral_status(status, program_or_sheet)


def pct(num: float, den: float) -> float:
    return (float(num) / float(den) * 100.0) if den else 0.0


def pp(x: float) -> str:
    if pd.isna(x):
        return "—"
    sign = "+" if x > 0 else ""
    return f"{sign}{x:.1f} pp"


def format_pct(x: float) -> str:
    return "—" if pd.isna(x) else f"{x:.1f}%"


def safe_int(x) -> int:
    try:
        return int(round(float(x)))
    except Exception:
        return 0


def classify_core_type(event_type: str, event_name: str = "") -> Optional[str]:
    typ = clean_text(event_type).lower()
    name = clean_text(event_name).lower()
    combined = f"{typ} {name}"

    if "hackathon" in typ or (not typ and "hackathon" in name):
        return "Hackathon"
    if "competition" in typ or "challenge" in typ or (not typ and ("competition" in name or "challenge" in name)):
        return "Competition"
    if "masterclass" in typ or "skill bootcamp" in typ or (not typ and "masterclass" in name):
        return "Masterclass"
    if "online event" in typ or "ama" in typ or (not typ and ("ama" in name or "webinar" in name or "online" in name)):
        return "Online Event"
    if "tif" in combined and "masterclass" not in combined:
        # Explicit TIF participant file is added separately. This branch only
        # catches a typed TIF field if one appears in the Google Sheet later.
        return "TIF"
    return None


def online_event_group(event_name: str) -> str:
    low = clean_text(event_name).lower()
    for group, patterns in ONLINE_EVENT_GROUP_PATTERNS.items():
        if any(re.search(p, low, flags=re.I) for p in patterns):
            return group
    return "Other Online Event"


def nice_layout(fig, height: int = 380):
    fig.update_layout(
        paper_bgcolor="white",
        plot_bgcolor="white",
        font=dict(color=DARK),
        margin=dict(l=20, r=20, t=60, b=40),
        height=height,
        legend_title_text="",
    )
    fig.update_xaxes(showgrid=True, gridcolor="#e7f1ea")
    fig.update_yaxes(showgrid=True, gridcolor="#e7f1ea")
    return fig


# -----------------------------------------------------------------------------
# GOOGLE SHEETS CONNECTION - SAME PATTERN AS EXISTING APP
# -----------------------------------------------------------------------------

def get_spreadsheet_id() -> str:
    """Resolve the live New Master Engagement sheet ID.

    This intentionally mirrors the older deployed Tetr dashboard: Streamlit
    Secrets can override the ID, but the known master sheet ID is also kept as
    a fallback so the app does not fail when only GOOGLE_SERVICE_ACCOUNT is
    configured in Secrets.
    """
    try:
        # Primary key used by the existing dashboards.
        value = st.secrets.get("GSHEET_SPREADSHEET_ID", "")
        if clean_text(value):
            return clean_text(value)

        # Accept a couple of harmless aliases in case the secret was named
        # differently in a deployment.
        for key in ("GOOGLE_SHEET_ID", "SPREADSHEET_ID"):
            value = st.secrets.get(key, "")
            if clean_text(value):
                return clean_text(value)

        # Optional nested layouts.
        for section in ("GSHEETS", "google_sheets", "sheets"):
            try:
                block = st.secrets.get(section, {})
                if block:
                    for key in ("spreadsheet_id", "sheet_id", "id"):
                        value = block.get(key, "")
                        if clean_text(value):
                            return clean_text(value)
            except Exception:
                pass
    except Exception:
        pass

    return HARDCODED_SHEET_ID


def get_service_account_dict() -> Dict:
    try:
        if "GOOGLE_SERVICE_ACCOUNT" in st.secrets:
            return dict(st.secrets["GOOGLE_SERVICE_ACCOUNT"])
        if "gcp_service_account" in st.secrets:
            return dict(st.secrets["gcp_service_account"])
    except Exception:
        pass
    return {}


@st.cache_resource(show_spinner=False)
def get_gsheets_client():
    if gspread is None or Credentials is None:
        raise RuntimeError("Install gspread and google-auth (see requirements.txt).")
    key_dict = get_service_account_dict()
    if not key_dict:
        raise RuntimeError("Missing [GOOGLE_SERVICE_ACCOUNT] in Streamlit Secrets.")
    creds = Credentials.from_service_account_info(key_dict, scopes=GSHEETS_SCOPES)
    return gspread.authorize(creds)


@st.cache_data(show_spinner=False, ttl=600)
def gsheets_get_sheet_names(spreadsheet_id: str) -> List[str]:
    gc = get_gsheets_client()
    sh = gc.open_by_key(spreadsheet_id)
    return [ws.title for ws in sh.worksheets()]


def quote_sheet_range(sheet_name: str) -> str:
    safe = str(sheet_name).replace("'", "''")
    return f"'{safe}'"


def values_to_raw_df(values: Sequence[Sequence]) -> pd.DataFrame:
    if not values:
        return pd.DataFrame()
    df = pd.DataFrame(values)
    df.replace("", np.nan, inplace=True)
    return df.dropna(how="all")


@st.cache_data(show_spinner=False, ttl=600)
def gsheets_batch_read(spreadsheet_id: str, sheet_names_tuple: Tuple[str, ...]) -> Dict[str, pd.DataFrame]:
    names = list(sheet_names_tuple)
    if not names:
        return {}
    gc = get_gsheets_client()
    sh = gc.open_by_key(spreadsheet_id)
    ranges = [quote_sheet_range(s) for s in names]
    response = sh.values_batch_get(ranges=ranges)
    value_ranges = response.get("valueRanges", []) if isinstance(response, dict) else []
    out: Dict[str, pd.DataFrame] = {}
    for name, vr in zip(names, value_ranges):
        out[name] = values_to_raw_df(vr.get("values", []))
    for name in names:
        out.setdefault(name, pd.DataFrame())
    return out


# -----------------------------------------------------------------------------
# SHEET PARSERS
# -----------------------------------------------------------------------------

def detect_activity_header_row(raw: pd.DataFrame, default_row: int = 5, max_scan: int = 30) -> Optional[int]:
    if raw is None or raw.empty:
        return None

    def score(i: int) -> int:
        vals = [clean_text(v).lower() for v in raw.iloc[i].tolist()]
        joined = " | ".join(vals)
        s = 0
        if any(v in {"student name", "student names", "name", "full name"} for v in vals):
            s += 8
        if "student name" in joined or "student names" in joined:
            s += 8
        if any("email" in v for v in vals):
            s += 5
        if any("status" in v for v in vals):
            s += 3
        if any("payment" in v for v in vals):
            s += 2
        if any("mobile" in v or "phone" in v for v in vals):
            s += 2
        if sum(bool(v) for v in vals) >= 4:
            s += 1
        return s

    if len(raw) > default_row and score(default_row) >= 8:
        return default_row

    candidates = [(score(i), i) for i in range(min(max_scan, len(raw)))]
    best_score, best_i = max(candidates, default=(-1, None))
    return best_i if best_score >= 8 else None


def parse_master_sheet(raw: pd.DataFrame, program: str, sheet_name: str) -> pd.DataFrame:
    if raw is None or raw.empty or len(raw) < 2:
        return pd.DataFrame()

    # Existing Master sheets: row 1 header, data begins row 4.
    header = make_unique(raw.iloc[0].tolist())
    data_start = 3 if len(raw) > 3 else 1
    df = raw.iloc[data_start:].copy().reset_index(drop=True)
    df.columns = header
    df = df.dropna(how="all")

    name_col = best_matching_col(df, ["name"])
    email_col = best_matching_col(df, ["email"])
    phone_col = best_matching_col(df, ["mobile", "phone", "contact"])
    batch_col = best_matching_col(df, ["batch"])
    country_col = best_matching_col(df, ["country"])
    status_col = best_matching_col(df, ["status"])
    payment_flag_col = best_matching_col(df, ["payment"])
    payment_date_col = best_matching_col(df, ["payment date", "date of payment", "paid date"])

    if not name_col:
        return pd.DataFrame()

    df = df[df[name_col].apply(is_valid_student_name)].copy()
    out = pd.DataFrame(index=df.index)
    out["program"] = program
    out["student_name"] = df[name_col].map(clean_text)
    out["student_key"] = out["student_name"].map(normalize_name)
    out["email_key"] = df[email_col].map(normalize_email) if email_col else ""
    out["phone_key"] = df[phone_col].map(normalize_phone) if phone_col else ""
    out["batch"] = df[batch_col].map(clean_text) if batch_col else ""
    out["batch_key"] = out["batch"].map(normalize_batch_token)
    out["country"] = df[country_col].map(clean_text) if country_col else ""
    out["master_status"] = df[status_col].map(clean_text) if status_col else ""
    out["master_payment_flag"] = df[payment_flag_col].map(clean_text) if payment_flag_col else ""
    out["master_payment_date"] = df[payment_date_col].apply(parse_date) if payment_date_col else pd.NaT
    out["source_sheet"] = sheet_name

    # Prefer email key; if absent use name. Program prefix prevents UG/PG collision.
    out["student_id"] = np.where(
        out["email_key"].astype(str).str.len().gt(3),
        out["program"] + "|e|" + out["email_key"],
        out["program"] + "|n|" + out["student_key"],
    )
    out = out.drop_duplicates("student_id", keep="first").reset_index(drop=True)
    return out


def select_payment_date_col(df: pd.DataFrame, sheet_name: str) -> Optional[str]:
    low_sheet = clean_text(sheet_name).lower()
    if low_sheet == "tetr-x-ug":
        c = exact_matching_col(df, ["Payment date (c3)"])
        if c:
            return c
    if low_sheet == "tetr-x-pg":
        c = exact_matching_col(df, ["Payment date"])
        if c:
            return c
    return best_matching_col(df, ["payment date", "date of payment", "paid date", "community join date"])


@dataclass
class ParsedActivitySheet:
    sheet_name: str
    program: str
    students: pd.DataFrame
    event_info: pd.DataFrame


def infer_program_from_sheet(sheet_name: str) -> str:
    s = clean_text(sheet_name).lower()
    if "ug" in s:
        return "UG"
    if "pg" in s:
        return "PG"
    return ""


def parse_activity_sheet(raw: pd.DataFrame, sheet_name: str) -> ParsedActivitySheet:
    program = infer_program_from_sheet(sheet_name)
    header_row = detect_activity_header_row(raw)
    if header_row is None:
        return ParsedActivitySheet(sheet_name, program, pd.DataFrame(), pd.DataFrame())

    type_idx = max(0, header_row - 5)
    event_idx = max(0, header_row - 4)
    date_idx = max(0, header_row - 3)

    type_row = raw.iloc[type_idx].tolist() if len(raw) > type_idx else []
    event_row = raw.iloc[event_idx].tolist() if len(raw) > event_idx else []
    date_row = raw.iloc[date_idx].tolist() if len(raw) > date_idx else []
    header_cells = raw.iloc[header_row].tolist()

    cols: List[str] = []
    event_rows: List[Dict] = []
    for idx, h in enumerate(header_cells):
        header_name = clean_text(h)
        event_name = clean_text(event_row[idx]) if idx < len(event_row) else ""
        event_type = clean_text(type_row[idx]) if idx < len(type_row) else ""
        event_date = parse_date(date_row[idx]) if idx < len(date_row) else pd.NaT

        if header_name:
            cols.append(header_name)
            # Existing activity sheets keep event columns from approximately T onward.
            if idx >= 19 and (event_name or event_type or pd.notna(event_date)):
                event_rows.append({
                    "column_name": header_name,
                    "event_name": event_name or header_name,
                    "event_type_raw": event_type or "Other",
                    "event_date": event_date,
                    "source_sheet": sheet_name,
                })
        elif event_name or event_type or pd.notna(event_date):
            synthetic = f"EVENT_{idx}"
            cols.append(synthetic)
            event_rows.append({
                "column_name": synthetic,
                "event_name": event_name or synthetic,
                "event_type_raw": event_type or "Other",
                "event_date": event_date,
                "source_sheet": sheet_name,
            })
        else:
            cols.append(f"Unnamed_{idx}")

    cols = make_unique(cols)
    # Remap synthetic placeholders after make_unique.
    for r in event_rows:
        if r["column_name"].startswith("EVENT_"):
            idx = int(r["column_name"].split("_")[-1])
            if idx < len(cols):
                r["column_name"] = cols[idx]

    df = raw.iloc[header_row + 1:].copy().reset_index(drop=True)
    df.columns = cols
    df = df.dropna(how="all")

    name_col = best_matching_col(df, ["student name", "name"])
    email_col = best_matching_col(df, ["email"])
    phone_col = best_matching_col(df, ["mobile", "phone", "contact"])
    batch_col = best_matching_col(df, ["batch"])
    status_col = best_matching_col(df, ["payment status", "status"])
    payment_date_col = select_payment_date_col(df, sheet_name)

    if not name_col:
        return ParsedActivitySheet(sheet_name, program, pd.DataFrame(), pd.DataFrame(event_rows))

    df = df[df[name_col].apply(is_valid_student_name)].copy()
    df["student_name"] = df[name_col].map(clean_text)
    df["student_key"] = df["student_name"].map(normalize_name)
    df["email_key"] = df[email_col].map(normalize_email) if email_col else ""
    df["phone_key"] = df[phone_col].map(normalize_phone) if phone_col else ""
    df["batch"] = df[batch_col].map(clean_text) if batch_col else sheet_name
    df["batch_key"] = df["batch"].map(normalize_batch_token)
    df["program"] = program
    df["source_sheet"] = sheet_name
    df["sheet_status"] = df[status_col].map(clean_text) if status_col else ""
    df["payment_date"] = df[payment_date_col].apply(parse_date) if payment_date_col else pd.NaT

    event_info = pd.DataFrame(event_rows)
    if not event_info.empty:
        event_info["event_category"] = [
            classify_core_type(t, n) for t, n in zip(event_info["event_type_raw"], event_info["event_name"])
        ]
        event_info = event_info[event_info["event_category"].isin(CORE_TYPES)].copy()
        event_info["online_group"] = np.where(
            event_info["event_category"].eq("Online Event"),
            event_info["event_name"].map(online_event_group),
            "",
        )

    event_cols = [c for c in event_info.get("column_name", pd.Series(dtype=str)).tolist() if c in df.columns]
    for c in event_cols:
        df[c] = df[c].map(normalize_yes_no).astype(int)

    return ParsedActivitySheet(sheet_name, program, df, event_info)


def parse_dates_sheet(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()

    header_row = 0
    for i in range(min(12, len(raw))):
        vals = [clean_text(v).lower() for v in raw.iloc[i].tolist()]
        joined = " | ".join(vals)
        if "offered" in joined and "deadline" in joined and ("name" in joined or "email" in joined):
            header_row = i
            break

    df = raw.iloc[header_row + 1:].copy().reset_index(drop=True)
    df.columns = make_unique(raw.iloc[header_row].tolist())
    df = df.dropna(how="all")

    name_col = best_matching_col(df, ["name"])
    email_col = best_matching_col(df, ["email"])
    program_col = exact_matching_col(df, ["UG/PG", "UG PG", "Program"]) or best_matching_col(df, ["ug/pg", "program"])
    batch_col = best_matching_col(df, ["batch"])
    offered_col = best_matching_col(df, ["offered"])
    deadline_col = best_matching_col(df, ["deadline"])

    out = pd.DataFrame(index=df.index)
    out["student_name"] = df[name_col].map(clean_text) if name_col else ""
    out["student_key"] = out["student_name"].map(normalize_name)
    out["email_key"] = df[email_col].map(normalize_email) if email_col else ""
    out["program"] = df[program_col].map(clean_text).str.upper() if program_col else ""
    out["batch_key"] = df[batch_col].map(normalize_batch_token) if batch_col else ""
    out["offered_date"] = df[offered_col].apply(parse_date) if offered_col else pd.NaT
    out["deadline"] = df[deadline_col].apply(parse_date) if deadline_col else pd.NaT
    out = out[(out["student_name"].apply(is_valid_student_name)) | out["email_key"].astype(str).str.len().gt(3)].copy()
    return out.reset_index(drop=True)


# -----------------------------------------------------------------------------
# STUDENT MATCHING / DATA MODEL
# -----------------------------------------------------------------------------

@dataclass
class StudentLookups:
    by_email: Dict[Tuple[str, str], str]
    by_name: Dict[Tuple[str, str], str]
    by_phone: Dict[Tuple[str, str], str]


def build_unique_lookup(df: pd.DataFrame, key_col: str) -> Dict[Tuple[str, str], str]:
    if df.empty or key_col not in df.columns:
        return {}
    work = df[["program", key_col, "student_id"]].copy()
    work[key_col] = work[key_col].map(clean_text)
    work = work[work[key_col].ne("")]
    counts = work.groupby(["program", key_col])["student_id"].nunique()
    valid = counts[counts.eq(1)].index
    valid_set = set(valid)
    out = {}
    for _, r in work.iterrows():
        k = (r["program"], r[key_col])
        if k in valid_set:
            out[k] = r["student_id"]
    return out


def build_student_lookups(students: pd.DataFrame) -> StudentLookups:
    return StudentLookups(
        by_email=build_unique_lookup(students, "email_key"),
        by_name=build_unique_lookup(students, "student_key"),
        by_phone=build_unique_lookup(students, "phone_key"),
    )


def match_student_id(program: str, email_key: str, student_key: str, phone_key: str, lookups: StudentLookups) -> Tuple[Optional[str], str]:
    p = clean_text(program).upper()
    e = normalize_email(email_key)
    n = normalize_name(student_key)
    ph = normalize_phone(phone_key)
    if e and (p, e) in lookups.by_email:
        return lookups.by_email[(p, e)], "email"
    if n and (p, n) in lookups.by_name:
        return lookups.by_name[(p, n)], "name"
    if ph and (p, ph) in lookups.by_phone:
        return lookups.by_phone[(p, ph)], "phone"
    return None, "unmatched"


def attach_dates(students: pd.DataFrame, dates_df: pd.DataFrame) -> pd.DataFrame:
    out = students.copy()
    out["offered_date"] = pd.NaT
    out["deadline"] = pd.NaT
    if dates_df is None or dates_df.empty:
        return out

    # Build email/name indexes; preserve multiple rows to capture deadline extensions.
    by_email: Dict[Tuple[str, str], List[dict]] = {}
    by_name: Dict[Tuple[str, str], List[dict]] = {}
    for _, r in dates_df.iterrows():
        rec = r.to_dict()
        p = clean_text(r.get("program", "")).upper()
        e = normalize_email(r.get("email_key", ""))
        n = normalize_name(r.get("student_key", ""))
        if e:
            by_email.setdefault((p, e), []).append(rec)
        if n:
            by_name.setdefault((p, n), []).append(rec)

    offered_vals, deadline_vals = [], []
    for _, s in out.iterrows():
        p = clean_text(s.get("program", "")).upper()
        e = normalize_email(s.get("email_key", ""))
        n = normalize_name(s.get("student_key", ""))
        batch = normalize_batch_token(s.get("batch_key", s.get("batch", "")))

        cand = by_email.get((p, e), []) if e else []
        if not cand and n:
            cand = by_name.get((p, n), [])

        if batch and cand:
            same_batch = [r for r in cand if normalize_batch_token(r.get("batch_key", "")) == batch]
            if same_batch:
                cand = same_batch

        if cand:
            offered = pd.to_datetime(pd.Series([r.get("offered_date") for r in cand]), errors="coerce").min()
            deadline = pd.to_datetime(pd.Series([r.get("deadline") for r in cand]), errors="coerce").max()
            offered_vals.append(offered if pd.notna(offered) else pd.NaT)
            deadline_vals.append(deadline if pd.notna(deadline) else pd.NaT)
        else:
            offered_vals.append(pd.NaT)
            deadline_vals.append(pd.NaT)

    out["offered_date"] = offered_vals
    out["deadline"] = deadline_vals
    return out


def resolve_tx_status_and_payment(students: pd.DataFrame, parsed_sheets: Dict[str, ParsedActivitySheet], lookups: StudentLookups) -> pd.DataFrame:
    out = students.copy()
    tx_rows: List[dict] = []

    for sheet in TX_SHEETS:
        parsed = parsed_sheets.get(sheet)
        if not parsed or parsed.students.empty:
            continue
        for _, r in parsed.students.iterrows():
            sid, method = match_student_id(
                r.get("program", ""), r.get("email_key", ""), r.get("student_key", ""), r.get("phone_key", ""), lookups
            )
            if not sid:
                continue
            tx_rows.append({
                "student_id": sid,
                "status": clean_text(r.get("sheet_status", "")),
                "payment_date": pd.to_datetime(r.get("payment_date"), errors="coerce"),
                "source_sheet": sheet,
                "match_method": method,
            })

    tx = pd.DataFrame(tx_rows)
    tx_by_student: Dict[str, dict] = {}
    if not tx.empty:
        for sid, g in tx.groupby("student_id"):
            g = g.copy()
            valid_pay = pd.to_datetime(g["payment_date"], errors="coerce").dropna()
            pay = valid_pay.min() if not valid_pay.empty else pd.NaT

            # Prefer the latest nonblank status; if there is a refund anywhere, keep refund.
            statuses = [clean_text(x) for x in g["status"].tolist() if clean_text(x)]
            refund = next((x for x in statuses if "refund" in x.lower()), "")
            status = refund or (statuses[-1] if statuses else "")
            tx_by_student[sid] = {"status": status, "payment_date": pay}

    resolved_status = []
    resolved_payment = []
    for _, s in out.iterrows():
        rec = tx_by_student.get(s["student_id"])
        if rec:
            status = rec["status"] or clean_text(s.get("master_status", ""))
            payment = rec["payment_date"]
            if pd.isna(payment):
                payment = pd.to_datetime(s.get("master_payment_date"), errors="coerce")
        else:
            status = clean_text(s.get("master_status", ""))
            payment = pd.to_datetime(s.get("master_payment_date"), errors="coerce")

        resolved_status.append(status)
        resolved_payment.append(payment if pd.notna(payment) else pd.NaT)

    out["status"] = resolved_status
    out["payment_date"] = pd.to_datetime(pd.Series(resolved_payment), errors="coerce")
    out["is_refunded"] = out["status"].astype(str).str.lower().str.contains("refund", na=False)
    out["is_final_admitted"] = [
        is_final_admitted_status(s, p) for s, p in zip(out["status"], out["program"])
    ]
    out["is_deferred"] = [is_deferral_status(s, p) for s, p in zip(out["status"], out["program"])]
    out["ever_paid"] = out["payment_date"].notna() | out["is_final_admitted"] | out["is_refunded"]
    return out


def build_event_timeline(
    students: pd.DataFrame,
    parsed_sheets: Dict[str, ParsedActivitySheet],
    lookups: StudentLookups,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return attendance rows, event occurrences, and sheet membership.

    Attendance is deduped at student + date + event + category.
    Occurrences are deduped at date + event + category and retain all source sheets.
    Membership is used to approximate who was eligible for an occurrence.
    """
    attendance_rows: List[dict] = []
    occurrence_rows: List[dict] = []
    membership_rows: List[dict] = []

    for sheet_name, parsed in parsed_sheets.items():
        if parsed.students is None or parsed.students.empty:
            continue
        df = parsed.students

        # map every sheet row once
        row_sid: Dict[int, Tuple[Optional[str], str]] = {}
        for idx, r in df.iterrows():
            sid, method = match_student_id(
                r.get("program", ""), r.get("email_key", ""), r.get("student_key", ""), r.get("phone_key", ""), lookups
            )
            row_sid[idx] = (sid, method)
            if sid:
                membership_rows.append({"source_sheet": sheet_name, "student_id": sid, "program": parsed.program})

        if parsed.event_info is None or parsed.event_info.empty:
            continue

        for _, ev in parsed.event_info.iterrows():
            category = clean_text(ev.get("event_category", ""))
            if category not in CORE_TYPES:
                continue
            date = pd.to_datetime(ev.get("event_date"), errors="coerce")
            if pd.isna(date):
                continue
            name = clean_text(ev.get("event_name", "")) or clean_text(ev.get("column_name", ""))
            col = clean_text(ev.get("column_name", ""))
            raw_type = clean_text(ev.get("event_type_raw", ""))
            group = clean_text(ev.get("online_group", "")) if category == "Online Event" else ""

            occurrence_rows.append({
                "event_date": pd.Timestamp(date).normalize(),
                "event_name": name,
                "event_category": category,
                "online_group": group,
                "event_type_raw": raw_type,
                "source_sheet": sheet_name,
                "program": parsed.program,
            })

            if col not in df.columns:
                continue
            attended = pd.to_numeric(df[col], errors="coerce").fillna(0).gt(0)
            for idx in df.index[attended]:
                sid, method = row_sid.get(idx, (None, "unmatched"))
                if not sid:
                    continue
                attendance_rows.append({
                    "student_id": sid,
                    "program": parsed.program,
                    "event_date": pd.Timestamp(date).normalize(),
                    "event_name": name,
                    "event_category": category,
                    "online_group": group,
                    "event_type_raw": raw_type,
                    "source_sheet": sheet_name,
                    "match_method": method,
                    "origin": "Google Sheet",
                })

    att = pd.DataFrame(attendance_rows)
    if not att.empty:
        att = att.sort_values(["student_id", "event_date", "event_category", "event_name"])
        att = att.drop_duplicates(["student_id", "event_date", "event_category", "event_name"], keep="first")

    occ = pd.DataFrame(occurrence_rows)
    if not occ.empty:
        occ = (
            occ.groupby(["program", "event_date", "event_name", "event_category", "online_group", "event_type_raw"], as_index=False)
            .agg(source_sheets=("source_sheet", lambda x: sorted(set(map(clean_text, x)))))
        )

    membership = pd.DataFrame(membership_rows)
    if not membership.empty:
        membership = membership.drop_duplicates(["source_sheet", "student_id"])

    return att, occ, membership


# -----------------------------------------------------------------------------
# TIF LOCAL FILE
# -----------------------------------------------------------------------------

def discover_tif_file() -> Optional[Path]:
    try:
        configured = clean_text(st.secrets.get("TIF_FILE", ""))
    except Exception:
        configured = ""

    if configured:
        p = Path(configured)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent / p
        if p.exists():
            return p

    root = Path(__file__).resolve().parent
    candidates: List[Path] = []
    patterns = ["*TIF*.csv", "*TIF*.xlsx", "*Innovation*Fund*.csv", "*Innovation*Fund*.xlsx"]
    for pat in patterns:
        candidates.extend(root.glob(pat))
    candidates = [p for p in candidates if p.name not in {"requirements.txt"}]
    if not candidates:
        return None
    # Prefer newest local file if several versions are committed.
    return sorted(set(candidates), key=lambda p: p.stat().st_mtime, reverse=True)[0]


def get_default_tif_date() -> pd.Timestamp:
    try:
        configured = clean_text(st.secrets.get("TIF_EVENT_DATE", DEFAULT_TIF_DATE))
    except Exception:
        configured = DEFAULT_TIF_DATE
    d = parse_date(configured)
    return d if pd.notna(d) else pd.Timestamp(DEFAULT_TIF_DATE)


@st.cache_data(show_spinner=False, ttl=600)
def read_local_tif(path_str: str) -> pd.DataFrame:
    path = Path(path_str)
    if path.suffix.lower() == ".csv":
        try:
            return pd.read_csv(path, encoding="utf-8-sig")
        except UnicodeDecodeError:
            return pd.read_csv(path, encoding="latin1")
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    return pd.DataFrame()


def parse_tif_participation(
    tif_df: pd.DataFrame,
    students: pd.DataFrame,
    lookups: StudentLookups,
    default_date: pd.Timestamp,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if tif_df is None or tif_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    df = tif_df.copy()
    df.columns = make_unique(df.columns)
    name_col = best_matching_col(df, ["name", "student name"])
    email_col = best_matching_col(df, ["email"])
    phone_col = best_matching_col(df, ["mobile", "phone", "contact"])
    program_col = exact_matching_col(df, ["Program"]) or best_matching_col(df, ["program"])
    step_col = exact_matching_col(df, ["Step"]) or best_matching_col(df, ["step"])
    status_col = exact_matching_col(df, ["Status"]) or best_matching_col(df, ["status"])
    venture_col = best_matching_col(df, ["venture"])

    date_candidates = [
        c for c in df.columns
        if any(k in clean_text(c).lower() for k in ["registration date", "registered at", "created at", "application date", "date"])
    ]
    date_col = date_candidates[0] if date_candidates else None

    if not name_col and not email_col:
        return pd.DataFrame(), pd.DataFrame()

    # Participation definition for the historical Overall TIF file:
    # - Submitted / shortlisted records are participation.
    # - Lead records count only after moving beyond Step 0 (or when a venture is present).
    # This prevents plain untouched leads from being treated as TIF engagement.
    status_s = df[status_col].map(clean_text).str.lower() if status_col else pd.Series("", index=df.index)
    step_s = df[step_col].map(clean_text).str.lower() if step_col else pd.Series("", index=df.index)
    venture_s = df[venture_col].map(clean_text) if venture_col else pd.Series("", index=df.index)
    explicit_engaged = status_s.isin({"submitted", "shortlisted"})
    step_engaged = step_s.ne("") & ~step_s.str.contains(r"step\s*0\s*of", regex=True, na=False)
    venture_engaged = venture_s.ne("") & ~step_s.str.contains(r"step\s*0\s*of", regex=True, na=False)
    if status_col or step_col or venture_col:
        keep_mask = explicit_engaged | step_engaged | venture_engaged
        df = df[keep_mask].copy()

    rows = []
    unmatched = []
    for _, r in df.iterrows():
        program = clean_text(r.get(program_col, "")).upper() if program_col else ""
        if program == "GY":
            program = "Gap Year"
        if program not in {"UG", "PG"}:
            continue
        name = clean_text(r.get(name_col, "")) if name_col else ""
        email = normalize_email(r.get(email_col, "")) if email_col else ""
        phone = normalize_phone(r.get(phone_col, "")) if phone_col else ""
        sid, method = match_student_id(program, email, normalize_name(name), phone, lookups)
        date = parse_date(r.get(date_col)) if date_col else default_date
        if pd.isna(date):
            date = default_date
        if not sid:
            unmatched.append({"program": program, "name": name, "email": email})
            continue
        rows.append({
            "student_id": sid,
            "program": program,
            "event_date": pd.Timestamp(date).normalize(),
            "event_name": "Tetr Innovation Fund",
            "event_category": "TIF",
            "online_group": "",
            "event_type_raw": "TIF",
            "source_sheet": "Local TIF file",
            "match_method": method,
            "origin": "TIF file",
        })

    out = pd.DataFrame(rows)
    if not out.empty:
        # TIF counts as one engagement lever per student, as requested.
        out = out.sort_values("event_date").drop_duplicates(["student_id", "event_category"], keep="first")
    return out, pd.DataFrame(unmatched)


# -----------------------------------------------------------------------------
# LOAD ALL DATA
# -----------------------------------------------------------------------------

@st.cache_data(show_spinner=False, ttl=600)
def load_google_raw(spreadsheet_id: str) -> Tuple[List[str], Dict[str, pd.DataFrame]]:
    names = gsheets_get_sheet_names(spreadsheet_id)
    wanted = [s for s in REQUIRED_SHEETS if s in names]
    raw = gsheets_batch_read(spreadsheet_id, tuple(wanted))
    return names, raw


@st.cache_data(show_spinner=False, ttl=600)
def build_model(spreadsheet_id: str, tif_path: str, tif_default_date_iso: str):
    sheet_names, raw = load_google_raw(spreadsheet_id)

    master_frames = []
    for sheet in MASTER_SHEETS:
        if sheet in raw and not raw[sheet].empty:
            master_frames.append(parse_master_sheet(raw[sheet], "UG" if sheet.endswith("UG") else "PG", sheet))
    students = pd.concat(master_frames, ignore_index=True) if master_frames else pd.DataFrame()
    if students.empty:
        raise RuntimeError("Master UG / Master PG could not be parsed.")

    dates_df = parse_dates_sheet(raw.get(DATES_SHEET, pd.DataFrame()))
    students = attach_dates(students, dates_df)
    lookups = build_student_lookups(students)

    parsed_sheets: Dict[str, ParsedActivitySheet] = {}
    for sheet in UG_BATCH_SHEETS + PG_BATCH_SHEETS + TX_SHEETS:
        if sheet in raw and not raw[sheet].empty:
            parsed_sheets[sheet] = parse_activity_sheet(raw[sheet], sheet)

    students = resolve_tx_status_and_payment(students, parsed_sheets, lookups)
    # Rebuild lookups is not necessary because student ids did not change.

    attendance, occurrences, membership = build_event_timeline(students, parsed_sheets, lookups)

    tif_unmatched = pd.DataFrame()
    tif_loaded_name = "Not loaded"
    tif_att = pd.DataFrame()
    if tif_path:
        p = Path(tif_path)
        if p.exists():
            tif_df = read_local_tif(str(p))
            tif_att, tif_unmatched = parse_tif_participation(
                tif_df, students, lookups, pd.Timestamp(tif_default_date_iso)
            )
            tif_loaded_name = p.name

    if not tif_att.empty:
        attendance = pd.concat([attendance, tif_att], ignore_index=True, sort=False) if not attendance.empty else tif_att.copy()
        attendance = attendance.drop_duplicates(["student_id", "event_date", "event_category", "event_name"], keep="first")

        # TIF occurrence: one broad occurrence per date/program. Eligibility is
        # all offered students whose active pre-payment window covers the date.
        tif_occ = (
            tif_att.groupby(["program", "event_date", "event_name", "event_category", "online_group", "event_type_raw"], as_index=False)
            .agg(source_sheets=("source_sheet", lambda x: sorted(set(x))))
        )
        occurrences = pd.concat([occurrences, tif_occ], ignore_index=True, sort=False) if not occurrences.empty else tif_occ

    # Enrich timeline once with student metadata.
    meta_cols = [
        "student_id", "student_name", "email_key", "program", "batch", "batch_key", "country",
        "offered_date", "deadline", "payment_date", "status", "is_final_admitted", "is_deferred", "is_refunded", "ever_paid",
    ]
    attendance = attendance.merge(students[meta_cols], on=["student_id", "program"], how="left") if not attendance.empty else pd.DataFrame(columns=meta_cols)

    missing_expected = [s for s in REQUIRED_SHEETS if s not in sheet_names]
    return {
        "students": students,
        "attendance": attendance,
        "occurrences": occurrences,
        "membership": membership,
        "sheet_names": sheet_names,
        "missing_expected": missing_expected,
        "tif_unmatched": tif_unmatched,
        "tif_loaded_name": tif_loaded_name,
    }


# -----------------------------------------------------------------------------
# TARGET / ELIGIBILITY / IMPACT ENGINE
# -----------------------------------------------------------------------------

@dataclass
class TargetSpec:
    activity_type: str
    mode: str = "all"       # all | online_group | event_name
    value: str = ""

    @property
    def label(self) -> str:
        if self.mode == "all":
            return f"All {self.activity_type}s" if self.activity_type != "TIF" else "TIF"
        return self.value


def target_mask(df: pd.DataFrame, spec: TargetSpec) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=bool)
    m = df["event_category"].astype(str).eq(spec.activity_type)
    if spec.mode == "online_group":
        m &= df["online_group"].astype(str).eq(spec.value)
    elif spec.mode == "event_name":
        m &= df["event_name"].astype(str).eq(spec.value)
    return m


def student_active_end(row: pd.Series) -> pd.Timestamp:
    pay = pd.to_datetime(row.get("payment_date"), errors="coerce")
    deadline = pd.to_datetime(row.get("deadline"), errors="coerce")
    if pd.notna(pay):
        return pay
    if pd.notna(deadline):
        return deadline
    return pd.NaT


def filter_valid_prepayment_events(attendance: pd.DataFrame) -> pd.DataFrame:
    if attendance is None or attendance.empty:
        return pd.DataFrame(columns=attendance.columns if attendance is not None else [])
    df = attendance.copy()
    df["event_date"] = pd.to_datetime(df["event_date"], errors="coerce")
    df["offered_date"] = pd.to_datetime(df["offered_date"], errors="coerce")
    df["payment_date"] = pd.to_datetime(df["payment_date"], errors="coerce")
    df["deadline"] = pd.to_datetime(df["deadline"], errors="coerce")

    after_offer = df["offered_date"].isna() | df["event_date"].ge(df["offered_date"])
    pre_paid = df["payment_date"].isna() | df["event_date"].le(df["payment_date"])
    before_deadline_for_unpaid = df["payment_date"].notna() | df["deadline"].isna() | df["event_date"].le(df["deadline"])
    return df[after_offer & pre_paid & before_deadline_for_unpaid].copy()


def build_eligibility_index(
    students: pd.DataFrame,
    occurrences: pd.DataFrame,
    membership: pd.DataFrame,
    spec: TargetSpec,
    program: str,
) -> pd.DataFrame:
    """Approximate who could have attended each selected occurrence.

    For normal Google Sheet events, eligibility is constrained to students found
    in the batch/source sheets where that occurrence exists. Tetr-X-only source
    sheets are excluded from conversion controls because they contain already-paid
    students by construction.

    For TIF, the historical overall participant file does not contain an invite
    list, so eligibility is all offered students whose pre-payment window covers
    the TIF date. The dashboard labels this as broad TIF eligibility.
    """
    if occurrences is None or occurrences.empty:
        return pd.DataFrame(columns=["student_id", "index_date", "event_name"])

    occ = occurrences[(occurrences["program"].eq(program)) & target_mask(occurrences, spec)].copy()
    if occ.empty:
        return pd.DataFrame(columns=["student_id", "index_date", "event_name"])

    sprog = students[students["program"].eq(program)].copy()
    smap = sprog.set_index("student_id")
    members_by_sheet: Dict[str, set] = {}
    if membership is not None and not membership.empty:
        m = membership[membership["program"].eq(program)]
        for sheet, g in m.groupby("source_sheet"):
            members_by_sheet[sheet] = set(g["student_id"])

    rows = []
    for _, ev in occ.iterrows():
        ev_date = pd.to_datetime(ev["event_date"], errors="coerce")
        if pd.isna(ev_date):
            continue

        if spec.activity_type == "TIF":
            candidates = set(sprog["student_id"])
        else:
            candidates = set()
            for sheet in ev.get("source_sheets", []) or []:
                if clean_text(sheet).lower().startswith("tetr-x"):
                    continue
                candidates |= members_by_sheet.get(sheet, set())

        for sid in candidates:
            if sid not in smap.index:
                continue
            sr = smap.loc[sid]
            offered = pd.to_datetime(sr.get("offered_date"), errors="coerce")
            end = student_active_end(sr)
            if pd.notna(offered) and ev_date < offered:
                continue
            if pd.notna(end) and ev_date > end:
                continue
            rows.append({"student_id": sid, "index_date": ev_date, "event_name": ev["event_name"]})

    if not rows:
        return pd.DataFrame(columns=["student_id", "index_date", "event_name"])
    idx = pd.DataFrame(rows).sort_values(["student_id", "index_date"])
    # First selected occurrence for which the student was eligible is the index
    # date for non-attendee comparison.
    return idx.drop_duplicates("student_id", keep="first").reset_index(drop=True)


def count_events_between(events_by_student: Dict[str, pd.DataFrame], sid: str, start, end, exclude_keys: Optional[set] = None) -> int:
    g = events_by_student.get(sid)
    if g is None or g.empty:
        return 0
    s = pd.to_datetime(start, errors="coerce")
    e = pd.to_datetime(end, errors="coerce")
    if pd.isna(s) or pd.isna(e):
        return 0
    part = g[g["event_date"].gt(s) & g["event_date"].le(e)]
    if exclude_keys:
        key = part["event_date"].astype(str) + "|" + part["event_category"] + "|" + part["event_name"]
        part = part[~key.isin(exclude_keys)]
    return int(len(part))


def days_bucket_to_deadline(days: float) -> str:
    if pd.isna(days):
        return "Unknown"
    if days <= 2:
        return "0-2 days"
    if days <= 5:
        return "3-5 days"
    if days <= 10:
        return "6-10 days"
    return "11+ days"


def pre_engagement_band(n: int) -> str:
    if n <= 0:
        return "0"
    if n <= 2:
        return "1-2"
    if n <= 5:
        return "3-5"
    return "6+"


def compute_target_student_features(
    students: pd.DataFrame,
    all_pre_events: pd.DataFrame,
    all_events: pd.DataFrame,
    spec: TargetSpec,
    program: str,
    engagement_window: int,
    deadline_confound_days: int,
    catalyst_after_count: int,
    reactivation_gap_days: int,
) -> pd.DataFrame:
    sprog = students[students["program"].eq(program)].copy()
    target = all_pre_events[(all_pre_events["program"].eq(program)) & target_mask(all_pre_events, spec)].copy()
    if target.empty:
        return pd.DataFrame()

    # Engagement lift is intentionally calculated only inside the valid
    # pre-payment / pre-deadline window. Post-payment participation can rise
    # simply because a student has joined Tetr-X, so crediting it to the
    # pre-payment event would inflate impact.
    pre_prog = all_pre_events[all_pre_events["program"].eq(program)].copy()
    pre_prog["event_date"] = pd.to_datetime(pre_prog["event_date"], errors="coerce")
    events_by_student = {sid: g.sort_values("event_date") for sid, g in pre_prog.groupby("student_id")}
    pre_by_student = events_by_student
    smap = sprog.set_index("student_id")

    rows = []
    for sid, tg in target.groupby("student_id"):
        if sid not in smap.index:
            continue
        sr = smap.loc[sid]
        tg = tg.sort_values("event_date")
        first_dt = pd.to_datetime(tg["event_date"], errors="coerce").min()
        last_dt = pd.to_datetime(tg["event_date"], errors="coerce").max()
        payment = pd.to_datetime(sr.get("payment_date"), errors="coerce")
        deadline = pd.to_datetime(sr.get("deadline"), errors="coerce")
        offer = pd.to_datetime(sr.get("offered_date"), errors="coerce")

        # For paid students, last selected touch before payment is most useful.
        index_dt = last_dt if pd.notna(payment) else first_dt
        days_to_payment = (payment - index_dt).days if pd.notna(payment) and pd.notna(index_dt) else np.nan
        days_to_deadline = (deadline - index_dt).days if pd.notna(deadline) and pd.notna(index_dt) else np.nan
        days_from_offer = (index_dt - offer).days if pd.notna(offer) and pd.notna(index_dt) else np.nan
        deadline_confounded = bool(pd.notna(days_to_deadline) and 0 <= days_to_deadline <= deadline_confound_days)

        g_all = events_by_student.get(sid, pd.DataFrame())
        g_pre = pre_by_student.get(sid, pd.DataFrame())

        before = 0
        after = 0
        prior_total = 0
        other_within_7 = 0
        first_touch = False
        last_touch = False
        if not g_all.empty and pd.notna(first_dt):
            before = int((
                g_all["event_date"].gt(first_dt - pd.Timedelta(days=engagement_window))
                & g_all["event_date"].lt(first_dt)
            ).sum())
            after = int((
                g_all["event_date"].gt(first_dt)
                & g_all["event_date"].le(first_dt + pd.Timedelta(days=engagement_window))
            ).sum())
            prior_total = int(g_all["event_date"].lt(first_dt).sum())
            other_within_7 = int(
                (g_all["event_date"].gt(first_dt)
                 & g_all["event_date"].le(first_dt + pd.Timedelta(days=7))
                 & ~target_mask(g_all, spec)).sum()
            )
            first_touch = prior_total == 0

        if pd.notna(payment) and not g_pre.empty:
            between = g_pre[(g_pre["event_date"].gt(last_dt)) & (g_pre["event_date"].le(payment))]
            last_touch = between.empty

        catalyst = before <= 1 and after >= catalyst_after_count
        # Reactivation: no touch in the configured gap before the first target,
        # followed by at least 2 later core touches in the engagement window.
        if not g_all.empty:
            recent_prior = g_all[
                g_all["event_date"].gt(first_dt - pd.Timedelta(days=reactivation_gap_days))
                & g_all["event_date"].lt(first_dt)
            ]
            reactivated = recent_prior.empty and after >= 2 and not first_touch
        else:
            reactivated = False

        pre_categories = set(g_pre["event_category"].dropna().astype(str)) if not g_pre.empty else set()
        only_target_category = bool(pre_categories and pre_categories.issubset({spec.activity_type}))
        overlap_categories = max(0, len(pre_categories - {spec.activity_type}))
        replacement_signal = other_within_7 > 0
        engagement_lift = after - before

        # Behavioural evidence score for *admitted* attendee risk. It is not
        # interpreted as causal probability; it only ranks evidence strength.
        score = 0
        if pd.notna(days_to_payment):
            if 0 <= days_to_payment <= 3:
                score += 3
            elif days_to_payment <= 7:
                score += 2
            elif days_to_payment <= 14:
                score += 1
        if engagement_lift >= 3:
            score += 2
        elif engagement_lift >= 1:
            score += 1
        if catalyst:
            score += 2
        if reactivated:
            score += 1
        if first_touch:
            score += 1
        if last_touch:
            score += 2
        if only_target_category:
            score += 1
        if deadline_confounded:
            score -= 2
        if prior_total >= 6:
            score -= 1
        if replacement_signal:
            score -= 1

        if score >= 5:
            evidence_band = "High"
        elif score >= 3:
            evidence_band = "Medium"
        else:
            evidence_band = "Low"

        rows.append({
            "student_id": sid,
            "student_name": sr.get("student_name", ""),
            "email": sr.get("email_key", ""),
            "batch": sr.get("batch", ""),
            "country": sr.get("country", ""),
            "status": sr.get("status", ""),
            "is_final_admitted": bool(sr.get("is_final_admitted", False)),
            "ever_paid": bool(sr.get("ever_paid", False)),
            "is_refunded": bool(sr.get("is_refunded", False)),
            "offered_date": offer,
            "deadline": deadline,
            "payment_date": payment,
            "first_target_date": first_dt,
            "last_target_date": last_dt,
            "index_date": index_dt,
            "target_attendances": int(len(tg)),
            "days_to_payment": days_to_payment,
            "days_to_deadline": days_to_deadline,
            "days_from_offer": days_from_offer,
            "deadline_bucket": days_bucket_to_deadline(days_to_deadline),
            "deadline_confounded": deadline_confounded,
            "engagement_before": before,
            "engagement_after": after,
            "engagement_lift": engagement_lift,
            "prior_engagement_total": prior_total,
            "prior_engagement_band": pre_engagement_band(prior_total),
            "already_highly_engaged": prior_total >= 6,
            "catalyst": catalyst,
            "reactivated": reactivated,
            "first_touch": first_touch,
            "last_touch": last_touch,
            "only_target_category": only_target_category,
            "overlap_categories": overlap_categories,
            "replacement_signal": replacement_signal,
            "post_event_disengaged": after == 0,
            "evidence_score": score,
            "evidence_band": evidence_band,
            "event_names": "; ".join(sorted(set(tg["event_name"].astype(str))))[:500],
        })

    return pd.DataFrame(rows)


def deadline_adjusted_rates(
    students: pd.DataFrame,
    eligible_idx: pd.DataFrame,
    attendee_features: pd.DataFrame,
    outcome_col: str,
    deadline_confound_days: int,
) -> Tuple[float, float, int, int]:
    if eligible_idx is None or eligible_idx.empty:
        return np.nan, np.nan, 0, 0

    base = students[["student_id", "deadline", outcome_col]].copy()
    elig = eligible_idx.merge(base, on="student_id", how="left")
    elig["index_date"] = pd.to_datetime(elig["index_date"], errors="coerce")
    elig["deadline"] = pd.to_datetime(elig["deadline"], errors="coerce")

    attendees = set(attendee_features["student_id"]) if attendee_features is not None and not attendee_features.empty else set()
    elig["attended"] = elig["student_id"].isin(attendees)

    # For an attendee, deadline proximity must be measured from the event they
    # actually attended, not from the first event they happened to be eligible
    # for. This matters when analysing an entire category across many dates.
    if attendee_features is not None and not attendee_features.empty and "index_date" in attendee_features.columns:
        actual_index = attendee_features.set_index("student_id")["index_date"]
        mask = elig["attended"] & elig["student_id"].isin(actual_index.index)
        elig.loc[mask, "index_date"] = elig.loc[mask, "student_id"].map(actual_index)

    elig["days_to_deadline"] = (elig["deadline"] - elig["index_date"]).dt.days
    elig["confounded"] = elig["days_to_deadline"].between(0, deadline_confound_days, inclusive="both")
    clean = elig[~elig["confounded"]].copy()

    a = clean[clean["attended"]]
    c = clean[~clean["attended"]]
    ar = pct(a[outcome_col].sum(), len(a)) if len(a) else np.nan
    cr = pct(c[outcome_col].sum(), len(c)) if len(c) else np.nan
    return ar, cr, len(a), len(c)


def behavioural_risk_range(admitted_features: pd.DataFrame) -> Tuple[float, float, float]:
    """Translate evidence bands into a transparent behavioural-risk band.

    We intentionally keep a wide range:
      High evidence   -> 35% / 60% / 85%
      Medium evidence -> 10% / 25% / 45%
      Low evidence    ->  0% /  5% / 15%

    These are sensitivity weights, not learned causal probabilities.
    """
    if admitted_features is None or admitted_features.empty:
        return 0.0, 0.0, 0.0
    weights = {
        "High": (0.35, 0.60, 0.85),
        "Medium": (0.10, 0.25, 0.45),
        "Low": (0.00, 0.05, 0.15),
    }
    low = mid = high = 0.0
    for band, n in admitted_features["evidence_band"].value_counts().items():
        w = weights.get(band, weights["Low"])
        low += n * w[0]
        mid += n * w[1]
        high += n * w[2]
    return low, mid, high


def engagement_risk_range(features: pd.DataFrame) -> Tuple[float, float, float]:
    """Estimate follow-on pre-payment participations exposed by removal.

    Only positive before/after engagement lift is considered. The same wide
    evidence-band sensitivity approach is used so this remains a transparent
    decision-support range rather than a causal forecast.
    """
    if features is None or features.empty:
        return 0.0, 0.0, 0.0
    weights = {
        "High": (0.35, 0.60, 0.85),
        "Medium": (0.10, 0.25, 0.45),
        "Low": (0.00, 0.05, 0.15),
    }
    low = mid = high = 0.0
    for _, r in features.iterrows():
        lift = max(0.0, float(r.get("engagement_lift", 0) or 0))
        if lift <= 0:
            continue
        w = weights.get(clean_text(r.get("evidence_band", "Low")), weights["Low"])
        low += lift * w[0]
        mid += lift * w[1]
        high += lift * w[2]
    return low, mid, high


def recommendation_label(metrics: Dict) -> str:
    n = metrics.get("attendees", 0)
    controls = metrics.get("controls", 0)
    lift = metrics.get("deadline_adjusted_lift", np.nan)
    if pd.isna(lift):
        lift = metrics.get("observed_payment_gap", 0.0)
    eng = metrics.get("avg_engagement_lift", 0.0)
    catalyst = metrics.get("catalyst_rate", 0.0)
    conf = metrics.get("deadline_confounded_rate", 0.0)
    repl = metrics.get("replacement_rate", 0.0)
    unique = metrics.get("unique_only_rate", 0.0)

    if n < 10 or controls < 10:
        return "Need More Data"
    if repl >= 70 and unique <= 15 and (lift > 0 or eng > 0):
        return "Replaceable / Consolidate"
    if conf >= 50 and lift > 0:
        return "Optimize Timing"
    if lift >= 10 and (eng >= 0.5 or catalyst >= 15):
        return "Must Continue"
    if lift >= 5 or eng >= 0.5 or catalyst >= 10:
        return "Continue"
    if lift > 0 or eng > 0:
        return "Optimize"
    if lift <= 0 and eng <= 0 and catalyst < 5 and n >= 20:
        return "Review / Remove Candidate"
    return "Review"


def evaluate_target(
    students: pd.DataFrame,
    attendance: pd.DataFrame,
    occurrences: pd.DataFrame,
    membership: pd.DataFrame,
    spec: TargetSpec,
    program: str,
    outcome_col: str,
    engagement_window: int,
    deadline_confound_days: int,
    catalyst_after_count: int,
    reactivation_gap_days: int,
) -> Tuple[Dict, pd.DataFrame, pd.DataFrame]:
    all_pre = filter_valid_prepayment_events(attendance)
    features = compute_target_student_features(
        students, all_pre, attendance, spec, program,
        engagement_window, deadline_confound_days, catalyst_after_count, reactivation_gap_days,
    )

    sprog = students[students["program"].eq(program)].copy()
    eligible_idx = build_eligibility_index(students, occurrences, membership, spec, program)
    eligible_ids = set(eligible_idx["student_id"]) if not eligible_idx.empty else set()
    attendee_ids = set(features["student_id"]) if not features.empty else set()
    control_ids = eligible_ids - attendee_ids

    outcome_map = sprog.set_index("student_id")[outcome_col].to_dict()
    attendee_outcomes = [bool(outcome_map.get(sid, False)) for sid in attendee_ids]
    control_outcomes = [bool(outcome_map.get(sid, False)) for sid in control_ids]

    attendee_rate = pct(sum(attendee_outcomes), len(attendee_outcomes)) if attendee_outcomes else np.nan
    control_rate = pct(sum(control_outcomes), len(control_outcomes)) if control_outcomes else np.nan
    observed_gap = attendee_rate - control_rate if pd.notna(attendee_rate) and pd.notna(control_rate) else np.nan

    adj_a, adj_c, adj_an, adj_cn = deadline_adjusted_rates(
        sprog, eligible_idx, features, outcome_col, deadline_confound_days
    )
    adj_lift = adj_a - adj_c if pd.notna(adj_a) and pd.notna(adj_c) else np.nan

    admitted_features = features[features[outcome_col].fillna(False).astype(bool)].copy() if not features.empty else pd.DataFrame()
    risk_low, risk_mid, risk_high = behavioural_risk_range(admitted_features)
    eng_risk_low, eng_risk_mid, eng_risk_high = engagement_risk_range(features)

    metrics = {
        "label": spec.label,
        "activity_type": spec.activity_type,
        "attendees": len(attendee_ids),
        "eligible": len(eligible_ids),
        "controls": len(control_ids),
        "reach": pct(len(attendee_ids), len(eligible_ids)) if eligible_ids else np.nan,
        "admitted_attendees": int(sum(attendee_outcomes)) if attendee_outcomes else 0,
        "attendee_conversion": attendee_rate,
        "control_conversion": control_rate,
        "observed_payment_gap": observed_gap,
        "deadline_adjusted_attendee_conversion": adj_a,
        "deadline_adjusted_control_conversion": adj_c,
        "deadline_adjusted_lift": adj_lift,
        "adjusted_attendee_n": adj_an,
        "adjusted_control_n": adj_cn,
        "avg_engagement_lift": float(features["engagement_lift"].mean()) if not features.empty else np.nan,
        "median_engagement_lift": float(features["engagement_lift"].median()) if not features.empty else np.nan,
        "catalyst_rate": pct(features["catalyst"].sum(), len(features)) if not features.empty else np.nan,
        "reactivation_rate": pct(features["reactivated"].sum(), len(features)) if not features.empty else np.nan,
        "first_touch_rate": pct(features["first_touch"].sum(), len(features)) if not features.empty else np.nan,
        "last_touch_rate": pct(admitted_features["last_touch"].sum(), len(admitted_features)) if not admitted_features.empty else np.nan,
        "deadline_confounded_rate": pct(admitted_features["deadline_confounded"].sum(), len(admitted_features)) if not admitted_features.empty else np.nan,
        "replacement_rate": pct(features["replacement_signal"].sum(), len(features)) if not features.empty else np.nan,
        "unique_only_rate": pct(features["only_target_category"].sum(), len(features)) if not features.empty else np.nan,
        "post_event_disengagement_rate": pct(features["post_event_disengaged"].sum(), len(features)) if not features.empty else np.nan,
        "pay_3d": int(((admitted_features["days_to_payment"] >= 0) & (admitted_features["days_to_payment"] <= 3)).sum()) if not admitted_features.empty else 0,
        "pay_7d": int(((admitted_features["days_to_payment"] >= 0) & (admitted_features["days_to_payment"] <= 7)).sum()) if not admitted_features.empty else 0,
        "pay_14d": int(((admitted_features["days_to_payment"] >= 0) & (admitted_features["days_to_payment"] <= 14)).sum()) if not admitted_features.empty else 0,
        "risk_low": risk_low,
        "risk_mid": risk_mid,
        "risk_high": risk_high,
        "engagement_risk_low": eng_risk_low,
        "engagement_risk_mid": eng_risk_mid,
        "engagement_risk_high": eng_risk_high,
        "positive_engagement_students": int(features["engagement_lift"].gt(0).sum()) if not features.empty else 0,
    }
    metrics["recommendation"] = recommendation_label(metrics)
    return metrics, features, eligible_idx


# -----------------------------------------------------------------------------
# IMPACT MATRIX
# -----------------------------------------------------------------------------

@st.cache_data(show_spinner=False, ttl=600)
def compute_impact_matrix_cached(
    students: pd.DataFrame,
    attendance: pd.DataFrame,
    occurrences: pd.DataFrame,
    membership: pd.DataFrame,
    program: str,
    outcome_col: str,
    engagement_window: int,
    deadline_confound_days: int,
    catalyst_after_count: int,
    reactivation_gap_days: int,
) -> pd.DataFrame:
    specs = [TargetSpec(t) for t in CORE_TYPES]

    # Add speaker/category rows inside Online Events.
    groups = sorted(
        g for g in attendance.loc[
            (attendance["program"].eq(program)) & attendance["event_category"].eq("Online Event"), "online_group"
        ].dropna().astype(str).unique()
        if g
    )
    specs.extend(TargetSpec("Online Event", "online_group", g) for g in groups)

    rows = []
    for spec in specs:
        m, _, _ = evaluate_target(
            students, attendance, occurrences, membership, spec, program, outcome_col,
            engagement_window, deadline_confound_days, catalyst_after_count, reactivation_gap_days,
        )
        rows.append(m)
    return pd.DataFrame(rows)


# -----------------------------------------------------------------------------
# RENDER HELPERS
# -----------------------------------------------------------------------------

def render_method_note():
    st.markdown(
        """
        <div class="impact-note">
        <b>How to read this:</b> "Paid after an event" is not automatically credited to the event.
        The dashboard separately shows deadline-confounded payments, pre/post engagement change,
        reactivation, first/last touch, overlap and replacement signals. Removal estimates are a
        behavioural risk range, not a claim that the event alone caused the admission.
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_overview(students: pd.DataFrame, attendance: pd.DataFrame, program: str, outcome_col: str):
    s = students[students["program"].eq(program)].copy()
    pre = filter_valid_prepayment_events(attendance)
    pre = pre[pre["program"].eq(program)]
    core_students = pre["student_id"].nunique() if not pre.empty else 0
    admitted = int(s[outcome_col].sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Offered", f"{len(s):,}")
    c2.metric("Final admitted" if outcome_col == "is_final_admitted" else "Ever paid", f"{admitted:,}")
    c3.metric("Conversion", f"{pct(admitted, len(s)):.1f}%")
    c4.metric("Core engaged pre-payment", f"{core_students:,}", f"{pct(core_students, len(s)):.1f}% of offered")

    if not pre.empty:
        by_type = (
            pre.groupby("event_category")["student_id"].nunique()
            .reindex(CORE_TYPES, fill_value=0)
            .reset_index(name="Unique students")
        )
        fig = px.bar(by_type, x="event_category", y="Unique students", text="Unique students", title="Pre-payment reach by activity type")
        fig.update_traces(marker_color=GREEN_2)
        st.plotly_chart(nice_layout(fig, 360), use_container_width=True)


def style_matrix_display(matrix: pd.DataFrame) -> pd.DataFrame:
    if matrix.empty:
        return matrix
    show = matrix.copy()
    show["Reach"] = show["reach"].map(lambda x: format_pct(x))
    show["Attendee conversion"] = show["attendee_conversion"].map(format_pct)
    show["Eligible non-attendee"] = show["control_conversion"].map(format_pct)
    show["Observed payment gap"] = show["observed_payment_gap"].map(pp)
    show["Deadline-adjusted lift"] = show["deadline_adjusted_lift"].map(pp)
    show["Avg engagement lift"] = show["avg_engagement_lift"].map(lambda x: "—" if pd.isna(x) else f"{x:+.2f}")
    show["Catalyst"] = show["catalyst_rate"].map(format_pct)
    show["Deadline confounded"] = show["deadline_confounded_rate"].map(format_pct)
    show["Replacement"] = show["replacement_rate"].map(format_pct)
    show["Admissions risk"] = [f"{l:.1f}–{h:.1f}" for l, h in zip(show["risk_low"], show["risk_high"])]
    show["Follow-on engagement risk"] = [
        f"{l:.1f}–{h:.1f}" for l, h in zip(show["engagement_risk_low"], show["engagement_risk_high"])
    ]
    cols = [
        "label", "attendees", "admitted_attendees", "Reach", "Attendee conversion",
        "Eligible non-attendee", "Observed payment gap", "Deadline-adjusted lift",
        "Avg engagement lift", "Catalyst", "Deadline confounded", "Replacement",
        "Admissions risk", "Follow-on engagement risk", "recommendation",
    ]
    out = show[cols].rename(columns={
        "label": "Activity",
        "attendees": "Attendees",
        "admitted_attendees": "Admitted attendees",
        "recommendation": "Recommendation",
    })
    return out


def render_impact_matrix(matrix: pd.DataFrame):
    st.subheader("Activity Impact Matrix")
    st.caption("Top-level activity types plus Online Event speaker/category groups.")
    st.dataframe(style_matrix_display(matrix), use_container_width=True, hide_index=True, height=440)

    plot = matrix.copy()
    plot = plot[(plot["attendees"] > 0) & plot["deadline_adjusted_lift"].notna() & plot["avg_engagement_lift"].notna()]
    if not plot.empty:
        fig = px.scatter(
            plot,
            x="avg_engagement_lift",
            y="deadline_adjusted_lift",
            size="attendees",
            hover_name="label",
            hover_data={
                "attendees": True,
                "catalyst_rate": ":.1f",
                "deadline_confounded_rate": ":.1f",
                "replacement_rate": ":.1f",
                "avg_engagement_lift": ":.2f",
                "deadline_adjusted_lift": ":.1f",
            },
            title="Payment impact vs engagement impact",
        )
        fig.add_hline(y=0, line_dash="dash", line_color="#999")
        fig.add_vline(x=0, line_dash="dash", line_color="#999")
        st.plotly_chart(nice_layout(fig, 460), use_container_width=True)


def target_selector(attendance: pd.DataFrame, program: str, key_prefix: str) -> TargetSpec:
    activity_type = st.selectbox("Remove / analyse activity type", CORE_TYPES, key=f"{key_prefix}_type")
    if activity_type == "Online Event":
        mode_label = st.radio(
            "Online Event level",
            ["All Online Events", "Speaker / Category", "Individual Event"],
            horizontal=True,
            key=f"{key_prefix}_mode",
        )
        if mode_label == "Speaker / Category":
            opts = sorted(
                x for x in attendance.loc[
                    (attendance["program"].eq(program)) & attendance["event_category"].eq("Online Event"), "online_group"
                ].dropna().astype(str).unique() if x
            )
            value = st.selectbox("Speaker / category", opts or ["Other Online Event"], key=f"{key_prefix}_group")
            return TargetSpec("Online Event", "online_group", value)
        if mode_label == "Individual Event":
            opts = sorted(
                x for x in attendance.loc[
                    (attendance["program"].eq(program)) & attendance["event_category"].eq("Online Event"), "event_name"
                ].dropna().astype(str).unique() if x
            )
            value = st.selectbox("Online event", opts or ["No events found"], key=f"{key_prefix}_event")
            return TargetSpec("Online Event", "event_name", value)
        return TargetSpec("Online Event")

    if activity_type == "TIF":
        return TargetSpec("TIF")

    level = st.radio(
        "Level",
        [f"All {activity_type}s", "Individual Event"],
        horizontal=True,
        key=f"{key_prefix}_level",
    )
    if level == "Individual Event":
        opts = sorted(
            x for x in attendance.loc[
                (attendance["program"].eq(program)) & attendance["event_category"].eq(activity_type), "event_name"
            ].dropna().astype(str).unique() if x
        )
        value = st.selectbox("Event", opts or ["No events found"], key=f"{key_prefix}_event")
        return TargetSpec(activity_type, "event_name", value)
    return TargetSpec(activity_type)


def render_removal_simulator(
    students: pd.DataFrame,
    attendance: pd.DataFrame,
    occurrences: pd.DataFrame,
    membership: pd.DataFrame,
    program: str,
    outcome_col: str,
    settings: Dict,
):
    st.subheader("Removal Simulator")
    st.caption("Select one activity. The app estimates which admissions and engagement behaviours are most exposed if it disappears.")

    spec = target_selector(attendance, program, key_prefix=program.lower())
    metrics, features, eligible_idx = evaluate_target(
        students, attendance, occurrences, membership, spec, program, outcome_col,
        settings["engagement_window"], settings["deadline_confound_days"],
        settings["catalyst_after_count"], settings["reactivation_gap_days"],
    )

    total_admitted = int(students.loc[students["program"].eq(program), outcome_col].sum())
    central = metrics["risk_mid"]
    projected = max(0, total_admitted - central)

    st.markdown(f"### If **{spec.label}** is removed")
    offered_n = int(students["program"].eq(program).sum())
    projected_conversion = pct(projected, offered_n)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Current admitted", f"{total_admitted:,}")
    c2.metric("Admitted who attended", f"{metrics['admitted_attendees']:,}")
    c3.metric("Estimated admissions at risk", f"{metrics['risk_low']:.1f}–{metrics['risk_high']:.1f}", f"central {metrics['risk_mid']:.1f}")
    c4.metric("Central simulated admitted", f"{projected:.1f}", f"-{central:.1f}")
    c5.metric("Central simulated conversion", f"{projected_conversion:.1f}%")

    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Deadline-adjusted payment lift", pp(metrics["deadline_adjusted_lift"]))
    p2.metric("Avg pre-payment engagement lift", "—" if pd.isna(metrics["avg_engagement_lift"]) else f"{metrics['avg_engagement_lift']:+.2f}")
    p3.metric("Catalyst rate", format_pct(metrics["catalyst_rate"]))
    p4.metric("Recommendation", metrics["recommendation"])

    r1, r2, r3, r4 = st.columns(4)
    r1.metric("Paid within 7 days", f"{metrics['pay_7d']:,}")
    r2.metric("Deadline-confounded", format_pct(metrics["deadline_confounded_rate"]))
    r3.metric("Replacement signal", format_pct(metrics["replacement_rate"]))
    r4.metric("Post-event disengaged", format_pct(metrics["post_event_disengagement_rate"]))

    e1, e2, e3 = st.columns(3)
    e1.metric(
        "Follow-on engagements at risk",
        f"{metrics['engagement_risk_low']:.1f}–{metrics['engagement_risk_high']:.1f}",
        f"central {metrics['engagement_risk_mid']:.1f}",
    )
    e2.metric("Students with positive engagement lift", f"{metrics['positive_engagement_students']:,}")
    e3.metric("Reactivation rate", format_pct(metrics["reactivation_rate"]))

    if features.empty:
        st.info("No pre-payment attendees found for this selection.")
        return

    # Why the selected activity is or is not likely to matter.
    reason_rows = [
        ("Paid within 7 days after selected activity", int(((features["days_to_payment"] >= 0) & (features["days_to_payment"] <= 7)).sum())),
        ("Engagement increased afterwards", int(features["engagement_lift"].gt(0).sum())),
        ("Catalyst: low before, strongly active after", int(features["catalyst"].sum())),
        ("Reactivated after inactivity", int(features["reactivated"].sum())),
        ("First core touch", int(features["first_touch"].sum())),
        ("Last core touch before payment", int(features["last_touch"].sum())),
        ("Deadline-confounded / late-cycle", int(features["deadline_confounded"].sum())),
        ("Already highly engaged before it", int(features["already_highly_engaged"].sum())),
        ("Another core activity followed within 7 days", int(features["replacement_signal"].sum())),
        ("No later pre-payment core engagement", int(features["post_event_disengaged"].sum())),
    ]
    reasons = pd.DataFrame(reason_rows, columns=["Signal / possible explanation", "Students"])
    reasons["Share of attendees"] = reasons["Students"].map(lambda n: f"{pct(n, len(features)):.1f}%")
    with st.expander("Why this activity looks impactful / confounded", expanded=True):
        st.dataframe(reasons, use_container_width=True, hide_index=True)

    # Evidence bands among admitted attendees.
    admitted_features = features[features[outcome_col].fillna(False).astype(bool)].copy()
    if not admitted_features.empty:
        band = admitted_features["evidence_band"].value_counts().reindex(["High", "Medium", "Low"], fill_value=0).reset_index()
        band.columns = ["Evidence", "Students"]
        fig = px.bar(band, x="Evidence", y="Students", text="Students", title="How strongly the selected activity appears in admitted students' journeys")
        st.plotly_chart(nice_layout(fig, 340), use_container_width=True)

    # Payment timing.
    timing = pd.DataFrame({
        "Window": ["Same / ≤3 days", "≤7 days", "≤14 days"],
        "Admitted attendees": [metrics["pay_3d"], metrics["pay_7d"], metrics["pay_14d"]],
    })
    c1, c2 = st.columns(2)
    with c1:
        fig = px.bar(timing, x="Window", y="Admitted attendees", text="Admitted attendees", title="Payment after selected activity")
        fig.update_traces(marker_color=GREEN_2)
        st.plotly_chart(nice_layout(fig, 350), use_container_width=True)
    with c2:
        dl = admitted_features["deadline_bucket"].value_counts().reindex(["0-2 days", "3-5 days", "6-10 days", "11+ days", "Unknown"], fill_value=0).reset_index()
        dl.columns = ["Event to deadline", "Students"]
        fig = px.bar(dl, x="Event to deadline", y="Students", text="Students", title="Was payment already near the deadline?")
        st.plotly_chart(nice_layout(fig, 350), use_container_width=True)

    st.markdown("#### Student-level evidence")
    show = features.copy()
    show["Risk reason"] = np.select(
        [show["evidence_band"].eq("High"), show["evidence_band"].eq("Medium")],
        ["Strong behavioural signal", "Mixed / moderate signal"],
        default="Weak or confounded signal",
    )
    display_cols = [
        "student_name", "email", "batch", "status", "event_names", "first_target_date", "payment_date",
        "deadline", "days_from_offer", "days_to_payment", "days_to_deadline", "engagement_before", "engagement_after",
        "engagement_lift", "already_highly_engaged", "catalyst", "reactivated", "first_touch", "last_touch",
        "deadline_confounded", "replacement_signal", "evidence_band", "Risk reason",
    ]
    display_cols = [c for c in display_cols if c in show.columns]
    st.dataframe(
        show[display_cols].sort_values(["evidence_score", "student_name"], ascending=[False, True]),
        use_container_width=True,
        hide_index=True,
        height=520,
    )

    with st.expander("How this removal estimate is calculated"):
        st.markdown(
            """
            **High evidence** rises when the activity is close to payment, is the first/last meaningful touch,
            creates a clear engagement lift, catalyses an inactive student, or is relatively unique in that
            student's journey. Evidence is reduced when the event sits close to the deposit deadline, when the
            student was already highly engaged, or when another activity appears immediately afterwards.

            The displayed admissions-at-risk range applies deliberately wide sensitivity weights to High / Medium /
            Low evidence students. It is a decision-support range, **not a causal prediction**. A true causal answer
            would require a randomized holdout or a much stronger quasi-experimental design.
            """
        )


def render_student_journeys(students: pd.DataFrame, attendance: pd.DataFrame, program: str):
    st.subheader("Student Journey Explorer")
    s = students[students["program"].eq(program)].copy()
    search = st.text_input("Search by student name or email", key=f"journey_{program}")
    if not search:
        st.caption("Enter a student name/email to inspect Offer → Activity → Deadline → Payment.")
        return
    q = search.lower().strip()
    matches = s[
        s["student_name"].astype(str).str.lower().str.contains(re.escape(q), na=False)
        | s["email_key"].astype(str).str.lower().str.contains(re.escape(q), na=False)
    ]
    if matches.empty:
        st.warning("No matching student found.")
        return
    opts = [f"{r.student_name} · {r.email_key}" for _, r in matches.head(30).iterrows()]
    selected = st.selectbox("Student", opts, key=f"journey_select_{program}")
    idx = opts.index(selected)
    sr = matches.iloc[idx]
    sid = sr["student_id"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Offer", pd.to_datetime(sr.get("offered_date"), errors="coerce").strftime("%d-%b-%Y") if pd.notna(pd.to_datetime(sr.get("offered_date"), errors="coerce")) else "—")
    c2.metric("Deadline", pd.to_datetime(sr.get("deadline"), errors="coerce").strftime("%d-%b-%Y") if pd.notna(pd.to_datetime(sr.get("deadline"), errors="coerce")) else "—")
    c3.metric("Payment", pd.to_datetime(sr.get("payment_date"), errors="coerce").strftime("%d-%b-%Y") if pd.notna(pd.to_datetime(sr.get("payment_date"), errors="coerce")) else "—")
    c4.metric("Status", clean_text(sr.get("status", "")) or "Not Paid")

    ev = attendance[attendance["student_id"].eq(sid)].sort_values("event_date").copy()
    if ev.empty:
        st.info("No core activity participation found.")
        return
    st.dataframe(ev[["event_date", "event_category", "event_name", "source_sheet", "origin"]], use_container_width=True, hide_index=True)


def render_methodology(data: Dict, tif_path: Optional[Path]):
    st.subheader("Methodology & Data Quality")
    st.markdown(
        """
        **Outcome source:** Master UG/PG provides the offered roster; payment date and final status are resolved from
        Tetr-X UG/PG first, with Master values as fallback. UG deferrals and PG `Admitted: Deferral` are treated as paid/final admitted; refunds are excluded from the default final-admitted outcome.

        **Pre-payment window:** Offer Date → Payment Date for students who paid; Offer Date → Deadline for students who did not pay.

        **Core activities:** Online Event, Masterclass, Competition, Hackathon, and TIF only. General/Fun/Poll/Quiz are deliberately excluded from this simulator.

        **Engagement lift:** calculated only from core participation inside each student's valid pre-payment window, so post-payment Tetr-X activity is not credited back to the event.

        **Eligible non-attendee comparison:** students present in the same source/batch event sheets and active on the event date; Tetr-X-only rows are excluded from the control pool. Deadline-adjusted lift removes event/index dates within the configured deadline-confound window.

        **Matching:** exact normalized email → unique normalized full name → unique last-8 phone digits.
        """
    )
    students = data.get("students", pd.DataFrame())
    if students is not None and not students.empty:
        dq1, dq2, dq3, dq4 = st.columns(4)
        dq1.metric("Students loaded", f"{len(students):,}")
        dq2.metric("Missing offer date", f"{students['offered_date'].isna().sum():,}")
        dq3.metric("Missing deadline", f"{students['deadline'].isna().sum():,}")
        dq4.metric("Payment dates resolved", f"{students['payment_date'].notna().sum():,}")

    st.write(f"**TIF file:** {data.get('tif_loaded_name', 'Not loaded')}")
    if tif_path:
        st.write(f"**TIF local path:** `{tif_path.name}`")
    missing = data.get("missing_expected", [])
    if missing:
        st.warning("Missing expected Google Sheet tabs: " + ", ".join(missing))
    unmatched = data.get("tif_unmatched", pd.DataFrame())
    if unmatched is not None and not unmatched.empty:
        st.warning(f"TIF records unmatched to the UG/PG offered base: {len(unmatched):,}")
        st.dataframe(unmatched.head(100), use_container_width=True, hide_index=True)


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------

def main():
    st.title("Tetr Activity Impact Simulator")
    st.caption("Live source: Google Sheets · spreadsheet ID from Streamlit Secrets" if secret_has_sheet_id else "Live source: Google Sheets · using the same master-sheet fallback as the previous Tetr dashboard")
    st.caption("What should we continue, optimize, consolidate or remove — and what could happen to payments and engagement if we do?")
    render_method_note()

    spreadsheet_id = get_spreadsheet_id()
    try:
        secret_has_sheet_id = bool(clean_text(st.secrets.get("GSHEET_SPREADSHEET_ID", "")))
    except Exception:
        secret_has_sheet_id = False
    tif_path = discover_tif_file()
    default_tif_date = get_default_tif_date()

    with st.sidebar:
        st.markdown("## Impact Simulator")
        section = st.radio("Section", ["UG Impact", "PG Impact"], index=0)
        program = "UG" if section.startswith("UG") else "PG"

        st.markdown("---")
        st.markdown("### Analysis settings")
        outcome_label = st.radio(
            "Primary outcome",
            ["Final admitted", "Ever paid incl. refunds"],
            index=0,
            help="Final admitted = Admitted + valid Deferral, refunds excluded.",
        )
        outcome_col = "is_final_admitted" if outcome_label == "Final admitted" else "ever_paid"
        engagement_window = st.slider("Engagement before/after window (days)", 7, 30, 14, 1)
        deadline_confound_days = st.slider("Flag event as deadline-confounded within", 0, 10, 3, 1, format="%d days")
        catalyst_after_count = st.slider("Catalyst = at least this many later activities", 2, 6, 3, 1)
        reactivation_gap_days = st.slider("Reactivation inactivity gap", 7, 30, 10, 1, format="%d days")

        st.markdown("---")
        tif_date = st.date_input("TIF fallback activity date", value=default_tif_date.date())
        st.caption("Used only when the local TIF file has no date column.")

        if st.button("Refresh live data"):
            st.cache_data.clear()
            st.cache_resource.clear()
            st.rerun()

    if not spreadsheet_id:
        st.error("Google Sheet ID could not be resolved. Add `GSHEET_SPREADSHEET_ID` to Streamlit Secrets.")
        st.stop()
    if not get_service_account_dict():
        st.error("Add `[GOOGLE_SERVICE_ACCOUNT]` to Streamlit Secrets, using the same service-account JSON as your existing dashboard.")
        st.stop()

    try:
        with st.spinner("Loading live Google Sheet and building student timelines..."):
            data = build_model(
                spreadsheet_id,
                str(tif_path) if tif_path else "",
                pd.Timestamp(tif_date).strftime("%Y-%m-%d"),
            )
    except Exception as e:
        st.exception(e)
        st.stop()

    students = data["students"]
    attendance = data["attendance"]
    occurrences = data["occurrences"]
    membership = data["membership"]

    with st.sidebar:
        st.success(f"Google Sheets connected · {len(data['sheet_names'])} tabs")
        if tif_path:
            st.success(f"TIF loaded · {data['tif_loaded_name']}")
        else:
            st.warning("No local TIF file found. Commit it beside this .py file or set TIF_FILE in Secrets.")

    settings = {
        "engagement_window": engagement_window,
        "deadline_confound_days": deadline_confound_days,
        "catalyst_after_count": catalyst_after_count,
        "reactivation_gap_days": reactivation_gap_days,
    }

    st.header(f"{program} Impact Analysis")
    render_overview(students, attendance, program, outcome_col)

    matrix = compute_impact_matrix_cached(
        students, attendance, occurrences, membership, program, outcome_col,
        engagement_window, deadline_confound_days, catalyst_after_count, reactivation_gap_days,
    )

    tab1, tab2, tab3, tab4 = st.tabs([
        "Impact Matrix", "Removal Simulator", "Student Journeys", "Methodology",
    ])
    with tab1:
        render_impact_matrix(matrix)
    with tab2:
        render_removal_simulator(
            students, attendance, occurrences, membership, program, outcome_col, settings
        )
    with tab3:
        render_student_journeys(students, attendance, program)
    with tab4:
        render_methodology(data, tif_path)


if __name__ == "__main__":
    main()
