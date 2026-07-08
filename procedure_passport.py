import streamlit as st
import time
import pandas as pd
import uuid
import datetime
import io
import json
import html
import gspread
from gspread_dataframe import get_as_dataframe, set_with_dataframe
from google.oauth2.service_account import Credentials
import numpy as np


st.set_page_config(
    page_title="Procedure Passport",
    page_icon="🩺",
    layout="wide",
)

# ─────────────────────────────────────────────
# QUERY PARAMS  (magic link routing)
# ─────────────────────────────────────────────
query_params = st.query_params

# Only auto-route on the first load; once submitted we stay on the confirmation page.
if (
    query_params.get("mode") == "attending"
    and st.session_state.get("page", "login") not in ("attending_confirmation",)
    and not st.session_state.get("_magic_routed")
):
    st.session_state["page"]           = "attending_assessment"
    st.session_state["resident"]       = query_params.get("resident", "")
    st.session_state["procedure_id"]   = query_params.get("procedure_id", "")
    st.session_state["specialty_id"]   = query_params.get("specialty_id", "")
    st.session_state["attending_name"] = query_params.get("attending_name", "")
    st.session_state["_magic_routed"]  = True

# ─────────────────────────────────────────────
# SESSION STATE DEFAULTS
# ─────────────────────────────────────────────
_defaults: dict = {
    "page":                    "login",
    "resident":                None,
    "resident_name":           "",
    "scores":                  {},
    "date":                    datetime.date.today(),
    "notes":                   "",
    "current_case_id":         None,
    "attending_submission":    None,   # filled after magic-link submit
}
for _k, _v in _defaults.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
ADMINS = ["procedurepassport@gmail.com"]

RATING_OPTIONS = ["Not Assessed", "Shown/Told", "Not Yet", "Steer", "Prompt", "Back up", "Auto"]
RATING_TO_NUM  = {
    "Not Assessed": -1,
    "Shown/Told":    0,
    "Not Yet":       1,
    "Steer":         2,
    "Prompt":        3,
    "Back up":       4,
    "Auto":          5,
}
RATING_HEX = {
    "Not Assessed": "#F0F0F0",  # white/empty — explicitly rated as not assessed
    "Shown/Told":   "#9E9E9E",  # dark gray — explicitly shown or told
    "Not Yet":      "#5B8DB8",
    "Steer":        "#FF944D",
    "Prompt":       "#FFD633",
    "Back up":      "#99E699",
    "Auto":         "#33CC33",
}
RATING_COLOR = {
    k: f"background-color:{v}; color:{'white' if k in ('Not Yet','Auto') else 'black'};"
    for k, v in RATING_HEX.items()
}

def fmt_date(d):
    """Format a date value as MM-DD-YYYY; pass through non-date strings unchanged."""
    try:
        if pd.isna(d):
            return ""
    except TypeError:
        pass
    try:
        return pd.Timestamp(d).strftime("%m-%d-%Y")
    except Exception:
        return str(d)


def _norm_id(series: pd.Series) -> pd.Series:
    """Normalise a case_id Series to clean strings regardless of pandas version.

    pandas 3.x can infer all-digit hex IDs as float64, making astype(str)
    produce "123456789012.0" while the other sheet retains "123456789012".
    The three-step chain below is safe for every dtype:
      float64  123456789012.0  → "123456789012.0" → strip → remove .0 → "123456789012"
      int64    123456789012    → "123456789012"   → strip → no-op      → "123456789012"
      object   "abc123def456"  → "abc123def456"   → strip → no-op      → "abc123def456"
    """
    return (series.astype(str)
                  .str.strip()
                  .str.replace(r"\.0$", "", regex=True))


COMPLEXITY_HEX = {
    "Straight Forward": "#C8E6C9",
    "Moderate":         "#FFF59D",
    "Complex":          "#FFAB91",
}
O_SCORE_HEX = {
    "1": "#378ADD",
    "2": "#FF944D",
    "3": "#FFD633",
    "4": "#99E699",
    "5": "#33CC33",
}
O_SCORE_OPTIONS = [
    "— Make a selection —",
    "1 - Not Yet",
    "2 - Steer",
    "3 - Prompt",
    "4 - Backup",
    "5 - Auto",
]

SHEET_RESIDENTS  = "residents"
SHEET_ATTENDINGS = "attendings"
SHEET_PROCEDURES = "procedures"
SHEET_STEPS      = "steps"
SHEET_CASES      = "cases"
SHEET_SCORES     = "scores"
SHEET_SPECIALTY  = "specialties"

# ─────────────────────────────────────────────
# GOOGLE SHEETS HELPERS
# ─────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def get_gs_client():
    """Authorized gspread client — cached for the entire app session."""
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"],
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(creds)


def get_sheet(sheet_name: str):
    """Return a gspread worksheet, creating it if missing."""
    try:
        gc = get_gs_client()
        sh = gc.open_by_key(st.secrets["GOOGLE_SHEET_KEY"])
        try:
            return sh.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            return sh.add_worksheet(title=sheet_name, rows="500", cols="26")
    except Exception as exc:
        raise ConnectionError(f"Cannot reach Google Sheets: {exc}") from exc


@st.cache_data(ttl=300, show_spinner=False)
def read_sheet_df(sheet_name: str, expected_cols=None) -> pd.DataFrame:
    """Cached worksheet read (300 s TTL).  Returns empty DF if sheet is blank."""
    ws  = get_sheet(sheet_name)
    df  = get_as_dataframe(ws, evaluate_formulas=True, header=0)
    df  = df.dropna(how="all")
    if df.empty and expected_cols:
        return pd.DataFrame(columns=expected_cols)
    if expected_cols:
        for col in expected_cols:
            if col not in df.columns:
                df[col] = pd.NA
        df = df[expected_cols]
    return df


def write_sheet_df(sheet_name: str, df: pd.DataFrame) -> None:
    """Overwrite a worksheet then clear all cached reads so the UI stays fresh."""
    ws = get_sheet(sheet_name)
    ws.clear()
    set_with_dataframe(ws, df, include_index=False, include_column_header=True)
    st.cache_data.clear()  # invalidate all read caches after every write


@st.cache_data(ttl=300, show_spinner=False)
def load_refs():
    """Load all reference tables in one shot (cached 300 s)."""
    def _safe(name, cols):
        try:
            return read_sheet_df(name, expected_cols=cols)
        except Exception:
            return pd.DataFrame(columns=cols)

    spec_df  = _safe(SHEET_SPECIALTY,  ["specialty_id",  "specialty_name"])
    proc_df  = _safe(SHEET_PROCEDURES, ["procedure_id",  "procedure_name", "specialty_id"])
    steps_df = _safe(SHEET_STEPS,      ["step_id",       "procedure_id",   "step_order", "step_name"])
    atnd_df  = _safe(SHEET_ATTENDINGS, ["attending_id",  "attending_name", "specialty_id", "email"])
    return spec_df, proc_df, steps_df, atnd_df


# ─────────────────────────────────────────────
# DATA MUTATION HELPERS
# ─────────────────────────────────────────────

def ensure_resident(email: str, name: str = "", specialty_id=None) -> None:
    cols = ["email", "name", "specialty_id", "created_at"]
    df   = read_sheet_df(SHEET_RESIDENTS, expected_cols=cols)
    if email not in df["email"].values:
        df = pd.concat([df, pd.DataFrame([{
            "email":        email,
            "name":         name,
            "specialty_id": specialty_id,
            "created_at":   datetime.datetime.utcnow().isoformat(),
        }])], ignore_index=True)
        write_sheet_df(SHEET_RESIDENTS, df)   # also clears cache


def ensure_attending(name: str, specialty_id: str, email: str = "") -> None:
    cols = ["attending_id", "attending_name", "specialty_id", "email"]
    df   = read_sheet_df(SHEET_ATTENDINGS, expected_cols=cols)
    if name not in df["attending_name"].values:
        att_id = "A_" + specialty_id + "_" + name.replace(" ", "_").upper()
        df = pd.concat([df, pd.DataFrame([{
            "attending_id":   att_id,
            "attending_name": name,
            "specialty_id":   specialty_id,
            "email":          email,
        }])], ignore_index=True)
        write_sheet_df(SHEET_ATTENDINGS, df)


