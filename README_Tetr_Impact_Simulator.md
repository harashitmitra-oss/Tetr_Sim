# Tetr Activity Impact Simulator

## Files
- `tetr_impact_simulator.py` — Streamlit app
- `requirements.txt` — Python dependencies
- `runtime.txt` — pins Python 3.12 for Streamlit Cloud

## Google Sheets secrets
Use the same Streamlit Secrets structure as the existing Tetr dashboard:

```toml
GSHEET_SPREADSHEET_ID = "YOUR_SHEET_ID"

[GOOGLE_SERVICE_ACCOUNT]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "..."
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
```

Optional:

```toml
TIF_FILE = "Tetr Innovation Fund - Overall Data.csv"
TIF_EVENT_DATE = "2026-06-16"
```

`TIF_FILE` is optional. If omitted, the app automatically looks beside the Python file for a CSV/XLSX whose filename contains `TIF` or `Innovation Fund`.

## TIF file
Commit your TIF CSV/XLSX into the same GitHub repository as the Streamlit app. The historical Overall Data format with columns such as `Name`, `Email`, `Program`, `Status`, `Step`, and `Venture` is supported.

If the TIF file has no activity/registration date column, the app uses the fallback TIF date shown in the sidebar. The default is 16-Jun-2026 and can be changed without changing code.

## What the app reads from Google Sheets
- Master UG / Master PG
- UG batch sheets through UG B16
- PG batch sheets through PG B8
- Tetr-X-UG / Tetr-X-PG
- Dates

## Main sections
- UG Impact
- PG Impact

Each includes:
- Impact Matrix
- Removal Simulator
- Student Journeys
- Methodology & Data Quality

## Core activity types
- Online Event
- Masterclass
- Competition
- Hackathon
- TIF

Online Events can be analysed as all events, by speaker/category (Pratham, Tarun, Garima, Mohammed, etc.), or by individual event.

## Important interpretation
The simulator deliberately does **not** treat `paid after event` as proof that the event caused payment. It flags deadline proximity, prior engagement, replacement/overlap, first/last touch, catalyst behaviour, and reactivation. The removal output is a behavioural risk range, not a causal forecast.