def ensure_procedure(proc_id: str, proc_name: str, specialty_id: str, steps_list: list) -> None:
    proc_cols = ["procedure_id", "procedure_name", "specialty_id"]
    procs_df  = read_sheet_df(SHEET_PROCEDURES, expected_cols=proc_cols)
    if proc_id not in procs_df["procedure_id"].values:
        procs_df = pd.concat([procs_df, pd.DataFrame([{
            "procedure_id":   proc_id,
            "procedure_name": proc_name,
            "specialty_id":   specialty_id,
        }])], ignore_index=True)
        write_sheet_df(SHEET_PROCEDURES, procs_df)

    step_cols = ["step_id", "procedure_id", "step_order", "step_name"]
    steps_df  = read_sheet_df(SHEET_STEPS, expected_cols=step_cols)
    if not (steps_df["procedure_id"] == proc_id).any():
        new_steps = pd.DataFrame([{
            "step_id":      f"S_{proc_id}_{i+1:02d}",
            "procedure_id": proc_id,
            "step_order":   i + 1,
            "step_name":    step,
        } for i, step in enumerate(steps_list)])
        steps_df = pd.concat([steps_df, new_steps], ignore_index=True)
        write_sheet_df(SHEET_STEPS, steps_df)


def save_case(
    resident_email: str,
    date,
    specialty_id: str,
    procedure_id: str,
    attending_id: str,
    scores_dict: dict,
    notes: str = "",
    case_complexity=None,
    overall_performance=None,
) -> str:
    """Persist a case + its step scores; returns the new case_id."""
    case_id   = uuid.uuid4().hex[:12]

    case_cols = ["case_id", "resident_email", "date", "specialty_id",
                 "procedure_id", "attending_id", "notes",
                 "case_complexity", "overall_performance"]
    cases_df  = read_sheet_df(SHEET_CASES, expected_cols=case_cols)
    cases_df  = pd.concat([cases_df, pd.DataFrame([{
        "case_id":             case_id,
        "resident_email":      resident_email,
        "date":                str(date),
        "specialty_id":        specialty_id,
        "procedure_id":        procedure_id,
        "attending_id":        attending_id,
        "notes":               notes,
        "case_complexity":     case_complexity,
        "overall_performance": overall_performance,
    }])], ignore_index=True)
    write_sheet_df(SHEET_CASES, cases_df)  # clears cache

    score_cols = ["case_id", "step_id", "rating", "rating_num",
                  "case_complexity", "overall_performance"]
    scores_df  = read_sheet_df(SHEET_SCORES, expected_cols=score_cols)
    # Normalise existing case_ids before concat so the written sheet is consistent.
    if not scores_df.empty:
        scores_df["case_id"] = _norm_id(scores_df["case_id"])
    new_rows   = [{
        "case_id":             case_id,
        "step_id":             step_id,
        "rating":              rating,
        "rating_num":          RATING_TO_NUM.get(rating),
        "case_complexity":     case_complexity,
        "overall_performance": overall_performance,
    } for step_id, rating in scores_dict.items()]
    scores_df  = pd.concat([scores_df, pd.DataFrame(new_rows)], ignore_index=True)
    write_sheet_df(SHEET_SCORES, scores_df)  # clears cache

    return case_id


# ─────────────────────────────────────────────
# STYLING HELPERS
# ─────────────────────────────────────────────

def style_df(df: pd.DataFrame, col: str):
    return df.style.map(lambda v: RATING_COLOR.get(v, ""), subset=[col])


def attending_display_name(attending_id: str, atnds_lookup: dict) -> str:
    """Resolve a display name from an attending_id, including magic_ IDs."""
    if attending_id in atnds_lookup:
        return atnds_lookup[attending_id]
    if isinstance(attending_id, str) and attending_id.startswith("magic_"):
        return attending_id[len("magic_"):].replace("_", " ")
    return attending_id or "Unknown"


def show_gs_error(exc: Exception) -> None:
    st.error(
        "⚠️ **Could not reach Google Sheets.** "
        "Check your network connection or try refreshing the page.\n\n"
        f"_Details: {exc}_"
    )


# ─────────────────────────────────────────────
# NAV HELPER
# ─────────────────────────────────────────────
def go_to(page: str) -> None:
    st.session_state["page"] = page
    st.rerun()


# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
st.sidebar.title("🩺 Procedure Passport")

_logged_in = st.session_state.get("resident")
if _logged_in in ADMINS:
    if st.sidebar.button("⚙️ Admin Panel"):
        go_to("admin")

if _logged_in and st.session_state["page"] not in ("login", "attending_assessment", "attending_confirmation"):
    st.sidebar.markdown(f"👤 **{st.session_state.get('resident_name', '')}**")
    st.sidebar.markdown(f"_{_logged_in}_")
    st.sidebar.markdown("---")
    if st.sidebar.button("🚪 Logout"):
        for _k in list(st.session_state.keys()):
            del st.session_state[_k]
        st.cache_data.clear()
        st.rerun()

# ── Sidebar nav shortcuts (shown when logged in on relevant pages) ──
if _logged_in and st.session_state["page"] not in ("login", "attending_assessment", "attending_confirmation"):
    st.sidebar.markdown("---")
    if st.sidebar.button("➕ Start Assessment", key="sb_start"):
        st.session_state["page"] = "start"
        st.rerun()
    if st.sidebar.button("📊 Cumulative Dashboard", key="sb_cumulative"):
        st.session_state["page"] = "cumulative"
        st.rerun()
    if st.sidebar.button("💬 Comments Dashboard", key="sb_comments"):
        st.session_state["page"] = "comments"
        st.rerun()
    if st.sidebar.button("🏠 Back to Home", key="sb_home"):
        st.session_state["page"] = "home"
        st.rerun()

# ── Sidebar rating legend (shown only on relevant pages) ──
if st.session_state.get("page") in ("start", "assessment", "dashboard", "cumulative", "attending_assessment"):
    st.sidebar.markdown("---")
    st.sidebar.markdown("**Rating Scale**")
    _LEGEND_ITEMS = [
        ("Not Assessed",   "#F0F0F0", "1px solid #aaa"),
        ("Shown/Told",     "#9E9E9E", ""),
        ("Not Yet",        "#378ADD", ""),
        ("Steer",          "#FF944D", ""),
        ("Prompt",         "#FFD633", ""),
        ("Back up",        "#99E699", ""),
        ("Auto",           "#33CC33", ""),
        ("Never Attempted","#E0E0E0", ""),
    ]
    for _label, _color, _border in _LEGEND_ITEMS:
        _border_css = f"border:{_border};" if _border else ""
        st.sidebar.markdown(
            f'<span style="display:inline-block;width:13px;height:13px;'
            f'background:{_color};{_border_css}border-radius:2px;'
            f'margin-right:6px;vertical-align:middle;"></span>{_label}',
            unsafe_allow_html=True,
        )

# ─────────────────────────────────────────────
# SHARED CSS
# ─────────────────────────────────────────────
st.markdown(
    """
<style>
/* Card-style sections */
.pp-card {
    background: var(--secondary-background-color);
    border-radius: 10px;
    padding: 1.2rem 1.5rem;
    margin-bottom: 1rem;
}
/* Pill badge */
.pp-badge {
    display: inline-block;
    border-radius: 12px;
    padding: 2px 10px;
    font-size: 0.82rem;
    font-weight: 600;
    margin: 2px;
}
/* Legend row */
.legend-row {
    display: flex;
    gap: 1rem;
    flex-wrap: wrap;
    align-items: center;
    margin-bottom: 0.5rem;
}
.legend-item {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-size: 0.85rem;
}
.legend-swatch {
    width: 14px;
    height: 14px;
    border-radius: 3px;
    border: 1px solid var(--secondary-background-color);
    display: inline-block;
}
/* Home page cards: keep the three action buttons vertically aligned
   even when title/description text wraps to different heights. */
.st-key-home_cards [data-testid="stColumn"] > [data-testid="stVerticalBlock"] {
    display: flex;
    flex-direction: column;
    height: 100%;
}
.st-key-home_cards [data-testid="stElementContainer"]:has([data-testid="stButton"]) {
    margin-top: auto;
}
.st-key-home_cards [data-testid="stElementContainer"]:has(h3) {
    height: 5.5rem;
    overflow: hidden;
    container-type: inline-size;
}
.st-key-home_cards [data-testid="stElementContainer"]:has(h3) h3 > span:first-child {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    font-size: clamp(0.95rem, 12cqw, 1.75rem);
    line-height: 1.2;
}
</style>
""",
    unsafe_allow_html=True,
)

# ─────────────────────────────────────────────
# PAGE ROUTER
# ─────────────────────────────────────────────
page = st.session_state["page"]


# ════════════════════════════════════════════════════════════
# PAGE: LOGIN
# ════════════════════════════════════════════════════════════
if page == "login":
    col_c, col_r = st.columns([1, 1])
    with col_c:
        st.markdown("# 🩺 Procedure Passport")
        st.markdown("_Track your surgical skills journey, one procedure at a time._")
        st.markdown("---")
        email = st.text_input("Email address", placeholder="you@hospital.org")

        if st.button("Login →", width="stretch", type="primary"):
            if not email.strip():
                st.error("Please enter your email address.")
            else:
                try:
                    residents = read_sheet_df(
                        SHEET_RESIDENTS,
                        expected_cols=["email", "name", "specialty_id", "created_at"],
                    )
                    if email in ADMINS:
                        st.session_state.update(
                            resident=email, resident_name="Admin", page="admin"
                        )
                        st.rerun()
                    elif email in residents["email"].values:
                        row = residents.loc[residents["email"] == email].iloc[0]
                        st.session_state.update(
                            resident=email,
                            resident_name=row["name"],
                            specialty_id=row["specialty_id"],
                            page="home",
                        )
                        st.rerun()
                    else:
                        st.error("❌ Email not recognised. Ask an admin to add you.")
                except ConnectionError as exc:
                    show_gs_error(exc)


# ════════════════════════════════════════════════════════════
# PAGE: ADMIN PANEL
# ════════════════════════════════════════════════════════════
elif page == "admin":
    st.title("⚙️ Admin Panel")
    if st.button("🏠 Back to Home", key="admin_home_top"):
        go_to("home")

    if st.button("🔄 Reload Data"):
        st.cache_data.clear()
        st.rerun()

    # ── Specialties ──────────────────────────────────────
    st.subheader("Specialties")
    try:
        specialties = read_sheet_df(SHEET_SPECIALTY, expected_cols=["specialty_id", "specialty_name"])
        st.dataframe(specialties, width="stretch")

        with st.expander("➕ Add Specialty"):
            new_spec_id   = st.text_input("Specialty ID (e.g., GS)")
            new_spec_name = st.text_input("Specialty name (e.g., General Surgery)")
            if st.button("Add Specialty", key="btn_add_spec"):
                if new_spec_id and new_spec_name:
                    if new_spec_id in specialties["specialty_id"].values:
                        st.warning("That ID already exists.")
                    else:
                        specialties = pd.concat(
                            [specialties, pd.DataFrame([{"specialty_id": new_spec_id,
                                                          "specialty_name": new_spec_name}])],
                            ignore_index=True,
                        )
                        write_sheet_df(SHEET_SPECIALTY, specialties)
                        st.success(f"✅ Added {new_spec_name}")
                        time.sleep(0.5)
                        st.rerun()
                else:
                    st.error("Please fill in both fields.")
    except ConnectionError as exc:
        show_gs_error(exc)

    st.markdown("---")

    # ── Residents ────────────────────────────────────────
    st.subheader("Residents")
    try:
        spec_df = read_sheet_df(SHEET_SPECIALTY, expected_cols=["specialty_id", "specialty_name"])
        spec_name_to_id = dict(zip(spec_df["specialty_name"], spec_df["specialty_id"]))

        residents = read_sheet_df(
            SHEET_RESIDENTS, expected_cols=["email", "name", "specialty_id", "created_at"]
        )
        disp = residents.merge(spec_df, how="left", on="specialty_id")
        st.dataframe(disp[["email", "name", "specialty_name", "created_at"]], width="stretch")

        with st.expander("➕ Add Resident"):
            new_res_email = st.text_input("Email")
            new_res_name  = st.text_input("Full name")
            new_res_spec  = st.selectbox("Specialty", list(spec_name_to_id.keys()), key="add_res_spec")
            if st.button("Add Resident", key="btn_add_res"):
                if new_res_email and new_res_name and new_res_spec:
                    ensure_resident(new_res_email, new_res_name, spec_name_to_id[new_res_spec])
                    st.success(f"✅ Added {new_res_email}")
                    time.sleep(0.5)
                    st.rerun()
                else:
                    st.warning("Please fill in all fields.")

        if not residents.empty:
            with st.expander("🗑️ Delete Resident"):
                del_email = st.selectbox("Select resident to delete", residents["email"], key="del_res")
                if st.button("Delete", key="btn_del_res"):
                    updated = residents[residents["email"] != del_email].reset_index(drop=True)
                    write_sheet_df(SHEET_RESIDENTS, updated)
                    st.success(f"Deleted {del_email}")
                    time.sleep(0.5)
                    st.rerun()
    except ConnectionError as exc:
        show_gs_error(exc)

    st.markdown("---")

    # ── Attendings ───────────────────────────────────────
    st.subheader("Attendings")
    try:
        attendings = read_sheet_df(
            SHEET_ATTENDINGS, expected_cols=["attending_id", "attending_name", "specialty_id", "email"]
        )
        spec_df, _, _, _ = load_refs()
        st.dataframe(attendings, width="stretch")

        with st.expander("➕ Add Attending"):
            new_att_name  = st.text_input("Attending name")
            new_att_spec  = st.selectbox("Specialty", spec_df["specialty_name"], key="add_att_spec")
            new_att_email = st.text_input("Email (optional)")
            if st.button("Add Attending", key="btn_add_att"):
                if new_att_name:
                    _spec_match = spec_df[spec_df["specialty_name"].astype(str).str.strip() == str(new_att_spec).strip()]
                    spec_id = _spec_match["specialty_id"].values[0] if len(_spec_match) > 0 else None
                    ensure_attending(new_att_name, spec_id, new_att_email)
                    st.success(f"✅ Added {new_att_name}")
                    time.sleep(0.5)
                    st.rerun()
                else:
                    st.error("Please enter an attending name.")

        if not attendings.empty:
            with st.expander("🗑️ Delete Attending"):
                del_att = st.selectbox("Select attending to delete", attendings["attending_name"], key="del_att")
                if st.button("Delete", key="btn_del_att"):
                    updated = attendings[attendings["attending_name"] != del_att].reset_index(drop=True)
                    write_sheet_df(SHEET_ATTENDINGS, updated)
                    st.success(f"Deleted {del_att}")
                    time.sleep(0.5)
                    st.rerun()
    except ConnectionError as exc:
        show_gs_error(exc)

    st.markdown("---")

    # ── Procedures ───────────────────────────────────────
    st.subheader("Procedures")
    try:
        spec_df, _, _, _ = load_refs()

        with st.expander("➕ Add New Procedure"):
            new_proc_id   = st.text_input("Procedure ID (e.g., CSEC)").strip().upper()
            new_proc_name = st.text_input("Procedure name (e.g., Cesarean Section)")
            new_proc_spec = st.selectbox("Specialty", spec_df["specialty_name"], key="add_proc_spec")
            steps_raw     = st.text_area("Steps (one per line)")
            new_steps     = [s.strip() for s in steps_raw.split("\n") if s.strip()]
            if st.button("Add Procedure", key="btn_add_proc"):
                if new_proc_id and new_proc_name and new_steps:
                    _spec_match = spec_df[spec_df["specialty_name"].astype(str).str.strip() == str(new_proc_spec).strip()]
                    spec_id = _spec_match["specialty_id"].values[0] if len(_spec_match) > 0 else None
                    ensure_procedure(new_proc_id, new_proc_name, spec_id, new_steps)
                    st.success(f"✅ Added {new_proc_name}")
                    time.sleep(0.5)
                    st.rerun()
                else:
                    st.error("Please fill in all fields and at least one step.")

        with st.expander("✏️ Edit Existing Procedure"):
            procs_df = read_sheet_df(SHEET_PROCEDURES, expected_cols=["procedure_id", "procedure_name", "specialty_id"])
            if procs_df.empty:
                st.info("No procedures yet.")
            else:
                edit_proc    = st.selectbox("Select procedure", procs_df["procedure_name"], key="edit_proc_sel")
                _proc_match = procs_df[procs_df["procedure_name"].astype(str).str.strip() == str(edit_proc).strip()]
                sel_proc_id  = _proc_match["procedure_id"].values[0] if len(_proc_match) > 0 else None
                new_pname    = st.text_input("Updated name", value=edit_proc, key="edit_proc_name")
                new_steps_ra = st.text_area("Updated steps (blank = keep current)", key="edit_proc_steps")
                new_edit_stp = [s.strip() for s in new_steps_ra.split("\n") if s.strip()]

                if st.button("Update Procedure", key="btn_upd_proc"):
                    procs_df.loc[procs_df["procedure_id"] == sel_proc_id, "procedure_name"] = new_pname
                    write_sheet_df(SHEET_PROCEDURES, procs_df)
                    if new_edit_stp:
                        steps_df = read_sheet_df(
                            SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"]
                        )
                        steps_df = steps_df[steps_df["procedure_id"] != sel_proc_id]
                        updated_steps = pd.DataFrame([{
                            "step_id":      f"S_{sel_proc_id}_{i+1:02d}",
                            "procedure_id": sel_proc_id,
                            "step_order":   i + 1,
                            "step_name":    s,
                        } for i, s in enumerate(new_edit_stp)])
                        steps_df = pd.concat([steps_df, updated_steps], ignore_index=True)
                        write_sheet_df(SHEET_STEPS, steps_df)
                    st.success(f"✅ Updated '{new_pname}'")
                    time.sleep(0.5)
                    st.rerun()
    except ConnectionError as exc:
        show_gs_error(exc)

    st.markdown("---")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("⬅️ Back to Login"):
            go_to("login")
    with col2:
        if st.button("🏠 Resident Home"):
            go_to("home")


# ════════════════════════════════════════════════════════════
# PAGE: HOME
# ════════════════════════════════════════════════════════════
elif page == "home":
    st.title(f"👋 Welcome back, {st.session_state['resident_name']}")
    st.markdown("_What would you like to do today?_")
    st.markdown("")
    st.info("📱 On mobile: tap the ≡ icon at top left to access navigation and rating legend.")

    with st.container(key="home_cards"):
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown('<div class="pp-card">', unsafe_allow_html=True)
            st.markdown("### ➕ New Assessment")
            st.markdown("Start a new procedure case and record step ratings.")
            if st.button("Start Assessment", width="stretch", type="primary"):
                go_to("start")
            st.markdown("</div>", unsafe_allow_html=True)

        with c2:
            st.markdown('<div class="pp-card">', unsafe_allow_html=True)
            st.markdown("### 📊 Cumulative Dashboard")
            st.markdown("View your progress heatmap over time.")
            if st.button("View Dashboard", width="stretch"):
                go_to("cumulative")
            st.markdown("</div>", unsafe_allow_html=True)

        with c3:
            st.markdown('<div class="pp-card">', unsafe_allow_html=True)
            st.markdown("### 💬 Comments")
            st.markdown("Browse and export all attending feedback.")
            if st.button("View Comments", width="stretch"):
                go_to("comments")
            st.markdown("</div>", unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════
# PAGE: START CASE
# ════════════════════════════════════════════════════════════
elif page == "start":
    st.title("📋 Start Assessment")
    if st.button("🏠 Back to Home", key="start_home_top"):
        go_to("home")
    st.info("📱 On mobile: tap the ≡ icon at top left to view the sidebar.")

    try:
        spec_df, proc_df, steps_df, atnd_df = load_refs()
    except ConnectionError as exc:
        show_gs_error(exc)
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    spec_map   = dict(zip(spec_df["specialty_name"], spec_df["specialty_id"]))
    id_to_name = dict(zip(spec_df["specialty_id"],   spec_df["specialty_name"]))
    is_admin   = st.session_state["resident"] in ADMINS

    if is_admin:
        selected_spec_name = st.selectbox("Specialty", list(spec_map.keys()))
        specialty_id       = spec_map[selected_spec_name]
        st.session_state["specialty_id"] = specialty_id
    else:
        specialty_id       = st.session_state.get("specialty_id")
        selected_spec_name = id_to_name.get(specialty_id, "Unknown Specialty")
        st.markdown(f"**Specialty:** {selected_spec_name}")
        if specialty_id is None:
            st.error("No specialty assigned. Contact an admin.")
            st.stop()

    procs = proc_df[proc_df["specialty_id"] == specialty_id]
    atnds = atnd_df[atnd_df["specialty_id"] == specialty_id]

    if procs.empty:
        st.warning("⚠️ No procedures configured for this specialty.")
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()
    if atnds.empty:
        st.warning("⚠️ No attendings configured for this specialty.")
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    proc_map = dict(zip(procs["procedure_name"], procs["procedure_id"]))
    atnd_map = dict(zip(atnds["attending_name"], atnds["attending_id"]))

    procedure = st.selectbox("Procedure", sorted(proc_map.keys()))
    attending = st.selectbox("Attending", sorted(atnd_map.keys(), key=lambda n: n.split()[-1] if n.split() else n))
    case_date = st.date_input("Date", st.session_state["date"])

    st.session_state["procedure_id"] = proc_map[procedure]
    st.session_state["attending_id"] = atnd_map[attending]
    st.session_state["date"]         = case_date

    # ── Magic link for attending ──────────────────────────
    if not is_admin:
        _att_match = atnds[atnds["attending_id"].astype(str).str.strip() == str(st.session_state.get("attending_id", "")).strip()]
        safe_att  = _att_match["attending_name"].values[0].replace(" ", "_") if len(_att_match) > 0 else "Unknown"
        base_url  = st.secrets.get("APP_BASE_URL", "https://procedurepassport.streamlit.app")
        magic_url = (
            f"{base_url}/?mode=attending"
            f"&resident={st.session_state['resident']}"
            f"&procedure_id={st.session_state['procedure_id']}"
            f"&specialty_id={specialty_id}"
            f"&attending_name={safe_att}"
        )
        with st.expander("🔗 Magic Link for Attending (click to expand)", expanded=False):
            st.markdown(
                "Share this link with your attending so they can submit their evaluation directly:"
            )
            st.code(magic_url, language="text")
            st.caption("On mobile: tap the link once for the copy button to appear.")

    st.markdown("---")
    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        if st.button("⬅️ Back to Home"):
            go_to("home")
    with col3:
        if st.button("Start Assessment →", type="primary", width="stretch"):
            st.session_state["scores"] = {}
            st.session_state["notes"]  = ""
            go_to("assessment")


# ════════════════════════════════════════════════════════════
# PAGE: ASSESSMENT
# ════════════════════════════════════════════════════════════
elif page == "assessment":
    try:
        _, proc_df, steps_df, _ = load_refs()
    except ConnectionError as exc:
        show_gs_error(exc)
        if st.button("⬅️ Back to Start"):
            go_to("start")
        st.stop()

    steps = steps_df[steps_df["procedure_id"] == st.session_state["procedure_id"]].sort_values("step_order")
    if steps.empty:
        st.error("No steps defined for this procedure. Ask an admin to add steps.")
        if st.button("⬅️ Back to Start"):
            go_to("start")
        st.stop()

    # Resolve procedure name for the page title (Fix 3)
    _proc_rows = proc_df.loc[proc_df["procedure_id"] == st.session_state["procedure_id"], "procedure_name"].values
    _proc_name = _proc_rows[0] if len(_proc_rows) else "Assessment"
    st.title(f"📝 {_proc_name} Assessment")

    # Back button placed at the top, clearly separated from Finish (Fix 7)
    _top_cols_assess = st.columns([1, 1, 4])
    with _top_cols_assess[0]:
        if st.button("⬅️ Back to Start", key="back_top"):
            go_to("start")
    with _top_cols_assess[1]:
        if st.button("🏠 Home", key="assess_home_top"):
            go_to("home")
    st.markdown("---")
    st.info("📱 On mobile: tap the ≡ icon at top left to view the sidebar.", icon=None)

    _cc_opts = ["— Select complexity —", "Straight Forward", "Moderate", "Complex"]
    _cc_default = st.session_state.get("case_complexity", "— Select complexity —")
    _cc_idx = _cc_opts.index(_cc_default) if _cc_default in _cc_opts else 0
    st.session_state["case_complexity"] = st.selectbox(
        "Case Complexity",
        _cc_opts,
        index=_cc_idx,
    )

    st.markdown("#### Step-Level Ratings")

    # Fix 5: also reset the widget key state so selectboxes visually update
    if st.button("↺ Mark All as 'Not Assessed'"):
        for _, row in steps.iterrows():
            st.session_state["scores"][row["step_id"]] = "Not Assessed"
            st.session_state[f"score_{row['step_id']}"] = "Not Assessed"
        st.rerun()

    # Fix 6: reverting to "Not Assessed" is supported — "Not Assessed" is index 0
    # in RATING_OPTIONS so the user can always select it from the dropdown.
    for _, row in steps.iterrows():
        step_id   = row["step_id"]
        step_name = row["step_name"]
        current   = st.session_state["scores"].get(step_id, "Not Assessed")
        st.session_state["scores"][step_id] = st.selectbox(
            step_name,
            RATING_OPTIONS,
            index=RATING_OPTIONS.index(current) if current in RATING_OPTIONS else 0,
            key=f"score_{step_id}",
        )

    current_o = st.session_state.get("overall_performance", O_SCORE_OPTIONS[0])
    st.session_state["overall_performance"] = st.selectbox(
        "Overall Performance Rating",
        O_SCORE_OPTIONS,
        index=O_SCORE_OPTIONS.index(current_o) if current_o in O_SCORE_OPTIONS else 0,
    )

    with st.expander("💬 Comment guide (tap to expand)", expanded=False):
        st.markdown(
            "_Use these prompts to structure your feedback:_\n\n"
            "- The resident demonstrated ___\n"
            "- Improvements made on ___\n"
            "- Still working on ___\n"
            "- Next steps/improvements expected ___"
        )
    st.session_state["notes"] = st.text_area("Comments / Feedback", st.session_state.get("notes", ""))

    if all(v == "Not Assessed" for v in st.session_state["scores"].values()):
        st.warning("⚠️ All steps are marked 'Not Assessed'.")

    # Fix 7: Finish button alone at the bottom with a confirmation note
    st.markdown("---")
    st.caption("✅ The case is saved automatically when you click Finish & Save.")
    if st.button("🏁 Finish & Save →", type="primary", width="stretch"):
        if st.session_state.get("case_complexity", "— Select complexity —") == "— Select complexity —":
            st.warning("Please select a Case Complexity.")
        elif st.session_state["overall_performance"] == O_SCORE_OPTIONS[0]:
            st.warning("Please select an Overall Performance rating.")
        else:
            try:
                st.session_state["current_case_id"] = save_case(
                    resident_email=st.session_state["resident"],
                    date=st.session_state["date"],
                    specialty_id=st.session_state["specialty_id"],
                    procedure_id=st.session_state["procedure_id"],
                    attending_id=st.session_state["attending_id"],
                    scores_dict=st.session_state["scores"],
                    case_complexity=st.session_state["case_complexity"],
                    overall_performance=st.session_state["overall_performance"],
                    notes=st.session_state.get("notes", ""),
                )
                go_to("dashboard")
            except ConnectionError as exc:
                show_gs_error(exc)


# ════════════════════════════════════════════════════════════
# PAGE: SINGLE-CASE DASHBOARD
# ════════════════════════════════════════════════════════════
elif page == "dashboard":
    try:
        _, _, steps_df, _ = load_refs()
    except ConnectionError as exc:
        show_gs_error(exc)
        st.stop()

    steps = steps_df[steps_df["procedure_id"] == st.session_state["procedure_id"]].sort_values("step_order")

    st.title("✅ Case Saved")
    st.success(f"Case ID: `{st.session_state.get('current_case_id', '—')}`")

    data = [{"Step": row["step_name"],
             "Rating": st.session_state["scores"].get(row["step_id"], "")}
            for _, row in steps.iterrows()]
    df   = pd.DataFrame(data)
    st.dataframe(style_df(df, "Rating"), width="stretch")

    meta_col1, meta_col2 = st.columns(2)
    with meta_col1:
        st.markdown(f"**Date:** {fmt_date(st.session_state.get('date', ''))}")
        st.markdown(f"**Case Complexity:** {st.session_state.get('case_complexity', '—')}")
    with meta_col2:
        st.markdown(f"**Overall Performance:** {st.session_state.get('overall_performance', '—')}")

    if st.session_state.get("notes", "").strip():
        st.markdown("**Comments:**")
        st.info(st.session_state["notes"])

    st.markdown("---")
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("⬅️ Back to Assessment"):
            go_to("assessment")
    with col2:
        if st.button("🏠 Home"):
            go_to("home")
    with col3:
        if st.button("➕ New Assessment", type="primary"):
            go_to("start")


# ════════════════════════════════════════════════════════════
# PAGE: COMMENTS DASHBOARD
# ════════════════════════════════════════════════════════════
elif page == "comments":
    st.title("💬 Comments Dashboard")
    if st.button("🏠 Back to Home", key="comments_home_top"):
        go_to("home")
    resident = st.session_state.get("resident")
    if not resident:
        st.error("Not logged in.")
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    try:
        cases_df = read_sheet_df(
            SHEET_CASES,
            expected_cols=["case_id", "resident_email", "date", "specialty_id",
                           "procedure_id", "attending_id", "notes",
                           "case_complexity", "overall_performance"],
        )
        procs_df = read_sheet_df(SHEET_PROCEDURES, expected_cols=["procedure_id", "procedure_name", "specialty_id"])
        atnds_df = read_sheet_df(SHEET_ATTENDINGS, expected_cols=["attending_id", "attending_name", "specialty_id", "email"])
    except ConnectionError as exc:
        show_gs_error(exc)
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    # Normalise case_id then deduplicate to prevent fan-out from duplicate rows.
    cases_df["case_id"] = _norm_id(cases_df["case_id"])
    cases_df = cases_df.drop_duplicates(subset=["case_id"])

    res_cases = cases_df[cases_df["resident_email"] == resident].copy()
    res_cases["notes"] = res_cases["notes"].fillna("").astype(str)
    res_cases = res_cases[res_cases["notes"].str.strip() != ""]

    if res_cases.empty:
        st.info("No comments recorded yet.")
        if st.button("⬅️ Back to Home"):
            go_to("home")
    else:
        # Resolve attending names — magic_ IDs never appear in the attendings sheet,
        # so we decode them directly from the ID string instead of joining.
        atnds_lookup = dict(zip(atnds_df["attending_id"], atnds_df["attending_name"]))
        res_cases["attending_name"] = res_cases["attending_id"].apply(
            lambda aid: attending_display_name(str(aid), atnds_lookup)
        )

        # Deduplicate procs so a fanout can't multiply rows.
        procs_dedup = procs_df.drop_duplicates(subset=["procedure_id"])
        merged = res_cases.merge(procs_dedup[["procedure_id", "procedure_name"]], on="procedure_id", how="left")
        merged = merged.rename(columns={
            "date":           "Date",
            "procedure_name": "Procedure",
            "attending_name": "Attending",
            "notes":          "Comments",
        })
        # Fix 1: format dates as MM-DD-YYYY — sort by datetime first, then format
        merged["_date_sort"] = pd.to_datetime(merged["Date"], errors="coerce")
        merged = merged[["Date", "Procedure", "Attending", "Comments", "_date_sort"]].sort_values("_date_sort", ascending=False).drop(columns=["_date_sort"])
        merged["Date"] = merged["Date"].apply(fmt_date)

        st.caption("💡 Tip: To screenshot the full table — on mobile use print preview; on desktop use File > Print (Cmd+P / Ctrl+P), then adjust the scale percentage down until all columns fit on one page before screenshotting.")

        # Fix 8: procedure filter dropdown
        _proc_opts = ["All Procedures"] + sorted(merged["Procedure"].dropna().unique().tolist())
        _proc_filter = st.selectbox("Filter by Procedure", _proc_opts, key="comments_proc_filter")
        if _proc_filter != "All Procedures":
            merged = merged[merged["Procedure"] == _proc_filter]

        # Fix 8: render with wrapped Comments column using HTML table
        st.markdown("""
<style>
.comments-tbl {width:100%;border-collapse:collapse;font-size:0.88rem;}
.comments-tbl th {background:var(--secondary-background-color);padding:8px 10px;
    text-align:left;border-bottom:2px solid #ccc;font-weight:600;}
.comments-tbl td {padding:8px 10px;vertical-align:top;border-bottom:1px solid var(--secondary-background-color);}
.comments-tbl td.date-col {white-space:nowrap;}
.comments-tbl td.comments-col {white-space:pre-wrap;word-break:break-word;min-width:260px;}
</style>""", unsafe_allow_html=True)

        _rows_html = ""
        for _, r in merged.reset_index(drop=True).iterrows():
            _rows_html += (
                f"<tr>"
                f"<td class='date-col'>{html.escape(str(r['Date']))}</td>"
                f"<td>{html.escape(str(r['Procedure']))}</td>"
                f"<td>{html.escape(str(r['Attending']))}</td>"
                f"<td class='comments-col'>{html.escape(str(r['Comments'])).replace(chr(10), '<br>')}</td>"
                f"</tr>"
            )
        st.markdown(
            "<table class='comments-tbl'>"
            "<thead><tr><th>Date</th><th>Procedure</th><th>Attending</th><th>Comments</th></tr></thead>"
            f"<tbody>{_rows_html}</tbody></table>",
            unsafe_allow_html=True,
        )

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            merged.to_excel(writer, index=False, sheet_name="Comments")
        st.download_button(
            label="📥 Download as Excel",
            data=output.getvalue(),
            file_name=f"{resident}_comments.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        if st.button("⬅️ Back to Home"):
            go_to("home")


# ════════════════════════════════════════════════════════════
# PAGE: CUMULATIVE DASHBOARD
# ════════════════════════════════════════════════════════════
elif page == "cumulative":
    st.title("📊 Cumulative Dashboard")
    if st.button("🏠 Back to Home", key="cumulative_home_top"):
        go_to("home")
    st.info("📱 On mobile: tap the ≡ icon at top left to view the sidebar.")
    resident = st.session_state.get("resident")
    if not resident:
        st.error("Not logged in.")
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    try:
        cases_df  = read_sheet_df(SHEET_CASES,  expected_cols=["case_id", "resident_email", "date",
                                                                "specialty_id", "procedure_id",
                                                                "attending_id", "notes",
                                                                "case_complexity", "overall_performance"])
        scores_df = read_sheet_df(SHEET_SCORES, expected_cols=["case_id", "step_id", "rating", "rating_num",
                                                                "case_complexity", "overall_performance"])
        steps_df  = read_sheet_df(SHEET_STEPS,  expected_cols=["step_id", "procedure_id", "step_order", "step_name"])
        procs_df  = read_sheet_df(SHEET_PROCEDURES, expected_cols=["procedure_id", "procedure_name", "specialty_id"])
        atnds_df  = read_sheet_df(SHEET_ATTENDINGS, expected_cols=["attending_id", "attending_name", "specialty_id", "email"])
    except ConnectionError as exc:
        show_gs_error(exc)
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    # ── Pure-Python join pipeline (pandas-version-agnostic) ───────────────────
    # Every case_id is coerced to a clean string at read time using native Python
    # str() so no pandas dtype inference can silently break the join.

    def _clean_id(val) -> str:
        """str(x).strip() then remove a trailing .0 left by float→str conversion."""
        s = str(val).strip()
        return s[:-2] if s.endswith(".0") else s

    # Build a dict of case_id → case-metadata for this resident only.
    # Duplicates are handled by last-write-wins (same result as drop_duplicates).
    atnds_lookup = {
        str(r.get("attending_id", "")): str(r.get("attending_name", ""))
        for _, r in atnds_df.iterrows()
    }
    procs_map = {
        str(r.get("procedure_id", "")): str(r.get("procedure_name", ""))
        for _, r in procs_df.iterrows()
    }

    resident_cases: dict = {}  # clean_case_id → metadata dict
    for _, row in cases_df.iterrows():
        if str(row.get("resident_email", "")).strip() != str(resident).strip():
            continue
        cid = _clean_id(row.get("case_id", ""))
        if not cid or cid == "nan":
            continue
        aid = str(row.get("attending_id", ""))
        resident_cases[cid] = {
            "case_id":             cid,
            "date":                str(row.get("date", "")),
            "case_procedure_id":   str(row.get("procedure_id", "")),
            "attending_name":      attending_display_name(aid, atnds_lookup),
            "case_complexity":     row.get("case_complexity"),
            "overall_performance": row.get("overall_performance"),
        }

    if not resident_cases:
        st.info("No cases logged yet.")
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    # Build a dict of step_id → step-metadata.
    steps_lookup: dict = {}
    for _, row in steps_df.iterrows():
        sid = str(row.get("step_id", "")).strip()
        if not sid or sid == "nan":
            continue
        steps_lookup[sid] = {
            "step_procedure_id": str(row.get("procedure_id", "")),
            "step_name":         str(row.get("step_name", "")),
            "step_order":        row.get("step_order", 0),
        }

    # Walk every score row; look up case + step with dict gets — no merge needed.
    seen_case_step: set = set()   # deduplicate (case_id, step_id) pairs
    merged_rows: list = []
    for _, row in scores_df.iterrows():
        cid = _clean_id(row.get("case_id", ""))
        if cid not in resident_cases:
            continue
        sid = str(row.get("step_id", "")).strip()
        if not sid or sid == "nan":
            continue
        key = (cid, sid)
        if key in seen_case_step:
            continue
        seen_case_step.add(key)
        step_meta = steps_lookup.get(sid, {})
        merged_rows.append({
            "case_id":             cid,
            "step_id":             sid,
            "rating":              str(row.get("rating", "")),
            "rating_num":          row.get("rating_num"),
            **resident_cases[cid],
            "step_procedure_id":   step_meta.get("step_procedure_id", ""),
            "step_name":           step_meta.get("step_name", ""),
            "step_order":          step_meta.get("step_order", 0),
        })



    if not merged_rows:
        st.info("No assessment data yet.")
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    merged = pd.DataFrame(merged_rows)
    # Alias so the rest of the page (which references case_procedure_id) works unchanged.
    if "case_procedure_id" not in merged.columns:
        merged["case_procedure_id"] = ""

    # ── Procedure selector ────────────────────────────────
    proc_ids      = merged["case_procedure_id"].dropna().unique()
    selected_proc = st.selectbox(
        "Procedure",
        options=sorted(proc_ids, key=lambda x: procs_map.get(x, x)),
        format_func=lambda x: procs_map.get(x, x),
    )

    proc_data = merged[merged["case_procedure_id"] == selected_proc].copy()
    ordered_steps = (
        steps_df[steps_df["procedure_id"] == selected_proc]
        .sort_values("step_order")["step_name"]
        .tolist()
    )

    # Build display names for step column headers.
    # With writing-mode: vertical-rl + rotate(180deg) + justify-content: flex-end,
    # text is anchored at the visual bottom. Overflow clips the visual top (end of name).
    # Truncate from the end so the beginning is always visible at visual bottom.
    def _fmt_step_hdr(name, max_len=18):
        if isinstance(name, str) and len(name) > max_len:
            return name[:max_len - 1] + "\u2026"
        return name if isinstance(name, str) else name

    _step_display        = {s: _fmt_step_hdr(s) for s in ordered_steps}
    ordered_steps_display = [_step_display[s] for s in ordered_steps]

    # ── Pivot for heatmap ─────────────────────────────────
    pivot = proc_data.pivot_table(
        index=["date", "attending_name", "case_id", "case_complexity", "overall_performance"],
        columns="step_name",
        values="rating",
        aggfunc="first",
    ).reset_index()

    for step in ordered_steps:
        if step not in pivot.columns:
            pivot[step] = pd.NA

    pivot = pivot[["date", "attending_name", "case_id", "case_complexity", "overall_performance"] + ordered_steps]

    # ── Screenshot-friendly heatmap ──────────────────────────────
    proc_display_name = procs_map.get(selected_proc, selected_proc)
    st.markdown(
        f"### {proc_display_name} — Progress Heatmap\n"
        "Most recent cases at the top. Zoom out to screenshot this grid. 📸"
    )
    st.caption("💡 Tip: To screenshot the full table — on mobile use print preview; on desktop use File > Print (Cmd+P / Ctrl+P), then adjust the scale percentage down until all columns fit on one page before screenshotting.")

    # Sort cases by date (desc) for Most Recent computation and display
    pivot_sorted = pivot.sort_values("date", ascending=False)

    # Compute "Most Recent" summary row — per step, first non-null, non-"Not Assessed" value
    # Label is placed in attending_name so it appears under Attending column (right side of metadata)
    _mr = {"date": "", "attending_name": "📌 Most Recent", "case_complexity": pd.NA, "overall_performance": pd.NA}
    for _s in ordered_steps:
        _vals = pivot_sorted[_s].dropna()
        _vals = _vals[_vals != "Not Assessed"]
        _mr[_s] = _vals.iloc[0] if not _vals.empty else pd.NA

    # Compute "Best" summary row — per step, highest rating_num ever
    _best = {"date": "", "attending_name": "🏆 Best", "case_complexity": pd.NA, "overall_performance": pd.NA}
    for _s in ordered_steps:
        _vals = pivot_sorted[_s].dropna()
        if _vals.empty:
            _best[_s] = pd.NA
        else:
            _best[_s] = max(_vals.tolist(), key=lambda v: RATING_TO_NUM.get(v, -1))

    _summary_df = pd.DataFrame([_mr, _best])
    _meta_cols  = ["date", "attending_name", "case_complexity", "overall_performance"]

    # Build display df: summary rows first, then sorted case rows (case_id dropped)
    display_df = pd.concat(
        [_summary_df[_meta_cols + ordered_steps],
         pivot_sorted.drop(columns=["case_id"])[_meta_cols + ordered_steps]],
        ignore_index=True,
    )

    # Fix 1: format dates as MM-DD-YYYY (leaves summary labels unchanged)
    display_df["date"] = display_df["date"].apply(fmt_date)

    # Fix 11: rename metadata columns; also apply display truncations to step headers
    display_df = display_df.rename(columns={
        "date":                "Date",
        "attending_name":      "Attending",
        "case_complexity":     "Case Complexity",
        "overall_performance": "Overall Performance",
        **_step_display,
    })
    all_cols = list(display_df.columns)

    # Prevent "nan" text in Date/Attending for summary rows
    display_df["Date"]      = display_df["Date"].fillna("")
    display_df["Attending"] = display_df["Attending"].fillna("")

    # Step 1: store original values for ALL rating columns before blanking
    _rating_cols = [c for c in ordered_steps_display + ["Case Complexity", "Overall Performance"]
                    if c in display_df.columns]

    # Build _orig_vals robustly: if a column name is duplicated (due to truncation producing
    # identical display names), display_df[col] returns a DataFrame rather than a Series —
    # normalise to a Series and reindex to display_df.index to prevent the crash.
    _orig_vals = {}
    for col in _rating_cols:
        _v = display_df[col].copy()
        if isinstance(_v, pd.DataFrame):
            _v = _v.iloc[:, 0]
        _orig_vals[col] = _v.reindex(display_df.index)

    # Step 2: blank ALL rating columns so no text appears in any cell
    for _c in _rating_cols:
        display_df[_c] = " "

    # Determine "never attempted" step columns: every non-summary data cell is
    # NaN or "Not Assessed" (step was never meaningfully attempted by this resident).
    _never_attempted_cols = set()
    _n_summary = 2  # rows 0 and 1 are Most Recent / Best
    for _sc in ordered_steps_display:
        if _sc not in _orig_vals:
            continue
        _data_vals = _orig_vals[_sc].iloc[_n_summary:]
        _meaningful = _data_vals[~(_data_vals.isna() | (_data_vals == "Not Assessed"))]
        if _meaningful.empty:
            _never_attempted_cols.add(_sc)

    # Color functions — operate on original (pre-blank) values
    def _color_step(val, col=None):
        """Return background-color CSS for a single step cell."""
        _is_na = val is None or (isinstance(val, float) and np.isnan(val)) or val == ""
        try:
            _is_na = _is_na or pd.isna(val)
        except (TypeError, ValueError):
            pass
        if col in _never_attempted_cols:
            # Never-attempted column: Not Assessed and blank → gray; Shown/Told keeps its color
            if _is_na or val == "Not Assessed":
                return "background-color: #E0E0E0"
        if _is_na:
            return "background-color: #E0E0E0"  # blank/NaN = Never Attempted gray
        color = RATING_HEX.get(val, "")
        return f"background-color: {color}" if color else ""

    def _color_complexity(val):
        if pd.isna(val) or val == "":
            return ""
        return f"background-color: {COMPLEXITY_HEX.get(val, '')}"

    def _color_o_score(val):
        if not isinstance(val, str) or val == "":
            return ""
        key = val.split("-")[0].strip()
        return f"background-color: {O_SCORE_HEX.get(key, '')}"

    # Build styler — all colors from original values, display values are blank
    try:
        styled = display_df.style

        if ordered_steps_display:
            # Build _orig_steps_df safely, skipping any column missing from _orig_vals
            _safe_step_cols = [c for c in ordered_steps_display if c in _orig_vals]
            _orig_steps_df = pd.DataFrame(
                {c: _orig_vals[c] for c in _safe_step_cols},
                index=display_df.index,
            )

            def _color_steps_matrix(df):
                result = {}
                for col in df.columns:
                    col_colors = []
                    is_never = col in _never_attempted_cols
                    for i, v in enumerate(_orig_steps_df[col]):
                        _na = v is None or v == ""
                        try:
                            _na = _na or pd.isna(v)
                        except (TypeError, ValueError):
                            pass
                        if is_never and i < _n_summary:
                            # Summary rows in never-attempted columns → gray
                            col_colors.append("background-color: #E0E0E0")
                        else:
                            col_colors.append(_color_step(v, col=col))
                    result[col] = col_colors
                return pd.DataFrame(result, index=df.index)

            if _safe_step_cols:
                styled = styled.apply(_color_steps_matrix, subset=_safe_step_cols, axis=None)

        def _apply_complexity_colors(col):
            return [_color_complexity(v) for v in _orig_vals["Case Complexity"]]

        def _apply_o_score_colors(col):
            return [_color_o_score(v) for v in _orig_vals["Overall Performance"]]

        styled = (
            styled
            .apply(_apply_complexity_colors, subset=["Case Complexity"], axis=0)
            .apply(_apply_o_score_colors,    subset=["Overall Performance"], axis=0)
            .hide(axis="index")
            .set_properties(
                subset=["Date", "Attending"],
                **{"min-width": "120px", "white-space": "nowrap"},
            )
            .set_properties(
                subset=["Case Complexity", "Overall Performance"],
                **{"min-width": "40px", "max-width": "40px", "width": "40px", "text-align": "center"},
            )
        )
        if ordered_steps_display:
            styled = styled.set_properties(
                subset=ordered_steps_display,
                **{"min-width": "40px", "max-width": "40px", "width": "40px", "text-align": "center"},
            )

        table_styles = [
            {"selector": "table",       "props": [("border-collapse", "collapse"), ("margin", "0 auto"),
                                                   ("border", "2px solid #555")]},
            {"selector": "th, td",      "props": [("border", "1px solid #bbb"),
                                                   ("padding", "4px"), ("font-size", "0.8rem")]},
            # Bottom-justify all column headers
            {"selector": "th.col_heading", "props": [("text-align", "center"), ("vertical-align", "bottom"),
                                                       ("font-weight", "600")]},
            # Strong border below header row
            {"selector": "thead tr:last-child th", "props": [("border-bottom", "2px solid #555")]},
            # Strong horizontal borders between data rows
            {"selector": "tbody tr", "props": [("border-bottom", "1px solid #bbb")]},
            # Summary rows (first two) — strong bottom border
            {"selector": "tbody tr:nth-child(1)", "props": [("border-bottom", "2px solid #555")]},
            {"selector": "tbody tr:nth-child(2)", "props": [("border-bottom", "2px solid #555")]},
            # Summary row labels: right-justify in the Attending (2nd) column;
            # visually merge Date+Attending by removing their shared border.
            {"selector": "tbody tr:nth-child(1) td:nth-child(1)",
             "props": [("border-right", "none")]},
            {"selector": "tbody tr:nth-child(2) td:nth-child(1)",
             "props": [("border-right", "none")]},
            {"selector": "tbody tr:nth-child(1) td:nth-child(2)",
             "props": [("text-align", "right"), ("font-weight", "600"),
                       ("padding-right", "6px"), ("border-left", "none")]},
            {"selector": "tbody tr:nth-child(2) td:nth-child(2)",
             "props": [("text-align", "right"), ("font-weight", "600"),
                       ("padding-right", "6px"), ("border-left", "none")]},
        ]
        # Vertical step headers: writing-mode + rotate(180deg) makes text read bottom-to-top.
        # vertical-align: bottom anchors text to the visual bottom of the header cell.
        # text-align: left means the start of each step name is visible; overflow: hidden
        # clips the end. No flex — flex collapses columns into one.
        for idx, col_name in enumerate(all_cols):
            if col_name in ordered_steps_display or col_name in ("Case Complexity", "Overall Performance"):
                table_styles.append({
                    "selector": f"th.col_heading.level0.col{idx}",
                    "props": [
                        ("writing-mode", "vertical-rl"),
                        ("transform", "rotate(180deg)"),
                        ("vertical-align", "bottom"),
                        ("text-align", "left"),
                        ("white-space", "nowrap"),
                        ("overflow", "hidden"),
                        ("text-overflow", "ellipsis"),
                        ("max-height", "180px"),
                        ("height", "180px"),
                        ("font-size", "0.75rem"),
                        ("padding", "4px 2px"),
                        ("width", "36px"),
                        ("min-width", "36px"),
                        ("max-width", "36px"),
                    ],
                })

        styled = styled.set_table_styles(table_styles)
        st.markdown(styled.to_html(), unsafe_allow_html=True)

    except Exception as _heatmap_err:
        st.warning(
            f"⚠️ Could not render the heatmap for this procedure: {_heatmap_err}\n\n"
            "Please try a different procedure, or contact your program coordinator."
        )

    # ── Legends ───────────────────────────────────────────
    def _swatch(color, label, border=""):
        _bdr = f"border:{border};" if border else ""
        return (
            f'<span class="legend-item">'
            f'<span class="legend-swatch" style="background-color:{color};{_bdr}"></span>{label}'
            f'</span>'
        )

    st.markdown("#### Ratings Legend")
    _rating_legend_html = "".join(
        _swatch(v, k, "1px solid #aaa" if k == "Not Assessed" else "")
        for k, v in RATING_HEX.items()
    )
    _rating_legend_html += _swatch("#E0E0E0", "Never Attempted")
    st.markdown('<div class="legend-row">' + _rating_legend_html + "</div>", unsafe_allow_html=True)

    st.markdown("#### Case Complexity")
    st.markdown(
        '<div class="legend-row">' +
        "".join(_swatch(v, k) for k, v in COMPLEXITY_HEX.items()) +
        "</div>",
        unsafe_allow_html=True,
    )

    # ── Excel export ──────────────────────────────────────
    st.markdown("---")
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        # Rename pivot columns for readability; fix 1: date as MM-DD-YYYY
        pivot_excel = pivot.copy()
        pivot_excel["date"] = pivot_excel["date"].apply(fmt_date)
        pivot_excel = pivot_excel.rename(columns={
            "date":                "Date",
            "attending_name":      "Attending",
            "case_id":             "Case ID",
            "case_complexity":     "Case Complexity",
            "overall_performance": "Overall Performance",
        })
        pivot_excel.to_excel(writer, index=False, sheet_name="Cumulative")
        ws_xl = writer.sheets["Cumulative"]
        from openpyxl.styles import PatternFill, Font

        step_fill_map = {k: v.lstrip("#") for k, v in RATING_HEX.items() if k not in ("Not Assessed",)}
        step_fill_map["Not Assessed"] = "E0E0E0"  # light gray in Excel

        start_col = 6
        for xl_row in ws_xl.iter_rows(
            min_row=2, max_row=ws_xl.max_row,
            min_col=start_col, max_col=5 + len(ordered_steps),
        ):
            for cell in xl_row:
                val = cell.value
                if val in step_fill_map:
                    cell.fill = PatternFill(
                        start_color=step_fill_map[val],
                        end_color=step_fill_map[val],
                        fill_type="solid",
                    )
                    cell.font = Font(color="FFFFFF" if val in ("Not Yet", "Auto") else "000000")

    st.download_button(
        label=f"📥 Download Excel — {procs_map.get(selected_proc, selected_proc)}",
        data=output.getvalue(),
        file_name=f"{resident}_{selected_proc}_cumulative.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    if st.button("⬅️ Back to Home"):
        go_to("home")


# ════════════════════════════════════════════════════════════
# PAGE: ATTENDING ASSESSMENT (magic link)
# ════════════════════════════════════════════════════════════
elif page == "attending_assessment":
    resident_email = st.session_state.get("resident", "")
    procedure_id   = st.session_state.get("procedure_id", "")
    specialty_id   = st.session_state.get("specialty_id", "")
    attending_name = st.session_state.get("attending_name", "Unknown")

    if not (resident_email and procedure_id and specialty_id):
        st.error("⚠️ Missing required information in this link. Please ask the resident to resend.")
        st.stop()

    # Decode URL-safe attending name
    display_attending = attending_name.replace("_", " ")

    st.title("📝 Attending Evaluation")
    try:
        _, proc_df_att, steps_df, _ = load_refs()
    except ConnectionError as exc:
        show_gs_error(exc)
        st.stop()

    # Resolve procedure name for display (Fix 3)
    _att_proc_rows = proc_df_att.loc[proc_df_att["procedure_id"] == procedure_id, "procedure_name"].values
    _att_proc_name = _att_proc_rows[0] if len(_att_proc_rows) else procedure_id

    st.markdown(
        f'<div class="pp-card">'
        f'<b>Resident:</b> {resident_email}<br>'
        f'<b>Procedure:</b> {_att_proc_name}<br>'
        f'<b>Attending:</b> {display_attending}'
        f'</div>',
        unsafe_allow_html=True,
    )

    steps = steps_df[steps_df["procedure_id"] == procedure_id].sort_values("step_order")
    if steps.empty:
        st.error("This procedure has no defined steps. Please contact the program coordinator.")
        st.stop()

    case_date       = st.date_input("Date of Procedure", value=datetime.date.today())
    _att_cc_opts = ["— Select complexity —", "Straight Forward", "Moderate", "Complex"]
    case_complexity = st.selectbox("Case Complexity", _att_cc_opts)

    st.markdown("#### Step-Level Ratings")
    scores: dict = {}
    for _, row in steps.iterrows():
        step_id   = row["step_id"]
        step_name = row["step_name"]
        scores[step_id] = st.selectbox(
            step_name, RATING_OPTIONS, key=f"att_score_{step_id}"
        )

    o_score = st.selectbox("Overall Performance Rating", O_SCORE_OPTIONS)
    with st.expander("💬 Comment guide (tap to expand)", expanded=False):
        st.markdown(
            "_Use these prompts to structure your feedback:_\n\n"
            "- The resident demonstrated ___\n"
            "- Improvements made on ___\n"
            "- Still working on ___\n"
            "- Next steps/improvements expected ___"
        )
    notes   = st.text_area("Comments / Feedback (optional)")

    st.markdown("---")
    if st.button("✅ Submit Evaluation", type="primary", width="stretch"):
        if case_complexity == "— Select complexity —":
            st.warning("Please select a Case Complexity before submitting.")
        elif o_score == O_SCORE_OPTIONS[0]:
            st.warning("Please select an Overall Performance rating before submitting.")
        else:
            try:
                case_id = save_case(
                    resident_email=resident_email,
                    date=case_date,
                    specialty_id=specialty_id,
                    procedure_id=procedure_id,
                    attending_id=f"magic_{attending_name}",   # magic_ prefix; decoded on display
                    scores_dict=scores,
                    notes=notes,
                    case_complexity=case_complexity,
                    overall_performance=o_score,
                )
                # Store submission summary for the confirmation page
                st.session_state["attending_submission"] = {
                    "case_id":             case_id,
                    "resident_email":      resident_email,
                    "procedure_id":        procedure_id,
                    "procedure_name":      _att_proc_name,
                    "attending_name":      display_attending,
                    "date":                str(case_date),
                    "case_complexity":     case_complexity,
                    "overall_performance": o_score,
                    "notes":               notes,
                    "scores":              scores,
                    "steps":               steps[["step_id", "step_name"]].to_dict("records"),
                }
                go_to("attending_confirmation")
            except ConnectionError as exc:
                show_gs_error(exc)


# ════════════════════════════════════════════════════════════
# PAGE: ATTENDING CONFIRMATION
# ════════════════════════════════════════════════════════════
elif page == "attending_confirmation":
    sub = st.session_state.get("attending_submission")
    if not sub:
        st.error("No submission data found.")
        st.stop()

    st.title("✅ Evaluation Submitted")
    st.success("Thank you! Your evaluation has been recorded.")

    st.markdown(
        f'<div class="pp-card">'
        f'<b>Resident:</b> {sub["resident_email"]}<br>'
        f'<b>Attending:</b> {sub["attending_name"]}<br>'
        f'<b>Procedure:</b> {sub.get("procedure_name", sub["procedure_id"])}<br>'
        f'<b>Date:</b> {fmt_date(sub["date"])}<br>'
        f'<b>Case Complexity:</b> {sub["case_complexity"]}<br>'
        f'<b>Overall Performance:</b> {sub["overall_performance"]}<br>'
        f'<b>Case ID:</b> <code>{sub["case_id"]}</code>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if sub["notes"].strip():
        st.markdown("**Comments submitted:**")
        st.info(sub["notes"])

    st.markdown("#### Step Ratings Submitted")
    step_rows = []
    for step_rec in sub["steps"]:
        step_id   = step_rec["step_id"]
        step_name = step_rec["step_name"]
        rating    = sub["scores"].get(step_id, "—")
        step_rows.append({"Step": step_name, "Rating": rating})

    summary_df = pd.DataFrame(step_rows)
    st.dataframe(style_df(summary_df, "Rating"), width="stretch")

    st.markdown("---")
    st.markdown("_You may now close this window. The resident can view the evaluation in their dashboard._")
