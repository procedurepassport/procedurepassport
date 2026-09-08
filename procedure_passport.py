import streamlit as st
import time
import pandas as pd
import uuid
import datetime
import json
import html
import re
import difflib
import hashlib
import secrets
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
    st.session_state["page"]              = "attending_assessment"
    st.session_state["resident"]          = query_params.get("resident", "")
    st.session_state["procedure_id"]      = query_params.get("procedure_id", "")
    st.session_state["specialty_id"]      = query_params.get("specialty_id", "")
    st.session_state["attending_name"]    = query_params.get("attending_name", "")
    st.session_state["draft_id"]          = query_params.get("draft_id", "")
    # Only carried by the blank-link flow (the self-assess/pre-filled flow
    # gets its date from the draft instead) — the date the resident chose
    # on the Start page before generating the blank link, attached to the
    # submission silently; there's no UI on the attending page to view or
    # edit it.
    st.session_state["attending_link_date"] = query_params.get("date", "")
    st.session_state["_magic_routed"]     = True

# An attending's magic link requesting a resident's own self-
# evaluation (see attending_start's "Create Magic Link Request for
# Resident Self-Evaluation" button) — pre-fills a Self-Assess entry
# (procedure/attending/date from the link) and routes straight to the
# assessment page, skipping Start's own pickers entirely. Unlike the
# attending flow above, this can't work anonymously: a self-assessment
# is tied to a real resident account, so nothing happens here until
# `role` is actually "resident" — which, on a first visit via this
# link, only becomes true after they log in and _complete_login()
# reruns the script, landing back here with the same query params
# (still in the URL) and picking this same block back up.
if (
    query_params.get("mode") == "resident_self"
    and st.session_state.get("role") == "resident"
    and st.session_state.get("resident")
    and not st.session_state.get("_magic_routed")
):
    st.session_state["procedure_id"] = query_params.get("procedure_id", "")
    st.session_state["specialty_id"] = query_params.get("specialty_id", "")
    st.session_state["attending_id"] = query_params.get("attending_id", "")
    st.session_state["self_eval_request_id"] = query_params.get("request_id", "")
    _self_link_date = query_params.get("date", "")
    try:
        st.session_state["date"] = datetime.date.fromisoformat(_self_link_date) if _self_link_date else datetime.date.today()
    except ValueError:
        st.session_state["date"] = datetime.date.today()
    st.session_state["assessment_mode"]      = "self"
    st.session_state["self_eval_requested_by_attending"] = True
    st.session_state["scores"]               = {}
    st.session_state["notes"]                = ""
    st.session_state["improve"]              = ""
    st.session_state["how"]                  = ""
    st.session_state["generated_magic_link"] = None
    st.session_state["page"]                 = "assessment"
    st.session_state["_magic_routed"]        = True

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
    "improve":                 "",
    "how":                     "",
    "current_case_id":         None,
    "viewing_case_id":         None,   # set before go_to("view_evaluation")
    "attending_submission":    None,   # filled after magic-link submit
    "generated_magic_link":    None,   # filled after Generate Magic Link
    "draft_id":                "",
    "assessment_mode":         "together",  # "together" or "self", set from Start
    "self_eval_requested_by_attending": False,  # True only when assessment_mode "self" came from an attending's magic link (mode=resident_self), not the resident's own Start page
    "self_eval_request_id":    "",     # set alongside self_eval_requested_by_attending — the self_eval_requests row to delete once this is fulfilled
    "blank_magic_link":        None,   # filled after Generate a Blank Magic Link
    "attending_link_date":     "",     # resident's chosen date, carried by a blank magic link
    "last_assessment_type":    None,   # "Assessed Together" or "Self-Assessment"
    "role":                    None,   # "resident", "admin", or "attending" — set at login
    "attending_login_email":   "",     # set only when role == "attending" (their own account)
    "attending_login_name":    "",
    "attending_login_id":      "",
    "attending_login_specialty_id": "",
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
# Subtle diagonal-hatch pattern layered over "Never Attempted" cells (see
# _render_resident_heatmap's _color_step) so they read as visually
# distinct from a flat "Not Assessed" gray at a glance, not just a
# slightly different shade — used both on the heatmap itself and in both
# rating legends, so all three stay in sync.
NEVER_ATTEMPTED_STRIPE_CSS = (
    "repeating-linear-gradient(45deg, rgba(0,0,0,0.06) 0, "
    "rgba(0,0,0,0.06) 1px, transparent 1px, transparent 6px)"
)
RATING_HEX = {
    "Not Assessed": "#E0E0E0",  # gray — explicitly rated as not assessed
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
# Descriptive text for the Step/Skill Autonomy Rating Legend
# (render_rating_legend()). "Not Assessed" has no description of its
# own — it just means no rating was given for that step.
RATING_DESCRIPTIONS = {
    "Shown/Told": "Attending actively teaches the step/skill through verbal explanation (likely for the first time).",
    "Not Yet":    "Resident attempts but is as yet unable to perform the step/skill.",
    "Steer":      "Attending offers physical assistance or must perform parts of the step/skill for it to be accomplished.",
    "Prompt":     "Attending offers verbal assistance to accomplish the step/skill.",
    "Back up":    "Attending may offer feedback and refinement not necessary to the safe execution of the step/skill.",
    "Auto":       "Resident demonstrates the skill level expected for graduation. Is able to lead this step/skill without attending input.",
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
# Descriptive text for the Case Complexity Legend (render_complexity_legend()).
# Keyed the same as COMPLEXITY_HEX so the two stay in lockstep.
COMPLEXITY_DESCRIPTIONS = {
    "Straight Forward": "Easiest 1/3 of Cases",
    "Moderate":          "Middle 1/3 of Cases",
    "Complex":           "Most Difficult 1/3 of Cases",
}
PREP_HEX = {
    "Unprepared":          "#FF8A80",
    "Poorly Prepared":     "#FFAB91",
    "Adequately Prepared": "#FFD633",
    "Well Prepared":       "#99E699",
    "Highly Prepared":     "#33CC33",
}
# Descriptive text for the Preparation Scale Legend (render_prep_legend()).
# Keyed the same as PREP_HEX so the two stay in lockstep.
PREP_DESCRIPTIONS = {
    "Unprepared":          "Lacks essential knowledge, skills, or resources; cannot perform the task or meet basic expectations.",
    "Poorly Prepared":     "Has some knowledge but significant gaps; requires substantial coaching or support to perform adequately.",
    "Adequately Prepared": "Meets most requirements; can perform the task with standard support.",
    "Well Prepared":       "Fully equipped to perform the task; can handle challenges with minimal supervision.",
    "Highly Prepared":     "Exceeds requirements; can adapt to new challenges or complex situations.",
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
# last_seen_evaluations_at drives the "new evaluation" badge (see
# get_resident_last_seen()/mark_resident_evaluations_seen()) — every
# read of this sheet that later writes it back must use this same full
# column list, or a write-back drops whatever isn't in its own narrower
# list, silently wiping this field for every OTHER resident.
RESIDENT_COLS = ["email", "name", "specialty_id", "created_at", "last_seen_evaluations_at"]
SHEET_ATTENDINGS = "attendings"
SHEET_PROCEDURES = "procedures"
SHEET_STEPS      = "steps"
SHEET_CASES      = "cases"
# Canonical full column list for the cases sheet — matches save_case()'s
# own list exactly, so any read/write here never drops a column
# write_sheet_df would otherwise silently lose. Also used consistently
# by every OTHER read of this sheet (not just writes/save_case) so
# they all share one st.cache_data entry instead of each narrower
# ad-hoc column subset costing its own separate Google Sheets API read
# for the same underlying data.
_CASE_COLS = ["case_id", "resident_email", "date", "specialty_id",
              "procedure_id", "attending_id", "notes",
              "case_complexity", "case_preparation", "overall_performance",
              "robo_type", "improve", "how", "assessment_type",
              "self_assessment_diff", "submitted_at"]
SHEET_SCORES     = "scores"
SHEET_SPECIALTY  = "specialties"
SHEET_DRAFTS     = "drafts"
SHEET_AUTH       = "auth"
SHEET_EVAL_VIEWS = "eval_views"

# One row per (resident, case) a resident has opened via the new-
# evaluation badge's per-item link — see get_viewed_case_ids()/
# mark_evaluation_viewed(). Once viewed, that one evaluation drops off
# the badge for good, independent of any other new/unviewed ones —
# the badge would otherwise clear its whole batch the moment the
# resident so much as looked at Home, rather than only the specific
# evaluation they actually opened.
EVAL_VIEW_COLS = ["resident_email", "case_id", "viewed_at"]

# Password auth, one row per email (resident or admin) that has ever set a
# password. Deliberately its own sheet, not columns on `residents` — the
# admin account isn't a residents-sheet row at all, and keeping credential
# material out of the general roster is good hygiene regardless.
AUTH_COLS = ["email", "password_hash", "password_salt", "created_at"]
PBKDF2_ITERATIONS = 200_000

# Pre-filled magic-link drafts: a resident's in-progress assessment, saved
# so the attending's link can carry a short draft_id instead of embedding
# every field's value in the URL itself.
DRAFT_COLS = [
    "draft_id", "resident_email", "date", "specialty_id", "procedure_id",
    "attending_id", "case_complexity", "case_preparation",
    "overall_performance", "robo_type", "improve", "how", "notes",
    "scores_json", "created_at",
]

# An attending's outstanding request for a resident's self-evaluation
# (see attending_start's "Create Magic Link Request for Resident
# Self-Evaluation" button) — tracked so it can be shown as a Home page
# notification for the resident IN ADDITION to the magic link itself,
# with the two cross-referenced against the same row: its mere
# presence means "not yet fulfilled"; completing that self-assessment,
# by either route, deletes it — so it can only ever be fulfilled once.
SHEET_SELF_EVAL_REQUESTS = "self_eval_requests"
SELF_EVAL_REQUEST_COLS = [
    "request_id", "resident_email", "date", "specialty_id",
    "procedure_id", "attending_id", "created_at",
]

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


def write_sheet_df_no_shrink(sheet_name: str, df: pd.DataFrame) -> None:
    """Like write_sheet_df(), but for call sites that only ever mean to
    rename/reorder/append — never to remove rows. Every write in this
    app is "read the whole table, mutate a local copy, write the whole
    table back" with no per-row transactionality and no other guard
    against it: a stale cached read (up to write_sheet_df's own
    5-minute TTL), a concurrent edit from another tab/session/admin
    landing in between, or simply a bug in the mutation logic can all
    silently turn into "write back a table that's missing rows it
    shouldn't be" — which looks, from the outside, exactly like data
    being deleted, because it is.

    Re-reads the sheet fresh (st.cache_data.clear() first, so this
    can't itself be fooled by the same staleness it's guarding against)
    immediately before comparing, and raises ValueError instead of
    writing if `df` has fewer rows than what's live right now. Callers
    that genuinely intend to remove rows (deleting a resident, an
    attending, a step a user chose to delete, ...) should keep calling
    write_sheet_df() directly — this is only for the "should never
    shrink" case."""
    st.cache_data.clear()
    current = read_sheet_df(sheet_name)
    if len(df) < len(current):
        raise ValueError(
            f'Refusing to save "{sheet_name}": that would go from {len(current)} '
            f"rows to {len(df)}. Please reload and try again — nothing was written."
        )
    write_sheet_df(sheet_name, df)


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
    try:
        atnd_df = _read_attendings_df()
    except Exception:
        atnd_df = pd.DataFrame(columns=ATTENDING_COLS)
    return spec_df, proc_df, steps_df, atnd_df


# ─────────────────────────────────────────────
# DATA MUTATION HELPERS
# ─────────────────────────────────────────────

def _hash_password(password: str, salt_hex: str) -> str:
    """PBKDF2-HMAC-SHA256, hex-encoded. salt_hex is a hex string (not raw
    bytes) so it round-trips through Google Sheets as plain text."""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ITERATIONS
    ).hex()


def get_password_row(email: str):
    """Return the auth row dict for this email, or None if no password has
    ever been set for it."""
    auth_df = read_sheet_df(SHEET_AUTH, expected_cols=AUTH_COLS)
    if auth_df.empty:
        return None
    email_norm = email.strip().lower()
    match = auth_df[auth_df["email"].astype(str).str.strip().str.lower() == email_norm]
    if match.empty:
        return None
    row = match.iloc[0]
    if pd.isna(row.get("password_hash")) or not str(row.get("password_hash", "")).strip():
        return None
    return {"password_hash": str(row["password_hash"]), "password_salt": str(row["password_salt"])}


def set_password(email: str, password: str) -> None:
    """Create or overwrite the stored password for this email."""
    auth_df = read_sheet_df(SHEET_AUTH, expected_cols=AUTH_COLS)
    email_norm = email.strip().lower()
    salt_hex = secrets.token_bytes(16).hex()
    password_hash = _hash_password(password, salt_hex)
    auth_df = auth_df[auth_df["email"].astype(str).str.strip().str.lower() != email_norm]
    auth_df = pd.concat([auth_df, pd.DataFrame([{
        "email":         email.strip(),
        "password_hash": password_hash,
        "password_salt": salt_hex,
        "created_at":    datetime.datetime.utcnow().isoformat(),
    }])], ignore_index=True)
    write_sheet_df(SHEET_AUTH, auth_df)


def verify_password(email: str, password: str) -> bool:
    row = get_password_row(email)
    if row is None:
        return False
    candidate = _hash_password(password, row["password_salt"])
    return secrets.compare_digest(candidate, row["password_hash"])


def clear_password(email: str) -> None:
    """Remove the stored password for this email — their next login will
    prompt them to set a new one. Silently no-ops if none is on file."""
    auth_df = read_sheet_df(SHEET_AUTH, expected_cols=AUTH_COLS)
    if auth_df.empty:
        return
    email_norm = email.strip().lower()
    remaining = auth_df[auth_df["email"].astype(str).str.strip().str.lower() != email_norm]
    if len(remaining) != len(auth_df):
        write_sheet_df(SHEET_AUTH, remaining)


def ensure_resident(email: str, name: str = "", specialty_id=None) -> None:
    cols = RESIDENT_COLS
    df   = read_sheet_df(SHEET_RESIDENTS, expected_cols=cols)
    if email not in df["email"].values:
        df = pd.concat([df, pd.DataFrame([{
            "email":        email,
            "name":         name,
            "specialty_id": specialty_id,
            "created_at":   datetime.datetime.utcnow().isoformat(),
        }])], ignore_index=True)
        write_sheet_df(SHEET_RESIDENTS, df)   # also clears cache


def get_resident_last_seen(email: str):
    """The timestamp a resident last had their "new evaluation" badge
    (see get_new_evaluations_for_resident()) cleared, or None if
    they've never had one recorded yet (a brand-new resident, or one
    from before this field existed)."""
    residents_df = read_sheet_df(SHEET_RESIDENTS, expected_cols=RESIDENT_COLS)
    email_norm = str(email).strip().lower()
    match = residents_df[residents_df["email"].astype(str).str.strip().str.lower() == email_norm]
    if match.empty:
        return None
    raw = match.iloc[0].get("last_seen_evaluations_at")
    ts = pd.to_datetime(raw, errors="coerce", utc=True)
    return None if pd.isna(ts) else ts


def mark_resident_evaluations_seen(email: str) -> None:
    """Stamp last_seen_evaluations_at to now for this resident — clears
    their "new evaluation" badge until the next attending submission."""
    residents_df = read_sheet_df(SHEET_RESIDENTS, expected_cols=RESIDENT_COLS)
    email_norm = str(email).strip().lower()
    mask = residents_df["email"].astype(str).str.strip().str.lower() == email_norm
    if not mask.any():
        return
    residents_df.loc[mask, "last_seen_evaluations_at"] = datetime.datetime.utcnow().isoformat()
    write_sheet_df(SHEET_RESIDENTS, residents_df)


def get_viewed_case_ids(email: str) -> set:
    """case_ids this resident has already opened via the new-evaluation
    badge's own per-item link (see mark_evaluation_viewed()) — once
    opened, that one evaluation is excluded from
    get_new_evaluations_for_resident() for good, independent of any
    other new/unviewed ones."""
    views_df = read_sheet_df(SHEET_EVAL_VIEWS, expected_cols=EVAL_VIEW_COLS)
    if views_df.empty:
        return set()
    email_norm = str(email).strip().lower()
    mine = views_df[views_df["resident_email"].astype(str).str.strip().str.lower() == email_norm]
    return set(_norm_id(mine["case_id"]))


def mark_evaluation_viewed(email: str, case_id: str) -> None:
    """Records that this resident has opened this one evaluation —
    excludes it from their new-evaluation badge from here on, leaving
    any other still-unopened ones untouched. A no-op if already
    recorded (e.g. the resident reopens the same link)."""
    if not case_id:
        return
    if _norm_id(pd.Series([case_id])).iloc[0] in get_viewed_case_ids(email):
        return
    views_df = read_sheet_df(SHEET_EVAL_VIEWS, expected_cols=EVAL_VIEW_COLS)
    views_df = pd.concat([views_df, pd.DataFrame([{
        "resident_email": email,
        "case_id":        case_id,
        "viewed_at":      datetime.datetime.utcnow().isoformat(),
    }])], ignore_index=True)
    write_sheet_df(SHEET_EVAL_VIEWS, views_df)


def get_new_evaluations_for_resident(email: str) -> pd.DataFrame:
    """Attending-confirmed cases (never a resident's own Self-Assessment)
    submitted for this resident since their last_seen_evaluations_at and
    not yet individually opened (see get_viewed_case_ids()), newest-
    submitted first — case_id/procedure_id/attending_id/date, for the
    Home page to both count (len(...)) and link out to individually (see
    load_case_detail()). last_seen_evaluations_at itself is only ever a
    one-time bootstrap cutoff (see mark_resident_evaluations_seen()) —
    it does NOT advance just from the badge being shown or Home being
    revisited, only from a resident actually opening one of these links;
    a resident with no last-seen timestamp yet (brand new, or predating
    this field) gets an empty result, since otherwise every historical
    case would count as "new" the first time this ships, rather than
    only genuinely new activity from here on."""
    _cols = ["case_id", "procedure_id", "attending_id", "date"]
    last_seen = get_resident_last_seen(email)
    if last_seen is None:
        return pd.DataFrame(columns=_cols)
    cases_df = read_sheet_df(
        SHEET_CASES,
        expected_cols=["case_id", "resident_email", "procedure_id", "attending_id",
                       "date", "assessment_type", "submitted_at"],
    )
    email_norm = str(email).strip().lower()
    mine = cases_df[
        (cases_df["resident_email"].astype(str).str.strip().str.lower() == email_norm)
        & (cases_df["assessment_type"].fillna("").astype(str).str.strip() != "Self-Assessment")
    ].copy()
    mine["submitted_at"] = pd.to_datetime(mine["submitted_at"], errors="coerce", utc=True)
    mine = mine[mine["submitted_at"] > last_seen]
    mine["case_id"] = _norm_id(mine["case_id"])
    mine = mine[~mine["case_id"].isin(get_viewed_case_ids(email))]
    mine = mine.sort_values("submitted_at", ascending=False)
    return mine[_cols].reset_index(drop=True)


def ensure_attending(name: str, specialty_id: str, email: str = "") -> None:
    df = _read_attendings_df()
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
    """`steps_list` is a list of (step_id, step_name) pairs — every step
    a brand-new procedure starts with must already exist in the catalog
    (created via ➕ Add Step, or reused from another procedure), resolved
    and promoted to a shared id by the caller (see the Add New Procedure
    admin handler's use of _resolve_step_id_for_attach()) before this is
    called; this function no longer mints its own fresh per-procedure
    step_ids."""
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
            "step_id":      step_id,
            "procedure_id": proc_id,
            "step_order":   i + 1,
            "step_name":    step_name,
        } for i, (step_id, step_name) in enumerate(steps_list)])
        steps_df = pd.concat([steps_df, new_steps], ignore_index=True)
        write_sheet_df(SHEET_STEPS, steps_df)


# ─────────────────────────────────────────────
# STEP MERGING (admin "Merge Shared Steps" tool)
# ─────────────────────────────────────────────
# A step (e.g. "Patient Positioning") that's conceptually the same across
# several procedures currently gets its own independent step_id under
# each one (see ensure_procedure() above: S_{procedure_id}_{n}), with no
# link between them — a resident's ratings for that step on one
# procedure never connect to their ratings for "the same" step on
# another. Nothing about the schema actually *requires* that, though:
# steps rows are already scoped by procedure_id, and scores rows just
# match whatever step_id a rating was saved under — so giving several
# steps rows (one per procedure) the same step_id, and relinking their
# scores rows to match, is a data migration, not a schema change.

_STEP_FUZZY_MATCH_THRESHOLD = 0.70  # difflib ratio; see _differ_only_by_opposite_term
                                     # below for why this alone isn't sufficient

# Word pairs where a same-except-this-word step name pair *often* means
# two deliberately *distinct* steps (e.g. "Left Ureter Identification"
# vs "Right Ureter Identification"), not the same step worded two ways —
# despite scoring a plain similarity ratio *higher* than genuine
# synonyms like "Patient Positioning" vs "Position the Patient" do,
# since it differs by only one word. Still surfaced as a suggestion
# (per feedback) rather than excluded outright — some pairs really are
# meant to merge (a step rated the same way regardless of side, say) —
# just flagged and sorted lower so it reads as "look closer" rather
# than "as good a match as any other fuzzy suggestion". Nothing in
# either tier auto-merges or auto-groups on its own regardless.
_STEP_OPPOSITE_TERMS = [
    ("left", "right"), ("proximal", "distal"), ("anterior", "posterior"),
    ("superior", "inferior"), ("upper", "lower"), ("medial", "lateral"),
    ("internal", "external"), ("superficial", "deep"), ("ipsilateral", "contralateral"),
]


def _normalize_step_text(text) -> str:
    """Lowercase, trim, collapse whitespace — for comparing step names
    regardless of case/spacing differences alone."""
    return re.sub(r"\s+", " ", str(text).strip().lower())


def _differ_only_by_opposite_term(norm_a: str, norm_b: str) -> bool:
    """True if two normalized, same-length step names differ in exactly
    one word, and that word pair is a known opposite (see
    _STEP_OPPOSITE_TERMS) — see that constant's own comment for why this
    guards against a specific, common false-positive pattern."""
    words_a, words_b = norm_a.split(), norm_b.split()
    if len(words_a) != len(words_b):
        return False
    diffs = [(wa, wb) for wa, wb in zip(words_a, words_b) if wa != wb]
    if len(diffs) != 1:
        return False
    wa, wb = diffs[0]
    return any({wa, wb} == {x, y} for x, y in _STEP_OPPOSITE_TERMS)


def _slugify_step_label(text: str) -> str:
    """UPPER_SNAKE_CASE-ish slug for a shared step_id, e.g. "Patient
    Positioning" -> "PATIENT_POSITIONING"."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", text.strip()).strip("_").upper()
    return slug[:40] or "STEP"


def _unique_shared_step_id(label: str, existing_ids) -> str:
    """A fresh SHARED_ step_id for `label` that doesn't collide with any
    id in `existing_ids` — appends _2, _3, ... on collision (two
    different labels slugifying to the same text, e.g. differing only in
    punctuation)."""
    base = f"SHARED_{_slugify_step_label(label)}"
    candidate = base
    n = 2
    while candidate in existing_ids:
        candidate = f"{base}_{n}"
        n += 1
    return candidate


def _find_step_merge_candidates(steps_df: pd.DataFrame) -> list:
    """Find groups of `steps` rows — each row from a different procedure
    — that look like the same real-world step but don't yet share a
    step_id. Two tiers, each a list-of-dicts entry with keys "kind"
    ("exact"/"fuzzy"), "label" (default suggested canonical text),
    "rows" (the matching steps_df rows, one per procedure) and "score":
      - "exact": step_name is identical (after trimming/case-folding)
        across 2+ procedures.
      - "fuzzy": step_name is merely *similar* (difflib ratio, at least
        _STEP_FUZZY_MATCH_THRESHOLD) across exactly 2 procedures — always
        needs a human to confirm, and never auto-chained transitively
        across more than one pair, so one bad suggestion can't drag
        unrelated steps together. Carries an extra "opposite_term" flag
        (see _STEP_OPPOSITE_TERMS) when the only difference is a paired
        opposite like left/right — still suggested, just worth a closer
        look before confirming.
    A step_name reused more than once *within the same procedure* (a
    genuine duplicate inside one procedure — a different and riskier
    problem: collapsing two of one case's own steps into one, with a
    ratings-reconciliation question this tool doesn't attempt to answer)
    is excluded from every candidate entirely, not just left ungrouped.
    Already-merged groups (every row already sharing one step_id) don't
    reappear, so this is safe to call fresh after each merge — and
    calling it fresh (not caching the result) is exactly what makes that
    true, since a merge changes what's in `steps`."""
    df = steps_df.copy()
    df["_norm"] = df["step_name"].map(_normalize_step_text)
    df = df[df["_norm"] != ""]

    _dupe_within_proc = df.duplicated(subset=["procedure_id", "_norm"], keep=False)
    df = df[~_dupe_within_proc]

    candidates = []
    _grouped_norms = set()
    for norm, group in df.groupby("_norm"):
        # The intra-procedure dedup above guarantees at most one row per
        # procedure_id in `group`, so distinct step_ids and distinct
        # procedures move in lockstep here — the procedure-count check is
        # belt-and-suspenders, not load-bearing on its own.
        if group["step_id"].nunique() <= 1 or group["procedure_id"].nunique() < 2:
            continue
        candidates.append({
            "kind":  "exact",
            "label": group.iloc[0]["step_name"],
            "rows":  group.drop(columns=["_norm"]),
            "score": 1.0,
        })
        _grouped_norms.add(norm)

    # Fuzzy: pairwise only, among distinct normalized texts not already
    # exact-grouped. Every remaining text has exactly one row left at
    # this point — 2+ procedures would already be an exact-match group
    # above, and 2+ rows in one procedure were excluded above too.
    _remaining = df[~df["_norm"].isin(_grouped_norms)].drop_duplicates(subset=["_norm"])
    _remaining_list = _remaining.to_dict("records")
    for i in range(len(_remaining_list)):
        for j in range(i + 1, len(_remaining_list)):
            a, b = _remaining_list[i], _remaining_list[j]
            if a["procedure_id"] == b["procedure_id"]:
                continue  # can't tell two of one procedure's own steps apart this way
            ratio = difflib.SequenceMatcher(None, a["_norm"], b["_norm"]).ratio()
            if ratio >= _STEP_FUZZY_MATCH_THRESHOLD:
                # Still suggested, per feedback — never auto-grouped or
                # auto-merged either way (nothing in this list merges
                # without an explicit confirmed click) — just flagged so
                # the UI can warn louder, since this pattern scores
                # *higher* on plain text similarity than genuine
                # synonyms do (differing by only one word), yet is
                # usually two deliberately distinct steps.
                candidates.append({
                    "kind":          "fuzzy",
                    "label":         a["step_name"],
                    "rows":          pd.DataFrame([a, b]).drop(columns=["_norm"]),
                    "score":         ratio,
                    "opposite_term": _differ_only_by_opposite_term(a["_norm"], b["_norm"]),
                })

    # Exact first, then fuzzy by score — but a flagged opposite-term
    # fuzzy pair sorts after every other fuzzy suggestion regardless of
    # its own (often high) score, so it doesn't crowd out more likely
    # genuine matches near the top of the list.
    candidates.sort(key=lambda c: (c["kind"] != "exact", c.get("opposite_term", False), -c["score"]))
    return candidates


def _apply_step_merge(rows: pd.DataFrame, canonical_label: str) -> str:
    """Repoint every steps/scores row identified by `rows` (one per
    procedure, from a _find_step_merge_candidates() entry, possibly
    narrowed by the admin unchecking some) onto one shared step_id,
    renaming them all to `canonical_label`. Reuses an already-shared id
    (SHARED_...) if exactly one is already present among `rows` — keeps
    a step's id stable across repeat merges (e.g. folding a third
    procedure's matching step in later) rather than minting a new one
    each time — and refuses if `rows` spans two *different* existing
    shared ids (merging two already-distinct shared steps into one is a
    real decision this function won't make silently on its own).
    Nothing is ever deleted — steps rows are relabeled in place and
    scores rows are relinked, so rating history stays intact. Returns
    the canonical step_id that ended up in use."""
    existing_shared = sorted({sid for sid in rows["step_id"] if str(sid).startswith("SHARED_")})
    if len(existing_shared) > 1:
        raise ValueError(
            "These steps already belong to two different shared groups "
            f"({', '.join(existing_shared)}) — merge them together in a separate step first."
        )

    steps_df = read_sheet_df(SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"])
    canonical_id = existing_shared[0] if existing_shared else _unique_shared_step_id(
        canonical_label, set(steps_df["step_id"])
    )
    old_ids = set(rows["step_id"]) - {canonical_id}

    updated_steps = steps_df.copy()
    _smask = updated_steps["step_id"].isin(rows["step_id"])
    if _smask.sum() != len(rows):
        raise ValueError("Could not find all selected steps — please reload and try again.")
    updated_steps.loc[_smask, "step_id"]   = canonical_id
    updated_steps.loc[_smask, "step_name"] = canonical_label
    write_sheet_df(SHEET_STEPS, updated_steps)

    if old_ids:
        score_cols = ["case_id", "step_id", "rating", "rating_num",
                      "case_complexity", "case_preparation", "overall_performance"]
        scores_df = read_sheet_df(SHEET_SCORES, expected_cols=score_cols)
        _scmask = scores_df["step_id"].isin(old_ids)
        if _scmask.any():
            scores_df.loc[_scmask, "step_id"] = canonical_id
            write_sheet_df(SHEET_SCORES, scores_df)

    return canonical_id


def _count_step_ratings(step_id: str, procedure_id: str) -> int:
    """How many `scores` rows reference `step_id` from one of
    `procedure_id`'s own cases. Scoped through `cases` (case_id ->
    procedure_id) rather than just counting every scores row with this
    step_id, because a shared step_id (see _apply_step_merge) can be
    linked from several procedures at once — a case belonging to a
    *different* procedure that also uses this step_id must not be
    counted (or later deleted) here."""
    cases_df = read_sheet_df(
        SHEET_CASES, expected_cols=_CASE_COLS
    )
    case_ids = set(cases_df.loc[cases_df["procedure_id"] == procedure_id, "case_id"])
    score_cols = ["case_id", "step_id", "rating", "rating_num",
                  "case_complexity", "case_preparation", "overall_performance"]
    scores_df = read_sheet_df(SHEET_SCORES, expected_cols=score_cols)
    return int(((scores_df["step_id"] == step_id) & (scores_df["case_id"].isin(case_ids))).sum())


def _delete_step(step_id: str, procedure_id: str, delete_ratings: bool) -> int:
    """Remove one procedure's link to a step — its single
    (procedure_id, step_id) row in `steps`. If `delete_ratings`, also
    deletes the `scores` rows counted by _count_step_ratings() for this
    exact (step_id, procedure_id) pair — never every scores row with
    this step_id globally, for the same shared-step reason described
    there. `steps` is written first, `scores` second, so a failure
    partway through leaves ratings merely orphaned (recoverable) rather
    than deleted without ever having removed the step they belonged to.
    Returns the number of ratings rows deleted (0 if `delete_ratings`
    is False or none existed)."""
    steps_df = read_sheet_df(SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"])
    _mask = (steps_df["step_id"] == step_id) & (steps_df["procedure_id"] == procedure_id)
    if not _mask.any():
        raise ValueError("Could not find that step — please reload and try again.")
    write_sheet_df(SHEET_STEPS, steps_df[~_mask].reset_index(drop=True))

    if not delete_ratings:
        return 0

    cases_df = read_sheet_df(
        SHEET_CASES, expected_cols=_CASE_COLS
    )
    case_ids = set(cases_df.loc[cases_df["procedure_id"] == procedure_id, "case_id"])
    score_cols = ["case_id", "step_id", "rating", "rating_num",
                  "case_complexity", "case_preparation", "overall_performance"]
    scores_df = read_sheet_df(SHEET_SCORES, expected_cols=score_cols)
    _scmask = (scores_df["step_id"] == step_id) & (scores_df["case_id"].isin(case_ids))
    n_deleted = int(_scmask.sum())
    if n_deleted:
        write_sheet_df(SHEET_SCORES, scores_df[~_scmask].reset_index(drop=True))
    return n_deleted


def _list_existing_steps(exclude_procedure_id=None) -> pd.DataFrame:
    """One row per distinct step_id across the whole catalog — including
    a step created via ➕ Add Step but not yet attached to any procedure
    (a `steps` row with a blank procedure_id) — optionally excluding
    whatever `exclude_procedure_id` already has (by step_id), so
    building/editing one procedure's own step list only offers steps it
    doesn't already include. Used everywhere a step is picked from the
    existing catalog rather than typed: Add New Procedure, and Edit
    Existing Procedure's "add an existing step" picker."""
    all_steps_df = read_sheet_df(SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"])
    candidates = all_steps_df.drop_duplicates(subset=["step_id"])
    if exclude_procedure_id is not None:
        _own_ids = set(all_steps_df.loc[all_steps_df["procedure_id"] == exclude_procedure_id, "step_id"])
        candidates = candidates[~candidates["step_id"].isin(_own_ids)]
    return candidates


def _resolve_step_id_for_attach(step_id: str) -> str:
    """Promote `step_id` to a shared (SHARED_...) id if it isn't one
    already, reusing _apply_step_merge()'s own mint-and-relink logic
    even for a single row — the effect is exactly a rename-in-place
    plus a ratings relink, which is what's needed before a step
    created (or historically added) under a plain procedure-scoped id
    can safely be attached to one more procedure. Returns the id
    actually in use afterward (unchanged if it was already shared).
    Every path that attaches an existing step to a procedure — Add New
    Procedure, Edit Existing Procedure's picker, and the standalone
    attach used to live at ➕ Add Step — funnels through this so a step
    only ever needs promoting once, the first time it's reused."""
    if str(step_id).startswith("SHARED_"):
        return step_id
    steps_df = read_sheet_df(SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"])
    _rows = steps_df[steps_df["step_id"] == step_id]
    if _rows.empty:
        raise ValueError("Could not find that step — please reload and try again.")
    return _apply_step_merge(_rows, _rows.iloc[0]["step_name"])


def _attach_existing_step(source_step_id: str, target_procedure_id: str, step_order) -> str:
    """Add `source_step_id` (an existing step, from any procedure, or
    not yet attached to one at all) to `target_procedure_id` as a new
    step at `step_order`, reusing its exact step_id (promoted to a
    shared one first via _resolve_step_id_for_attach() if it isn't
    already) rather than minting a fresh, disconnected one for the same
    text — so ratings for it connect the same way no matter which
    procedure it's attached through, with no separate Merge Shared
    Steps pass needed afterward.

    Raises ValueError if `source_step_id` doesn't exist, or if
    `target_procedure_id` already has it (attaching a step already on
    the target procedure is the intra-procedure-duplicate problem this
    tool doesn't attempt to resolve — see _find_step_merge_candidates).
    Returns the step_id actually used."""
    steps_df = read_sheet_df(SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"])
    _source_rows = steps_df[steps_df["step_id"] == source_step_id]
    if _source_rows.empty:
        raise ValueError("Could not find that step — please reload and try again.")
    if (_source_rows["procedure_id"] == target_procedure_id).any():
        raise ValueError("This procedure already has that step.")
    step_name = _source_rows.iloc[0]["step_name"]

    canonical_id = _resolve_step_id_for_attach(source_step_id)
    steps_df = read_sheet_df(SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"])

    new_row = pd.DataFrame([{
        "step_id":      canonical_id,
        "procedure_id": target_procedure_id,
        "step_order":   step_order,
        "step_name":    step_name,
    }])
    write_sheet_df(SHEET_STEPS, pd.concat([steps_df, new_row], ignore_index=True))
    return canonical_id


def _create_new_step(step_name: str) -> str:
    """Create a brand-new catalog step — a `steps` row with a blank
    procedure_id, not yet attached to any procedure — for the ➕ Add
    Step admin tool. Refuses if a step with this name (trimmed/
    case-folded) already exists anywhere: the point of this tool is
    specifically steps that don't exist yet, so an exact rediscovery of
    one that does should be attached instead (Add New Procedure or Edit
    Existing Procedure's existing-steps picker) rather than creating a
    second, disconnected entry with the same text. Returns the new
    step_id."""
    steps_df = read_sheet_df(SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"])
    _norm = _normalize_step_text(step_name)
    if (steps_df["step_name"].map(_normalize_step_text) == _norm).any():
        raise ValueError(f'"{step_name}" already exists — pick it from the existing-steps list instead of creating a duplicate.')
    new_id = _unique_shared_step_id(step_name, set(steps_df["step_id"]))
    new_row = pd.DataFrame([{
        "step_id":      new_id,
        "procedure_id": "",
        "step_order":   pd.NA,
        "step_name":    step_name,
    }])
    write_sheet_df(SHEET_STEPS, pd.concat([steps_df, new_row], ignore_index=True))
    return new_id


def _count_procedure_cases(procedure_id: str) -> int:
    """How many `cases` rows reference `procedure_id`."""
    cases_df = read_sheet_df(SHEET_CASES, expected_cols=_CASE_COLS)
    return int((cases_df["procedure_id"] == procedure_id).sum())


def _delete_procedure(procedure_id: str, delete_cases: bool) -> dict:
    """Delete a procedure entirely: its row in `procedures`, and its own
    steps rows in `steps` — a step shared with other procedures (via
    Merge Shared Steps or Add Step) keeps its rows for those other
    procedures untouched; only this procedure's own (procedure_id,
    step_id) link is removed, same scoping as _delete_step(). If
    `delete_cases`, also deletes every `cases` row for this procedure
    and every `scores` row belonging to those cases.

    Write order: steps, then the procedure row, then scores, then
    cases — so a failure partway through leaves the more easily
    reconstructed definitions (steps/procedure) gone before the
    harder-to-recover historical data (scores/cases) is ever touched,
    and a scores row is never left pointing at an already-deleted
    case. Returns counts actually removed: {"steps", "cases",
    "scores"}."""
    steps_df = read_sheet_df(SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"])
    _step_mask = steps_df["procedure_id"] == procedure_id
    n_steps = int(_step_mask.sum())
    write_sheet_df(SHEET_STEPS, steps_df[~_step_mask].reset_index(drop=True))

    procs_df = read_sheet_df(SHEET_PROCEDURES, expected_cols=["procedure_id", "procedure_name", "specialty_id"])
    write_sheet_df(SHEET_PROCEDURES, procs_df[procs_df["procedure_id"] != procedure_id].reset_index(drop=True))

    n_cases = 0
    n_scores = 0
    if delete_cases:
        cases_df = read_sheet_df(SHEET_CASES, expected_cols=_CASE_COLS)
        _case_mask = cases_df["procedure_id"] == procedure_id
        case_ids = set(cases_df.loc[_case_mask, "case_id"])
        n_cases = int(_case_mask.sum())

        if case_ids:
            score_cols = ["case_id", "step_id", "rating", "rating_num",
                          "case_complexity", "case_preparation", "overall_performance"]
            scores_df = read_sheet_df(SHEET_SCORES, expected_cols=score_cols)
            _score_mask = scores_df["case_id"].isin(case_ids)
            n_scores = int(_score_mask.sum())
            if n_scores:
                write_sheet_df(SHEET_SCORES, scores_df[~_score_mask].reset_index(drop=True))

        if n_cases:
            write_sheet_df(SHEET_CASES, cases_df[~_case_mask].reset_index(drop=True))

    return {"steps": n_steps, "cases": n_cases, "scores": n_scores}


def _count_specialty_usage(specialty_id: str) -> dict:
    """How many residents/attendings/procedures reference
    `specialty_id`. Unlike a procedure's cases (tightly-coupled
    historical data), these are just categorized *by* the specialty —
    deleting it doesn't cascade to them (see _delete_specialty()), so
    this is purely informational for the confirmation prompt."""
    residents_df  = read_sheet_df(SHEET_RESIDENTS, expected_cols=RESIDENT_COLS)
    attendings_df = _read_attendings_df()
    procs_df      = read_sheet_df(SHEET_PROCEDURES, expected_cols=["procedure_id", "procedure_name", "specialty_id"])
    return {
        "residents":  int((residents_df["specialty_id"] == specialty_id).sum()),
        "attendings": int((attendings_df["specialty_id"] == specialty_id).sum()),
        "procedures": int((procs_df["specialty_id"] == specialty_id).sum()),
    }


def _delete_specialty(specialty_id: str) -> None:
    """Remove one specialty's row in `specialties`. Residents/
    attendings/procedures that reference it are left exactly as they
    are — not deleted or reassigned, unlike deleting a procedure (see
    _delete_procedure()) or a step. Their specialty_id just stops
    resolving to a friendly name anywhere it's looked up (every such
    lookup in this app already falls back to showing the raw id via
    `.map(lookup).fillna(id)`, so nothing breaks — it just reads as an
    id instead of a name until it's given a specialty again). Raises
    ValueError if the specialty doesn't exist."""
    spec_df = read_sheet_df(SHEET_SPECIALTY, expected_cols=["specialty_id", "specialty_name"])
    _mask = spec_df["specialty_id"] == specialty_id
    if not _mask.any():
        raise ValueError("Could not find that specialty — please reload and try again.")
    write_sheet_df(SHEET_SPECIALTY, spec_df[~_mask].reset_index(drop=True))


def save_case(
    resident_email: str,
    date,
    specialty_id: str,
    procedure_id: str,
    attending_id: str,
    scores_dict: dict,
    notes: str = "",
    case_complexity=None,
    case_preparation=None,
    overall_performance=None,
    robo_type=None,
    improve: str = "",
    how: str = "",
    assessment_type: str = "",
    self_assessment_diff: str = "",
) -> str:
    """Persist a case + its step scores; returns the new case_id.

    assessment_type distinguishes who the case record represents:
    "Self-Assessment" for a resident's own entry (Finish & Save, or the
    dual-save that happens when generating a pre-filled magic link) vs
    "Attending Evaluation" for the attending's magic-link submission.

    robo_type ("Xi"/"SP"/"DV5") is only meaningful for a robotic
    procedure (see _is_robotic_procedure) — None otherwise.

    self_assessment_diff: JSON string ({"had_draft": bool, "changes":
    [[label, old, new], ...]}) capturing what the attending changed
    from the resident's original self-assessment, if this case row was
    reviewed from a draft — empty string otherwise. Persisted (rather
    than kept only in session_state) so the same "what changed" view
    can be shown to the resident later, in a different session, via
    load_case_detail().
    """
    case_id   = uuid.uuid4().hex[:12]

    case_cols = ["case_id", "resident_email", "date", "specialty_id",
                 "procedure_id", "attending_id", "notes",
                 "case_complexity", "case_preparation", "overall_performance",
                 "robo_type", "improve", "how", "assessment_type",
                 "self_assessment_diff", "submitted_at"]
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
        "case_preparation":    case_preparation,
        "overall_performance": overall_performance,
        "robo_type":           robo_type,
        "improve":             improve,
        "how":                 how,
        "assessment_type":     assessment_type,
        "self_assessment_diff": self_assessment_diff,
        # Distinct from `date` (the procedure's own date, picked by
        # whoever filled the form — can be well in the past) — this is
        # when the row was actually saved, which the "new evaluation"
        # badge (see get_new_evaluations_for_resident()) compares
        # against a resident's own last-seen timestamp.
        "submitted_at":        datetime.datetime.utcnow().isoformat(),
    }])], ignore_index=True)
    write_sheet_df(SHEET_CASES, cases_df)  # clears cache

    score_cols = ["case_id", "step_id", "rating", "rating_num",
                  "case_complexity", "case_preparation", "overall_performance"]
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
        "case_preparation":    case_preparation,
        "overall_performance": overall_performance,
    } for step_id, rating in scores_dict.items()]
    scores_df  = pd.concat([scores_df, pd.DataFrame(new_rows)], ignore_index=True)
    write_sheet_df(SHEET_SCORES, scores_df)  # clears cache

    return case_id


def save_draft(
    resident_email: str,
    date,
    specialty_id: str,
    procedure_id: str,
    attending_id: str,
    scores_dict: dict,
    notes: str = "",
    case_complexity=None,
    case_preparation=None,
    overall_performance=None,
    robo_type=None,
    improve: str = "",
    how: str = "",
) -> str:
    """Save a resident's in-progress assessment as a pre-fill draft for a
    magic link; returns the draft_id to embed in the link's query string."""
    draft_id  = uuid.uuid4().hex[:12]
    drafts_df = read_sheet_df(SHEET_DRAFTS, expected_cols=DRAFT_COLS)
    drafts_df = pd.concat([drafts_df, pd.DataFrame([{
        "draft_id":             draft_id,
        "resident_email":       resident_email,
        "date":                 str(date),
        "specialty_id":         specialty_id,
        "procedure_id":         procedure_id,
        "attending_id":         attending_id,
        "case_complexity":      case_complexity,
        "case_preparation":     case_preparation,
        "overall_performance":  overall_performance,
        "robo_type":            robo_type,
        "improve":              improve,
        "how":                  how,
        "notes":                notes,
        "scores_json":          json.dumps(scores_dict),
        "created_at":           datetime.datetime.utcnow().isoformat(),
    }])], ignore_index=True)
    write_sheet_df(SHEET_DRAFTS, drafts_df)
    return draft_id


def load_draft(draft_id: str):
    """Fetch a pre-fill draft by id. Returns None if missing or blank —
    raises ConnectionError if the sheet can't be reached, rather than
    swallowing it, so the caller can tell "genuinely already consumed"
    (safe to treat as already-reviewed) apart from "transient network
    failure" (must not be treated as already-reviewed)."""
    if not draft_id:
        return None
    drafts_df = read_sheet_df(SHEET_DRAFTS, expected_cols=DRAFT_COLS)
    if drafts_df.empty:
        return None
    drafts_df   = drafts_df.copy()
    drafts_df["draft_id"] = _norm_id(drafts_df["draft_id"])
    target      = _norm_id(pd.Series([draft_id])).iloc[0]
    match       = drafts_df[drafts_df["draft_id"] == target]
    if match.empty:
        return None
    row = match.iloc[0]

    def _clean(v):
        # Blank sheet cells round-trip through pandas as NaN (a float), not
        # "" — and NaN is truthy in Python, so a plain `v or ""` doesn't
        # catch it, leaving the literal text "nan" in text inputs/areas.
        return "" if pd.isna(v) else str(v)

    try:
        scores = json.loads(_clean(row.get("scores_json")) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        scores = {}
    return {
        "date":                 row.get("date"),
        "case_complexity":      row.get("case_complexity"),
        "case_preparation":     row.get("case_preparation"),
        "overall_performance":  row.get("overall_performance"),
        "robo_type":            row.get("robo_type"),
        "improve":              _clean(row.get("improve")),
        "how":                  _clean(row.get("how")),
        "notes":                _clean(row.get("notes")),
        "scores":               scores,
    }


def delete_draft(draft_id: str) -> None:
    """Remove a consumed draft. Cleanup only — never raises."""
    if not draft_id:
        return
    try:
        drafts_df = read_sheet_df(SHEET_DRAFTS, expected_cols=DRAFT_COLS)
        if drafts_df.empty:
            return
        drafts_df = drafts_df.copy()
        drafts_df["draft_id"] = _norm_id(drafts_df["draft_id"])
        target    = _norm_id(pd.Series([draft_id])).iloc[0]
        remaining = drafts_df[drafts_df["draft_id"] != target]
        if len(remaining) != len(drafts_df):
            write_sheet_df(SHEET_DRAFTS, remaining)
    except Exception:
        pass


def save_self_eval_request(
    resident_email: str,
    date,
    specialty_id: str,
    procedure_id: str,
    attending_id: str,
) -> str:
    """Persist an attending's request for a resident's self-evaluation;
    returns the new request_id, embedded in the magic link's query
    string AND used to find/clear this same row again once fulfilled
    (see delete_self_eval_request()) — the one thing both the magic
    link and the resident Home page notification have in common."""
    request_id = uuid.uuid4().hex[:12]
    df = read_sheet_df(SHEET_SELF_EVAL_REQUESTS, expected_cols=SELF_EVAL_REQUEST_COLS)
    df = pd.concat([df, pd.DataFrame([{
        "request_id":      request_id,
        "resident_email":  resident_email,
        "date":            str(date),
        "specialty_id":    specialty_id,
        "procedure_id":    procedure_id,
        "attending_id":    attending_id,
        "created_at":      datetime.datetime.utcnow().isoformat(),
    }])], ignore_index=True)
    write_sheet_df(SHEET_SELF_EVAL_REQUESTS, df)
    return request_id


def load_self_eval_request(request_id: str):
    """Fetch a pending self-eval request by id. Returns None if missing,
    already fulfilled (deleted), or the sheet can't be reached."""
    if not request_id:
        return None
    drafts_df = read_sheet_df(SHEET_SELF_EVAL_REQUESTS, expected_cols=SELF_EVAL_REQUEST_COLS)
    if drafts_df.empty:
        return None
    drafts_df = drafts_df.copy()
    drafts_df["request_id"] = _norm_id(drafts_df["request_id"])
    target = _norm_id(pd.Series([request_id])).iloc[0]
    match  = drafts_df[drafts_df["request_id"] == target]
    if match.empty:
        return None
    row = match.iloc[0]
    return {
        "request_id":     row["request_id"],
        "resident_email": row.get("resident_email", ""),
        "date":           row.get("date", ""),
        "specialty_id":   row.get("specialty_id", ""),
        "procedure_id":   row.get("procedure_id", ""),
        "attending_id":   row.get("attending_id", ""),
    }


def delete_self_eval_request(request_id: str) -> None:
    """Remove a fulfilled (or cancelled) request. Cleanup only — never
    raises. This is what actually cross-references the magic link and
    the Home page notification against each other: whichever route the
    resident completes the self-assessment through, this same row
    disappears, so the other route reads it as already fulfilled too."""
    if not request_id:
        return
    try:
        df = read_sheet_df(SHEET_SELF_EVAL_REQUESTS, expected_cols=SELF_EVAL_REQUEST_COLS)
        if df.empty:
            return
        df = df.copy()
        df["request_id"] = _norm_id(df["request_id"])
        target    = _norm_id(pd.Series([request_id])).iloc[0]
        remaining = df[df["request_id"] != target]
        if len(remaining) != len(df):
            write_sheet_df(SHEET_SELF_EVAL_REQUESTS, remaining)
    except Exception:
        pass


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


ATTENDING_COLS = ["attending_id", "attending_name", "specialty_id", "email"]


def _read_attendings_df() -> pd.DataFrame:
    """Read the attendings sheet as ATTENDING_COLS, tolerating a sheet whose
    login-email column is still named "attending_email" (an older schema —
    some existing sheets/export snapshots use that name) instead of "email".
    Always returns a single, populated "email" column regardless of which
    name the live sheet actually has, preferring "email" when both are
    present. Every read of the attendings sheet should go through this,
    not a raw read_sheet_df(SHEET_ATTENDINGS, ...) call, so that whichever
    column the data is really in, it's picked up — and so any write that
    follows (which only ever writes ATTENDING_COLS) carries the value
    forward under "email" instead of silently dropping it."""
    df = read_sheet_df(SHEET_ATTENDINGS, expected_cols=ATTENDING_COLS + ["attending_email"])
    _email  = df["email"].fillna("").astype(str).str.strip()
    _legacy = df["attending_email"].fillna("").astype(str).str.strip()
    df["email"] = _email.where(_email != "", _legacy)
    return df[ATTENDING_COLS]


def load_case_detail(case_id: str):
    """Assembles one case's full details, by case_id alone, in the same
    shape as the attending_submission dict built right after a live
    submission — so _render_evaluation_card() below can render either
    one identically. Backs the "view one evaluation" page linked from
    the Home page's new-evaluation badge, opened well after the actual
    submission, in a different session, with none of that in-memory
    state available — everything has to come from the sheets fresh.
    Returns None if no such case exists."""
    if not case_id:
        return None
    cases_df = read_sheet_df(SHEET_CASES, expected_cols=_CASE_COLS)
    cases_df["case_id"] = _norm_id(cases_df["case_id"])
    target = _norm_id(pd.Series([case_id])).iloc[0]
    match = cases_df[cases_df["case_id"] == target]
    if match.empty:
        return None
    row = match.iloc[0]

    def _clean(v):
        return "" if pd.isna(v) else str(v)

    scores_df = read_sheet_df(SHEET_SCORES, expected_cols=["case_id", "step_id", "rating"])
    scores_df["case_id"] = _norm_id(scores_df["case_id"])
    case_scores = scores_df[scores_df["case_id"] == row["case_id"]]
    scores = dict(zip(case_scores["step_id"].astype(str), case_scores["rating"].astype(str)))

    steps_df = read_sheet_df(SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"])
    proc_steps = (
        steps_df[steps_df["procedure_id"].astype(str) == str(row["procedure_id"])]
        .sort_values("step_order")
    )
    steps = [{"step_id": str(r["step_id"]), "step_name": str(r["step_name"])} for _, r in proc_steps.iterrows()]

    procs_df = read_sheet_df(SHEET_PROCEDURES, expected_cols=["procedure_id", "procedure_name", "specialty_id"])
    proc_match = procs_df[procs_df["procedure_id"].astype(str) == str(row["procedure_id"])]
    procedure_name = proc_match["procedure_name"].values[0] if len(proc_match) else str(row["procedure_id"])

    atnds_lookup = dict(zip(_read_attendings_df()["attending_id"], _read_attendings_df()["attending_name"]))
    attending_name = attending_display_name(str(row.get("attending_id", "")), atnds_lookup)

    residents_df = read_sheet_df(SHEET_RESIDENTS, expected_cols=RESIDENT_COLS)
    res_match = residents_df[
        residents_df["email"].astype(str).str.strip().str.lower()
        == str(row.get("resident_email", "")).strip().lower()
    ]
    resident_name = res_match["name"].values[0] if len(res_match) else row.get("resident_email", "")

    # Reconstitutes the "what changed from the resident's self-assessment"
    # diff persisted by save_case() at attending-review time, if any —
    # same shape as the live attending_submission dict built right after
    # submission, so this case can show identical "before/after" info
    # even when opened much later by the resident.
    _had_draft, _changes = False, []
    _diff_raw = row.get("self_assessment_diff")
    # pd.isna() must run first: a column missing from an older sheet
    # row reads back as pd.NA (not NaN/None), and pd.NA's own __bool__
    # raises TypeError ("boolean value is ambiguous") rather than
    # returning False, so `if _diff_raw` alone would crash on it.
    if not pd.isna(_diff_raw) and _diff_raw:
        try:
            _diff_parsed = json.loads(_diff_raw)
            _had_draft = bool(_diff_parsed.get("had_draft"))
            _changes   = [tuple(c) for c in _diff_parsed.get("changes", [])]
        except (ValueError, TypeError):
            pass

    return {
        "case_id":             row["case_id"],
        "resident_email":      row.get("resident_email", ""),
        "resident_name":       resident_name,
        "procedure_id":        row.get("procedure_id", ""),
        "procedure_name":      procedure_name,
        "attending_id":        row.get("attending_id", ""),
        "attending_name":      attending_name,
        "date":                row.get("date", ""),
        "case_complexity":     row.get("case_complexity"),
        "case_preparation":    row.get("case_preparation"),
        "overall_performance": row.get("overall_performance"),
        "robo_type":           row.get("robo_type"),
        "notes":               _clean(row.get("notes")),
        "improve":             _clean(row.get("improve")),
        "how":                 _clean(row.get("how")),
        "assessment_type":     row.get("assessment_type", ""),
        "had_draft":           _had_draft,
        "changes":             _changes,
        "scores":              scores,
        "steps":               steps,
    }


def _render_self_assessment_diff(sub: dict) -> None:
    """The "what changed from the resident's original self-assessment"
    section — a top-of-page note plus a side-by-side, highlighted
    before/after row per changed field. Shared by attending_confirmation
    (right after a live submission) and view_evaluation (opened later,
    by the resident, from their own history/dashboard) so both show the
    identical comparison, sourced from the same "had_draft"/"changes"
    keys — live in session_state right after submission, or reconstituted
    from the persisted self_assessment_diff column via load_case_detail()
    when viewed later. No-op when the case had no self-assessment draft
    to compare against at all (e.g. a blank attending evaluation)."""
    if not sub.get("had_draft"):
        return
    _changes = sub.get("changes") or []
    if _changes:
        st.warning(
            f"✏️ {len(_changes)} change{'s' if len(_changes) != 1 else ''} "
            f"{'were' if len(_changes) != 1 else 'was'} made from the resident's "
            f"original self-assessment:"
        )
        for _label, _old, _new in _changes:
            st.markdown(f"**{_label}**")
            _diff_cols = st.columns(2)
            with _diff_cols[0]:
                st.error(f"Before: {_old}")
            with _diff_cols[1]:
                st.success(f"After: {_new}")
    else:
        st.info("✅ No changes were made from the resident's original self-assessment.")


def _render_evaluation_card(sub: dict) -> None:
    """The evaluation-summary card + step ratings table shown right
    after a live submission (attending_confirmation) and, identically,
    on the standalone "view one evaluation" page — same dict shape,
    same rendering, so the resident sees the exact same thing either
    way."""
    # Blank/missing sheet cells round-trip through pandas as NaN (a float),
    # not "" — and NaN is truthy in Python, so a plain `sub.get(...)` check
    # let older records with no robo_type recorded show up as "Robot: nan".
    _robo_type = sub.get("robo_type")
    _robo_type_line = f'<br><b>Robot:</b> {_robo_type}' if pd.notna(_robo_type) and str(_robo_type).strip() else ""
    # Same NaN guard as robo_type above, but Daily Preparation always
    # shows (unlike Robot, which only applies to robotic procedures) —
    # falls back to "Not Assessed" rather than being hidden, matching
    # the assessment forms' own default for this field.
    _case_prep = sub.get("case_preparation")
    _case_prep_display = str(_case_prep) if pd.notna(_case_prep) and str(_case_prep).strip() else "Not Assessed"
    st.markdown(
        f'<div class="pp-card">'
        f'<b>Resident:</b> {sub.get("resident_name", sub["resident_email"])}<br>'
        f'<b>Attending:</b> {sub["attending_name"]}<br>'
        f'<b>Procedure:</b> {sub.get("procedure_name", sub["procedure_id"])}<br>'
        f'<b>Date:</b> {fmt_date(sub["date"])}<br>'
        f'<b>Overall Performance:</b> {sub["overall_performance"]}<br>'
        f'<b>Daily Preparation:</b> {_case_prep_display}'
        f'{_robo_type_line}'
        f'</div>',
        unsafe_allow_html=True,
    )

    if sub.get("improve", "").strip() or sub.get("how", "").strip():
        st.markdown(f"**In order to improve this:** {sub.get('improve', '') or '_(blank)_'}.")
        st.markdown(f"**Do this:** {sub.get('how', '') or '_(blank)_'}.")

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


# Pinned procedures always come first, in this order (when present for
# the current specialty); everything else follows alphabetically. Matched
# case/whitespace-insensitively (normalizing runs of whitespace, including
# non-breaking spaces, to a single plain space) since a sheet value that
# differs from these only by case or stray spacing should still be
# recognized as the same procedure and pinned correctly.
_PINNED_PROCS = [
    "Robotic Surgical Skills Feedback",
    "Robotic Bedsiding",
    "Open Surgical Skills Feedback",
    "Endoscopic Surgical Skills Feedback",
]


def _norm_proc(name: str) -> str:
    return " ".join(name.replace("\xa0", " ").split()).casefold()


def _ordered_procedure_names(proc_map: dict) -> list:
    """Procedure names for a Procedure dropdown: the pinned procedures
    first (in _PINNED_PROCS order), then everything else alphabetically."""
    _pinned_rank = {_norm_proc(name): i for i, name in enumerate(_PINNED_PROCS)}
    return sorted(
        proc_map.keys(),
        key=lambda n: (
            _pinned_rank.get(_norm_proc(n), len(_PINNED_PROCS)),
            n if _norm_proc(n) not in _pinned_rank else "",
        ),
    )


def _is_robotic_procedure(procedure_name: str) -> bool:
    """True if a procedure's name suggests it's done on a robotic
    platform, so the assessment form should show the Xi/SP/DV5 robot
    picker (see render_robo_type_picker). "robotic"/"robo" match
    case-insensitively anywhere in the name; "RAL" is matched only as
    an exact-case substring — case-insensitively it would false-positive
    on ordinary words that happen to end in "ral", e.g. "General"."""
    name = str(procedure_name or "")
    lname = name.lower()
    return "robotic" in lname or "robo" in lname or "RAL" in name


def _on_robo_checkbox_change(value_key: str, widget_keys: dict, clicked_label: str) -> None:
    """Keeps the Xi/SP/DV5 checkbox trio behaving like a single-select
    group even though st.checkbox has no native radio-group mode:
    checking one unchecks the other two and becomes the recorded
    selection; trying to uncheck the only checked one is refused (there
    must always be exactly one) by immediately re-checking it."""
    if st.session_state[widget_keys[clicked_label]]:
        for label, k in widget_keys.items():
            if label != clicked_label:
                st.session_state[k] = False
        st.session_state[value_key] = clicked_label
    else:
        st.session_state[widget_keys[clicked_label]] = True


def render_prep_legend(key: str, expanded: bool = False, container=None) -> None:
    """Preparation Scale Legend: a color swatch plus its full description
    for each Daily Preparation level (PREP_HEX/PREP_DESCRIPTIONS). Shared
    by the assessment/pre-fill forms (right under the Daily Preparation
    dropdown) and the dashboards (alongside the Ratings/Case Complexity
    legends) so the scale is explained the same way everywhere it's
    used. `container` lets this render inside st.sidebar instead of the
    main area, same as render_rating_legend(). `key` must be unique
    among expanders visible at once."""
    _container = container if container is not None else st
    # Display order only (PREP_HEX itself — used elsewhere for color
    # lookups — is untouched): highest preparation (Highly Prepared)
    # first, descending to Unprepared, same convention as
    # render_rating_legend()'s Auto-first ordering.
    _items = list(PREP_HEX.items())
    _items.reverse()
    with _container.expander("Preparation Scale Legend", expanded=expanded, key=key):
        st.markdown(
            '<div class="legend-desc-list">' +
            "".join(
                f'<div class="legend-desc-row">'
                f'<span class="legend-swatch" style="background-color:{color}"></span>'
                f'<span><b>{label}</b> — {PREP_DESCRIPTIONS[label]}</span>'
                f'</div>'
                for label, color in _items
            ) +
            '</div>',
            unsafe_allow_html=True,
        )


def render_complexity_legend(key: str, expanded: bool = False, container=None) -> None:
    """Case Complexity Legend: a color swatch plus its description for
    each complexity level (COMPLEXITY_HEX/COMPLEXITY_DESCRIPTIONS), same
    expandable swatch+description layout as render_prep_legend().
    `container` lets this render inside st.sidebar instead of the main
    area, same as render_rating_legend()."""
    _container = container if container is not None else st
    with _container.expander("Case Complexity Legend", expanded=expanded, key=key):
        st.markdown(
            '<div class="legend-desc-list">' +
            "".join(
                f'<div class="legend-desc-row">'
                f'<span class="legend-swatch" style="background-color:{color}"></span>'
                f'<span><b>{label}</b> — {COMPLEXITY_DESCRIPTIONS[label]}</span>'
                f'</div>'
                for label, color in COMPLEXITY_HEX.items()
            ) +
            '</div>',
            unsafe_allow_html=True,
        )


def render_rating_legend(key: str, expanded: bool = False, container=None) -> None:
    """Step/Skill Autonomy Rating Legend: a color swatch plus description
    for each step rating (RATING_HEX/RATING_DESCRIPTIONS), plus the
    heatmap-only "Never Attempted" stripe (no rating of its own — a step
    that simply wasn't part of that particular case). Shared by the
    dashboards (alongside the Preparation Scale/Case Complexity legends)
    and the sidebar (shown while filling out an assessment), same
    swatch+description layout as render_prep_legend(). `container` lets
    this render inside st.sidebar instead of the main area; `key` must
    be unique among expanders visible at once."""
    _container = container if container is not None else st
    # Display order only (RATING_OPTIONS/RATING_HEX — the actual dropdown
    # order and color lookups — are untouched): highest autonomy (Auto)
    # first, descending to Shown/Told, with the two non-graded items
    # (Not Assessed, Never Attempted) grouped at the bottom.
    _graded = [(label, color, "", "") for label, color in RATING_HEX.items() if label != "Not Assessed"]
    _graded.reverse()
    _items = _graded + [
        ("Not Assessed",    "#E0E0E0", "1px solid #aaa", ""),
        ("Never Attempted", "#FAFAFA", "", NEVER_ATTEMPTED_STRIPE_CSS),
    ]
    _rows = []
    for label, color, border, pattern in _items:
        _bdr = f"border:{border};" if border else ""
        _pat = f"background-image:{pattern};" if pattern else ""
        _desc = RATING_DESCRIPTIONS.get(label)
        _text = f'<b>{label}</b> — {_desc}' if _desc else f'<b>{label}</b>'
        _rows.append(
            f'<div class="legend-desc-row">'
            f'<span class="legend-swatch" style="background-color:{color};{_bdr}{_pat}"></span>'
            f'<span>{_text}</span>'
            f'</div>'
        )
    with _container.expander("Step/Skill Autonomy Rating Legend", expanded=expanded, key=key):
        st.markdown('<div class="legend-desc-list">' + "".join(_rows) + '</div>', unsafe_allow_html=True)


def render_robo_type_picker(value_key: str, default: str = "Xi") -> str:
    """Renders "Robot:" and the Xi/SP/DV5 checkboxes inline in one tight
    row (the .st-key-assess_robo_row CSS rule shrinks each column to its
    own content instead of stretching evenly across the full row, which
    is what st.columns() does by default) and returns the current
    selection (also left in st.session_state[value_key] for
    save_case()/save_draft() to read). `default` only takes effect the
    first time `value_key` is ever set for this session — e.g. seeded
    from a pre-fill draft's own "robo_type" on the attending's page —
    and is ignored on every later rerun once the form has actually been
    interacted with."""
    _labels = ["Xi", "SP", "DV5"]
    if st.session_state.get(value_key) not in _labels:
        st.session_state[value_key] = default if default in _labels else "Xi"
    _current = st.session_state[value_key]
    _widget_keys = {label: f"{value_key}_cb_{label}" for label in _labels}
    for label, k in _widget_keys.items():
        if k not in st.session_state:
            st.session_state[k] = (label == _current)
    with st.container(key="assess_robo_row"):
        _label_col, *_cb_cols = st.columns(1 + len(_labels))
        with _label_col:
            st.markdown("**Robot:**")
        for _col, label in zip(_cb_cols, _labels):
            with _col:
                st.checkbox(
                    label, key=_widget_keys[label],
                    on_change=_on_robo_checkbox_change,
                    args=(value_key, _widget_keys, label),
                )
    return st.session_state[value_key]


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
# RESIDENT DATA HELPERS
# (shared by a resident's own Comments/Cumulative dashboards and by an
# attending's Resident Dashboard, which shows the same views for a resident
# of the attending's choosing)
# ─────────────────────────────────────────────
def _build_comments(row) -> str:
    """Plain-text Comments value (used for the Excel export): the
    "In order to improve this: ..." sentence (if either field was
    answered), followed by the free-text notes."""
    imp = row["improve"].strip()
    how = row["how"].strip()
    parts = []
    if imp or how:
        parts.append(f"In order to improve this: {imp or '(blank)'}.\nDo this: {how or '(blank)'}.")
    if row["notes"].strip():
        parts.append(row["notes"].strip())
    return "\n\n".join(parts)


def _build_comments_html(row) -> str:
    """HTML Comments value for the on-screen table: "In order to
    improve this:" and "Do this:" are bold labels, each starting its
    own line; the resident's own answers are underlined, not bold."""
    imp = row["improve"].strip()
    how = row["how"].strip()
    parts = []
    if imp or how:
        imp_html = f"<u>{html.escape(imp)}</u>" if imp else "(blank)"
        how_html = f"<u>{html.escape(how)}</u>" if how else "(blank)"
        parts.append(f"<b>In order to improve this:</b> {imp_html}.<br><b>Do this:</b> {how_html}.")
    if row["notes"].strip():
        parts.append(html.escape(row["notes"].strip()).replace(chr(10), "<br>"))
    return "<br><br>".join(parts)


def _build_resident_comments_df(resident_email: str) -> pd.DataFrame:
    """Attending-confirmed cases with a comment (improve/how/notes) for one
    resident, as a Date/Procedure/Attending/Comments/Comments_html table
    sorted newest first. Self-assessments are excluded — same as the
    heatmap, these are the resident's own unverified entry, not an
    attending-confirmed one. Empty DataFrame (with those columns) if there's
    nothing to show."""
    _cols = ["Date", "Procedure", "Attending", "Comments", "Comments_html"]
    cases_df = read_sheet_df(
        SHEET_CASES,
        expected_cols=["case_id", "resident_email", "date", "specialty_id",
                       "procedure_id", "attending_id", "notes",
                       "case_complexity", "overall_performance", "assessment_type",
                       "improve", "how"],
    )
    procs_df = read_sheet_df(SHEET_PROCEDURES, expected_cols=["procedure_id", "procedure_name", "specialty_id"])
    atnds_df = _read_attendings_df()

    cases_df["case_id"] = _norm_id(cases_df["case_id"])
    cases_df = cases_df.drop_duplicates(subset=["case_id"])

    res_cases = cases_df[cases_df["resident_email"] == resident_email].copy()
    res_cases = res_cases[res_cases["assessment_type"].fillna("").astype(str).str.strip() != "Self-Assessment"]
    res_cases["notes"]   = res_cases["notes"].fillna("").astype(str)
    res_cases["improve"] = res_cases["improve"].fillna("").astype(str)
    res_cases["how"]     = res_cases["how"].fillna("").astype(str)
    res_cases = res_cases[
        (res_cases["notes"].str.strip() != "")
        | (res_cases["improve"].str.strip() != "")
        | (res_cases["how"].str.strip() != "")
    ]
    if res_cases.empty:
        return pd.DataFrame(columns=_cols)

    res_cases["comments_html"] = res_cases.apply(_build_comments_html, axis=1)
    res_cases["notes"] = res_cases.apply(_build_comments, axis=1)

    atnds_lookup = dict(zip(atnds_df["attending_id"], atnds_df["attending_name"]))
    res_cases["attending_name"] = res_cases["attending_id"].apply(
        lambda aid: attending_display_name(str(aid), atnds_lookup)
    )

    procs_dedup = procs_df.drop_duplicates(subset=["procedure_id"])
    merged = res_cases.merge(procs_dedup[["procedure_id", "procedure_name"]], on="procedure_id", how="left")
    merged = merged.rename(columns={
        "date":           "Date",
        "procedure_name": "Procedure",
        "attending_name": "Attending",
        "notes":          "Comments",
        "comments_html":  "Comments_html",
    })
    merged["_date_sort"] = pd.to_datetime(merged["Date"], errors="coerce")
    merged = merged[_cols + ["_date_sort"]].sort_values("_date_sort", ascending=False).drop(columns=["_date_sort"])
    merged["Date"] = merged["Date"].apply(fmt_date)
    return merged


def _build_resident_evaluation_list(resident_email: str) -> pd.DataFrame:
    """One row per attending-confirmed case for one resident — every
    entry on the resident's "Complete Evaluation History" page, newest
    first. Self-assessments are excluded, same convention as the
    heatmap/Comments Dashboard (an unverified entry the resident wrote
    about themselves isn't "an evaluation" in that sense). Columns:
    case_id, Date, Procedure, Attending, _date_sort (drop the last
    before display — kept only for re-sorting after filtering)."""
    _cols = ["case_id", "Date", "Procedure", "Attending", "_date_sort"]
    cases_df = read_sheet_df(SHEET_CASES, expected_cols=_CASE_COLS)
    cases_df["case_id"] = _norm_id(cases_df["case_id"])
    cases_df = cases_df.drop_duplicates(subset=["case_id"])

    res_cases = cases_df[
        cases_df["resident_email"].astype(str).str.strip().str.lower()
        == str(resident_email).strip().lower()
    ].copy()
    res_cases = res_cases[res_cases["assessment_type"].fillna("").astype(str).str.strip() != "Self-Assessment"]
    if res_cases.empty:
        return pd.DataFrame(columns=_cols)

    atnds_lookup = dict(zip(_read_attendings_df()["attending_id"], _read_attendings_df()["attending_name"]))
    res_cases["Attending"] = res_cases["attending_id"].apply(
        lambda aid: attending_display_name(str(aid), atnds_lookup)
    )

    procs_df = read_sheet_df(SHEET_PROCEDURES, expected_cols=["procedure_id", "procedure_name", "specialty_id"])
    procs_dedup = procs_df.drop_duplicates(subset=["procedure_id"])
    merged = res_cases.merge(procs_dedup[["procedure_id", "procedure_name"]], on="procedure_id", how="left")
    merged["Procedure"] = merged["procedure_name"].fillna(merged["procedure_id"].astype(str))
    merged["_date_sort"] = pd.to_datetime(merged["date"], errors="coerce")
    merged["Date"] = merged["date"].apply(fmt_date)
    merged = merged.sort_values("_date_sort", ascending=False)
    return merged[_cols].reset_index(drop=True)


def _build_attending_evaluation_list(attending_id: str | None) -> pd.DataFrame:
    """One row per case this attending has evaluated (directly, or was
    present for via "Assessed Together") — every entry on the
    attending's own "Complete Evaluation History" page, across every
    resident, newest first. Same Self-Assessment exclusion and column
    shape as _build_resident_evaluation_list, but keyed by attending_id
    and returning Resident instead of Attending.

    attending_id=None skips that filter entirely, returning every
    confirmed evaluation in the system regardless of which attending
    performed it — backs that page's "Show all evaluations" toggle."""
    _cols = ["case_id", "Date", "Procedure", "Resident", "_date_sort"]
    cases_df = read_sheet_df(SHEET_CASES, expected_cols=_CASE_COLS)
    cases_df["case_id"] = _norm_id(cases_df["case_id"])
    cases_df = cases_df.drop_duplicates(subset=["case_id"])

    att_cases = cases_df if attending_id is None else cases_df[cases_df["attending_id"].astype(str) == str(attending_id)]
    att_cases = att_cases.copy()
    att_cases = att_cases[att_cases["assessment_type"].fillna("").astype(str).str.strip() != "Self-Assessment"]
    if att_cases.empty:
        return pd.DataFrame(columns=_cols)

    residents_df = read_sheet_df(SHEET_RESIDENTS, expected_cols=RESIDENT_COLS)
    res_lookup = dict(zip(
        residents_df["email"].astype(str).str.strip().str.lower(),
        residents_df["name"],
    ))
    att_cases["Resident"] = att_cases["resident_email"].astype(str).str.strip().str.lower().map(res_lookup)
    att_cases["Resident"] = att_cases["Resident"].fillna(att_cases["resident_email"])

    procs_df = read_sheet_df(SHEET_PROCEDURES, expected_cols=["procedure_id", "procedure_name", "specialty_id"])
    procs_dedup = procs_df.drop_duplicates(subset=["procedure_id"])
    merged = att_cases.merge(procs_dedup[["procedure_id", "procedure_name"]], on="procedure_id", how="left")
    merged["Procedure"] = merged["procedure_name"].fillna(merged["procedure_id"].astype(str))
    merged["_date_sort"] = pd.to_datetime(merged["date"], errors="coerce")
    merged["Date"] = merged["date"].apply(fmt_date)
    merged = merged.sort_values("_date_sort", ascending=False)
    return merged[_cols].reset_index(drop=True)


def _render_evaluation_history_list(
    df: pd.DataFrame,
    *,
    person_col: str,
    person_noun: str,
    preposition: str,
    session_prefix: str,
    return_page: str,
) -> None:
    """Shared body of the Complete Evaluation History page for both
    logins: procedure/[person] filters that narrow each other's options
    (same cross-narrowing as the Comments Dashboard's own Procedure/
    Attending filters), a date-range filter alongside them, a
    "{Procedure} — Evaluations {preposition} {person}"-style heading
    that updates with the filters (same design as the Comments
    Dashboard's own heading), and the filtered list itself as one
    clickable row per evaluation — each opening it on the "view one
    evaluation" page.

    `df` is the full, unfiltered list for this resident/attending, from
    _build_resident_evaluation_list / _build_attending_evaluation_list
    (case_id, Date, Procedure, [person_col], _date_sort). `person_col`
    is "Attending" or "Resident" — whichever the *other* party is from
    this viewer's own login; `preposition` reads naturally in front of
    it ("by {Attending}" vs. "for {Resident}"); `session_prefix` keeps
    each login's filter widgets in their own session_state namespace;
    `return_page` is where "view one evaluation"'s own Back button
    returns to."""
    _all_proc = "All Procedures"
    _all_person = f"All {person_noun}s"
    _proc_key = f"{session_prefix}_proc_filter"
    _person_key = f"{session_prefix}_person_filter"
    _date_key = f"{session_prefix}_date_range"
    # The date pickers' own keys carry a nonce, bumped by "Reset Date
    # Range" below — popping a date_input's session_state entry alone
    # (tried first) reset the value it returns, but not always the
    # visible text in the box itself: once a date's been typed directly
    # rather than picked from the calendar, the underlying BaseWeb
    # component tracks its own internal text separately from the value
    # Streamlit re-sends it, and doesn't always resync just because that
    # value changed. Giving it a brand new key instead forces Streamlit
    # to unmount and recreate the widget from scratch, which reliably
    # clears that stale internal state too — confirmed empirically (see
    # scratchpad/test_filter_order_reset.py).
    _date_nonce = st.session_state.get(f"{session_prefix}_date_nonce", 0)
    _start_key = f"{_date_key}_start_{_date_nonce}"
    _end_key = f"{_date_key}_end_{_date_nonce}"

    _min_date = df["_date_sort"].min()
    _max_date = df["_date_sort"].max()
    _min_date = _min_date.date() if pd.notna(_min_date) else datetime.date.today()
    _max_date = _max_date.date() if pd.notna(_max_date) else datetime.date.today()

    # Everything the heading needs is read from session_state before any
    # of this run's own widgets are instantiated below — Streamlit
    # already applies this rerun's trigger (e.g. just having picked a
    # new Start Date) to session_state before the script starts
    # executing, so the heading built from these reflects the current
    # picks immediately, in the same run, without waiting for a second
    # rerun.
    _proc_selected = st.session_state.get(_proc_key, _all_proc)
    _person_selected = st.session_state.get(_person_key, _all_person)
    _start_selected = st.session_state.get(_start_key, _min_date)
    _end_selected = st.session_state.get(_end_key, _max_date)
    # Swapped rather than filtered-to-nothing if Start ends up picked
    # later than End — whichever order the two were actually set in.
    _lo, _hi = min(_start_selected, _end_selected), max(_start_selected, _end_selected)
    _date_narrowed = _lo != _min_date or _hi != _max_date

    _proc_chosen = _proc_selected != _all_proc
    _person_chosen = _person_selected != _all_person
    if _proc_chosen and _person_chosen:
        _heading = f"{_proc_selected} — Evaluations {preposition} {_person_selected}"
    elif _proc_chosen:
        _heading = f"{_proc_selected} — All Evaluations"
    elif _person_chosen:
        _heading = f"All Evaluations {preposition} {_person_selected}"
    else:
        _heading = "All Evaluations"
    if _date_narrowed:
        _heading = f"{_heading} — {_lo.strftime('%m/%d/%Y')} - {_hi.strftime('%m/%d/%Y')}"
    # If this needs to wrap onto a second line (a long procedure/person
    # name, narrow screen), force the break to land right after one of
    # the segment separators, never before one and never mid-phrase:
    # every space and hyphen within each segment becomes its non-
    # breaking counterpart (_protect_from_wrapping) — including the
    # plain "-" between a narrowed date range's two dates, so that
    # stays intact as one unit too — and the space right before each
    # dash is glued to it with a non-breaking space, leaving only the
    # regular space right after each dash as an actual wrap point.
    _heading_wrapped = " — ".join(_protect_from_wrapping(_seg) for _seg in _heading.split(" — "))
    st.markdown(f"### 📜 {_heading_wrapped}")

    # Each dropdown's options are narrowed by the *other* dropdown's
    # current selection — same behavior as the Comments Dashboard's
    # Procedure/Attending filters.
    _proc_pool = df if _person_selected == _all_person else df[df[person_col] == _person_selected]
    _proc_opts = [_all_proc] + sorted(_proc_pool["Procedure"].dropna().unique().tolist())
    _person_pool = df if _proc_selected == _all_proc else df[df["Procedure"] == _proc_selected]
    _person_opts = [_all_person] + sorted(
        _person_pool[person_col].dropna().unique().tolist(),
        key=lambda n: n.split()[-1] if n.split() else n,
    )
    # A previously-selected filter value can fall out of the newly
    # narrowed options (because the other filter now excludes it) —
    # reset it before the widget renders, rather than letting
    # st.selectbox raise on a default no longer in its options.
    if _proc_selected not in _proc_opts:
        st.session_state[_proc_key] = _all_proc
    if _person_selected not in _person_opts:
        st.session_state[_person_key] = _all_person

    # Back on one row, per feedback that the full extra row was too much —
    # Procedure gets a bit more room than [person_noun] (a title runs
    # longer than a name), and gap="small" alone reclaims real width
    # from Streamlit's default inter-column spacing on top of that.
    # Verified both together (ratio + smaller gap) against the previous
    # single-row layout at 1024px and 1280px: Procedure gains ~19-31px
    # (~2.5-4 characters) with every other field still safely fitting
    # its content with no wrap/truncation — the most this row can give
    # Procedure without something else in it breaking.
    _filter_col1, _filter_col2, _filter_col3, _filter_col4, _filter_col5 = st.columns(
        [1.15, 1.35, 0.85, 0.85, 1.35], vertical_alignment="bottom", gap="small"
    )
    with _filter_col1:
        _person_filter = st.selectbox(f"Filter by {person_noun}", _person_opts, key=_person_key)
    with _filter_col2:
        _proc_filter = st.selectbox("Filter by Procedure", _proc_opts, key=_proc_key)
    with _filter_col3:
        _start_date = st.date_input(
            "Start Date", value=_min_date,
            min_value=_min_date, max_value=_max_date, key=_start_key,
            format="MM/DD/YYYY",
        )
    with _filter_col4:
        _end_date = st.date_input(
            "End Date", value=_max_date,
            min_value=_min_date, max_value=_max_date, key=_end_key,
            format="MM/DD/YYYY",
        )
    with _filter_col5:
        if st.button("🔄 Reset Date Range", key=f"{session_prefix}_reset_dates"):
            st.session_state[f"{session_prefix}_date_nonce"] = _date_nonce + 1
            st.rerun()

    filtered = df
    if _proc_filter != _all_proc:
        filtered = filtered[filtered["Procedure"] == _proc_filter]
    if _person_filter != _all_person:
        filtered = filtered[filtered[person_col] == _person_filter]
    filtered = filtered[
        (filtered["_date_sort"].dt.date >= _lo) & (filtered["_date_sort"].dt.date <= _hi)
    ]

    if filtered.empty:
        st.info("No evaluations match these filters.")
        return

    for _, _row in filtered.iterrows():
        _label = f"📄 {_row['Procedure']} — {_row[person_col]} ({_row['Date']})"
        if st.button(_label, key=f"{session_prefix}_row_{_row['case_id']}", width="stretch"):
            st.session_state["viewing_case_id"] = _row["case_id"]
            st.session_state["viewing_case_return_page"] = return_page
            go_to("view_evaluation")


def _render_comments_html_table(merged: pd.DataFrame, show_proc: bool, show_att: bool) -> None:
    """Render a Date/[Procedure]/[Attending]/Comments table as wrapped HTML
    (so the Comments column can wrap), with the same shrink-to-fit script
    the Comments Dashboard uses."""
    st.markdown("""
<style>
.comments-tbl {width:100%;border-collapse:collapse;font-size:0.88rem;}
.comments-tbl th {background:var(--secondary-background-color);padding:8px 10px;
    text-align:left;border-bottom:2px solid #ccc;font-weight:600;}
.comments-tbl td {padding:8px 10px;vertical-align:top;border-bottom:1px solid var(--secondary-background-color);}
.comments-tbl td.date-col, .comments-tbl td.attending-col, .comments-tbl td.procedure-col {
    white-space:nowrap;font-size:var(--cmts-sync-font, inherit);
}
.comments-tbl td.comments-col {white-space:pre-wrap;word-break:break-word;min-width:260px;}
</style>""", unsafe_allow_html=True)

    _rows_html = ""
    for _, r in merged.reset_index(drop=True).iterrows():
        _rows_html += (
            f"<tr>"
            f"<td class='date-col'>{html.escape(str(r['Date']))}</td>"
            + (f"<td class='procedure-col'>{html.escape(str(r['Procedure']))}</td>" if show_proc else "")
            + (f"<td class='attending-col'>{html.escape(str(r['Attending']))}</td>" if show_att else "")
            + f"<td class='comments-col'>{r['Comments_html']}</td>"
            f"</tr>"
        )
    _header_html = (
        "<th>Date</th>"
        + ("<th>Procedure</th>" if show_proc else "")
        + ("<th>Attending</th>" if show_att else "")
        + "<th>Comments</th>"
    )
    st.markdown(
        "<table class='comments-tbl'>"
        f"<thead><tr>{_header_html}</tr></thead>"
        f"<tbody>{_rows_html}</tbody></table>",
        unsafe_allow_html=True,
    )
    # Date, Procedure, and Attending default to the table's normal font
    # size (the CSS above just falls back to `inherit`) and Comments
    # absorbs the squeeze down to its own min-width first. Only if that
    # still isn't enough room does this shrink Date/Procedure/Attending
    # — together, to the same size as each other via one shared CSS
    # var — just enough to fit without wrapping.
    st.iframe(
        """
        <script>
        (function() {
            var doc = window.parent.document;
            var tables = doc.querySelectorAll('.comments-tbl');
            var table = tables[tables.length - 1];
            if (!table) return;
            function fit() {
                table.style.removeProperty('--cmts-sync-font');
                var container = table.parentElement;
                if (!container) return;
                var containerWidth = container.clientWidth;
                if (!containerWidth) return;
                var natural = table.scrollWidth;
                if (natural <= containerWidth) return;
                var dateCells = table.querySelectorAll('td.date-col');
                var procCells = table.querySelectorAll('td.procedure-col');
                var attCells = table.querySelectorAll('td.attending-col');
                if (!dateCells.length) return;
                function maxWidth(cells) {
                    var m = 0;
                    cells.forEach(function(c) { m = Math.max(m, c.scrollWidth); });
                    return m;
                }
                var threeW = maxWidth(dateCells) + maxWidth(procCells) + maxWidth(attCells);
                if (threeW <= 0) return;
                var otherW = natural - threeW;
                var availableForThree = containerWidth - otherW;
                var baseSize = parseFloat(window.getComputedStyle(dateCells[0]).fontSize);
                var ratio = Math.min(1, availableForThree / threeW) * 0.98;
                var newSize = Math.max(baseSize * ratio, 9);
                table.style.setProperty('--cmts-sync-font', newSize + 'px');
            }
            fit();
            window.parent.addEventListener('resize', fit);
            if (window.parent.ResizeObserver) {
                new window.parent.ResizeObserver(fit).observe(table.parentElement);
            }
        })();
        </script>
        """,
        height=1,
    )


def _build_resident_case_matrix(resident_email: str):
    """Every attending-confirmed case for one resident, joined down to
    per-step rating rows (self-assessments and steps left "Not Assessed"
    excluded, same as the resident's own Cumulative Dashboard).

    Returns (merged, steps_df, procs_map); `merged` is empty when there's
    no case or no meaningful rating yet — callers should treat that as
    "nothing to show" rather than distinguishing the two."""
    cases_df  = read_sheet_df(SHEET_CASES,  expected_cols=["case_id", "resident_email", "date",
                                                             "specialty_id", "procedure_id",
                                                             "attending_id", "notes",
                                                             "case_complexity", "overall_performance",
                                                             "case_preparation", "assessment_type"])
    scores_df = read_sheet_df(SHEET_SCORES, expected_cols=["case_id", "step_id", "rating", "rating_num",
                                                             "case_complexity", "overall_performance"])
    steps_df  = read_sheet_df(SHEET_STEPS,  expected_cols=["step_id", "procedure_id", "step_order", "step_name"])
    procs_df  = read_sheet_df(SHEET_PROCEDURES, expected_cols=["procedure_id", "procedure_name", "specialty_id"])
    atnds_df  = _read_attendings_df()

    def _clean_id(val) -> str:
        s = str(val).strip()
        return s[:-2] if s.endswith(".0") else s

    atnds_lookup = {
        str(r.get("attending_id", "")): str(r.get("attending_name", ""))
        for _, r in atnds_df.iterrows()
    }
    procs_map = {
        str(r.get("procedure_id", "")): str(r.get("procedure_name", ""))
        for _, r in procs_df.iterrows()
    }

    resident_cases: dict = {}
    for _, row in cases_df.iterrows():
        if str(row.get("resident_email", "")).strip() != str(resident_email).strip():
            continue
        if str(row.get("assessment_type", "")).strip() == "Self-Assessment":
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
            "case_preparation":    row.get("case_preparation"),
        }

    if not resident_cases:
        return pd.DataFrame(), steps_df, procs_map

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

    seen_case_step: set = set()
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

    _meaningful_case_ids = {r["case_id"] for r in merged_rows if r["rating"] != "Not Assessed"}
    merged_rows = [r for r in merged_rows if r["case_id"] in _meaningful_case_ids]

    if not merged_rows:
        return pd.DataFrame(), steps_df, procs_map

    merged = pd.DataFrame(merged_rows)
    if "case_procedure_id" not in merged.columns:
        merged["case_procedure_id"] = ""
    return merged, steps_df, procs_map


def _render_resident_heatmap(merged: pd.DataFrame, steps_df: pd.DataFrame, procs_map: dict,
                              selected_proc: str, filename_stub: str,
                              heading_suffix: str = "Progress Heatmap",
                              show_heading: bool = True) -> None:
    """Render the progress heatmap + legends for one resident's one
    procedure. `merged` is the resident's full case matrix from
    _build_resident_case_matrix (not yet filtered to a procedure) —
    this filters it to `selected_proc` itself.

    `heading_suffix` follows the procedure name in the section heading
    ("{procedure} — {heading_suffix}") — the attending's Resident
    Dashboard overrides it to "Progress Heatmap and Comments" since
    that page's Comments section right below no longer has a heading
    of its own once a procedure is chosen.

    `show_heading=False` skips that "{procedure} — {heading_suffix}"
    line entirely (the "Most recent cases at the top." caption still
    shows) — the Cumulative Dashboard uses this once a procedure is
    chosen, since by then its own page-level header already shows that
    exact text; showing it a second time right here would be a plain
    duplicate."""
    proc_data = merged[merged["case_procedure_id"] == selected_proc].copy()
    if proc_data.empty:
        st.info("No assessment data yet for this procedure.")
        return

    # pivot_table (below) uses this as one of its index/group-by keys —
    # pandas silently drops any row whose group-by key is NaN, which
    # would otherwise erase a whole older case (predating this field, or
    # simply never touched) from the entire heatmap, not just leave its
    # Daily Preparation column blank.
    if "case_preparation" not in proc_data.columns:
        proc_data["case_preparation"] = "Not Assessed"
    else:
        proc_data["case_preparation"] = proc_data["case_preparation"].fillna("Not Assessed")

    ordered_steps = (
        steps_df[steps_df["procedure_id"] == selected_proc]
        .sort_values("step_order")["step_name"]
        .tolist()
    )

    def _fmt_step_hdr(name):
        """Column label shown above the heatmap: any "(...)" parenthetical
        is dropped, e.g. "Suture Placement (interrupted vs. running)"
        displays as just "Suture Placement". The full name is still used
        everywhere else (pivot table columns, dict keys) — this only
        changes what's shown in the header."""
        if not isinstance(name, str):
            return name
        return re.sub(r"\s*\([^)]*\)", "", name).strip()

    # If two steps in this procedure differ only by their "(...)"
    # parenthetical (e.g. "Knot Tying (left hand)" / "(right hand)"),
    # stripping it from both would give them the same header — and this
    # heatmap's own display_df further down uses these headers as real
    # column names, so a collision means duplicate columns, which
    # crashes the Styler ("...not compatible with non-unique index or
    # columns") the moment it's rendered. Falls back to each colliding
    # step's full, still-unique name instead of the shortened one.
    _step_display: dict = {}
    _display_groups: dict = {}
    for _s in ordered_steps:
        _display_groups.setdefault(_fmt_step_hdr(_s), []).append(_s)
    for _disp, _members in _display_groups.items():
        if len(_members) == 1:
            _step_display[_members[0]] = _disp
        else:
            for _m in _members:
                _step_display[_m] = _m
    ordered_steps_display = [_step_display[s] for s in ordered_steps]

    def _is_na(val) -> bool:
        # pd.isna() must be checked (and short-circuit) before anything
        # that compares val itself, e.g. val == "" — pandas' NA sentinel
        # (distinct from plain NaN/None, and increasingly what actually
        # shows up here) returns pd.NA rather than True/False from such a
        # comparison, and Python raises "boolean value of NA is
        # ambiguous" the moment something tries to use that as a bool
        # (here, `or`'s implicit truth test).
        try:
            if pd.isna(val):
                return True
        except (TypeError, ValueError):
            pass
        return isinstance(val, str) and val.strip() == ""

    # "Not Done" isn't one of RATING_OPTIONS/RATING_HEX — it doesn't
    # appear anywhere in this file — but shows up as raw data in some
    # older score rows (predating the current rating labels). Treated as
    # a synonym for "Not Assessed" everywhere below (both here, when
    # falling back to a "Case Preparation" step and picking "Most
    # Recent", and further down for "Never Attempted" coloring) via the
    # shared _is_unrated() rather than each place re-deriving its own
    # notion of "unrated" and risking drifting out of sync with each
    # other again.
    _UNRATED_VALUES = ("Not Assessed", "Not Done")

    def _is_unrated(val) -> bool:
        """Blank/NaN or an explicit "Not Assessed"/"Not Done" — a cell
        with nothing meaningfully observed."""
        return _is_na(val) or (isinstance(val, str) and val.strip() in _UNRATED_VALUES)

    pivot = proc_data.pivot_table(
        index=["date", "attending_name", "case_id", "overall_performance", "case_complexity", "case_preparation"],
        columns="step_name",
        values="rating",
        aggfunc="first",
    ).reset_index()

    for step in ordered_steps:
        if step not in pivot.columns:
            pivot[step] = pd.NA

    pivot = pivot[["date", "attending_name", "case_id", "overall_performance", "case_complexity", "case_preparation"] + ordered_steps]

    # Some procedures still have a step literally named "Case
    # Preparation" — a legacy per-step rating from before Daily
    # Preparation existed as its own case-level field. Fall back to it,
    # mapped onto the same 5-level Prep scale, for any case where Daily
    # Preparation itself was left blank/"Not Assessed" — "Shown/Told"
    # doesn't map to anything and is deliberately left as a non-fallback
    # (same as _is_real_progress treats it elsewhere: shown/told isn't
    # the resident actually demonstrating it). Matched case-insensitively
    # since the exact casing isn't guaranteed in older data. Only ever
    # applies to real case rows — pivot_sorted (built from this pivot)
    # is what the two summary rows below are concatenated onto top of,
    # and they always report pd.NA for this column regardless.
    _STEP_RATING_TO_PREP_LABEL = {
        "Not Yet":  "Unprepared",
        "Steer":    "Poorly Prepared",
        "Prompt":   "Adequately Prepared",
        "Back up":  "Well Prepared",
        "Auto":     "Highly Prepared",
    }
    _case_prep_step = next(
        (s for s in ordered_steps if s.strip().lower() == "case preparation"), None
    )
    if _case_prep_step:
        def _effective_prep(row):
            cp = row["case_preparation"]
            if _is_unrated(cp):
                mapped = _STEP_RATING_TO_PREP_LABEL.get(row[_case_prep_step])
                if mapped:
                    return mapped
            return cp
        pivot["case_preparation"] = pivot.apply(_effective_prep, axis=1)
        # Already folded into Daily Preparation above — don't also show it
        # as its own step column (on-screen or in the Excel export, both
        # built from this same `pivot`).
        pivot = pivot.drop(columns=[_case_prep_step])
        ordered_steps = [s for s in ordered_steps if s != _case_prep_step]
        ordered_steps_display = [_step_display[s] for s in ordered_steps]

    proc_display_name = procs_map.get(selected_proc, selected_proc)
    # One deliberate break point (see header_break_before): stays on one
    # line when it fits, and if it doesn't, wraps with "Progress Heatmap"
    # intact on the second line rather than splitting the procedure name
    # or "Progress"/"Heatmap" from each other.
    _heatmap_heading = header_break_before(f"{proc_display_name} —", heading_suffix)
    with st.container(key="heatmap_heading_row"):
        if show_heading:
            st.markdown(f"### {_heatmap_heading}\nMost recent cases at the top.")
        else:
            st.markdown("Most recent cases at the top.")

    pivot_sorted = pivot.sort_values("date", ascending=False)

    _mr = {"date": "", "attending_name": "📌 Most Recent", "case_complexity": pd.NA,
           "overall_performance": pd.NA, "case_preparation": pd.NA}
    for _s in ordered_steps:
        _vals = pivot_sorted[_s]
        _vals = _vals[~_vals.apply(_is_unrated)]
        _mr[_s] = _vals.iloc[0] if not _vals.empty else pd.NA

    _best = {"date": "", "attending_name": "🏆 Best", "case_complexity": pd.NA,
             "overall_performance": pd.NA, "case_preparation": pd.NA}
    for _s in ordered_steps:
        _vals = pivot_sorted[_s]
        _vals = _vals[~_vals.apply(_is_unrated)]
        if _vals.empty:
            _best[_s] = pd.NA
        else:
            _best[_s] = max(_vals.tolist(), key=lambda v: RATING_TO_NUM.get(v, -1))

    _summary_df = pd.DataFrame([_mr, _best])
    # Daily Preparation is the third meta column after Overall
    # Performance/Case Complexity — never reported for the two summary
    # rows above (pd.NA, same as the other two), only per real case.
    _meta_cols  = ["date", "attending_name", "overall_performance", "case_complexity", "case_preparation"]

    display_df = pd.concat(
        [_summary_df[_meta_cols + ordered_steps],
         pivot_sorted.drop(columns=["case_id"])[_meta_cols + ordered_steps]],
        ignore_index=True,
    )

    display_df["date"] = display_df["date"].apply(fmt_date)

    display_df = display_df.rename(columns={
        "date":                "Date",
        "attending_name":      "Attending",
        "case_complexity":     "Case Complexity",
        "overall_performance": "Overall Performance",
        "case_preparation":    "Daily Preparation",
        **_step_display,
    })
    all_cols = list(display_df.columns)

    display_df["Date"]      = display_df["Date"].fillna("")
    display_df["Attending"] = display_df["Attending"].fillna("")

    _rating_cols = [c for c in ordered_steps_display + ["Case Complexity", "Overall Performance", "Daily Preparation"]
                    if c in display_df.columns]

    _orig_vals = {}
    for col in _rating_cols:
        _v = display_df[col].copy()
        if isinstance(_v, pd.DataFrame):
            _v = _v.iloc[:, 0]
        _orig_vals[col] = _v.reindex(display_df.index)

    for _c in _rating_cols:
        display_df[_c] = " "

    # The first two display rows are always the "📌 Most Recent"/"🏆 Best"
    # summary rows; real case rows (in pivot_sorted's newest-first order)
    # start right after them.
    _N_SUMMARY_ROWS = 2

    def _is_real_progress(val) -> bool:
        """A rating that actually demonstrates something — everything
        except unrated cells and "Shown/Told" (shown/told doesn't count
        as the resident having attempted the step themselves)."""
        return not _is_unrated(val) and not (isinstance(val, str) and val.strip() == "Shown/Told")

    def _never_attempted_positions(col_values) -> set:
        """Case-row positions (within col_values, which starts with the
        two summary rows) where an unrated cell has no real rating on
        any OLDER case for this step. col_values' case rows are already
        newest-first (pivot_sorted's own order), so "older" means later
        positions — walk oldest-to-newest (i.e. backwards) tracking
        whether a real rating has been seen among the rows already
        walked, which are exactly the ones older than whichever row
        comes next."""
        never = set()
        seen_real = False
        for pos in range(len(col_values) - 1, _N_SUMMARY_ROWS - 1, -1):
            val = col_values.iloc[pos]
            if _is_unrated(val) and not seen_real:
                never.add(pos)
            if _is_real_progress(val):
                seen_real = True
        return never

    def _color_step(val, never_attempted: bool = False):
        # A blank/NaN cell (no score row at all for this step on this
        # case) reads identically to an explicit "Not Assessed" rating —
        # same very light gray either way, since neither means anything
        # was actually observed. Unless no older case has a real rating
        # for this step either, in which case it reads as "Never
        # Attempted" instead (a distinct, lighter gray) — see
        # _never_attempted_positions.
        if _is_unrated(val):
            if never_attempted:
                return f"background-color: #FAFAFA; background-image: {NEVER_ATTEMPTED_STRIPE_CSS};"
            return f"background-color: {RATING_HEX['Not Assessed']}"
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

    def _color_prep(val):
        # "Not Assessed" isn't in PREP_HEX (deliberately, same as Case
        # Complexity's own placeholder falling through .get(..., '')) so
        # it renders with no color, same as an unset selection.
        if not isinstance(val, str) or val == "":
            return ""
        return f"background-color: {PREP_HEX.get(val, '')}"

    try:
        styled = display_df.style

        if ordered_steps_display:
            _safe_step_cols = [c for c in ordered_steps_display if c in _orig_vals]

            def _apply_step_colors(col):
                vals = _orig_vals[col.name]
                never_positions = _never_attempted_positions(vals)
                # If every case row for this step came out "Never
                # Attempted" (the resident has no real rating for it on
                # any case at all), the two summary rows — which
                # otherwise just show blank/"Not Assessed" once there's
                # nothing real to summarize — match that too, instead of
                # looking like an ordinary unrated cell.
                _total_case_rows = len(vals) - _N_SUMMARY_ROWS
                if _total_case_rows > 0 and len(never_positions) == _total_case_rows:
                    never_positions = never_positions | set(range(_N_SUMMARY_ROWS))
                return [
                    _color_step(v, never_attempted=(i in never_positions))
                    for i, v in enumerate(vals)
                ]

            if _safe_step_cols:
                styled = styled.apply(_apply_step_colors, subset=_safe_step_cols, axis=0)

        def _apply_complexity_colors(col):
            return [_color_complexity(v) for v in _orig_vals["Case Complexity"]]

        def _apply_o_score_colors(col):
            return [_color_o_score(v) for v in _orig_vals["Overall Performance"]]

        def _apply_prep_colors(col):
            return [_color_prep(v) for v in _orig_vals["Daily Preparation"]]

        # Every colored cell (step ratings, Case Complexity, Overall
        # Performance, Daily Preparation — all blanked to a single space,
        # the color is the only content) has a fixed width/height
        # independent of this table's actual rendered row height (a
        # table cell's height is only ever a minimum, and other cells in
        # the same row can still push it taller), tuned by eye. Case
        # Complexity/Overall Performance/Daily Preparation share one,
        # wider column width than the step cells.
        _COLOR_CELL_HEIGHT_PX = 20
        _STEP_CELL_WIDTH_PX = 25
        _META_CELL_WIDTH_PX = 30
        _META_COL_NAMES = ("Case Complexity", "Overall Performance", "Daily Preparation")

        def _color_cell_props(width_px):
            return {
                "width": f"{width_px}px", "min-width": f"{width_px}px", "max-width": f"{width_px}px",
                "height": f"{_COLOR_CELL_HEIGHT_PX}px", "min-height": f"{_COLOR_CELL_HEIGHT_PX}px",
                "max-height": f"{_COLOR_CELL_HEIGHT_PX}px",
                "text-align": "center", "box-sizing": "border-box", "line-height": "1",
            }

        _STEP_CELL_PROPS = _color_cell_props(_STEP_CELL_WIDTH_PX)
        _META_CELL_PROPS = _color_cell_props(_META_CELL_WIDTH_PX)
        styled = (
            styled
            .apply(_apply_complexity_colors, subset=["Case Complexity"], axis=0)
            .apply(_apply_o_score_colors,    subset=["Overall Performance"], axis=0)
            .apply(_apply_prep_colors,       subset=["Daily Preparation"], axis=0)
            .hide(axis="index")
            .set_properties(
                subset=["Attending"],
                # font-size/line-height set directly here (inline, same as
                # the colored cells' own props below) rather than relying
                # on the table-wide "th, td" rule to reach it — Date/
                # Attending's text was still forcing the row taller than
                # the colored cells' own 25px square. set_properties()
                # only ever targets the <td> data cells (Styler gives
                # each one its own #T_..._rowR_colC id) — the <th>
                # header keeps its own separate centered styling from
                # th.col_heading below untouched.
                **{"min-width": "120px", "white-space": "nowrap", "text-align": "right",
                   "font-size": "0.7rem", "line-height": "1"},
            )
            .set_properties(
                subset=["Date"],
                # fmt_date()'s output is always exactly 10 characters
                # (MM-DD-YYYY) — fixed rather than Attending's own
                # min-width (sized for a name), with a little buffer
                # either side rather than sized to the exact pixel.
                **{"width": "70px", "min-width": "70px", "max-width": "70px",
                   "white-space": "nowrap", "font-size": "0.7rem", "line-height": "1"},
            )
            .set_properties(
                subset=list(_META_COL_NAMES),
                **_META_CELL_PROPS,
            )
        )
        if ordered_steps_display:
            styled = styled.set_properties(
                subset=ordered_steps_display,
                **_STEP_CELL_PROPS,
            )

        table_styles = [
            {"selector": "table",       "props": [("border-collapse", "collapse"), ("margin", "0 auto"),
                                                   ("border", "2px solid #555")]},
            {"selector": "th, td",      "props": [("border", "1px solid #bbb"),
                                                   ("padding", "4px"), ("font-size", "0.8rem"),
                                                   ("line-height", "1")]},
            {"selector": "th.col_heading", "props": [("text-align", "center"), ("vertical-align", "bottom"),
                                                       ("font-weight", "600")]},
            {"selector": "thead tr:last-child th", "props": [("border-bottom", "2px solid #555")]},
            {"selector": "tbody tr", "props": [("border-bottom", "1px solid #bbb")]},
            {"selector": "tbody tr:nth-child(1)", "props": [("border-bottom", "2px solid #555")]},
            {"selector": "tbody tr:nth-child(2)", "props": [("border-bottom", "2px solid #555")]},
            # Bold bottom edge, same as the left/right ones below —
            # :last-child always resolves to whichever row actually ends
            # up last regardless of case count.
            {"selector": "tbody tr:last-child td", "props": [("border-bottom", "2px solid #555")]},
        ]
        # Bold divider between Attending and Overall Performance — only
        # from the fourth row down (tbody's 3rd child on): the header is
        # row 1, and tbody rows 1-2 are the merged Most Recent/Best
        # summary cells, which have no separate Attending/Overall
        # Performance cells to put a border between at all (that whole
        # span is one cell). :nth-child(n+3) selects tbody row 3
        # onward — the first real case and every one after it.
        _attending_idx = all_cols.index("Attending")
        table_styles.append({
            "selector": f"tbody tr:nth-child(n+3) td.col{_attending_idx}",
            "props": [("border-right", "2px solid #555")],
        })
        if "Daily Preparation" in all_cols and ordered_steps_display:
            # Bold divider between the meta columns and the actual step
            # columns — same weight as the table's other bold rules
            # (outer border, header/summary-row separators). Starts from
            # the second row down: td.col{idx} only ever matches <td>
            # body cells, never the <th> header cell, so the header row
            # itself is left with its normal (thin) border. The merged
            # Most Recent/Best summary cells get the same bold edge via
            # _merge_summary_label_cells' own extra_style below, since
            # their Daily Preparation <td> no longer exists separately
            # to be matched by this selector.
            _daily_prep_idx = all_cols.index("Daily Preparation")
            table_styles.append(
                {"selector": f"td.col{_daily_prep_idx}", "props": [("border-right", "2px solid #555")]}
            )
        # Bold the table's own left/right outer edges too, starting from
        # the second row down (same td.col{idx}-only trick — never
        # matches the <th> header). The rightmost column (always a real,
        # non-merged cell even on the two summary rows) gets this
        # automatically on every body row; the leftmost (Date) doesn't
        # exist as its own cell on the merged summary rows, so those two
        # get the matching left border added directly to the merged
        # cell's own style below instead.
        table_styles.append({"selector": "td.col0", "props": [("border-left", "2px solid #555")]})
        table_styles.append({
            "selector": f"td.col{len(all_cols) - 1}",
            "props": [("border-right", "2px solid #555")],
        })
        _vheader_cols  = [c for c in all_cols
                           if c in ordered_steps_display or c in _META_COL_NAMES]

        # Rotated column headers: forced to one line (no wrapping, no
        # shrink-to-fit) with no cap on the header row's height — a long
        # label just makes that row taller instead of wrapping or
        # shrinking. Revisit if very long labels end up making the
        # header row uncomfortably tall.
        for idx, col_name in enumerate(all_cols):
            if col_name in _vheader_cols:
                _hdr_width_px = (
                    _META_CELL_WIDTH_PX if col_name in _META_COL_NAMES
                    else _STEP_CELL_WIDTH_PX
                )
                table_styles.append({
                    "selector": f"th.col_heading.level0.col{idx}",
                    "props": [
                        ("writing-mode", "vertical-rl"),
                        ("transform", "rotate(180deg)"),
                        ("vertical-align", "bottom"),
                        ("text-align", "left"),
                        ("padding", "4px 2px"),
                        ("width", f"{_hdr_width_px}px"),
                        ("min-width", f"{_hdr_width_px}px"),
                        ("max-width", f"{_hdr_width_px}px"),
                        ("white-space", "nowrap"),
                        ("font-size", "0.75rem"),
                    ],
                })

        table_styles.append({
            "selector": "th.col_heading .pp-vhdr-inner",
            "props": [
                ("display", "flex"),
                ("align-items", "center"),
                ("justify-content", "flex-start"),
                ("width", "100%"),
                ("height", "100%"),
            ],
        })

        def _merge_summary_label_cells(html_str, n_summary_rows, first_col, last_col, label_col, extra_style=""):
            """Merges the <td> cells from first_col..last_col (0-indexed,
            inclusive) into one, in each of the first n_summary_rows
            <tbody> rows — used to let the "📌 Most Recent"/"🏆 Best"
            label push right up against Case Complexity instead of being
            boxed into Attending's own (narrower) column. label_col's own
            cell (which carries the actual label text) survives with a
            colspan added; the other cells in the range are dropped
            outright. pandas Styler gives every cell a unique
            id="..._rowR_colC", so each is matched and removed/modified
            independently regardless of the others — order doesn't
            matter, and nothing outside the targeted cells is touched."""
            span = last_col - first_col + 1
            for row in range(n_summary_rows):
                for col in range(first_col, last_col + 1):
                    if col == label_col:
                        continue
                    html_str = re.sub(
                        rf'<td id="[^"]*_row{row}_col{col}"[^>]*>.*?</td>\s*',
                        "",
                        html_str, count=1, flags=re.DOTALL,
                    )
                html_str = re.sub(
                    rf'(<td id="[^"]*_row{row}_col{label_col}"[^>]*)(>)',
                    rf'\1 colspan="{span}" style="text-align:right;font-weight:600;padding-right:6px;{extra_style}"\2',
                    html_str, count=1,
                )
            return html_str

        def _wrap_vheader_labels(html_str, vheader_indices):
            if not vheader_indices:
                return html_str
            _col_idx_re = re.compile(r"\bcol(\d+)\b")

            def _wrap(m):
                open_tag, inner, close_tag = m.group(1), m.group(2), m.group(3)
                if "col_heading" not in open_tag:
                    return m.group(0)
                idx_match = _col_idx_re.search(open_tag)
                if not idx_match or int(idx_match.group(1)) not in vheader_indices:
                    return m.group(0)
                return f'{open_tag}<div class="pp-vhdr-inner">{inner}</div>{close_tag}'

            return re.sub(r"(<th\b[^>]*>)(.*?)(</th>)", _wrap, html_str, flags=re.DOTALL)

        styled = styled.set_table_styles(table_styles)
        _vheader_idx = {idx for idx, c in enumerate(all_cols) if c in _vheader_cols}
        _heatmap_html = styled.to_html()
        _heatmap_html = _merge_summary_label_cells(
            _heatmap_html, n_summary_rows=2,
            first_col=all_cols.index("Date"), last_col=all_cols.index("Daily Preparation"),
            label_col=all_cols.index("Attending"),
            # border-right matches the same bold meta/steps divider added
            # below for the real case rows — this merged cell's own right
            # edge is that same boundary (it ends exactly at Daily
            # Preparation). border-left matches the table's own bold left
            # edge, also added below for real case rows via td.col0 —
            # this merged cell starts at the table's left edge (Date's
            # original position) but isn't itself class="col0" (that
            # cell was dropped in the merge), so it needs its own edge
            # set directly instead of picking up the td.col0 rule.
            extra_style=(
                "border-left:2px solid #555;"
                + ("border-right:2px solid #555;" if ordered_steps_display else "")
            ),
        )
        _heatmap_html = _wrap_vheader_labels(_heatmap_html, _vheader_idx)
        st.markdown(_heatmap_html, unsafe_allow_html=True)

    except Exception as _heatmap_err:
        st.warning(
            f"⚠️ Could not render the heatmap for this procedure: {_heatmap_err}\n\n"
            "Please try a different procedure, or contact your program coordinator."
        )

    render_rating_legend(key="rating_legend_dashboard")

    render_complexity_legend(key="complexity_legend_dashboard")

    render_prep_legend(key="prep_legend_dashboard")


# ─────────────────────────────────────────────
# PAGE HEADER HELPER
# ─────────────────────────────────────────────
def _header_max(text: str) -> float:
    """Font-size ceiling (rem) for a header, tuned to text length. There is
    deliberately no floor — on skinny windows the font should keep shrinking
    (down to whatever -webkit-line-clamp: 2 and ellipsis allow) rather than
    being held at a minimum size and forced to wrap or truncate."""
    length = len(text)
    if length <= 20:
        return 2.75
    elif length <= 35:
        return 2.3
    elif length <= 55:
        return 1.9
    else:
        return 1.6


def _protect_from_wrapping(text: str) -> str:
    """Replace every space and hyphen with its non-breaking Unicode
    counterpart, so nothing inside `text` offers the browser a place
    to wrap — a plain hyphen is a valid line-break opportunity on its
    own by default (independent of spaces), which let a procedure name
    like "Robotic-Assisted ..." break there instead of only at a
    forced-break helper's designated point."""
    return text.replace(" ", " ").replace("-", "‑")


def header_break_before(prefix: str, suffix: str) -> str:
    """Join "{prefix} {suffix}", protecting every space and hyphen in
    each part from wrapping except the single regular space between
    them — so if page_header()'s mobile 2-line allowance (see its own
    fit script) ever does need to wrap this header, the break can only
    land right at that boundary, never mid-word within either part."""
    return f"{_protect_from_wrapping(prefix)} {_protect_from_wrapping(suffix)}"


def suppress_picker_keyboards() -> None:
    """Set inputmode="none" on every st.selectbox and st.date_input
    input on the currently rendered page, so tapping one to open its
    dropdown/calendar doesn't also pop up the on-screen keyboard on
    mobile — they're pick-from-a-list-or-calendar controls, not
    free-text fields anyone needs to type into. Global (not scoped to
    specific widget keys) so it covers every such widget on every page,
    including ones added later, without each needing its own key and
    call site. A MutationObserver re-applies it whenever Streamlit
    re-renders an input (e.g. after a selection), since a plain
    one-time pass would only catch whatever's in the DOM at that
    instant. Called once, after every page render (see the bottom of
    this file), same as fit_all_button_labels().

    The date input also gets readonly: inputmode="none" alone wasn't
    enough to stop it opening the keyboard — confirmed it renders as a
    plain type="text" input, where mobile browser support for
    respecting inputmode="none" is inconsistent. readonly blocks any
    on-screen keyboard unconditionally, and (verified directly) doesn't
    stop it from being clicked/tapped to open the calendar popup, which
    is the only way this field is meant to be filled in anyway.
    Selectboxes don't get readonly: they support typing to search/
    filter their own option list, which readonly would break.

    Confirmed on a real Android phone (both Chrome and Edge — same
    Chromium engine, so consistent with an Android/Chromium-level
    behavior rather than a browser-specific quirk) that none of that
    stopped the keyboard, which stayed up until manually dismissed.
    Two more layers were tried and kept as defense-in-depth, but the
    one that actually solves it for a real touch is the last one below:

    1. BaseWeb (the underlying component library) can reset an input's
       own DOM attributes on a React re-render faster than the
       childList/subtree observer below reacts to it — that observer
       only fires on nodes being added/removed, not on an existing
       node's attributes changing. Each date input additionally gets
       its own dedicated attribute-level observer, which reacts to
       exactly that case.

    2. A focus listener that blurs the input ~50ms after it's focused.
       Confirmed directly that the calendar popup opens on focus but
       doesn't close again on blur (its open state isn't tied to
       staying focused), so this dismisses whatever keyboard attempt
       is in flight without dismissing the popup — an immediate
       (0ms) blur was tried first and closed the popup too, so the
       delay is deliberate: long enough for the popup's own
       open-on-focus effect to have already run.

    3. The actual fix: a real touch landing directly on an editable
       input is what triggers a mobile OS keyboard, regardless of
       readonly/inputmode/blur timing — so stop a real touch from ever
       reaching the input at all. The input gets pointer-events: none,
       and an invisible same-sized overlay div sits on top of it inside
       its immediate wrapper (BaseWeb's own [data-baseweb="base-input"]
       div, sized to match the input already, so the overlay's CSS
       inset:0 tracks it through any resize with no JS recalculation
       needed); tapping the overlay calls el.focus() programmatically
       instead. A script-triggered focus that didn't originate from a
       direct touch on that specific element is the one thing that
       reliably does NOT bring up the keyboard on Android Chrome/Edge —
       and (verified directly) still opens the calendar popup and
       supports picking a date from it, same as a real click always
       did."""
    st.iframe(
        """
        <script>
        (function() {
            var doc = window.parent.document;
            function apply() {
                doc.querySelectorAll('[data-testid="stSelectbox"] input').forEach(function(el) {
                    el.setAttribute('inputmode', 'none');
                });
                doc.querySelectorAll('[data-testid="stDateInput"] input').forEach(function(el) {
                    el.setAttribute('inputmode', 'none');
                    el.setAttribute('readonly', 'readonly');
                    if (el.__ppKeyboardGuard) return;
                    el.__ppKeyboardGuard = true;
                    if (window.parent.MutationObserver) {
                        new window.parent.MutationObserver(function() {
                            if (el.getAttribute('inputmode') !== 'none') {
                                el.setAttribute('inputmode', 'none');
                            }
                            if (!el.hasAttribute('readonly')) {
                                el.setAttribute('readonly', 'readonly');
                            }
                        }).observe(el, {attributes: true, attributeFilter: ['inputmode', 'readonly']});
                    }
                    el.addEventListener('focus', function() {
                        setTimeout(function() { el.blur(); }, 50);
                    });
                    var wrapper = el.closest('[data-baseweb="base-input"]') || el.parentElement;
                    if (window.parent.getComputedStyle(wrapper).position === 'static') {
                        wrapper.style.position = 'relative';
                    }
                    el.style.pointerEvents = 'none';
                    var overlay = doc.createElement('div');
                    overlay.style.position = 'absolute';
                    overlay.style.inset = '0';
                    overlay.style.zIndex = '5';
                    overlay.style.cursor = 'pointer';
                    overlay.addEventListener('click', function(e) {
                        e.preventDefault();
                        el.focus();
                    });
                    wrapper.appendChild(overlay);
                });
            }
            apply();
            if (window.parent.MutationObserver) {
                new window.parent.MutationObserver(apply).observe(doc.body, {
                    childList: true, subtree: true
                });
            }
        })();
        </script>
        """,
        height=1,
    )


def page_header(text: str, tier_text: str | None = None) -> None:
    """Render a page's main H1 header, then measure its actual rendered
    width in the browser and scale the font to exactly fill the
    container — no leftover right-hand margin — while never exceeding
    this length tier's max and never wrapping past two lines (a CSS
    safety net in case the measurement can't run, e.g. scripts disabled).
    A per-character cqw estimate can't do this precisely: real text
    width depends on which letters are in it, not just how many, so a
    constant safe enough to avoid ever wrapping always left a visible
    gap for shorter/narrower strings. Measuring the actual rendered
    width removes that guesswork entirely. Field labels, dropdown values,
    Step-Level Ratings, and Improve/How no longer scale off this header's
    size (see --pp-substep-font's own static definition) — a long title
    forcing the header down to avoid wrapping used to shrink everything
    else on the page right along with it.

    tier_text: text to base _header_max()'s length tier on, if different
    from what's actually displayed — e.g. a variable suffix (a resident's
    name) that shouldn't itself push the header into a smaller ceiling
    tier just for being long. The real fit still measures the full
    displayed text's actual rendered width, so it still shrinks further
    than that ceiling if the full text doesn't fit."""
    max_rem = _header_max(tier_text if tier_text is not None else text)
    escaped = html.escape(text)
    st.markdown(
        f'<div class="pp-page-header-wrap"><h1 class="pp-page-header">'
        f'{escaped}</h1></div>',
        unsafe_allow_html=True,
    )
    st.iframe(
        f"""
        <script>
        (function() {{
            var doc = window.parent.document;
            var wraps = doc.querySelectorAll('.pp-page-header-wrap');
            var wrap = wraps[wraps.length - 1];
            if (!wrap) return;
            var el = wrap.querySelector('.pp-page-header');
            if (!el) return;
            var maxPx = {max_rem} * 16;
            var nbsp = '\\u00a0';
            // Measures a string's rendered single-line width at a given
            // font size via a detached, invisible probe — never touches
            // el itself, so its real DOM (Streamlit wraps header text in
            // an anchor-link span) is never disturbed.
            function measureWidth(str, fontPx) {{
                var probe = doc.createElement('span');
                probe.style.position = 'absolute';
                probe.style.visibility = 'hidden';
                probe.style.whiteSpace = 'nowrap';
                probe.style.fontSize = fontPx + 'px';
                var computed = window.parent.getComputedStyle(el);
                probe.style.fontFamily = computed.fontFamily;
                probe.style.fontWeight = computed.fontWeight;
                probe.textContent = str;
                doc.body.appendChild(probe);
                var w = probe.scrollWidth;
                doc.body.removeChild(probe);
                return w;
            }}
            function fit() {{
                var containerWidth = wrap.clientWidth;
                if (!containerWidth) return;
                var fullText = el.textContent;
                // header_break_before() (see its Python definition)
                // builds some headers with
                // exactly one regular, breakable space and non-breaking
                // spaces (nbsp) everywhere else, marking one deliberate
                // wrap point. On a narrow (mobile) screen, measure each
                // side of that point separately and take the wider one —
                // rather than assuming (as an earlier version of this
                // script did) that the text splits into two *even*
                // halves, which let a lopsided split (e.g. a long
                // procedure name, short resident name) overflow its line
                // and trigger the CSS's overflow-wrap: break-word
                // mid-word. Headers without such a point (or on desktop)
                // measure as one line, same as always.
                var mobile = window.parent.innerWidth <= 600;
                var breakIdx = mobile && fullText.indexOf(nbsp) > -1
                    ? fullText.indexOf(' ') : -1;
                var widest = breakIdx > -1
                    ? Math.max(
                        measureWidth(fullText.slice(0, breakIdx), maxPx),
                        measureWidth(fullText.slice(breakIdx + 1), maxPx)
                      )
                    : measureWidth(fullText, maxPx);
                // The two-segment (breakIdx > -1) case approximates two
                // separate wrapped lines from single-line nowrap probe
                // measurements of each segment — each segment (kept
                // unbreakable internally via nbsp) still has to survive
                // the browser's own multi-line layout afterward, which
                // can round a hair differently than the probe. An
                // unusually long unbroken segment (e.g. a long combined
                // first+last name) could then overflow its line by a
                // pixel or two and get pushed onto a clipped 3rd line by
                // the CSS's -webkit-line-clamp safety net. The plain
                // single-line case doesn't have that extra layout step,
                // so it keeps the tighter margin that was already tuned
                // to fill the header's width precisely.
                var safety = breakIdx > -1 ? 0.9 : 0.96;
                var finalPx = widest <= containerWidth
                    ? maxPx
                    : Math.max(1, maxPx * (containerWidth / widest) * safety);
                el.style.fontSize = finalPx + 'px';
            }}
            fit();
            window.parent.addEventListener('resize', fit);
            // Also watch the container itself: Streamlit's own column/
            // layout reflow can change its width slightly after this
            // script's first run, with no window 'resize' event to catch
            // it — a plain fixed-width guess would miss that follow-up.
            if (window.parent.ResizeObserver) {{
                new window.parent.ResizeObserver(fit).observe(wrap);
            }}
        }})();
        </script>
        """,
        height=1,
    )


def assessment_instructions_note() -> None:
    """Info box explaining the assessment form's three main sections —
    shown under the page header, before the first divider, on all three
    assessment-filling pages (Assess Together, Self-Assess, and the
    attending's pre-filled/blank forms)."""
    st.info(
        "There are 3 main sections. Fill out as much or as little as you are able.\n\n"
        "1. Short Form: Improve this / Do this\n"
        "2. Step-Level Ratings of a Case or Skills\n"
        "3. Debrief: Development/Improvement/Feed-Forward"
    )


def copy_link_button(link: str, key: str) -> None:
    """Big, obvious "Copy Link" button. st.code()'s own built-in copy
    icon is small, only shows on hover, and on mobile needs a first tap
    just to reveal it — easy to miss entirely.

    Rendered via st.iframe rather than st.markdown(unsafe_allow_html=True):
    confirmed empirically that Streamlit strips onclick (and presumably
    any other inline event-handler attribute) from markdown HTML even
    with unsafe_allow_html=True — the button still renders, just inert,
    with no error or warning. st.iframe's content isn't run through that
    sanitizer at all, and — also confirmed empirically (an actual OS
    clipboard write was observed after a click, matching the intended
    text) — the Clipboard API works fine from inside it. Falls back to
    the older execCommand('copy') approach (via a temporary off-screen
    textarea) if navigator.clipboard isn't available."""
    safe_link = json.dumps(link)
    st.iframe(
        f"""
        <!DOCTYPE html>
        <html><head><style>
        body {{ margin: 0; font-family: "Source Sans Pro", sans-serif; }}
        button {{
            width: 100%;
            box-sizing: border-box;
            padding: 0.6rem 1rem;
            font-size: 1rem;
            font-weight: 600;
            border-radius: 8px;
            border: 3px solid #FF4B4B;
            background: #FFFFFF;
            color: #000000;
            cursor: pointer;
        }}
        button:hover {{
            background: #FFF0F0;
            border-color: #E63946;
        }}
        </style></head>
        <body>
        <button id="{key}">📋 Copy Link</button>
        <script>
        var text = {safe_link};
        var btn = document.getElementById("{key}");
        btn.addEventListener("click", function() {{
            function done(ok) {{
                btn.textContent = ok ? "✅ Copied!" : "⚠️ Copy failed — select manually below";
                setTimeout(function() {{ btn.textContent = "📋 Copy Link"; }}, 1800);
            }}
            function fallback() {{
                var ta = document.createElement("textarea");
                ta.value = text;
                ta.style.position = "fixed";
                ta.style.opacity = "0";
                document.body.appendChild(ta);
                ta.focus();
                ta.select();
                var ok = false;
                try {{ ok = document.execCommand("copy"); }} catch (e) {{}}
                document.body.removeChild(ta);
                done(ok);
            }}
            if (navigator.clipboard && navigator.clipboard.writeText) {{
                navigator.clipboard.writeText(text).then(function() {{ done(true); }}, fallback);
            }} else {{
                fallback();
            }}
        }});
        </script>
        </body></html>
        """,
        height=54,
    )


def mobile_tip(text: str) -> None:
    """Render the "On mobile: ..." tip, then shrink its font (down from the
    CSS-defined base size in the .st-key-mobile_tip rule) just enough that
    the label never wraps past one line — same measure-and-fit approach as
    page_header. white-space:nowrap + text-overflow:ellipsis in the CSS is
    the fallback if the measurement can't run (scripts disabled)."""
    with st.container(key="mobile_tip"):
        st.info(text)
    st.iframe(
        """
        <script>
        (function() {
            var doc = window.parent.document;
            var ps = doc.querySelectorAll('.st-key-mobile_tip [data-testid="stAlertContainer"] p');
            var el = ps[ps.length - 1];
            if (!el) return;
            // el's own clientWidth is unreliable here: with white-space:nowrap
            // forced (from the CSS fallback), the <p> won't shrink below its
            // own unwrapped content size, so el.clientWidth just reports that
            // same overflowing size instead of the space actually available.
            // Streamlit's stMarkdownContainer wrapper div, one level up,
            // already gets an explicit width (accounting for the icon) that
            // isn't affected by the <p>'s own sizing — measure against that.
            var avail = el.parentElement;
            function fit() {
                el.style.fontSize = '';
                var availWidth = avail.clientWidth;
                if (!availWidth) return;
                var natural = el.scrollWidth;
                if (natural > availWidth) {
                    var baseSize = parseFloat(window.getComputedStyle(el).fontSize);
                    el.style.fontSize = Math.max(1, baseSize * (availWidth / natural) * 0.98) + 'px';
                }
            }
            fit();
            window.parent.addEventListener('resize', fit);
            if (window.parent.ResizeObserver) {
                new window.parent.ResizeObserver(fit).observe(avail);
            }
        })();
        </script>
        """,
        height=1,
    )


def fit_all_button_labels() -> None:
    """Shrink any button's label just enough that it never wraps past one
    line — same measure-and-fit approach as page_header/mobile_tip, just
    applied to every button on the currently rendered page at once.
    white-space:nowrap in the global CSS is the fallback if the
    measurement can't run (scripts disabled) — it clips with an ellipsis
    instead of wrapping to a second line."""
    st.iframe(
        """
        <script>
        (function() {
            var doc = window.parent.document;
            function fit(p) {
                var btn = p.closest('button');
                if (!btn) return;
                p.style.fontSize = '';
                // Measure against the <p>'s own box, not the button's —
                // the button is wider than its label (padding), so
                // comparing against btn.clientWidth let text through
                // that still overflowed the <p>'s own narrower box and
                // got silently clipped by its ellipsis fallback instead
                // of actually being shrunk to fit.
                var availWidth = p.clientWidth;
                if (!availWidth) return;
                var natural = p.scrollWidth;
                if (natural > availWidth) {
                    var baseSize = parseFloat(window.getComputedStyle(p).fontSize);
                    var target = baseSize * (availWidth / natural) * 0.9;
                    p.style.fontSize = Math.max(target, baseSize * 0.55, 9) + 'px';
                }
            }
            function fitAll() {
                doc.querySelectorAll('button p').forEach(fit);
            }
            fitAll();
            window.parent.addEventListener('resize', fitAll);
            if (window.parent.ResizeObserver) {
                new window.parent.ResizeObserver(fitAll).observe(doc.body);
            }
        })();
        </script>
        """,
        height=1,
    )


def sync_improve_how_label_width() -> None:
    """Measure the "In order to improve this:" and "Do this:" labels'
    natural (unwrapped) widths and set --pp-improve-label-width to the
    wider of the two — same measure-and-fit approach as page_header/
    mobile_tip, but syncing a shared width across two separate st.columns()
    rows instead of a font-size. The CSS rule using that var then gives
    both label columns that same width, so each label sits snug against
    its own text box and the two text boxes' left edges line up between
    the two rows, regardless of which row's label text is longer. Call
    this once, right after rendering both rows."""
    st.iframe(
        """
        <script>
        (function() {
            var doc = window.parent.document;
            var containers = doc.querySelectorAll('.st-key-assess_improve_how');
            var container = containers[containers.length - 1];
            if (!container) return;
            var labels = container.querySelectorAll('[data-testid="stMarkdownContainer"] p');
            function fit() {
                var maxWidth = 0;
                labels.forEach(function(p) {
                    var prevDisplay = p.style.display;
                    var prevWhiteSpace = p.style.whiteSpace;
                    p.style.display = 'inline-block';
                    p.style.whiteSpace = 'nowrap';
                    maxWidth = Math.max(maxWidth, p.scrollWidth);
                    p.style.display = prevDisplay;
                    p.style.whiteSpace = prevWhiteSpace;
                });
                if (maxWidth > 0) {
                    container.style.setProperty('--pp-improve-label-width', (maxWidth + 3) + 'px');
                }
            }
            fit();
            window.parent.addEventListener('resize', fit);
            if (window.parent.ResizeObserver) {
                new window.parent.ResizeObserver(fit).observe(container);
            }
        })();
        </script>
        """,
        height=1,
    )


# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
st.sidebar.title("🩺 Procedure Passport")

_is_attending_role = st.session_state.get("role") == "attending"
# When an attending is logged in, "resident" holds whichever resident they're
# currently assessing (same repurposing the anonymous magic-link flow already
# does) — not their own identity, so it must never drive the resident-login
# sidebar below.
_logged_in = None if _is_attending_role else st.session_state.get("resident")
_attending_logged_in = st.session_state.get("attending_login_email") if _is_attending_role else None

if _logged_in in ADMINS:
    if st.sidebar.button("⚙️ Admin Panel"):
        go_to("admin")

if _logged_in and st.session_state["page"] not in ("login", "attending_assessment", "attending_confirmation"):
    st.sidebar.markdown(f"👤 **{st.session_state.get('resident_name', '')}**")
    st.sidebar.markdown(f"_{_logged_in}_")
    st.sidebar.markdown("---")
    if st.sidebar.button("🏠 Back to Home", key="sb_home"):
        st.session_state["page"] = "home"
        st.rerun()
    st.sidebar.markdown("---")
    if st.sidebar.button("➕ Start Assessment", key="sb_start"):
        st.session_state["page"] = "start"
        st.rerun()
    if st.sidebar.button("📊 Cumulative Dashboard", key="sb_cumulative"):
        # Reset to unselected so the page always opens on "Choose
        # procedure" rather than remembering the last one picked.
        st.session_state.pop("cumulative_proc_select", None)
        st.session_state["page"] = "cumulative"
        st.rerun()
    if st.sidebar.button("💬 Comments Dashboard", key="sb_comments"):
        st.session_state["page"] = "comments"
        st.rerun()
    if st.sidebar.button("📜 Evaluation History", key="sb_eval_history"):
        st.session_state["page"] = "eval_history"
        st.rerun()
    st.sidebar.markdown("---")
    if st.sidebar.button("🚪 Logout", key="sb_logout_resident"):
        for _k in list(st.session_state.keys()):
            del st.session_state[_k]
        st.cache_data.clear()
        st.rerun()

# ── Attending-account sidebar (own login, separate from the anonymous
# magic-link flow above) ──
if _attending_logged_in:
    st.sidebar.markdown(f"👤 **{st.session_state.get('attending_login_name', '')}**")
    st.sidebar.markdown(f"_{_attending_logged_in}_")
    st.sidebar.markdown("---")
    if st.sidebar.button("🏠 Back to Home", key="sb_att_home"):
        st.session_state["page"] = "attending_home"
        st.rerun()
    st.sidebar.markdown("---")
    if st.sidebar.button("➕ Start Assessment", key="sb_att_start"):
        st.session_state["page"] = "attending_start"
        st.rerun()
    if st.sidebar.button("📊 Resident Dashboard", key="sb_att_dashboard"):
        st.session_state["page"] = "attending_resident_dashboard"
        st.rerun()
    if st.sidebar.button("📜 Evaluation History", key="sb_att_eval_history"):
        st.session_state["page"] = "attending_eval_history"
        st.rerun()
    st.sidebar.markdown("---")
    if st.sidebar.button("🚪 Logout", key="sb_logout_attending"):
        for _k in list(st.session_state.keys()):
            del st.session_state[_k]
        st.cache_data.clear()
        st.rerun()

# ── Sidebar rating legend — shown whenever either sidebar above is
# (same conditions as the resident/attending nav blocks), so every left
# sidebar that actually appears carries it, expanded by default. ──
if (
    _logged_in and st.session_state["page"] not in ("login", "attending_assessment", "attending_confirmation")
) or _attending_logged_in:
    st.sidebar.markdown("---")
    render_rating_legend(key="rating_legend_sidebar", container=st.sidebar, expanded=True)
    render_complexity_legend(key="complexity_legend_sidebar", container=st.sidebar, expanded=False)
    render_prep_legend(key="prep_legend_sidebar", container=st.sidebar, expanded=False)

# ─────────────────────────────────────────────
# SHARED CSS
# ─────────────────────────────────────────────
st.markdown(
    """
<style>
/* Shift the whole page's content block to the left: with layout="wide"
   there's no true centering, but .block-container still carries its
   own left/right padding (80px at desktop widths) — the transform
   nudges content left within that padding, taking space from the left
   side and adding it to the right. Applies to every page, not just
   the heatmap. Gated to desktop widths only: below the ~900px
   breakpoint where that padding drops to 16px, an unconditional -50px
   shift overshot the padding entirely and pushed real content (e.g.
   the page header, form inputs) off-screen to the left by ~34px —
   exactly the "left side of the page is cut off" bug reported on
   mobile portrait. Confirmed via measurement (getBoundingClientRect)
   that 900px is comfortably past the breakpoint (80px padding there;
   still 16px as high as 850px). */
@media (min-width: 900px) {
    [data-testid="stAppViewContainer"] .block-container {
        transform: translateX(-50px);
    }
}
/* Heatmap section heading ("{procedure} — Progress Heatmap[ and
   Comments]", _render_resident_heatmap()): reclaim most of the
   desktop-tier 80px right padding for just this heading, since its
   text (procedure name + a deliberately unbreakable suffix — see
   header_break_before) can be long enough to wrap to a second line in
   a narrow band of desktop widths where that padding, not the
   viewport itself, is what's actually too tight (confirmed via
   measurement: e.g. at 900px wide the heading needed ~787px but only
   had 740px available). 16px of the 80px is kept as a right-hand
   buffer. Gated to the same >=900px tier as the shift above — below
   it, padding is already only 16px, and reclaiming further would
   overflow the heading past the viewport's right edge (the same
   mistake the shift above caused unconditionally on the left,
   before). */
@media (min-width: 900px) {
    .st-key-heatmap_heading_row h3 {
        margin-right: -64px !important;
    }
}
/* Every button's label stays on one line — fit_all_button_labels()
   shrinks the font to make it fit; this is the no-JS fallback (clips
   with an ellipsis instead of wrapping to a second line). */
button p {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
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
.legend-swatch {
    width: 14px;
    height: 14px;
    border-radius: 3px;
    border: 1px solid var(--secondary-background-color);
    display: inline-block;
}
/* Legend expanders (render_prep_legend()/render_complexity_legend()/
   render_rating_legend()): one row per level, swatch beside its
   description. */
.legend-desc-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin-bottom: 0.5rem;
}
.legend-desc-row {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    font-size: 0.85rem;
    line-height: 1.3;
}
.legend-desc-row .legend-swatch {
    flex-shrink: 0;
    margin-top: 0.15rem;
}
/* Home page cards: keep the three action buttons vertically aligned
   even when title/description text wraps to different heights. */
.st-key-home_cards [data-testid="stColumn"] > [data-testid="stVerticalBlock"] {
    display: flex;
    flex-direction: column;
    height: 100%;
    gap: 0.3rem;
}
.st-key-home_cards [data-testid="stElementContainer"]:has([data-testid="stButton"]) {
    margin-top: auto;
}
.st-key-home_cards h3 [data-testid="stHeaderActionElements"] {
    display: none;
}
.st-key-home_cards [data-testid="stElementContainer"]:has(h3) {
    height: 5rem;
    overflow: hidden;
    container-type: inline-size;
}
.st-key-home_cards [data-testid="stElementContainer"]:has(h3) h3 {
    padding-top: 0.3rem;
    padding-bottom: 0.2rem;
}
.st-key-home_cards [data-testid="stElementContainer"]:has(h3) h3 > span:first-child {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    font-size: clamp(0.95rem, 12cqw, 1.75rem);
    line-height: 1.25;
}
.st-key-home_cards [data-testid="stElementContainer"]:has(> [data-testid="stMarkdown"] p) {
    height: 2.4rem;
    overflow: hidden;
    container-type: inline-size;
}
.st-key-home_cards [data-testid="stElementContainer"]:has(> [data-testid="stMarkdown"] p) p {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    margin: 0;
    line-height: 1.3;
    font-size: clamp(0.7rem, 8.5cqw, 0.875rem);
}
/* Step-Level Ratings expander: label styled like a smaller page header.
   --pp-substep-font sets its size — a fixed value (see its own
   definition below), independent of the main header's size, so a long
   procedure/resident name that shrinks the header doesn't shrink this
   too. white-space: normal overrides Streamlit's own default (nowrap +
   ellipsis) for expander summary labels, which is otherwise sized for
   the typical short, single-line case — header_break_before() keeps
   "Step-Level Ratings for" and the procedure name each as their own
   unbreakable unit, with the one regular breakable space landing right
   after "for": one line whenever it fits, and if not, the wrap lands
   there rather than mid-phrase or partway through the name.
   overflow-wrap: break-word is a safety net for a name too long to
   fit even on its own full line. */
.st-key-step_ratings_expander_resident summary [data-testid="stMarkdownContainer"] p,
.st-key-step_ratings_expander_attending summary [data-testid="stMarkdownContainer"] p {
    font-size: var(--pp-substep-font, 1.3125rem);
    font-weight: 600;
    white-space: normal !important;
    overflow-wrap: break-word;
}
/* "Click to Expand"/"Click to Collapse" hint on its own line under
   that label, at 3/4 of its size — a generated ::after (rather than a
   second line of real text) since st.expander's label is a single
   inline markdown string. Which text shows is driven by the native
   <details> element's own "open" attribute (same signal the green
   border below reacts to) — no JS needed. */
.st-key-step_ratings_expander_resident details:not([open]) summary [data-testid="stMarkdownContainer"] p::after,
.st-key-step_ratings_expander_attending details:not([open]) summary [data-testid="stMarkdownContainer"] p::after {
    content: "Click to Expand";
    display: block;
    font-size: calc(var(--pp-substep-font, 1.3125rem) * 0.75);
    font-weight: normal;
}
.st-key-step_ratings_expander_resident details[open] summary [data-testid="stMarkdownContainer"] p::after,
.st-key-step_ratings_expander_attending details[open] summary [data-testid="stMarkdownContainer"] p::after {
    content: "Click to Collapse";
    display: block;
    font-size: calc(var(--pp-substep-font, 1.3125rem) * 0.75);
    font-weight: normal;
}
/* Green border while the Step-Level Ratings expander is collapsed, to
   draw the eye to it; gone once it's opened (the native <details>
   element's own "open" attribute drives this, no JS needed here). */
.st-key-step_ratings_expander_resident details:not([open]),
.st-key-step_ratings_expander_attending details:not([open]) {
    border: 2px solid #2E7D32 !important;
}
/* Main page headers: font-size is set by page_header()'s injected script
   after measuring the real rendered text width, so it exactly fills the
   container. This is the CSS-only fallback/safety net (script disabled,
   or before the script's first paint): still capped at two lines. */
.pp-page-header {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    line-height: 1.25;
    overflow-wrap: break-word;
    text-overflow: ellipsis;
}
/* Attending pre-filled-form notice: font-size is set by its own
   injected script (see the "if _draft:" block on the attending
   assessment page), which tries the whole notice on one line first,
   only shrinking down to a floor before allowing the deliberate
   two-line break header_break_before() marks. This is the CSS-only
   fallback/safety net (script disabled, or before its first paint):
   still capped at two lines. */
.pp-prefill-notice-text {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    line-height: 1.3;
    overflow-wrap: break-word;
}
/* --pp-substep-font sizes a whole hierarchy of form text (field labels,
   dropdown values, Step-Level Ratings, Improve/How, the mobile tip).
   It used to be tied to the page header's own live-measured size, so
   the page scaled as one unit with window width — but a long procedure/
   resident name could force the header down quite small to avoid
   wrapping (by design, see page_header() — no floor there), and
   everything tied to it shrank right along with it, well past
   comfortable reading size, on desktop as much as mobile. Fixed at all
   widths now, independent of the header entirely. */
:root {
    --pp-substep-font: 1.3125rem;
}
/* Assessment page top nav: Streamlit stacks columns onto separate rows
   below a width breakpoint (each stColumn gets min-width: ~100%). Force
   the Back/Home buttons to stay side by side at any width instead. Columns
   don't shrink past their button's natural (nowrap) width, so on very
   narrow screens the row scrolls horizontally rather than the two
   buttons shrinking into each other and overlapping. */
.st-key-assess_top_nav [data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap !important;
    overflow-x: auto;
}
.st-key-assess_top_nav [data-testid="stColumn"] {
    min-width: 0 !important;
    flex: 0 0 auto !important;
    width: auto !important;
}
.st-key-assess_top_nav button p {
    white-space: nowrap;
}
/* Tighten the nav row's default ~48px gap to the divider right below
   it down to 16px — but only when it's directly followed by that
   divider (non-robotic procedures). A robotic procedure has the Robot
   picker row between them instead (own already-tuned spacing) —
   :has(+ ...) below only matches the no-picker case, so the picker
   case is left alone rather than colliding with it. Both the
   resident's and the logged-in attending's assessment pages share
   this key. */
[data-testid="stLayoutWrapper"]:has(> .st-key-assess_top_nav)
    + [data-testid="stElementContainer"]:has(hr) {
    margin-top: -32px !important;
}
/* Robot picker row ("Robot:" plus the Xi/SP/DV5 checkboxes): same
   shrink-to-content trick as the top nav above, so the label and all
   three checkboxes sit close together on the left instead of each
   getting an even (and mostly empty) 1/4 of the row's full width —
   plus a tighter gap between them and vertical centering so the
   "Robot:" label lines up with the checkboxes beside it. */
.st-key-assess_robo_row [data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap !important;
    gap: 0.5rem !important;
    align-items: center !important;
}
.st-key-assess_robo_row [data-testid="stColumn"] {
    min-width: 0 !important;
    flex: 0 0 auto !important;
    width: auto !important;
}
/* IMPORTANT: each st.checkbox's own "Xi"/"SP"/"DV5" label text is ALSO
   rendered through a [data-testid="stMarkdownContainer"] > p — the same
   structure the plain "Robot:" text uses — so an unscoped selector
   matches (and repositions) both. Every rule below is scoped with
   :not(:has([data-testid="stCheckbox"])) to hit only the one column
   that's markdown-but-not-a-checkbox ("Robot:" itself), never the
   checkboxes' own internal labels — that's what made every previous
   attempt at nudging "Robot:" also drag Xi/SP/DV5 along with it. */
.st-key-assess_robo_row [data-testid="stColumn"]:has([data-testid="stMarkdownContainer"]):not(:has([data-testid="stCheckbox"])) {
    display: flex;
    align-items: center;
    height: 2.5rem;
}
.st-key-assess_robo_row [data-testid="stColumn"]:has([data-testid="stMarkdownContainer"]):not(:has([data-testid="stCheckbox"]))
    [data-testid="stMarkdownContainer"] {
    margin-bottom: 0 !important;
}
.st-key-assess_robo_row [data-testid="stColumn"]:has([data-testid="stMarkdownContainer"]):not(:has([data-testid="stCheckbox"]))
    [data-testid="stMarkdownContainer"] p {
    white-space: nowrap;
    margin: 0;
    /* Now that this selector is properly isolated from the checkboxes'
       own labels (see the comment above), this transform only ever
       repositions "Robot:" itself. */
    transform: translateY(11px);
}
/* "On mobile: tap the >> icon..." tip: shrink padding, and match the text
   size to "Daily Preparation" and similar field labels (0.75x --pp-substep-font)
   so it's part of the same live, width-responsive size hierarchy instead
   of a fixed size of its own. It's now the first element on its page, so
   pull it up out of Streamlit's default ~96px block-container top padding
   (reserved so content clears the sticky header) — scoped to this
   container only, so it doesn't touch top spacing on any other page. */
.st-key-mobile_tip {
    margin-top: -36px;
}
.st-key-mobile_tip [data-testid="stAlertContainer"] {
    padding-top: 0.35rem;
    padding-bottom: 0.35rem;
    display: flex;
    align-items: center;
}
/* Flex items default to min-width:auto, which refuses to shrink below the
   text's own unwrapped intrinsic width — with white-space:nowrap forced
   below, that let the icon+text row (and everything measuring against it,
   including mobile_tip()'s own fit script) balloon past the real available
   width instead of being constrained by it. min-width:0 at each flex level
   here lets it actually shrink to fit like normal content. */
.st-key-mobile_tip [data-testid="stAlertContainer"],
.st-key-mobile_tip [data-testid="stAlertContentInfo"],
.st-key-mobile_tip [data-testid="stMarkdownContainer"] {
    min-width: 0;
}
.st-key-mobile_tip [data-testid="stAlertContainer"] p {
    font-size: calc(var(--pp-substep-font, 1.3125rem) * 0.75);
    line-height: 1.3;
    /* mobile_tip()'s script shrinks this further so the label never wraps
       past one line; this is the CSS-only fallback if it can't run. */
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
/* Streamlit's alert internals are several nested flex/block layers deep,
   each defaulting to top/stretch alignment, which left the text sitting
   above center within our shorter box. Force every level to center. */
.st-key-mobile_tip [data-testid="stAlertContainer"] * {
    align-items: center !important;
    align-self: center !important;
}
/* Assessment page field labels (Daily Preparation, Overall Performance Rating,
   Development/Improvement/Feed-Forward) and every label inside the
   Step-Level Ratings expander (Case Complexity, then each procedure
   step): sized relative to that expander's own header (--pp-substep-font,
   itself 0.75x the page's main header), so the whole page scales
   together as one hierarchy with window width. */
.st-key-assess_preparation [data-testid="stWidgetLabel"] p,
.st-key-assess_overall_performance [data-testid="stWidgetLabel"] p,
.st-key-assess_notes [data-testid="stWidgetLabel"] p,
.st-key-step_ratings_expander_resident [data-testid="stWidgetLabel"] p,
.st-key-step_ratings_expander_attending [data-testid="stWidgetLabel"] p {
    font-size: calc(var(--pp-substep-font, 1.3125rem) * 0.75);
}
/* Dropdown value text (Daily Preparation, Overall Performance Rating, and every
   Step-Level Ratings entry incl. Case Complexity): 0.8x its own label's
   size above, i.e. 0.6x --pp-substep-font overall. The options popup
   itself renders in a portal under <body>, outside these scoped
   containers, so it isn't reachable here and keeps its default size. */
.st-key-assess_preparation [data-testid="stSelectbox"] input,
.st-key-assess_overall_performance [data-testid="stSelectbox"] input,
.st-key-step_ratings_expander_resident [data-testid="stSelectbox"] input,
.st-key-step_ratings_expander_attending [data-testid="stSelectbox"] input {
    font-size: calc(var(--pp-substep-font, 1.3125rem) * 0.6);
}
/* "In order to improve this:" / "Do this:" — two stacked label+textbox
   rows, one st.columns() call each. sync_improve_how_label_width()
   measures both labels' natural (unwrapped) widths and sets
   --pp-improve-label-width to the wider of the two, so both label
   columns below share that same width regardless of which row's label
   is actually longer — the text boxes' left edges line up between the
   two rows, and each row's label sits snug against its own text box
   instead of at a fixed ratio-based column boundary with a leftover
   gap. Falls back to auto (each column sized to its own content, which
   won't line up between rows) if the measurement script can't run. */
.st-key-assess_improve_how [data-testid="stHorizontalBlock"] {
    align-items: center;
    gap: 0.4rem;
}
.st-key-assess_improve_how [data-testid="stColumn"]:has([data-testid="stMarkdownContainer"]) {
    flex: 0 0 var(--pp-improve-label-width, auto) !important;
    width: var(--pp-improve-label-width, auto) !important;
    min-width: 0 !important;
}
.st-key-assess_improve_how [data-testid="stColumn"]:has([data-testid="stTextInput"]) {
    flex: 1 1 0 !important;
    width: auto !important;
    min-width: 100px !important;
}
/* The two rows are separate blocks stacked in the container's own
   vertical flow — tighten Streamlit's default inter-block gap between
   them so they read as one compact two-line field instead of two
   loosely related rows. */
.st-key-assess_improve_how [data-testid="stVerticalBlock"] {
    gap: 0.3rem;
}
/* Streamlit gives stMarkdownContainer a built-in -16px bottom margin
   (its own vertical-rhythm spacing trick). That negative margin
   collapses this column's layout height down to ~10px even though the
   text still renders at its full ~26px, so the text visually
   overflowed out the bottom of its (collapsed) box — which is what
   made it look bottom-aligned against the input next to it. Cancel it
   so the column's box actually matches its text, and the row's
   align-items:center above can center it correctly. */
.st-key-assess_improve_how [data-testid="stMarkdownContainer"] {
    margin-bottom: 0 !important;
}
.st-key-assess_improve_how [data-testid="stColumn"]:has([data-testid="stMarkdownContainer"]) [data-testid="stMarkdownContainer"] p {
    margin: 0;
    text-align: right;
    font-size: calc(var(--pp-substep-font, 1.3125rem) * 0.75);
}
/* Both rows are separate st.columns() calls, so each is the lone/first
   [data-testid="stHorizontalBlock"] under its own private wrapper —
   :first-of-type can't tell them apart (it matched both, which is why
   that approach briefly left "Do this:" flush left too). The "In order
   to improve this:" label instead gets an inline style="text-align:left"
   straight from Python, which — having higher specificity than this
   class-based rule without needing !important — cleanly overrides just
   that one label while "Do this:" keeps the rule's default right-align. */
.st-key-assess_improve_how [data-testid="stTextInput"] input {
    font-size: calc(var(--pp-substep-font, 1.3125rem) * 0.6);
}
/* The two dividers around this section still carry Streamlit's normal
   ~32px/48px vertical rhythm above/below (each element's own default
   spacing plus the page's inter-element gap) — pull the section's
   outer wrapper up/down with negative margins, unevenly (-16/-32
   rather than a flat -24/-24), so the section actually sits centered
   between the two lines (equal ~16px gaps to each) instead of just
   snug with mismatched gaps. */
[data-testid="stLayoutWrapper"]:has(> .st-key-assess_improve_how) {
    margin-top: -16px !important;
    margin-bottom: -32px !important;
}
/* Same idea for the Overall/Daily Preparation row (Case Complexity now
   lives inside the Step-Level Ratings expander below): its default
   gaps to the divider above (32px) and the expander below (16px)
   weren't equal — pull/push its wrapper so the gap above lands at
   24px and the gap below (bumped an extra 8px per feedback that it
   still looked tighter) at 32px. */
[data-testid="stLayoutWrapper"]:has(> .st-key-assess_ratings_row) {
    margin-top: -8px !important;
    margin-bottom: 16px !important;
}
/* Same idea for the Robot picker row: pull its wrapper down to close
   up most of its default ~32px gap to the divider right below it. */
[data-testid="stLayoutWrapper"]:has(> .st-key-assess_robo_row) {
    margin-bottom: -16px !important;
}
/* Step-Level Ratings expander, then a divider, then the three legend
   expanders (Rating/Prep/Complexity), then another divider before
   Development/Improvement/Feed-Forward. The expander-to-divider gap
   above the legends (~48px default) is pulled to 32px, matching that
   same target used elsewhere in this file. The legend block itself
   sits centered between its two divider lines at ~16px each — first
   legend's default ~33px gap to the line above it pulled in to match
   the last legend's default ~48px gap to the line below, same
   two-line-sandwich treatment the Improve/How section above gets. The
   key class lands directly on each expander's own wrapper (unlike a
   plain st.container), so no :has() indirection is needed here. */
.st-key-step_ratings_expander_resident,
.st-key-step_ratings_expander_attending {
    margin-bottom: -16px !important;
}
.st-key-rating_legend_resident,
.st-key-rating_legend_attending {
    margin-top: -17px !important;
}
.st-key-complexity_legend_resident,
.st-key-complexity_legend_attending {
    margin-bottom: -32px !important;
}
/* Start page: "Assess Together", "Self-Assessment", and "Blank
   Magic-Link for Attending" all get the same white background /
   bold red border look, overriding whichever primary/secondary
   button type each one otherwise renders as. Label text stays
   plain black, not red/bold, so only the border carries the color. */
.st-key-start_together_btn button,
.st-key-start_self_btn button,
.st-key-start_blank_link_btn button {
    background-color: #FFFFFF !important;
    border: 3px solid #FF4B4B !important;
    color: #000000 !important;
}
.st-key-start_together_btn button p,
.st-key-start_self_btn button p,
.st-key-start_blank_link_btn button p {
    color: #000000 !important;
    font-weight: normal !important;
}
.st-key-start_together_btn button:hover,
.st-key-start_self_btn button:hover,
.st-key-start_blank_link_btn button:hover {
    background-color: #FFF0F0 !important;
    border-color: #E63946 !important;
    color: #000000 !important;
}
</style>
""",
    unsafe_allow_html=True,
)

# ─────────────────────────────────────────────
# PAGE ROUTER
# ─────────────────────────────────────────────
page = st.session_state["page"]

# A rerun re-renders this whole script in place rather than navigating
# the browser, so switching pages (e.g. after submitting a form, or via
# a sidebar link) otherwise leaves the scroll position wherever it was
# on the *previous* page — landing mid-page, or on the confirmation
# screen after an attending submits an evaluation. Scroll back to the
# top, but only when the page actually changed: most reruns are just an
# ordinary widget interaction on the same page, and those should never
# yank the user's scroll position out from under them.
if st.session_state.get("_scroll_top_page") != page:
    st.session_state["_scroll_top_page"] = page
    # The page name is embedded in the iframe's own content (as an inert
    # HTML comment) purely so this srcdoc string actually differs from
    # whatever was last rendered at this spot. Streamlit doesn't tear
    # down and recreate an element whose content is byte-identical to
    # what it last sent for that same call site — it just revives the
    # existing (already-loaded) node, which never re-fires the <script>
    # inside since the iframe document itself never reloads. A plain
    # static script (tried first) silently no-op'd on every navigation
    # except the very first, for exactly that reason.
    st.iframe(
        f"""
        <!-- scroll-to-top for: {html.escape(page)} -->
        <script>
        (function() {{
            window.parent.scrollTo(0, 0);
            var doc = window.parent.document;
            var containers = doc.querySelectorAll(
                '[data-testid="stAppViewContainer"], [data-testid="stMain"], section.main'
            );
            containers.forEach(function(c) {{ c.scrollTop = 0; }});
        }})();
        </script>
        """,
        height=1,
    )


# ════════════════════════════════════════════════════════════
# PAGE: LOGIN
# ════════════════════════════════════════════════════════════
if page == "login":

    def _complete_login(canonical_email: str) -> None:
        """Password verified (or just created) — resolve the account and
        drop into the app proper."""
        residents = read_sheet_df(
            SHEET_RESIDENTS, expected_cols=RESIDENT_COLS
        )
        admins_lower = [a.lower() for a in ADMINS]
        email_lower = canonical_email.strip().lower()
        residents_lower = residents["email"].str.strip().str.lower()
        if email_lower in admins_lower:
            st.session_state.update(resident=canonical_email, resident_name="Admin", role="admin", page="admin")
        elif email_lower in residents_lower.values:
            row = residents.loc[residents_lower == email_lower].iloc[0]
            st.session_state.update(
                resident=row["email"], resident_name=row["name"],
                specialty_id=row["specialty_id"], role="resident", page="home",
            )
        else:
            attendings = _read_attendings_df()
            attendings_lower = attendings["email"].fillna("").str.strip().str.lower()
            row = attendings.loc[attendings_lower == email_lower].iloc[0]
            st.session_state.update(
                role="attending",
                attending_login_email=row["email"],
                attending_login_name=row["attending_name"],
                attending_login_id=row["attending_id"],
                attending_login_specialty_id=row["specialty_id"],
                page="attending_home",
            )
        st.rerun()

    page_header("🩺 Procedure Passport")
    st.markdown("_Track your surgical skills journey, one procedure at a time._")
    st.markdown("---")

    # Email and password on one screen, submitted together — whether
    # this is a first-ever login (no password on file yet) or a
    # returning one is only known once the email is looked up, which
    # only happens on submit, so both fields are always shown up
    # front rather than password appearing as a separate step after
    # the email is entered.
    email = st.text_input("Email address", placeholder="you@hospital.org", key="login_email_input")
    pw = st.text_input("Password", type="password", key="login_pw_input")
    st.caption(
        "First time here, or no password set yet? Leave Password blank and "
        "click Log In — you'll be prompted to choose one (8+ characters)."
    )
    if st.button("Log In →", width="stretch", type="primary"):
        if not email.strip():
            st.error("Please enter your email address.")
        else:
            # Password isn't required just to submit — whether one's
            # even needed depends on whether this account has one on
            # file yet, which isn't known until after the lookup
            # below. A first-time user can submit with the password
            # field left blank; they're told to choose one once
            # that's confirmed, without ever leaving this page.
            try:
                residents = read_sheet_df(
                    SHEET_RESIDENTS,
                    expected_cols=RESIDENT_COLS,
                )
                attendings = _read_attendings_df()
                email_lower = email.strip().lower()
                admins_lower = [a.lower() for a in ADMINS]
                residents_lower = residents["email"].str.strip().str.lower()
                attendings_lower = attendings["email"].fillna("").str.strip().str.lower()
                if email_lower in admins_lower:
                    canonical = ADMINS[admins_lower.index(email_lower)]
                elif email_lower in residents_lower.values:
                    canonical = residents.loc[residents_lower == email_lower].iloc[0]["email"]
                elif email_lower in attendings_lower.values:
                    canonical = attendings.loc[attendings_lower == email_lower].iloc[0]["email"]
                else:
                    canonical = None
                if canonical is None:
                    st.error("❌ Email not recognised. Ask an admin to add you.")
                elif get_password_row(canonical) is not None:
                    if not pw:
                        st.error("Please enter your password.")
                    elif verify_password(canonical, pw):
                        _complete_login(canonical)
                    else:
                        st.error("❌ Incorrect password.")
                elif len(pw) < 8:
                    st.info(
                        f"👋 First time logging in as **{canonical}** — "
                        "enter a password above (8+ characters) and log in again "
                        "to set it as your password."
                    )
                else:
                    set_password(canonical, pw)
                    _complete_login(canonical)
            except ConnectionError as exc:
                show_gs_error(exc)


# ════════════════════════════════════════════════════════════
# PAGE: ADMIN PANEL
# ════════════════════════════════════════════════════════════
elif page == "admin":
    page_header("⚙️ Admin Panel")
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

        if not specialties.empty:
            with st.expander("🗑️ Delete Specialty"):
                st.caption(
                    "Removes a specialty. Residents, attendings, and "
                    "procedures already assigned to it are left as-is — "
                    "not deleted or reassigned — they just show its raw "
                    "id instead of a name until given a different one."
                )
                _del_spec_name = st.selectbox(
                    "Specialty", specialties["specialty_name"], key="del_spec_sel"
                )
                _del_spec_id = specialties.loc[
                    specialties["specialty_name"] == _del_spec_name, "specialty_id"
                ].values[0]
                _del_spec_usage = _count_specialty_usage(_del_spec_id)
                _del_spec_total = sum(_del_spec_usage.values())

                st.markdown(f"**Specialty:** {_del_spec_name} ({_del_spec_id})")
                st.markdown(
                    f"**In use by:** {_del_spec_usage['residents']} resident(s), "
                    f"{_del_spec_usage['attendings']} attending(s), "
                    f"{_del_spec_usage['procedures']} procedure(s)"
                )

                _del_spec_confirmed = True
                if _del_spec_total > 0:
                    st.warning(
                        f'⚠️ "{_del_spec_name}" is still assigned to {_del_spec_total} '
                        "resident(s)/attending(s)/procedure(s). They won't be deleted "
                        "or changed — they'll just show its id instead of a name."
                    )
                    _del_spec_confirmed = st.checkbox(
                        f'Yes, delete "{_del_spec_name}"',
                        key=f"confirm_del_spec_{_del_spec_id}",
                    )

                if st.button("Delete Specialty", key="btn_del_spec"):
                    if not _del_spec_confirmed:
                        st.error("Please check the confirmation box above before deleting.")
                    else:
                        try:
                            _delete_specialty(_del_spec_id)
                            st.success(f'✅ Deleted "{_del_spec_name}"')
                            time.sleep(0.5)
                            st.rerun()
                        except ValueError as _del_spec_exc:
                            st.error(f"⚠️ {_del_spec_exc}")
    except ConnectionError as exc:
        show_gs_error(exc)

    st.markdown("---")

    # ── Residents ────────────────────────────────────────
    st.subheader("Residents")
    try:
        spec_df = read_sheet_df(SHEET_SPECIALTY, expected_cols=["specialty_id", "specialty_name"])
        spec_name_to_id = dict(zip(spec_df["specialty_name"], spec_df["specialty_id"]))

        residents = read_sheet_df(
            SHEET_RESIDENTS, expected_cols=RESIDENT_COLS
        )
        # Every resident-picking dropdown below shows email (the sheet's
        # own identifier), but is still ordered alphabetically by last
        # name for the same reason the name-based Resident/Attending
        # pickers elsewhere in the app already are — last name is what
        # people actually look up a colleague by. Computed once here and
        # reused (as _residents_by_last["email"]) rather than re-sorted
        # at each picker.
        _residents_by_last = residents.copy()
        _residents_by_last["_last"] = _residents_by_last["name"].astype(str).apply(
            lambda n: n.split()[-1] if n.split() else n
        )
        _residents_by_last = _residents_by_last.sort_values("_last", kind="stable")

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
            with st.expander("✏️ Edit Resident"):
                edit_res_email = st.selectbox(
                    "Select resident to edit", _residents_by_last["email"], key="edit_res_select"
                )
                _edit_res_row = residents[residents["email"] == edit_res_email].iloc[0]
                _edit_res_spec_match = spec_df.loc[
                    spec_df["specialty_id"] == _edit_res_row["specialty_id"], "specialty_name"
                ]
                _edit_res_spec_default = _edit_res_spec_match.values[0] if len(_edit_res_spec_match) else None
                _edit_res_spec_options = list(spec_df["specialty_name"])
                _edit_res_spec_idx = (
                    _edit_res_spec_options.index(_edit_res_spec_default)
                    if _edit_res_spec_default in _edit_res_spec_options else 0
                )
                # Keyed by edit_res_email, not a fixed key — same fix as Edit
                # Existing Procedure's name field: a fixed key means Streamlit
                # keeps showing whatever was last TYPED here for a DIFFERENT
                # resident once this widget has rendered once, ignoring
                # value= on every later rerun — switching "Select resident to
                # edit" alone didn't reset it, so a stale name silently
                # carried over. Email itself isn't editable here (unlike
                # Edit Attending) — it's the key every case/score/comment
                # record references a resident by, and changing it here
                # wouldn't update any of those, silently orphaning their
                # whole history.
                edit_res_name_new = st.text_input(
                    "Full name", value=str(_edit_res_row["name"]), key=f"edit_res_name_{edit_res_email}"
                )
                edit_res_spec = st.selectbox(
                    "Specialty", _edit_res_spec_options, index=_edit_res_spec_idx,
                    key=f"edit_res_spec_{edit_res_email}",
                )
                if st.button("Save Changes", key="btn_edit_res"):
                    if not edit_res_name_new.strip():
                        st.error("Please enter a resident name.")
                    else:
                        _spec_match = spec_df[spec_df["specialty_name"].astype(str).str.strip() == str(edit_res_spec).strip()]
                        _new_spec_id = _spec_match["specialty_id"].values[0] if len(_spec_match) > 0 else None
                        updated = residents.copy()
                        _mask = updated["email"] == edit_res_email
                        updated.loc[_mask, "name"]         = edit_res_name_new.strip()
                        updated.loc[_mask, "specialty_id"] = _new_spec_id
                        write_sheet_df(SHEET_RESIDENTS, updated)
                        st.success(f"✅ Saved {edit_res_name_new.strip()}")
                        time.sleep(0.5)
                        st.rerun()

            with st.expander("🔑 Reset Password"):
                st.caption("Clears their stored password — their next login will prompt them to set a new one.")
                reset_email = st.selectbox("Select resident", _residents_by_last["email"], key="reset_res_pw")
                if st.button("Reset Password", key="btn_reset_res_pw"):
                    clear_password(reset_email)
                    st.success(f"✅ Password cleared for {reset_email}")
                    time.sleep(0.5)
                    st.rerun()

            with st.expander("🗑️ Delete Resident"):
                del_email = st.selectbox("Select resident to delete", _residents_by_last["email"], key="del_res")
                if st.button("Delete", key="btn_del_res"):
                    updated = residents[residents["email"] != del_email].reset_index(drop=True)
                    write_sheet_df(SHEET_RESIDENTS, updated)
                    clear_password(del_email)
                    st.success(f"Deleted {del_email}")
                    time.sleep(0.5)
                    st.rerun()
    except ConnectionError as exc:
        show_gs_error(exc)

    st.markdown("---")

    # ── My Account ─────────────────────────────────────────
    st.subheader("My Account")
    with st.expander("🔑 Reset My Password"):
        st.caption(
            "Clears your own stored password — you'll set a new one the next time you log in. "
            "You'll stay logged in for this session."
        )
        if st.button("Reset My Password", key="btn_reset_own_pw"):
            try:
                clear_password(st.session_state["resident"])
                st.success("✅ Your password has been cleared. You'll set a new one next time you log in.")
            except ConnectionError as exc:
                show_gs_error(exc)

    st.markdown("---")

    # ── Attendings ───────────────────────────────────────
    st.subheader("Attendings")
    try:
        attendings = _read_attendings_df()
        spec_df, _, _, _ = load_refs()
        st.dataframe(attendings, width="stretch")

        with st.expander("➕ Add Attending"):
            new_att_name  = st.text_input("Attending name")
            new_att_spec  = st.selectbox("Specialty", spec_df["specialty_name"], key="add_att_spec")
            new_att_email = st.text_input(
                "Email (optional)",
                help="Set this to give the attending their own login — they'll see only "
                     "the residents in their specialty, and can start a blank assessment "
                     "or view a resident's dashboard directly (no magic link needed).",
            )
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
            with st.expander("✏️ Edit Attending / Assign Login"):
                st.caption(
                    "Give an existing attending an email here to designate their account "
                    "as an attending login — they'll then be able to log in themselves "
                    "(same Log In screen as residents) and get their own Attending Home, "
                    "Start Assessment, and Resident Dashboard."
                )
                edit_att_name = st.selectbox(
                    "Select attending to edit",
                    sorted(attendings["attending_name"], key=lambda n: n.split()[-1] if n.split() else n),
                    key="edit_att_select",
                )
                _edit_row = attendings[attendings["attending_name"] == edit_att_name].iloc[0]
                _edit_spec_name_match = spec_df.loc[
                    spec_df["specialty_id"] == _edit_row["specialty_id"], "specialty_name"
                ]
                _edit_spec_default = _edit_spec_name_match.values[0] if len(_edit_spec_name_match) else None
                _edit_spec_options = list(spec_df["specialty_name"])
                _edit_spec_idx = (
                    _edit_spec_options.index(_edit_spec_default)
                    if _edit_spec_default in _edit_spec_options else 0
                )
                # Keyed by attending_id, not a fixed key — same fix as Edit
                # Existing Procedure's name field: a fixed key means Streamlit
                # keeps showing whatever was last TYPED here for a DIFFERENT
                # attending once these widgets have rendered once, ignoring
                # value=/index= on every later rerun — switching "Select
                # attending to edit" alone didn't reset them, so stale values
                # from whichever attending was edited previously silently
                # carried over and could get saved onto this one instead.
                _sel_att_id = _edit_row["attending_id"]
                edit_att_name_new = st.text_input(
                    "Attending name", value=str(_edit_row["attending_name"]), key=f"edit_att_name_{_sel_att_id}"
                )
                edit_att_spec = st.selectbox(
                    "Specialty", _edit_spec_options, index=_edit_spec_idx, key=f"edit_att_spec_{_sel_att_id}"
                )
                edit_att_email = st.text_input(
                    "Email (blank = no login)",
                    value="" if pd.isna(_edit_row["email"]) else str(_edit_row["email"]),
                    key=f"edit_att_email_{_sel_att_id}",
                )
                if st.button("Save Changes", key="btn_edit_att"):
                    if not edit_att_name_new.strip():
                        st.error("Please enter an attending name.")
                    else:
                        _spec_match = spec_df[spec_df["specialty_name"].astype(str).str.strip() == str(edit_att_spec).strip()]
                        _new_spec_id = _spec_match["specialty_id"].values[0] if len(_spec_match) > 0 else None
                        _old_email = "" if pd.isna(_edit_row["email"]) else str(_edit_row["email"]).strip()
                        _new_email = edit_att_email.strip()
                        updated = attendings.copy()
                        _mask = updated["attending_name"] == edit_att_name
                        updated.loc[_mask, "attending_name"] = edit_att_name_new.strip()
                        updated.loc[_mask, "specialty_id"]   = _new_spec_id
                        updated.loc[_mask, "email"]          = _new_email
                        write_sheet_df(SHEET_ATTENDINGS, updated)
                        # Changing/removing the email invalidates any password stored
                        # under the old one — it would otherwise be an orphaned,
                        # unreachable credential.
                        if _old_email and _old_email.lower() != _new_email.lower():
                            clear_password(_old_email)
                        st.success(f"✅ Saved {edit_att_name_new.strip()}")
                        time.sleep(0.5)
                        st.rerun()

            with st.expander("🔑 Reset Attending Password"):
                st.caption("Clears their stored password — their next login will prompt them to set a new one.")
                _att_with_email = attendings[attendings["email"].fillna("").astype(str).str.strip() != ""]
                if _att_with_email.empty:
                    st.caption("_No attendings have a login email set yet — add one under Edit Attending above._")
                else:
                    reset_att = st.selectbox(
                        "Select attending",
                        sorted(_att_with_email["attending_name"], key=lambda n: n.split()[-1] if n.split() else n),
                        key="reset_att_pw",
                    )
                    if st.button("Reset Password", key="btn_reset_att_pw"):
                        _reset_email = _att_with_email.loc[
                            _att_with_email["attending_name"] == reset_att, "email"
                        ].values[0]
                        clear_password(str(_reset_email))
                        st.success(f"✅ Password cleared for {reset_att}")
                        time.sleep(0.5)
                        st.rerun()

            with st.expander("🗑️ Delete Attending"):
                del_att = st.selectbox(
                    "Select attending to delete",
                    sorted(attendings["attending_name"], key=lambda n: n.split()[-1] if n.split() else n),
                    key="del_att",
                )
                if st.button("Delete", key="btn_del_att"):
                    _del_row = attendings[attendings["attending_name"] == del_att].iloc[0]
                    _del_email = "" if pd.isna(_del_row["email"]) else str(_del_row["email"]).strip()
                    updated = attendings[attendings["attending_name"] != del_att].reset_index(drop=True)
                    write_sheet_df(SHEET_ATTENDINGS, updated)
                    if _del_email:
                        clear_password(_del_email)
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

            # Steps come from the existing catalog only, picked one by
            # one — not typed free text. A step that doesn't exist yet
            # anywhere has to be created first with ➕ Add Step below,
            # then picked here (or added afterward via Edit Existing
            # Procedure's own picker).
            _addproc_candidates = _list_existing_steps()
            if _addproc_candidates.empty:
                st.caption("No existing steps yet — create some with ➕ Add Step below first.")
                new_step_ids = []
            else:
                def _addproc_step_label(sid):
                    return _addproc_candidates.loc[_addproc_candidates["step_id"] == sid, "step_name"].values[0]

                new_step_ids = st.multiselect(
                    "Steps (pick existing steps, in order)",
                    _addproc_candidates["step_id"].tolist(),
                    format_func=_addproc_step_label,
                    key="add_proc_steps_ms",
                )
            if st.button("Add Procedure", key="btn_add_proc"):
                if new_proc_id and new_proc_name and new_step_ids:
                    _spec_match = spec_df[spec_df["specialty_name"].astype(str).str.strip() == str(new_proc_spec).strip()]
                    spec_id = _spec_match["specialty_id"].values[0] if len(_spec_match) > 0 else None
                    # Each pick promoted to a shared id first (a no-op if
                    # it already is one) — same as attaching an existing
                    # step anywhere else, so this procedure's copy
                    # connects to the same ratings as every other one.
                    _resolved_steps = [
                        (_resolve_step_id_for_attach(sid), _addproc_step_label(sid)) for sid in new_step_ids
                    ]
                    ensure_procedure(new_proc_id, new_proc_name, spec_id, _resolved_steps)
                    st.success(f"✅ Added {new_proc_name}")
                    time.sleep(0.5)
                    st.rerun()
                else:
                    st.error("Please fill in all fields and pick at least one step.")

        with st.expander("➕ Add Step"):
            st.caption(
                "Creates a brand-new step that doesn't exist anywhere yet "
                "— not attached to any procedure. Attach it afterward "
                "using the existing-steps picker in Add New Procedure "
                "above or Edit Existing Procedure below."
            )
            _new_step_name = st.text_input("New step name", key="new_step_name_input")
            if st.button("Add Step", key="btn_add_new_step"):
                if not _new_step_name.strip():
                    st.error("Please enter a step name.")
                else:
                    try:
                        _create_new_step(_new_step_name.strip())
                        st.success(f'✅ Created "{_new_step_name.strip()}"')
                        time.sleep(0.5)
                        st.rerun()
                    except ValueError as _create_exc:
                        st.error(f"⚠️ {_create_exc}")

        with st.expander("✏️ Edit Existing Procedure"):
            procs_df = read_sheet_df(SHEET_PROCEDURES, expected_cols=["procedure_id", "procedure_name", "specialty_id"])
            if procs_df.empty:
                st.info("No procedures yet.")
            else:
                edit_proc    = st.selectbox("Select procedure", procs_df["procedure_name"], key="edit_proc_sel")
                _proc_match = procs_df[procs_df["procedure_name"].astype(str).str.strip() == str(edit_proc).strip()]
                sel_proc_id  = _proc_match["procedure_id"].values[0] if len(_proc_match) > 0 else None
                # Keyed by sel_proc_id, not a fixed key — this is what
                # actually let "Robotic Bedsiding" get renamed to
                # "Laparoscopic Appendectomy": a fixed key means
                # Streamlit keeps showing whatever was last TYPED here
                # for a DIFFERENT procedure once this widget has
                # rendered once, ignoring value= on every later rerun —
                # switching "Select procedure" alone didn't reset it, so
                # a stale name from whichever procedure was edited
                # previously silently carried over and got saved onto
                # this one instead.
                new_pname    = st.text_input("Updated name", value=edit_proc, key=f"edit_proc_name_{sel_proc_id}")

                # Pre-filled, per-step editor rather than retyping the whole
                # step list as plain text: renaming or reordering a step no
                # longer regenerates its step_id (which used to bake in its
                # position, e.g. S_CIRC_01/02/03 — so ANY edit anywhere in
                # the list shifted every step after it onto a new ID, silently
                # cutting that step's historical ratings loose from it). Order
                # is a plain editable number rather than relying on drag-to-
                # reorder (data_editor doesn't support that) — retype a row's
                # Order to move it anywhere, including fractional values like
                # 2.5 to insert a new step between steps 2 and 3.
                _all_steps_df   = read_sheet_df(
                    SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"]
                )
                _proc_steps_df  = _all_steps_df[_all_steps_df["procedure_id"] == sel_proc_id].sort_values("step_order")
                _existing_step_ids = set(_proc_steps_df["step_id"])
                _steps_editor_df = pd.DataFrame({
                    "step_id": _proc_steps_df["step_id"].tolist(),
                    # float, not int — a plain range() here gives the
                    # column an integer dtype, which st.data_editor's
                    # NumberColumn then renders as an integer-only
                    # editor (no decimal point typeable at all), even
                    # though the column_config below asks for fractional
                    # values.
                    "Order":   [float(i) for i in range(1, len(_proc_steps_df) + 1)],
                    "Step":    _proc_steps_df["step_name"].tolist(),
                })
                st.caption(
                    "Rename or reorder existing steps — their rating history stays "
                    "linked either way. To add or delete a step, use the pickers "
                    "below instead of typing a row here or removing one — a "
                    "brand-new step name isn't allowed here at all (create it "
                    "first with ➕ Add Step above), and deleting needs to show "
                    "how many ratings it would take with it first."
                )
                _edited_steps_df = st.data_editor(
                    _steps_editor_df,
                    column_config={
                        "step_id": None,  # identity only — never shown or hand-edited
                        "Order": st.column_config.NumberColumn(
                            "Order", help="Position in the sequence. Fractional values are fine.",
                            step=0.5,
                        ),
                        "Step": st.column_config.TextColumn("Step", required=True),
                    },
                    # "fixed", not "dynamic" — adding/deleting a row here
                    # bypassed both the existing-steps-only rule (a typed
                    # new row) and the rating-count warning a real delete
                    # needs (a silently dropped row never told you what
                    # it was about to take with it). Both are now their
                    # own dedicated, checked actions below instead; this
                    # table is rename/reorder only.
                    num_rows="fixed",
                    hide_index=True,
                    width="stretch",
                    key=f"edit_proc_steps_editor_{sel_proc_id}",
                )

                # A pending rename needs an explicit, named confirmation
                # before it's allowed to save — exactly the check that
                # would have caught "Robotic Bedsiding" silently being
                # renamed to "Laparoscopic Appendectomy" (a stale
                # leftover value from editing a different procedure
                # earlier, saved without anyone noticing the name had
                # changed at all).
                _pending_rename = new_pname.strip() != str(edit_proc).strip()
                _rename_confirmed = True
                if _pending_rename:
                    _rename_confirmed = st.checkbox(
                        f'Yes, rename "{edit_proc}" to "{new_pname.strip()}"',
                        key=f"confirm_rename_{sel_proc_id}",
                    )
                    st.caption("⚠️ This changes the procedure's name everywhere it's shown. Check the box above to confirm.")

                if st.button("Update Procedure", key="btn_upd_proc"):
                    # Validated up front, before anything is written: an
                    # empty step list, a lost procedure identity, an
                    # unconfirmed rename, a name collision with another
                    # procedure, or a sign this update would touch other
                    # procedures' steps all bail out with nothing saved
                    # at all, rather than partially writing one half of
                    # the update and not the other.
                    _clean_steps = _edited_steps_df.dropna(subset=["Step"]).copy()
                    _clean_steps = _clean_steps[_clean_steps["Step"].astype(str).str.strip() != ""]
                    _name_collision = procs_df[
                        (procs_df["procedure_id"] != sel_proc_id)
                        & (procs_df["procedure_name"].astype(str).str.strip().str.lower() == new_pname.strip().lower())
                    ]
                    if _clean_steps.empty:
                        st.error("A procedure needs at least one step — add one before updating.")
                    elif _pending_rename and not _rename_confirmed:
                        st.error(
                            f'Please check the confirmation box above before renaming '
                            f'"{edit_proc}" to "{new_pname.strip()}".'
                        )
                    elif _pending_rename and not _name_collision.empty:
                        st.error(f'"{new_pname.strip()}" is already the name of another procedure — choose a different name.')
                    elif not sel_proc_id:
                        st.error("Could not identify which procedure to update — please reload and try again.")
                    else:
                        # Stable sort: rows with no Order (a just-added row
                        # left blank) fall to the end rather than the top.
                        _clean_steps["Order"] = pd.to_numeric(_clean_steps["Order"], errors="coerce")
                        _blank_order_fill = (
                            _clean_steps["Order"].max() + 1 if _clean_steps["Order"].notna().any() else 1
                        )
                        _clean_steps["Order"] = _clean_steps["Order"].fillna(_blank_order_fill)
                        _clean_steps = _clean_steps.sort_values("Order", kind="stable").reset_index(drop=True)

                        _new_step_rows = []
                        for i, _row in _clean_steps.iterrows():
                            # num_rows="fixed" above guarantees every row's
                            # step_id is one of this procedure's own
                            # existing ones — no new row could ever appear
                            # here, so no fallback id-minting is needed.
                            _sid = _row["step_id"]
                            _new_step_rows.append({
                                "step_id":      _sid,
                                "procedure_id": sel_proc_id,
                                "step_order":   i + 1,
                                "step_name":    str(_row["Step"]).strip(),
                            })
                        updated_steps = pd.DataFrame(_new_step_rows)
                        _other_procs_steps = _all_steps_df[_all_steps_df["procedure_id"] != sel_proc_id]
                        # Guardrail: this update should only ever touch
                        # sel_proc_id's own steps. If filtering it out
                        # somehow also dropped other procedures' steps
                        # (a type-mismatch on procedure_id, a bug here or
                        # upstream), _other_procs_steps would come out
                        # short — refuse rather than silently wiping
                        # steps that were never meant to be touched.
                        _expected_other_count = len(_all_steps_df) - len(_proc_steps_df)
                        if len(_other_procs_steps) != _expected_other_count:
                            st.error(
                                "⚠️ Safety check failed: this update would have changed steps "
                                "belonging to other procedures too. Nothing was saved — please "
                                "reload and try again, or contact support if this keeps happening."
                            )
                        else:
                            # Steps are allowed to shrink here — deleting
                            # a row is how an admin removes a step, and
                            # the _other_procs_steps check above already
                            # guarantees this write only ever touches
                            # sel_proc_id's own rows. The procedure list
                            # itself should never shrink from this
                            # flow (there's no "delete a procedure" here
                            # at all) — write_sheet_df_no_shrink refuses
                            # if it somehow would.
                            steps_df = pd.concat([_other_procs_steps, updated_steps], ignore_index=True)
                            write_sheet_df(SHEET_STEPS, steps_df)
                            try:
                                procs_df.loc[procs_df["procedure_id"] == sel_proc_id, "procedure_name"] = new_pname
                                write_sheet_df_no_shrink(SHEET_PROCEDURES, procs_df)
                            except ValueError as _guard_exc:
                                st.error(f"⚠️ {_guard_exc}")
                            else:
                                st.success(f"✅ Updated '{new_pname}'")
                                time.sleep(0.5)
                                st.rerun()

                st.markdown("---")
                st.markdown("**Add an existing step to this procedure**")
                _epadd_candidates = _list_existing_steps(exclude_procedure_id=sel_proc_id)
                if _epadd_candidates.empty:
                    st.caption("Every existing step is already on this procedure (or none exist yet — create one with ➕ Add Step above).")
                else:
                    def _epadd_label(sid):
                        return _epadd_candidates.loc[_epadd_candidates["step_id"] == sid, "step_name"].values[0]

                    _epadd_choice = st.selectbox(
                        "Existing step", _epadd_candidates["step_id"].tolist(),
                        format_func=_epadd_label, key=f"ep_add_step_choice_{sel_proc_id}",
                    )
                    _epadd_default_order = (
                        float(_proc_steps_df["step_order"].max()) + 1 if not _proc_steps_df.empty else 1.0
                    )
                    _epadd_order = st.number_input(
                        "Order", value=_epadd_default_order, step=0.5,
                        help="Position in this procedure's sequence. Fractional values are fine.",
                        key=f"ep_add_step_order_{sel_proc_id}_{_epadd_choice}",
                    )
                    if st.button("Add Step", key="btn_ep_add_step"):
                        try:
                            _attach_existing_step(_epadd_choice, sel_proc_id, _epadd_order)
                            st.success(f'✅ Added "{_epadd_label(_epadd_choice)}"')
                            time.sleep(0.5)
                            st.rerun()
                        except ValueError as _epadd_exc:
                            st.error(f"⚠️ {_epadd_exc}")

                st.markdown("---")
                st.markdown("**Delete a step from this procedure**")
                if _proc_steps_df.empty:
                    st.caption("No steps to delete.")
                else:
                    _epdel_choice_name = st.selectbox(
                        "Step to delete", _proc_steps_df["step_name"].tolist(),
                        key=f"ep_del_step_choice_{sel_proc_id}",
                    )
                    _epdel_step_id = _proc_steps_df.loc[
                        _proc_steps_df["step_name"] == _epdel_choice_name, "step_id"
                    ].values[0]
                    _epdel_rating_count = _count_step_ratings(_epdel_step_id, sel_proc_id)

                    _epdel_confirmed = True
                    if _epdel_rating_count > 0:
                        st.warning(
                            f"⚠️ {_epdel_rating_count} existing case rating(s) use this step "
                            f'under "{edit_proc}". Deleting it can also delete those '
                            "ratings — this can't be undone."
                        )
                        _epdel_confirmed = st.checkbox(
                            f'Yes, delete "{_epdel_choice_name}" and its {_epdel_rating_count} rating(s)',
                            key=f"confirm_ep_del_step_{sel_proc_id}_{_epdel_step_id}",
                        )

                    if st.button("Delete Step", key="btn_ep_del_step"):
                        if not _epdel_confirmed:
                            st.error("Please check the confirmation box above before deleting.")
                        else:
                            try:
                                _epdel_n = _delete_step(_epdel_step_id, sel_proc_id, delete_ratings=True)
                                st.success(
                                    f'✅ Deleted "{_epdel_choice_name}"'
                                    + (f" and {_epdel_n} rating(s)" if _epdel_n else "")
                                )
                                time.sleep(0.5)
                                st.rerun()
                            except ValueError as _epdel_exc:
                                st.error(f"⚠️ {_epdel_exc}")

        with st.expander("🗑️ Delete Procedure"):
            st.caption(
                "Removes a procedure entirely — its own steps too. A step "
                "shared with other procedures (see Merge Shared Steps/Add "
                "Step below) keeps its link to those other procedures; "
                "only this procedure's own copy is removed."
            )
            if procs_df.empty:
                st.caption("No procedures yet.")
            else:
                _del_proc_name = st.selectbox(
                    "Procedure", procs_df["procedure_name"], key="del_proc_sel"
                )
                _del_proc_id = procs_df.loc[
                    procs_df["procedure_name"] == _del_proc_name, "procedure_id"
                ].values[0]
                _del_proc_steps_df = read_sheet_df(
                    SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"]
                )
                _del_proc_steps = _del_proc_steps_df[
                    _del_proc_steps_df["procedure_id"] == _del_proc_id
                ].sort_values("step_order")
                _del_proc_case_count = _count_procedure_cases(_del_proc_id)

                st.markdown(f"**Procedure:** {_del_proc_name} ({_del_proc_id})")
                st.markdown("**Steps:**")
                if _del_proc_steps.empty:
                    st.caption("No steps.")
                else:
                    st.dataframe(
                        pd.DataFrame({"Step": _del_proc_steps["step_name"].tolist()}),
                        width="stretch", hide_index=True,
                    )
                st.markdown(f"**Entries (cases) recorded:** {_del_proc_case_count}")

                _del_proc_confirmed = True
                if _del_proc_case_count > 0:
                    st.warning(
                        f"⚠️ {_del_proc_case_count} case(s) have been recorded for "
                        f'"{_del_proc_name}". Deleting it will also delete those '
                        "cases and every rating in them — this can't be undone."
                    )
                    _del_proc_confirmed = st.checkbox(
                        f'Yes, delete "{_del_proc_name}" and its {_del_proc_case_count} case(s)',
                        key=f"confirm_del_proc_{_del_proc_id}",
                    )

                if st.button("Delete Procedure", key="btn_del_proc"):
                    if not _del_proc_confirmed:
                        st.error("Please check the confirmation box above before deleting.")
                    else:
                        _del_proc_result = _delete_procedure(_del_proc_id, delete_cases=True)
                        _del_proc_extra = (
                            f", {_del_proc_result['cases']} case(s), {_del_proc_result['scores']} rating(s)"
                            if _del_proc_result["cases"] else ""
                        )
                        st.success(
                            f'✅ Deleted "{_del_proc_name}" ({_del_proc_result["steps"]} step(s)'
                            f'{_del_proc_extra})'
                        )
                        time.sleep(0.5)
                        st.rerun()

        with st.expander("🗑️ Delete Step"):
            st.caption(
                "Removes one step from one procedure. If that step's id is "
                "shared across multiple procedures (see Merge Shared Steps "
                "below), only this procedure's link to it is removed — "
                "other procedures using the same shared step keep it."
            )
            if procs_df.empty:
                st.caption("No procedures yet.")
            else:
                _del_step_proc_name = st.selectbox(
                    "Procedure", procs_df["procedure_name"], key="del_step_proc_sel"
                )
                _del_step_proc_id = procs_df.loc[
                    procs_df["procedure_name"] == _del_step_proc_name, "procedure_id"
                ].values[0]
                _del_step_all_steps_df = read_sheet_df(
                    SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"]
                )
                _del_step_proc_steps = _del_step_all_steps_df[
                    _del_step_all_steps_df["procedure_id"] == _del_step_proc_id
                ].sort_values("step_order")
                if _del_step_proc_steps.empty:
                    st.caption("This procedure has no steps.")
                else:
                    _del_step_name = st.selectbox(
                        "Step", _del_step_proc_steps["step_name"], key="del_step_sel"
                    )
                    _del_step_id = _del_step_proc_steps.loc[
                        _del_step_proc_steps["step_name"] == _del_step_name, "step_id"
                    ].values[0]
                    _del_step_rating_count = _count_step_ratings(_del_step_id, _del_step_proc_id)

                    _del_step_confirmed = True
                    if _del_step_rating_count > 0:
                        st.warning(
                            f"⚠️ {_del_step_rating_count} existing case rating(s) use this step "
                            f'under "{_del_step_proc_name}". Deleting it can also delete those '
                            "ratings — this can't be undone."
                        )
                        _del_step_confirmed = st.checkbox(
                            f'Yes, delete "{_del_step_name}" and its {_del_step_rating_count} rating(s)',
                            key=f"confirm_del_step_{_del_step_id}_{_del_step_proc_id}",
                        )

                    if st.button("Delete Step", key="btn_del_step"):
                        if not _del_step_confirmed:
                            st.error("Please check the confirmation box above before deleting.")
                        else:
                            try:
                                _n_del = _delete_step(_del_step_id, _del_step_proc_id, delete_ratings=True)
                                st.success(
                                    f'✅ Deleted "{_del_step_name}"'
                                    + (f" and {_n_del} rating(s)" if _n_del else "")
                                )
                                time.sleep(0.5)
                                st.rerun()
                            except ValueError as _del_exc:
                                st.error(f"⚠️ {_del_exc}")

        # Read-only lookup, deliberately separate from the editor above —
        # for spotting a step name (e.g. "Case Preparation") reused, or
        # near-duplicated, across procedures before deciding which ones to
        # go edit. Doesn't touch any data.
        with st.expander("🔍 Find Steps by Name"):
            st.caption(
                "Pick one or more step names — each exact name that appears "
                "anywhere shows every procedure that has it, so you can spot "
                "one reused (or near-duplicated, e.g. \"Case Preparation\" vs. "
                "\"Daily Preparation\") across procedures before deciding which "
                "to go edit or delete."
            )
            _search_steps_df = read_sheet_df(
                SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"]
            )
            _all_step_names = sorted(
                _search_steps_df["step_name"].dropna().astype(str).str.strip().unique().tolist(),
                key=str.lower,
            )
            _search_terms = st.multiselect(
                "Step name(s) to search", _all_step_names, key="step_search_terms",
            )
            if _search_terms:
                _proc_name_lookup = dict(zip(procs_df["procedure_id"], procs_df["procedure_name"]))
                for term in _search_terms:
                    _hits = _search_steps_df[
                        _search_steps_df["step_name"].astype(str).str.strip() == term
                    ].sort_values(["procedure_id", "step_order"])
                    st.markdown(f"**\"{term}\"** — {len(_hits)} match{'es' if len(_hits) != 1 else ''}")
                    _hits_display = pd.DataFrame({
                        "Procedure": _hits["procedure_id"].map(_proc_name_lookup).fillna(_hits["procedure_id"]),
                        "Step":      _hits["step_name"],
                        "Order":     _hits["step_order"],
                    })
                    st.dataframe(_hits_display, width="stretch", hide_index=True)

        # Unlike "Find Steps by Name" above (read-only), this one writes:
        # it repoints steps/scores rows so a step shared across
        # procedures ends up under one step_id instead of one per
        # procedure. See _find_step_merge_candidates()/_apply_step_merge()
        # for the full design — nothing here is ever deleted, only
        # relabeled/relinked, and every merge needs an explicit checked
        # confirmation naming exactly what it's about to do.
        with st.expander("🔗 Merge Shared Steps"):
            st.caption(
                "Finds steps that look like the same real step (e.g. "
                "\"Patient Positioning\") recorded separately, with their "
                "own step_id, under two or more procedures — and lets you "
                "give them one shared step_id and one label, so a "
                "resident's ratings for that step connect up the same way "
                "no matter which procedure it was part of. Existing "
                "ratings are relinked, never lost. Similarity-based "
                "suggestions (marked \"possible match\") are a starting "
                "point only, not a promise — always check both sides "
                "before merging."
            )
            _merge_steps_df = read_sheet_df(
                SHEET_STEPS, expected_cols=["step_id", "procedure_id", "step_order", "step_name"]
            )
            _merge_proc_lookup = dict(zip(procs_df["procedure_id"], procs_df["procedure_name"]))
            _merge_candidates = _find_step_merge_candidates(_merge_steps_df)

            if not _merge_candidates:
                st.caption("No candidates found — every same-named step across procedures already shares one id.")
            else:
                # A merge changes how many candidates there are, so a
                # stale index left over from before a rerun can fall
                # outside the new range — reset it before the widget
                # renders rather than letting st.selectbox raise on an
                # out-of-range default (same guard used for the
                # Comments Dashboard's Procedure/Attending filters).
                # isinstance check first: an *already-open* browser tab
                # can keep session_state alive across a redeploy (the
                # server reruns the script but doesn't reset it), so a
                # value from a since-changed earlier version of this key
                # can persist here with the wrong type entirely — a bare
                # `>=` against that raises TypeError (str vs int)
                # instead of ever reaching a rerun that would fix it.
                _stored_step_merge_sel = st.session_state.get("step_merge_candidate_sel", 0)
                if not isinstance(_stored_step_merge_sel, int) or _stored_step_merge_sel >= len(_merge_candidates):
                    st.session_state["step_merge_candidate_sel"] = 0

                def _step_merge_candidate_label(i):
                    c = _merge_candidates[i]
                    _names = ", ".join(sorted(set(c["rows"]["step_name"])))
                    if c["kind"] == "exact":
                        _kind = "exact match"
                    elif c.get("opposite_term"):
                        _kind = f"⚠️ {c['score']:.0%} similar, but differs by e.g. left/right — usually distinct"
                    else:
                        _kind = f"possible match, {c['score']:.0%} similar"
                    return f'{_names} — {len(c["rows"])} procedures ({_kind})'

                _sel_idx = st.selectbox(
                    "Candidate", range(len(_merge_candidates)),
                    format_func=_step_merge_candidate_label, key="step_merge_candidate_sel",
                )
                _candidate  = _merge_candidates[_sel_idx]
                _cand_rows  = _candidate["rows"].copy()
                _cand_rows["Procedure"] = _cand_rows["procedure_id"].map(_merge_proc_lookup).fillna(_cand_rows["procedure_id"])
                _cand_rows.insert(0, "Include", True)

                # Keyed by this candidate's own step_ids, not _sel_idx — a
                # merge/delete elsewhere changes how many candidates there
                # are, which can shift a *different* candidate into the
                # position _sel_idx used to point to. A position-based key
                # would then show whatever was last typed/toggled for
                # WHATEVER candidate used to be at that position, on the
                # new one now shown there — same fix already applied to
                # Edit Existing Procedure/Attending/Resident: key by
                # content identity, not list position.
                _cand_key = "_".join(sorted(_cand_rows["step_id"].astype(str)))

                _edited_rows = st.data_editor(
                    _cand_rows[["Include", "Procedure", "step_name", "step_id"]].rename(
                        columns={"step_name": "Step name"}
                    ),
                    column_config={
                        "Include": st.column_config.CheckboxColumn(
                            "Include", help="Uncheck to leave this one out of the merge.",
                        ),
                        "step_id": None,  # identity only — never shown or hand-edited
                    },
                    disabled=["Procedure", "Step name"],
                    hide_index=True,
                    width="stretch",
                    key=f"step_merge_editor_{_cand_key}",
                )
                _included_rows = _cand_rows[_edited_rows["Include"].tolist()]

                _canonical_label = st.text_input(
                    "Shared label for this step", value=_candidate["label"],
                    key=f"step_merge_label_{_cand_key}",
                )

                if len(_included_rows) < 2:
                    st.caption("Select at least 2 rows to merge.")
                else:
                    _merge_scores_df = read_sheet_df(
                        SHEET_SCORES,
                        expected_cols=["case_id", "step_id", "rating", "rating_num",
                                       "case_complexity", "case_preparation", "overall_performance"],
                    )
                    _affected_scores = _merge_scores_df[_merge_scores_df["step_id"].isin(_included_rows["step_id"])]
                    st.caption(
                        f"This will merge {len(_included_rows)} steps across "
                        f"{_included_rows['procedure_id'].nunique()} procedures into one shared step, "
                        f"repointing {len(_affected_scores)} existing case rating(s) onto it. Nothing is deleted."
                    )
                    _confirm_merge = st.checkbox(
                        f'Yes, merge these into "{_canonical_label.strip()}"',
                        key=f"confirm_step_merge_{_cand_key}",
                    )
                    if st.button("Merge Steps", key="btn_merge_steps"):
                        if not _canonical_label.strip():
                            st.error("Please enter a label for the merged step.")
                        elif not _confirm_merge:
                            st.error("Please check the confirmation box above before merging.")
                        elif _included_rows["procedure_id"].duplicated().any():
                            st.error("Two selected rows belong to the same procedure — please reload and try again.")
                        else:
                            try:
                                _new_id = _apply_step_merge(_included_rows, _canonical_label.strip())
                                st.success(f'✅ Merged into "{_canonical_label.strip()}" ({_new_id})')
                                time.sleep(0.5)
                                st.rerun()
                            except ValueError as _merge_exc:
                                st.error(f"⚠️ {_merge_exc}")

                st.markdown("---")
                st.caption(
                    "Or, if one of these isn't actually a match — delete it "
                    "outright instead of merging it (or leaving it as-is)."
                )
                _del_cand_idx = st.selectbox(
                    "Delete a row from this candidate",
                    range(len(_cand_rows)),
                    format_func=lambda i: f'{_cand_rows.iloc[i]["Procedure"]} — {_cand_rows.iloc[i]["step_name"]}',
                    key=f"step_merge_delete_sel_{_cand_key}",
                )
                _del_cand_row = _cand_rows.iloc[_del_cand_idx]
                _del_cand_rating_count = _count_step_ratings(_del_cand_row["step_id"], _del_cand_row["procedure_id"])

                _del_cand_confirmed = True
                if _del_cand_rating_count > 0:
                    st.warning(
                        f"⚠️ {_del_cand_rating_count} existing case rating(s) use this step "
                        f'under "{_del_cand_row["Procedure"]}". Deleting it can also delete '
                        "those ratings — this can't be undone."
                    )
                    _del_cand_confirmed = st.checkbox(
                        f'Yes, delete "{_del_cand_row["step_name"]}" ({_del_cand_row["Procedure"]}) '
                        f"and its {_del_cand_rating_count} rating(s)",
                        # Keyed by the row's own step_id, not _del_cand_idx
                        # (also just a list position) — same reasoning as
                        # _cand_key above.
                        key=f"confirm_step_merge_delete_{_del_cand_row['step_id']}_{_del_cand_row['procedure_id']}",
                    )

                if st.button("Delete This Step", key="btn_step_merge_delete"):
                    if not _del_cand_confirmed:
                        st.error("Please check the confirmation box above before deleting.")
                    else:
                        try:
                            _n_del = _delete_step(
                                _del_cand_row["step_id"], _del_cand_row["procedure_id"], delete_ratings=True
                            )
                            st.success(
                                f'✅ Deleted "{_del_cand_row["step_name"]}" ({_del_cand_row["Procedure"]})'
                                + (f" and {_n_del} rating(s)" if _n_del else "")
                            )
                            time.sleep(0.5)
                            st.rerun()
                        except ValueError as _del_exc:
                            st.error(f"⚠️ {_del_exc}")
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
    mobile_tip("📱 On mobile: tap the >> icon at top left to access navigation and rating legend.")
    # tier_text excludes the resident's name from _header_max()'s length
    # tier so a long name doesn't needlessly drop the whole header into a
    # smaller ceiling; header_break_before() also protects every other
    # space so that if this does wrap, it can only break right after the
    # comma, never mid-name.
    page_header(
        header_break_before("👋 Welcome back,", st.session_state["resident_name"]),
        tier_text="👋 Welcome back,",
    )

    # "New evaluation" badge: which attending-confirmed cases have
    # landed since this resident's last Home visit, each linked to its
    # own read-only view (view_evaluation page), which is what actually
    # drops that one off the list (mark_evaluation_viewed(), called from
    # that page) — merely showing the badge here does NOT clear
    # anything, so it stays put across routine revisits/reruns instead
    # of vanishing before it's actually been read. The one-time
    # exception is a resident who's never had a last-seen timestamp at
    # all yet (mark_resident_evaluations_seen() bootstraps it to now) —
    # without that, every historical case would show as "new" forever.
    _resident_email = st.session_state["resident"]
    try:
        if get_resident_last_seen(_resident_email) is None:
            mark_resident_evaluations_seen(_resident_email)
        _new_evals = get_new_evaluations_for_resident(_resident_email)
        if not _new_evals.empty:
            _n = len(_new_evals)
            st.success(f"🔔 You have {_n} new evaluation{'s' if _n != 1 else ''} waiting to be viewed!")
            _, _home_proc_df, _, _home_atnd_df = load_refs()
            _home_proc_names = {str(k): v for k, v in zip(_home_proc_df["procedure_id"], _home_proc_df["procedure_name"])}
            _home_atnd_lookup = dict(zip(_home_atnd_df["attending_id"], _home_atnd_df["attending_name"]))
            for _, _eval_row in _new_evals.iterrows():
                _proc_label = _home_proc_names.get(str(_eval_row["procedure_id"]), str(_eval_row["procedure_id"]))
                _att_label = attending_display_name(str(_eval_row["attending_id"]), _home_atnd_lookup)
                if st.button(
                    f"📄 {_proc_label} — {_att_label} ({fmt_date(_eval_row['date'])})",
                    key=f"view_new_eval_{_eval_row['case_id']}",
                    width="stretch",
                ):
                    st.session_state["viewing_case_id"] = _eval_row["case_id"]
                    st.session_state["viewing_case_return_page"] = "home"
                    go_to("view_evaluation")
            st.markdown("")
    except ConnectionError:
        pass  # badge is a nice-to-have — don't block the Home page over it

    # "Self-evaluation requested" badge: attending-initiated requests
    # (see attending_start's "Create Magic Link Request for Resident
    # Self-Evaluation" button) for this resident, not yet fulfilled —
    # shown here in addition to whatever link the attending sent
    # directly, so this can be started even without that link. A
    # request's mere presence means "still pending"; completing the
    # self-assessment (either from here or from the link) is what
    # actually removes it (delete_self_eval_request()), so it can only
    # ever be fulfilled once — same cross-reference idea as the
    # attending Home page's own "pending self-evaluation" badge.
    try:
        _pending_requests = read_sheet_df(SHEET_SELF_EVAL_REQUESTS, expected_cols=SELF_EVAL_REQUEST_COLS)
        _pending_requests = _pending_requests[
            _pending_requests["resident_email"].astype(str).str.strip().str.lower()
            == str(_resident_email).strip().lower()
        ]
        if not _pending_requests.empty:
            _n = len(_pending_requests)
            st.info(f"📝 {_n} self-evaluation{'s' if _n != 1 else ''} requested by your attending!")
            _, _home_proc_df2, _, _home_atnd_df2 = load_refs()
            _home_proc_names2 = {str(k): v for k, v in zip(_home_proc_df2["procedure_id"], _home_proc_df2["procedure_name"])}
            _home_atnd_lookup2 = dict(zip(_home_atnd_df2["attending_id"], _home_atnd_df2["attending_name"]))
            _pending_requests = _pending_requests.copy()
            _pending_requests["_created_sort"] = pd.to_datetime(_pending_requests["created_at"], errors="coerce")
            _pending_requests = _pending_requests.sort_values("_created_sort", ascending=False)
            for _, _req_row in _pending_requests.iterrows():
                _proc_label = _home_proc_names2.get(str(_req_row["procedure_id"]), str(_req_row["procedure_id"]))
                _att_label  = attending_display_name(str(_req_row["attending_id"]), _home_atnd_lookup2)
                if st.button(
                    f"📝 {_proc_label} — requested by {_att_label} ({fmt_date(_req_row.get('date'))})",
                    key=f"start_requested_self_eval_{_req_row['request_id']}",
                    width="stretch",
                ):
                    # Same session_state keys the magic link's own
                    # ?mode=resident_self routing sets, just populated
                    # directly instead of via query params.
                    st.session_state["procedure_id"] = _req_row["procedure_id"]
                    st.session_state["specialty_id"] = _req_row["specialty_id"]
                    st.session_state["attending_id"] = _req_row["attending_id"]
                    try:
                        st.session_state["date"] = datetime.date.fromisoformat(str(_req_row["date"]))
                    except (ValueError, TypeError):
                        st.session_state["date"] = datetime.date.today()
                    st.session_state["assessment_mode"]      = "self"
                    st.session_state["self_eval_requested_by_attending"] = True
                    st.session_state["self_eval_request_id"] = _req_row["request_id"]
                    st.session_state["scores"]               = {}
                    st.session_state["notes"]                = ""
                    st.session_state["improve"]              = ""
                    st.session_state["how"]                  = ""
                    st.session_state["generated_magic_link"] = None
                    go_to("assessment")
            st.markdown("")
    except ConnectionError:
        pass  # badge is a nice-to-have — don't block the Home page over it

    st.markdown("_What would you like to do today?_")
    st.markdown("")

    with st.container(key="home_cards"):
        c1, c2, c3, c4 = st.columns(4)
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
                # Reset to unselected so the page always opens on
                # "Choose procedure" rather than remembering the last
                # one picked.
                st.session_state.pop("cumulative_proc_select", None)
                go_to("cumulative")
            st.markdown("</div>", unsafe_allow_html=True)

        with c3:
            st.markdown('<div class="pp-card">', unsafe_allow_html=True)
            st.markdown("### 💬 Comments")
            st.markdown("Browse and export all attending feedback.")
            if st.button("View Comments", width="stretch"):
                go_to("comments")
            st.markdown("</div>", unsafe_allow_html=True)

        with c4:
            st.markdown('<div class="pp-card">', unsafe_allow_html=True)
            st.markdown("### 📜 Evaluation History")
            st.markdown("Browse every evaluation you've received.")
            if st.button("View History", width="stretch", key="home_eval_history_btn"):
                go_to("eval_history")
            st.markdown("</div>", unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════
# PAGE: VIEW ONE EVALUATION. Linked from the resident Home page's
# new-evaluation badge and from either login's Complete Evaluation
# History list. Purely a read-only display — nothing here needs
# saving; the Home badge itself already cleared the moment Home was
# loaded, regardless of whether any individual link below is actually
# opened.
# ════════════════════════════════════════════════════════════
elif page == "view_evaluation":
    _view_eval_is_attending = st.session_state.get("role") == "attending"
    _resident = None if _view_eval_is_attending else st.session_state.get("resident")
    _viewer_attending_id = st.session_state.get("attending_login_id") if _view_eval_is_attending else None
    # Where "Back" returns to: whichever page linked here (Home's badge,
    # or either login's Evaluation History list) sets this before
    # navigating — falls back to each login's own home page for an old
    # link/session that never set it.
    _return_page = st.session_state.get("viewing_case_return_page") or (
        "attending_home" if _view_eval_is_attending else "home"
    )
    _return_label = {
        "home": "⬅️ Back to Home",
        "attending_home": "⬅️ Back to Home",
        "eval_history": "⬅️ Back to Evaluation History",
        "attending_eval_history": "⬅️ Back to Evaluation History",
    }.get(_return_page, "⬅️ Back")

    if not _resident and not _viewer_attending_id:
        st.error("Not logged in.")
        if st.button(_return_label):
            go_to(_return_page)
        st.stop()

    _viewing_case_id = st.session_state.get("viewing_case_id")
    try:
        _viewed_sub = load_case_detail(_viewing_case_id)
    except ConnectionError as exc:
        show_gs_error(exc)
        if st.button(_return_label):
            go_to(_return_page)
        st.stop()

    _owns_it = bool(_viewed_sub) and (
        (_resident and str(_viewed_sub.get("resident_email", "")).strip().lower() == str(_resident).strip().lower())
        or (_viewer_attending_id and str(_viewed_sub.get("attending_id", "")) == str(_viewer_attending_id))
    )
    if not _owns_it:
        # Missing case, or (shouldn't normally happen — every link here
        # is only ever generated for this viewer's own cases) one that
        # belongs to someone else — refuse either way rather than
        # showing someone else's evaluation.
        st.error("Evaluation not found.")
        if st.button(_return_label):
            go_to(_return_page)
        st.stop()

    # Confirmed valid and this resident's own — drops it off the Home
    # page's badge from here on, leaving any other still-unopened new
    # evaluations untouched. Only meaningful for a resident viewing
    # their own evaluation — an attending browsing their own history
    # has no such badge to clear.
    if _resident:
        try:
            mark_evaluation_viewed(_resident, _viewed_sub["case_id"])
        except ConnectionError:
            pass  # already showing them the evaluation either way

    page_header("📄 Evaluation")
    _render_self_assessment_diff(_viewed_sub)
    _render_evaluation_card(_viewed_sub)
    render_rating_legend(key="rating_legend_view_evaluation")
    render_prep_legend(key="prep_legend_view_evaluation")

    st.markdown("---")
    if st.button(_return_label):
        go_to(_return_page)


# ════════════════════════════════════════════════════════════
# PAGE: ATTENDING HOME
# ════════════════════════════════════════════════════════════
elif page == "attending_home":
    mobile_tip("📱 On mobile: tap the >> icon at top left to access navigation.")
    page_header(
        header_break_before("👋 Welcome back,", st.session_state["attending_login_name"]),
        tier_text="👋 Welcome back,",
    )

    # "Pending self-evaluation" badge: self-assessment drafts a
    # resident has generated a magic link for (see the Self-Assess
    # page's "Generate Pre-Filled Magic Link for Attending and Notify
    # the Attending" button) that this attending hasn't reviewed yet.
    # A draft's mere presence here means "still pending" — reviewing
    # it on attending_assessment (submitting or accepting as-is) is
    # what actually removes it (delete_draft()), same idea as the
    # resident Home page's own "new evaluation" badge tracking.
    try:
        _drafts_df = read_sheet_df(SHEET_DRAFTS, expected_cols=DRAFT_COLS)
        _pending_drafts = _drafts_df[
            _drafts_df["attending_id"].astype(str).str.strip()
            == str(st.session_state.get("attending_login_id", "")).strip()
        ]
        if not _pending_drafts.empty:
            _n = len(_pending_drafts)
            st.success(f"🔔 You have {_n} self-evaluation{'s' if _n != 1 else ''} pending your review!")
            _, _home_proc_df, _, _ = load_refs()
            _home_proc_names = {str(k): v for k, v in zip(_home_proc_df["procedure_id"], _home_proc_df["procedure_name"])}
            _home_residents_df = read_sheet_df(SHEET_RESIDENTS, expected_cols=RESIDENT_COLS)
            _home_res_lookup = dict(zip(
                _home_residents_df["email"].astype(str).str.strip().str.lower(),
                _home_residents_df["name"],
            ))
            _pending_drafts = _pending_drafts.copy()
            _pending_drafts["_created_sort"] = pd.to_datetime(_pending_drafts["created_at"], errors="coerce")
            _pending_drafts = _pending_drafts.sort_values("_created_sort", ascending=False)
            for _, _draft_row in _pending_drafts.iterrows():
                _proc_label = _home_proc_names.get(str(_draft_row["procedure_id"]), str(_draft_row["procedure_id"]))
                _res_email  = str(_draft_row.get("resident_email", ""))
                _res_label  = _home_res_lookup.get(_res_email.strip().lower(), _res_email)
                if st.button(
                    f"📝 {_proc_label} — {_res_label} ({fmt_date(_draft_row.get('date'))})",
                    key=f"review_self_eval_{_draft_row['draft_id']}",
                    width="stretch",
                ):
                    # Same session_state keys the magic link's own
                    # ?mode=attending routing sets, just populated
                    # directly instead of via query params — reuses
                    # attending_assessment as-is.
                    st.session_state["resident"]       = _draft_row["resident_email"]
                    st.session_state["procedure_id"]   = _draft_row["procedure_id"]
                    st.session_state["specialty_id"]   = _draft_row["specialty_id"]
                    st.session_state["attending_name"] = st.session_state.get("attending_login_name", "").replace(" ", "_")
                    st.session_state["draft_id"]        = _draft_row["draft_id"]
                    go_to("attending_assessment")
            st.markdown("")
    except ConnectionError:
        pass  # badge is a nice-to-have — don't block the Home page over it

    st.markdown("_What would you like to do today?_")
    st.markdown("")

    with st.container(key="home_cards"):
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown('<div class="pp-card">', unsafe_allow_html=True)
            st.markdown("### ➕ New Assessment")
            st.markdown("Start a blank assessment for one of your residents.")
            if st.button("Start Assessment", width="stretch", type="primary", key="att_home_start_btn"):
                go_to("attending_start")
            st.markdown("</div>", unsafe_allow_html=True)

        with c2:
            st.markdown('<div class="pp-card">', unsafe_allow_html=True)
            st.markdown("### 📊 Resident Dashboard")
            st.markdown("View resident progress heatmaps and comments.")
            if st.button("View Dashboard", width="stretch", key="att_home_dashboard_btn"):
                go_to("attending_resident_dashboard")
            st.markdown("</div>", unsafe_allow_html=True)

        with c3:
            st.markdown('<div class="pp-card">', unsafe_allow_html=True)
            st.markdown("### 📜 Evaluation History")
            st.markdown("Browse every evaluation you've filled out.")
            if st.button("View History", width="stretch", key="att_home_eval_history_btn"):
                go_to("attending_eval_history")
            st.markdown("</div>", unsafe_allow_html=True)


# ════════════════════════════════════════════════════════════
# PAGE: START CASE
# ════════════════════════════════════════════════════════════
elif page == "start":
    mobile_tip("📱 On mobile: tap the >> icon at top left to view the sidebar.")
    page_header("📋 Start Assessment")
    if st.button("🏠 Back to Home", key="start_home_top"):
        go_to("home")

    try:
        spec_df, proc_df, steps_df, atnd_df = load_refs()
    except ConnectionError as exc:
        show_gs_error(exc)
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    spec_map = dict(zip(spec_df["specialty_name"], spec_df["specialty_id"]))
    is_admin = st.session_state["resident"] in ADMINS

    if is_admin:
        selected_spec_name = st.selectbox("Specialty", list(spec_map.keys()))
        specialty_id       = spec_map[selected_spec_name]
        st.session_state["specialty_id"] = specialty_id
    else:
        specialty_id = st.session_state.get("specialty_id")
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

    _CHOOSE_PROC = "Choose Procedure"
    _CHOOSE_ATT  = "Choose Attending"

    _proc_options = _ordered_procedure_names(proc_map)

    attending = st.selectbox(
        "Attending",
        [_CHOOSE_ATT] + sorted(atnd_map.keys(), key=lambda n: n.split()[-1] if n.split() else n),
    )
    procedure = st.selectbox("Procedure", [_CHOOSE_PROC] + _proc_options)
    case_date = st.date_input("Date", st.session_state["date"])

    procedure_chosen = procedure != _CHOOSE_PROC
    attending_chosen = attending != _CHOOSE_ATT

    st.session_state["procedure_id"] = proc_map[procedure] if procedure_chosen else None
    st.session_state["attending_id"] = atnd_map[attending] if attending_chosen else None
    st.session_state["date"]         = case_date

    if not (procedure_chosen and attending_chosen):
        st.info("Choose an attending and a procedure to continue.")

    st.markdown("---")

    def _reset_and_start(mode: str):
        st.session_state["scores"]               = {}
        st.session_state["notes"]                = ""
        st.session_state["improve"]              = ""
        st.session_state["how"]                  = ""
        st.session_state["generated_magic_link"] = None
        st.session_state["assessment_mode"]      = mode
        # This is the resident's own voluntary pick, not one that came
        # from an attending's magic link — clears any stale True (and
        # its request_id) left over from an earlier self-eval-request
        # visit this session, so the assessment page's header doesn't
        # wrongly claim this one was attending-requested too, and so
        # finishing this fresh, unrelated self-assessment doesn't also
        # delete that old request out from under a still-pending one.
        st.session_state["self_eval_requested_by_attending"] = False
        st.session_state["self_eval_request_id"] = ""
        go_to("assessment")

    _selection_incomplete = not (procedure_chosen and attending_chosen)

    # Self-Assess's label is the longest of the three, so give it more of
    # the row's width and take it from the other two — fit_all_button_labels()
    # still shrinks per-button as a backstop, but this keeps all three
    # legible at typical widths instead of relying on that shrink alone.
    _start_cols = st.columns([1] if is_admin else [0.85, 1.3, 0.85])
    with _start_cols[0]:
        # Two trailing spaces before \n is Markdown's hard-break syntax —
        # renders as a real <br>, forcing exactly two lines regardless of
        # width (never collapses to one, and white-space:nowrap on
        # button p keeps either line from wrapping into a third).
        # fit_all_button_labels() shrinks the font if the wider of the
        # two lines would otherwise overflow, so neither line ever ends
        # in an ellipsis either — verified at both desktop and cramped
        # widths before applying. Every emoji sits at the end of its
        # own line's text (not before, not centered separately from
        # it) — simpler and it just reads as part of that line.
        if st.button("Assess Together ✅  \nResident + Attending", type="primary", width="stretch", key="start_together_btn", disabled=_selection_incomplete):
            _reset_and_start("together")

    if not is_admin:
        with _start_cols[1]:
            if st.button("Self-Assess ✅  \nPre-Filled Magic Link for Attending 🔗", width="stretch", key="start_self_btn", disabled=_selection_incomplete):
                _reset_and_start("self")
        with _start_cols[2]:
            if st.button("Blank Magic Link 🔗  \nSend to Attending ✉️", width="stretch", key="start_blank_link_btn", disabled=_selection_incomplete):
                _att_match = atnds[atnds["attending_id"].astype(str).str.strip()
                                    == str(st.session_state.get("attending_id", "")).strip()]
                safe_att = _att_match["attending_name"].values[0].replace(" ", "_") if len(_att_match) > 0 else "Unknown"
                base_url = st.secrets.get("APP_BASE_URL", "https://procedurepassport.streamlit.app")
                st.session_state["blank_magic_link"] = (
                    f"{base_url}/?mode=attending"
                    f"&resident={st.session_state['resident']}"
                    f"&procedure_id={st.session_state['procedure_id']}"
                    f"&specialty_id={specialty_id}"
                    f"&attending_name={safe_att}"
                    f"&date={st.session_state['date']}"
                )

    if st.session_state.get("blank_magic_link"):
        st.success("✅ A blank link is ready for your attending:")
        copy_link_button(st.session_state["blank_magic_link"], key="copy_blank_link")
        st.code(st.session_state["blank_magic_link"], language="text")

    st.markdown("---")
    if st.button("⬅️ Back to Home"):
        go_to("home")


# ════════════════════════════════════════════════════════════
# PAGE: ASSESSMENT
# ════════════════════════════════════════════════════════════
elif page == "assessment":
    try:
        _, proc_df, steps_df, atnd_df = load_refs()
    except ConnectionError as exc:
        show_gs_error(exc)
        if st.button("⬅️ Back to Start"):
            go_to("start")
        st.stop()

    is_admin = st.session_state["resident"] in ADMINS

    # If this page was reached via an attending's self-eval request
    # (magic link or the Home page notification) that's already been
    # fulfilled — a reused link, or clicking both the link and the
    # notification — block it here rather than letting the resident
    # redo (and duplicate) an already-completed self-assessment. A
    # request's mere presence in self_eval_requests means "not yet
    # fulfilled"; completing it below (the "self" Finish button)
    # deletes it, which is what this check is actually watching for.
    if (
        st.session_state.get("assessment_mode") == "self"
        and st.session_state.get("self_eval_requested_by_attending")
        and st.session_state.get("self_eval_request_id")
    ):
        try:
            _pending_request = load_self_eval_request(st.session_state["self_eval_request_id"])
        except ConnectionError as exc:
            show_gs_error(exc)
            st.stop()
        if _pending_request is None:
            page_header("✅ Already Completed")
            st.success("This self-evaluation has already been completed. Thank you!")
            st.markdown("_No further action is needed — your attending has already been notified._")
            if st.button("🏠 Back to Home", type="primary"):
                go_to("home")
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
    mobile_tip("📱 On mobile: tap the >> icon at top left to view the sidebar.")
    # tier_text excludes the resident's/attending's name(s) from
    # _header_max()'s length tier so a long one doesn't needlessly drop
    # the header into a smaller ceiling; the fit script still measures
    # and shrinks the full displayed text (names included) if it
    # doesn't actually fit.
    if st.session_state.get("assessment_mode") == "self" and st.session_state.get("self_eval_requested_by_attending"):
        # Reached via an attending's own "Create Magic Link Request for
        # Resident Self-Evaluation" — call out plainly that this
        # specific self-eval was requested by that attending, not one
        # the resident started on their own.
        _att_match = atnd_df[atnd_df["attending_id"].astype(str).str.strip()
                              == str(st.session_state.get("attending_id", "")).strip()]
        _requesting_attending = _att_match["attending_name"].values[0] if len(_att_match) else "Unknown"
        page_header(
            header_break_before(
                f"📝 {_proc_name} Self-Assessment for",
                f"{st.session_state['resident_name']} by {_requesting_attending}",
            ),
            tier_text=f"📝 {_proc_name} Self-Assessment",
        )
    else:
        page_header(
            header_break_before(f"📝 {_proc_name}", f"Assessment for {st.session_state['resident_name']}"),
            tier_text=f"📝 {_proc_name} Assessment",
        )
    assessment_instructions_note()

    # Back button placed at the top, clearly separated from Finish (Fix 7)
    with st.container(key="assess_top_nav"):
        _top_cols_assess = st.columns([1, 1, 4])
        with _top_cols_assess[0]:
            if st.button("⬅️ Back to Start", key="back_top"):
                go_to("start")
        with _top_cols_assess[1]:
            if st.button("🏠 Home", key="assess_home_top"):
                go_to("home")

    if _is_robotic_procedure(_proc_name):
        render_robo_type_picker("robo_type", default=st.session_state.get("robo_type", "Xi"))

    st.markdown("---")

    with st.container(key="assess_improve_how"):
        _imp_label_col, _imp_input_col = st.columns([2, 6])
        with _imp_label_col:
            st.markdown(
                '<p style="text-align: left;">In order to improve this:</p>',
                unsafe_allow_html=True,
            )
        with _imp_input_col:
            st.session_state["improve"] = st.text_input(
                "What to improve",
                value=st.session_state.get("improve", ""),
                key="assess_improve",
                label_visibility="collapsed",
                placeholder="e.g., suture technique",
            )
        _how_label_col, _how_input_col = st.columns([2, 6])
        with _how_label_col:
            st.markdown("Do this:")
        with _how_input_col:
            st.session_state["how"] = st.text_input(
                "How to improve it",
                value=st.session_state.get("how", ""),
                key="assess_how",
                label_visibility="collapsed",
                placeholder="e.g., practice two-handed knots",
            )
    sync_improve_how_label_width()

    st.markdown("---")

    with st.container(key="assess_ratings_row"):
        _overall_col, _prep_col = st.columns(2)
        with _overall_col:
            current_o = st.session_state.get("overall_performance", O_SCORE_OPTIONS[0])
            st.session_state["overall_performance"] = st.selectbox(
                "Overall Performance Rating",
                O_SCORE_OPTIONS,
                index=O_SCORE_OPTIONS.index(current_o) if current_o in O_SCORE_OPTIONS else 0,
                key="assess_overall_performance",
            )
        with _prep_col:
            _cp_opts = ["Not Assessed", "Unprepared", "Poorly Prepared",
                        "Adequately Prepared", "Well Prepared", "Highly Prepared"]
            _cp_default = st.session_state.get("case_preparation", "Not Assessed")
            _cp_idx = _cp_opts.index(_cp_default) if _cp_default in _cp_opts else 0
            st.session_state["case_preparation"] = st.selectbox(
                "Daily Preparation",
                _cp_opts,
                index=_cp_idx,
                key="assess_preparation",
            )

    with st.expander(
        header_break_before("Step-Level Ratings for", _proc_name),
        expanded=False,
        key="step_ratings_expander_resident",
    ):
        # Case Complexity leads the Step-Level Ratings section, then each
        # procedure step in order.
        _cc_opts = ["— Select complexity —", "Straight Forward", "Moderate", "Complex"]
        _cc_default = st.session_state.get("case_complexity", "— Select complexity —")
        _cc_idx = _cc_opts.index(_cc_default) if _cc_default in _cc_opts else 0
        st.session_state["case_complexity"] = st.selectbox(
            "Case Complexity",
            _cc_opts,
            index=_cc_idx,
            key="assess_case_complexity",
        )
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

    st.markdown("---")

    render_rating_legend(key="rating_legend_resident")
    render_prep_legend(key="prep_legend_resident")
    render_complexity_legend(key="complexity_legend_resident")

    st.markdown("---")

    st.session_state["notes"] = st.text_area(
        "Development / Improvement / Feed-Forward", st.session_state.get("notes", ""), key="assess_notes"
    )

    def _assessment_has_value() -> bool:
        return (
            st.session_state["case_complexity"] != "— Select complexity —"
            or st.session_state["case_preparation"] != "Not Assessed"
            or st.session_state["overall_performance"] != O_SCORE_OPTIONS[0]
            or any(v != "Not Assessed" for v in st.session_state["scores"].values())
            or st.session_state.get("notes", "").strip() != ""
            or st.session_state.get("improve", "").strip() != ""
            or st.session_state.get("how", "").strip() != ""
        )

    # Only meaningful for a robotic procedure — st.session_state["robo_type"]
    # could otherwise still hold a stale Xi/SP/DV5 pick left over from a
    # different, earlier robotic procedure this same session.
    _robo_type_to_save = st.session_state.get("robo_type") if _is_robotic_procedure(_proc_name) else None

    def _save_own_case(assessment_type: str) -> str:
        """Save the resident's own entry, tagged with how it was taken."""
        return save_case(
            resident_email=st.session_state["resident"],
            date=st.session_state["date"],
            specialty_id=st.session_state["specialty_id"],
            procedure_id=st.session_state["procedure_id"],
            attending_id=st.session_state["attending_id"],
            scores_dict=st.session_state["scores"],
            case_complexity=st.session_state["case_complexity"],
            case_preparation=st.session_state["case_preparation"],
            overall_performance=st.session_state["overall_performance"],
            robo_type=_robo_type_to_save,
            notes=st.session_state.get("notes", ""),
            improve=st.session_state.get("improve", ""),
            how=st.session_state.get("how", ""),
            assessment_type=assessment_type,
        )

    # Admins never see the magic-link options (Start page hides those
    # buttons for them), so treat any admin session as "together" too,
    # regardless of whatever assessment_mode happens to be stored.
    _mode = "self" if (not is_admin and st.session_state.get("assessment_mode") == "self") else "together"

    st.markdown("---")

    if _mode == "together":
        # Fix 7: Finish button alone at the bottom with a confirmation note
        st.caption("✅ The case is saved automatically when you click Finish & Save.")
        if st.button("🏁 Finish & Save →", type="primary", width="stretch"):
            if not _assessment_has_value():
                st.warning("Please provide at least one rating or comment before submitting.")
            else:
                try:
                    st.session_state["current_case_id"] = _save_own_case("Assessed Together")
                    st.session_state["last_assessment_type"] = "Assessed Together"
                    go_to("dashboard")
                except ConnectionError as exc:
                    show_gs_error(exc)

    else:  # _mode == "self"
        if st.button("🔗 Generate Pre-Filled Magic Link for Attending and Notify the Attending", type="primary", width="stretch"):
            if not _assessment_has_value():
                st.warning("Please provide at least one rating or comment before generating a link.")
            else:
                try:
                    st.session_state["current_case_id"] = _save_own_case("Self-Assessment")
                    st.session_state["last_assessment_type"] = "Self-Assessment"
                    draft_id = save_draft(
                        resident_email=st.session_state["resident"],
                        date=st.session_state["date"],
                        specialty_id=st.session_state["specialty_id"],
                        procedure_id=st.session_state["procedure_id"],
                        attending_id=st.session_state["attending_id"],
                        scores_dict=st.session_state["scores"],
                        case_complexity=st.session_state["case_complexity"],
                        case_preparation=st.session_state["case_preparation"],
                        overall_performance=st.session_state["overall_performance"],
                        robo_type=_robo_type_to_save,
                        notes=st.session_state.get("notes", ""),
                        improve=st.session_state.get("improve", ""),
                        how=st.session_state.get("how", ""),
                    )
                    # Fulfills the attending's original request (if this
                    # self-assessment came from one) — removes it from
                    # both the resident Home page badge and, on a future
                    # visit, the magic link itself (see the "Already
                    # Completed" check above), regardless of which of
                    # the two routes was actually used just now.
                    if st.session_state.get("self_eval_request_id"):
                        delete_self_eval_request(st.session_state["self_eval_request_id"])
                        st.session_state["self_eval_request_id"] = ""
                    _att_match = atnd_df[atnd_df["attending_id"].astype(str).str.strip()
                                          == str(st.session_state.get("attending_id", "")).strip()]
                    safe_att = _att_match["attending_name"].values[0].replace(" ", "_") if len(_att_match) > 0 else "Unknown"
                    base_url = st.secrets.get("APP_BASE_URL", "https://procedurepassport.streamlit.app")
                    st.session_state["generated_magic_link"] = (
                        f"{base_url}/?mode=attending"
                        f"&resident={st.session_state['resident']}"
                        f"&procedure_id={st.session_state['procedure_id']}"
                        f"&specialty_id={st.session_state['specialty_id']}"
                        f"&attending_name={safe_att}"
                        f"&draft_id={draft_id}"
                    )
                    go_to("magic_link_ready")
                except ConnectionError as exc:
                    show_gs_error(exc)


# ════════════════════════════════════════════════════════════
# PAGE: SINGLE-CASE DASHBOARD
# ════════════════════════════════════════════════════════════
elif page == "dashboard":
    try:
        _, proc_df, steps_df, _ = load_refs()
    except ConnectionError as exc:
        show_gs_error(exc)
        st.stop()

    steps = steps_df[steps_df["procedure_id"] == st.session_state["procedure_id"]].sort_values("step_order")
    _dash_proc_rows = proc_df.loc[proc_df["procedure_id"] == st.session_state["procedure_id"], "procedure_name"].values
    _dash_proc_name = _dash_proc_rows[0] if len(_dash_proc_rows) else ""

    page_header("✅ Case Saved")
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
        st.markdown(f"**Daily Preparation:** {st.session_state.get('case_preparation', '—')}")
    with meta_col2:
        st.markdown(f"**Overall Performance:** {st.session_state.get('overall_performance', '—')}")
        st.markdown(f"**Basis:** {st.session_state.get('last_assessment_type', '—')}")
        if _is_robotic_procedure(_dash_proc_name):
            st.markdown(f"**Robot:** {st.session_state.get('robo_type', '—')}")

    if st.session_state.get("improve", "").strip() or st.session_state.get("how", "").strip():
        st.markdown(f"**In order to improve this:** {st.session_state.get('improve', '') or '_(blank)_'}.")
        st.markdown(f"**Do this:** {st.session_state.get('how', '') or '_(blank)_'}.")

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
# PAGE: MAGIC LINK READY (after a self-assessment generates one)
# ════════════════════════════════════════════════════════════
elif page == "magic_link_ready":
    if not st.session_state.get("generated_magic_link"):
        st.error("No magic link found. Please generate one from the assessment page.")
        if st.button("⬅️ Back to Assessment"):
            go_to("assessment")
        st.stop()

    page_header("🔗 Magic Link Ready")
    st.success("✅ Your self-assessment was saved, and a pre-filled link is ready for your attending:")
    st.info(
        "📬 Your attending has also been notified in their own Procedure "
        "Passport account — they'll see this pending self-evaluation on "
        "their Home page even without the link below."
    )
    copy_link_button(st.session_state["generated_magic_link"], key="copy_generated_link")
    st.code(st.session_state.get("generated_magic_link", ""), language="text")
    st.caption("The attending can review and adjust every field before submitting.")

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
    page_header("💬 Comments Dashboard")
    if st.button("🏠 Back to Home", key="comments_home_top"):
        go_to("home")
    resident = st.session_state.get("resident")
    if not resident:
        st.error("Not logged in.")
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    try:
        merged = _build_resident_comments_df(resident)
    except ConnectionError as exc:
        show_gs_error(exc)
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    if merged.empty:
        st.info("No comments recorded yet.")
        if st.button("⬅️ Back to Home"):
            go_to("home")
    else:
        # Fix 8: procedure/attending filter dropdowns — each one's options
        # are narrowed by the OTHER dropdown's current selection, so e.g.
        # filtering to a procedure leaves only the attendings who have
        # entries for it in the Attending dropdown.
        _proc_selected = st.session_state.get("comments_proc_filter", "All Procedures")
        _att_selected = st.session_state.get("comments_att_filter", "All Attendings")

        _proc_chosen = _proc_selected != "All Procedures"
        _att_chosen = _att_selected != "All Attendings"
        if _proc_chosen and _att_chosen:
            _comments_heading = f"{_proc_selected} — Comments by {_att_selected}"
        elif _proc_chosen:
            _comments_heading = f"{_proc_selected} — All Comments"
        elif _att_chosen:
            _comments_heading = f"All Comments by {_att_selected}"
        else:
            _comments_heading = "All Comments"
        st.markdown(f"### 💬 {_comments_heading}")

        _proc_pool = merged if _att_selected == "All Attendings" else merged[merged["Attending"] == _att_selected]
        _proc_opts = ["All Procedures"] + sorted(_proc_pool["Procedure"].dropna().unique().tolist())
        _att_pool = merged if _proc_selected == "All Procedures" else merged[merged["Procedure"] == _proc_selected]
        _att_opts = ["All Attendings"] + sorted(
            _att_pool["Attending"].dropna().unique().tolist(),
            key=lambda n: n.split()[-1] if n.split() else n,
        )

        # A previously-selected filter value can fall out of the newly
        # narrowed options (because the other filter now excludes it) —
        # reset it to "All ..." before the widget renders, rather than
        # letting st.selectbox raise on a default no longer in its options.
        if _proc_selected not in _proc_opts:
            st.session_state["comments_proc_filter"] = "All Procedures"
        if _att_selected not in _att_opts:
            st.session_state["comments_att_filter"] = "All Attendings"

        _filter_col1, _filter_col2 = st.columns(2)
        with _filter_col1:
            _proc_filter = st.selectbox("Filter by Procedure", _proc_opts, key="comments_proc_filter")
        with _filter_col2:
            _att_filter = st.selectbox("Filter by Attending", _att_opts, key="comments_att_filter")
        if _proc_filter != "All Procedures":
            merged = merged[merged["Procedure"] == _proc_filter]
        if _att_filter != "All Attendings":
            merged = merged[merged["Attending"] == _att_filter]

        # Filtering down to a single procedure/attending makes that column
        # redundant (every row shows the same value) — drop it from the
        # on-screen table while filtered.
        _show_proc = _proc_filter == "All Procedures"
        _show_att = _att_filter == "All Attendings"

        _render_comments_html_table(merged, _show_proc, _show_att)

        if st.button("⬅️ Back to Home"):
            go_to("home")


# ════════════════════════════════════════════════════════════
# PAGE: CUMULATIVE DASHBOARD
# ════════════════════════════════════════════════════════════
elif page == "cumulative":
    if st.session_state.pop("_cumulative_scroll_to_top", False):
        # Set once, by "Hide Comments" (further down), right when it
        # collapses the comments section — consuming it (popping) here
        # means this only fires on the run right after that click. Same
        # scroll-to-top logic as the page-navigation trick above, just
        # triggered by this one-shot flag instead of a page change (page
        # doesn't change here — the user stays on this same page). A
        # fresh time.time() keeps this iframe's content from ever being
        # byte-identical to what it last sent at this call site — an
        # unchanged srcdoc never re-fires its own <script> on a later
        # Hide Comments click (same reasoning as that other trick).
        st.iframe(
            f"""
            <!-- scroll to top after hiding comments: {time.time()} -->
            <script>
            (function() {{
                window.parent.scrollTo(0, 0);
                var doc = window.parent.document;
                var containers = doc.querySelectorAll(
                    '[data-testid="stAppViewContainer"], [data-testid="stMain"], section.main'
                );
                containers.forEach(function(c) {{ c.scrollTop = 0; }});
            }})();
            </script>
            """,
            height=1,
        )

    mobile_tip("📱 On mobile: tap the >> icon at top left to view the sidebar.")

    # Login/data checks happen before the page header now (rather than
    # right after it, as most other pages do) because the header itself
    # needs procs_map — once a procedure is chosen it shows "{procedure}
    # — Progress Heatmap" instead of the generic title (see below), so
    # nothing here can be a plain page_header() call up front like usual.
    resident = st.session_state.get("resident")
    if not resident:
        page_header("📊 Cumulative Dashboard")
        st.error("Not logged in.")
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    try:
        merged, steps_df, procs_map = _build_resident_case_matrix(resident)
    except ConnectionError as exc:
        page_header("📊 Cumulative Dashboard")
        show_gs_error(exc)
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    if merged.empty:
        page_header("📊 Cumulative Dashboard")
        st.info("No cases logged yet.")
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    proc_ids = merged["case_procedure_id"].dropna().unique()

    # index=None + placeholder means no procedure is pre-selected — the
    # dropdown starts on "Choose procedure" rather than silently picking
    # the first one. Explicit key so the sidebar/home "Cumulative
    # Dashboard" buttons can reset it back to unselected on every fresh
    # navigation to this page (see those buttons) — without a key,
    # Streamlit would keep remembering whatever was last selected here.
    # Read straight out of session_state (rather than waiting for the
    # selectbox itself, further down) so the header/top button row below
    # can already reflect the current selection this run — Streamlit
    # already applies any change from this rerun's trigger to
    # session_state before the script starts executing.
    _selected_proc_id = st.session_state.get("cumulative_proc_select")
    if _selected_proc_id is not None and _selected_proc_id not in set(proc_ids):
        # Stale selection from an earlier visit (the underlying data
        # changed since) — clear it so neither the header nor the
        # selectbox below choke on an option that no longer exists.
        st.session_state.pop("cumulative_proc_select", None)
        _selected_proc_id = None

    # Once a procedure is chosen, its heatmap heading ("{procedure} —
    # Progress Heatmap") takes over as the page's main header instead of
    # the generic title — and, since it's already shown right here,
    # _render_resident_heatmap below is told not to repeat it.
    if _selected_proc_id:
        _header_proc_name = procs_map.get(_selected_proc_id, _selected_proc_id)
        page_header(header_break_before(f"📊 {_header_proc_name} —", "Progress Heatmap"))
    else:
        page_header("📊 Cumulative Dashboard")

    # This reserves the top row's spot in the page — right below the
    # header, above the selector — without actually filling it in yet.
    # It has to stay empty until *after* the selectbox is instantiated
    # below: st.rerun(), called from inside "See Comments"'s own click
    # handler, aborts the script right there, so anything defined later
    # in the script (the selectbox included) never runs *this specific
    # pass* — and Streamlit garbage-collects a keyed widget's
    # session_state the moment a pass completes without instantiating
    # it, which silently reset the whole page back to "Choose
    # procedure" every time this button was clicked. Filling the
    # placeholder only after the selectbox has already been created
    # this run sidesteps that entirely, while still rendering it in the
    # right visual spot.
    _top_row_placeholder = st.empty()

    # ── Procedure selector ────────────────────────────────
    selected_proc = st.selectbox(
        "Procedure",
        options=sorted(proc_ids, key=lambda x: procs_map.get(x, x)),
        format_func=lambda x: procs_map.get(x, x),
        index=None,
        placeholder="Choose procedure",
        key="cumulative_proc_select",
    )

    # "See Comments" sits to the left of the top "Back to Home" button,
    # but only once a procedure is chosen AND comments aren't already
    # showing — once they are, "Hide Comments" lives under the comments
    # section itself instead (see below), right where the user actually
    # is after this button scrolls them down to it.
    with _top_row_placeholder.container():
        if selected_proc and not st.session_state.get("cumulative_show_comments"):
            _top_col1, _top_col2, _top_spacer = st.columns([1, 1, 2])
            with _top_col1:
                if st.button("💬 See Comments", key="cumulative_see_comments"):
                    st.session_state["cumulative_show_comments"] = True
                    # Consumed once, right after the comments section
                    # renders below, to scroll it into view — see there.
                    st.session_state["_cumulative_scroll_to_comments"] = True
                    st.rerun()
            with _top_col2:
                if st.button("🏠 Back to Home", key="cumulative_home_top"):
                    go_to("home")
        else:
            if st.button("🏠 Back to Home", key="cumulative_home_top"):
                go_to("home")

    if selected_proc is None:
        st.info("Choose a procedure above to see its progress heatmap.")
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    # Switching procedures re-hides comments from whichever procedure
    # was previously showing, rather than leaving them displayed under
    # a heatmap they no longer belong to. Only reruns when this actually
    # flips comments from shown to hidden — the top "See Comments" button
    # already rendered this run, so its label needs a fresh run to catch
    # up; skipping the rerun when there's nothing to flip avoids an
    # infinite loop (the guard below wouldn't fire again either way,
    # since cumulative_comments_proc is already updated by then).
    if st.session_state.get("cumulative_comments_proc") != selected_proc:
        st.session_state["cumulative_comments_proc"] = selected_proc
        if st.session_state.get("cumulative_show_comments"):
            st.session_state["cumulative_show_comments"] = False
            st.rerun()

    _render_resident_heatmap(
        merged, steps_df, procs_map, selected_proc, filename_stub=resident,
        show_heading=False,
    )

    _selected_proc_name = procs_map.get(selected_proc, selected_proc)
    if st.session_state.get("cumulative_show_comments"):
        try:
            _cumulative_comments_df = _build_resident_comments_df(resident)
        except ConnectionError as exc:
            show_gs_error(exc)
        else:
            _proc_comments_df = _cumulative_comments_df[_cumulative_comments_df["Procedure"] == _selected_proc_name]
            # "Hide Comments" lives here, right under this section's own
            # header, rather than back up at the top of the page — after
            # scrolling down to read comments, the user shouldn't have to
            # scroll back up just to collapse them again.
            with st.container(key="cumulative_comments_anchor"):
                st.markdown(f"### 💬 Comments — {_selected_proc_name}")
                if st.button("🙈 Hide Comments", key="cumulative_hide_comments"):
                    st.session_state["cumulative_show_comments"] = False
                    # Consumed once, right at the top of the page, to
                    # scroll back up — see there.
                    st.session_state["_cumulative_scroll_to_top"] = True
                    st.rerun()

            if st.session_state.pop("_cumulative_scroll_to_comments", False):
                # Set once, by the top "See Comments" button, right when
                # it turns comments on — consuming it (popping) here means
                # this only scrolls on the run right after that click, not
                # on every later rerun while comments stay visible (e.g.
                # switching procedures elsewhere on the page would
                # otherwise yank the scroll position back down every
                # time too). The section name is embedded in the iframe's
                # own content purely so it differs from whatever was last
                # rendered at this spot — Streamlit doesn't re-fire a
                # <script> inside an iframe whose content is byte-
                # identical to what it last sent for that same call site
                # (same reason the page-level scroll-to-top trick above
                # embeds the page name).
                #
                # Computes its own target scroll offset against stMain
                # directly (the container that actually scrolls in this
                # layout) rather than el.scrollIntoView() — that landed
                # the header a fair bit lower than the true top in
                # practice, since it has to account for the same nested
                # container. The small SCROLL_HEADROOM_PX subtraction
                # lands the header just shy of flush against the very
                # top edge, rather than jammed right against it.
                st.iframe(
                    f"""
                    <!-- scroll to comments: {html.escape(_selected_proc_name)} -->
                    <script>
                    (function() {{
                        var doc = window.parent.document;
                        var el = doc.querySelector('.st-key-cumulative_comments_anchor');
                        var container = doc.querySelector('[data-testid="stMain"]')
                            || doc.querySelector('[data-testid="stAppViewContainer"]');
                        if (!el || !container) return;
                        var SCROLL_HEADROOM_PX = 12;
                        var elRect = el.getBoundingClientRect();
                        var containerRect = container.getBoundingClientRect();
                        var target = container.scrollTop + (elRect.top - containerRect.top) - SCROLL_HEADROOM_PX;
                        container.scrollTo({{top: Math.max(0, target), behavior: 'smooth'}});
                    }})();
                    </script>
                    """,
                    height=1,
                )

            if _proc_comments_df.empty:
                st.info("No comments recorded yet for this procedure.")
            else:
                _render_comments_html_table(_proc_comments_df, show_proc=False, show_att=True)

    if st.button("⬅️ Back to Home"):
        go_to("home")


# ════════════════════════════════════════════════════════════
# PAGE: EVALUATION HISTORY (resident) — every attending-confirmed
# evaluation this resident has received, filterable by date range,
# attending, and procedure; each row opens the matching "view one
# evaluation" page (see _render_evaluation_history_list).
# ════════════════════════════════════════════════════════════
elif page == "eval_history":
    mobile_tip("📱 On mobile: tap the >> icon at top left to view the sidebar.")
    page_header("📜 Complete Evaluation History")
    if st.button("🏠 Back to Home", key="eval_history_home_top"):
        go_to("home")

    resident = st.session_state.get("resident")
    if not resident:
        st.error("Not logged in.")
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    try:
        _eval_history_df = _build_resident_evaluation_list(resident)
    except ConnectionError as exc:
        show_gs_error(exc)
        if st.button("⬅️ Back to Home"):
            go_to("home")
        st.stop()

    if _eval_history_df.empty:
        st.info("No evaluations recorded yet.")
    else:
        _render_evaluation_history_list(
            _eval_history_df,
            person_col="Attending",
            person_noun="Attending",
            preposition="by",
            session_prefix="eval_hist",
            return_page="eval_history",
        )

    st.markdown("---")
    if st.button("⬅️ Back to Home"):
        go_to("home")


# ════════════════════════════════════════════════════════════
# PAGE: ATTENDING START ASSESSMENT
# ════════════════════════════════════════════════════════════
elif page == "attending_start":
    mobile_tip("📱 On mobile: tap the >> icon at top left to view the sidebar.")
    page_header("📋 Start Assessment")
    if st.button("🏠 Back to Home", key="att_start_home_top"):
        go_to("attending_home")

    specialty_id = st.session_state.get("attending_login_specialty_id")
    if not specialty_id:
        st.error("No specialty assigned. Contact an admin.")
        st.stop()

    try:
        _, proc_df, _, _ = load_refs()
        residents_df = read_sheet_df(
            SHEET_RESIDENTS, expected_cols=RESIDENT_COLS
        )
    except ConnectionError as exc:
        show_gs_error(exc)
        if st.button("⬅️ Back to Home", key="att_start_home_err"):
            go_to("attending_home")
        st.stop()

    my_residents = residents_df[residents_df["specialty_id"] == specialty_id]
    procs = proc_df[proc_df["specialty_id"] == specialty_id]

    if my_residents.empty:
        st.warning("⚠️ No residents configured for your specialty.")
        if st.button("⬅️ Back to Home", key="att_start_no_res"):
            go_to("attending_home")
        st.stop()
    if procs.empty:
        st.warning("⚠️ No procedures configured for your specialty.")
        if st.button("⬅️ Back to Home", key="att_start_no_proc"):
            go_to("attending_home")
        st.stop()

    res_map  = dict(zip(my_residents["name"], my_residents["email"]))
    proc_map = dict(zip(procs["procedure_name"], procs["procedure_id"]))

    _CHOOSE_RES  = "Choose Resident"
    _CHOOSE_PROC = "Choose Procedure"

    resident_choice = st.selectbox(
        "Resident",
        [_CHOOSE_RES] + sorted(res_map.keys(), key=lambda n: n.split()[-1] if n.split() else n),
    )
    procedure_choice = st.selectbox("Procedure", [_CHOOSE_PROC] + _ordered_procedure_names(proc_map))
    case_date = st.date_input("Date", st.session_state["date"])
    st.session_state["date"] = case_date

    resident_chosen  = resident_choice != _CHOOSE_RES
    procedure_chosen = procedure_choice != _CHOOSE_PROC

    if not (resident_chosen and procedure_chosen):
        st.info("Choose a resident and a procedure to continue.")

    st.markdown("---")

    _att_start_cols = st.columns(2)
    with _att_start_cols[0]:
        if st.button("Start Assessment", type="primary", width="stretch", key="att_start_go_btn",
                     disabled=not (resident_chosen and procedure_chosen)):
            # Reuses the same session keys — and the same blank assessment
            # page — the anonymous "Blank Magic Link" flow feeds into, just
            # populated directly instead of via a link's query params.
            st.session_state["resident"]            = res_map[resident_choice]
            st.session_state["procedure_id"]        = proc_map[procedure_choice]
            st.session_state["specialty_id"]        = specialty_id
            st.session_state["attending_name"]      = st.session_state.get("attending_login_name", "").replace(" ", "_")
            st.session_state["draft_id"]            = ""
            st.session_state["attending_link_date"] = str(case_date)
            go_to("attending_assessment")
    with _att_start_cols[1]:
        if st.button("🔗 Create Magic Link Request for Resident Self-Evaluation", width="stretch",
                     key="att_start_self_link_btn", disabled=not (resident_chosen and procedure_chosen)):
            # Unlike the anonymous attending magic-link flow above, a
            # self-evaluation is tied to the resident's own account —
            # this link only pre-fills the assessment (procedure/
            # attending/date) once the resident is logged in as
            # themselves; see the query-param routing near the top of
            # this file (mode=resident_self) and _complete_login()'s
            # resident branch, which picks it back up right after login
            # if they weren't already signed in when they opened it.
            # Also persisted as a self_eval_requests row (not just a
            # URL) so it can be shown on the resident's own Home page
            # too, in addition to this link — the request_id embedded
            # in the link cross-references the two against each other,
            # so fulfilling it through either route removes it from
            # both (see delete_self_eval_request(), called once the
            # resident actually completes this self-assessment).
            try:
                _self_eval_request_id = save_self_eval_request(
                    resident_email=res_map[resident_choice],
                    date=case_date,
                    specialty_id=specialty_id,
                    procedure_id=proc_map[procedure_choice],
                    attending_id=st.session_state.get("attending_login_id", ""),
                )
                base_url = st.secrets.get("APP_BASE_URL", "https://procedurepassport.streamlit.app")
                st.session_state["att_start_self_link"] = (
                    f"{base_url}/?mode=resident_self"
                    f"&resident={res_map[resident_choice]}"
                    f"&procedure_id={proc_map[procedure_choice]}"
                    f"&specialty_id={specialty_id}"
                    f"&attending_id={st.session_state.get('attending_login_id', '')}"
                    f"&date={case_date}"
                    f"&request_id={_self_eval_request_id}"
                )
            except ConnectionError as exc:
                show_gs_error(exc)

    if st.session_state.get("att_start_self_link"):
        st.success(f"✅ A self-evaluation link is ready to send {resident_choice}:")
        copy_link_button(st.session_state["att_start_self_link"], key="copy_att_self_link")
        st.code(st.session_state["att_start_self_link"], language="text")

    st.markdown("---")
    if st.button("⬅️ Back to Home", key="att_start_bottom_home"):
        go_to("attending_home")


# ════════════════════════════════════════════════════════════
# PAGE: ATTENDING RESIDENT DASHBOARD
# ════════════════════════════════════════════════════════════
elif page == "attending_resident_dashboard":
    mobile_tip("📱 On mobile: tap the >> icon at top left to view the sidebar.")
    page_header("📊 Resident Dashboard")
    if st.button("🏠 Back to Home", key="att_dash_home_top"):
        go_to("attending_home")

    specialty_id = st.session_state.get("attending_login_specialty_id")
    if not specialty_id:
        st.error("No specialty assigned. Contact an admin.")
        st.stop()

    try:
        _, proc_df, _, _ = load_refs()
        residents_df = read_sheet_df(
            SHEET_RESIDENTS, expected_cols=RESIDENT_COLS
        )
        cases_df = read_sheet_df(SHEET_CASES, expected_cols=_CASE_COLS)
    except ConnectionError as exc:
        show_gs_error(exc)
        if st.button("⬅️ Back to Home", key="att_dash_home_err"):
            go_to("attending_home")
        st.stop()

    # Same "attending-confirmed" definition used everywhere else on this
    # page (comments table, case matrix): a resident's own unsubmitted
    # self-assessment doesn't count as data an attending can review yet.
    confirmed_cases = cases_df[
        cases_df["assessment_type"].fillna("").astype(str).str.strip() != "Self-Assessment"
    ]
    my_residents = residents_df[
        (residents_df["specialty_id"] == specialty_id)
        & (residents_df["email"].isin(set(confirmed_cases["resident_email"])))
    ]
    if my_residents.empty:
        st.warning("⚠️ No residents with recorded cases in your specialty yet.")
        if st.button("⬅️ Back to Home", key="att_dash_no_res"):
            go_to("attending_home")
        st.stop()

    res_map = dict(zip(my_residents["name"], my_residents["email"]))
    _CHOOSE_RES = "Choose Resident"
    _res_col, _proc_col = st.columns(2)
    with _res_col:
        resident_choice = st.selectbox(
            "Resident",
            [_CHOOSE_RES] + sorted(res_map.keys(), key=lambda n: n.split()[-1] if n.split() else n),
            key="att_dash_resident",
        )
    if resident_choice == _CHOOSE_RES:
        with _proc_col:
            st.selectbox("Procedure (optional)", ["Choose a resident first"], disabled=True)
        st.info("Choose a resident to view their comments and progress.")
        st.stop()

    resident_email = res_map[resident_choice]

    # Fetched once here and reused below at the heatmap section — same
    # data, no need to ask for it twice.
    try:
        case_matrix, steps_df, procs_map = _build_resident_case_matrix(resident_email)
    except ConnectionError as exc:
        show_gs_error(exc)
        st.stop()

    # Only list procedures with actual step-level ratings data — i.e. ones
    # that would actually produce a heatmap, not just an attending-
    # confirmed case whose steps were all left "Not Assessed" (see
    # _build_resident_case_matrix's own docstring). procedure_id is
    # compared as a string, not the raw dtype — the cases and procedures
    # sheets can disagree on int vs. float vs. string for the same ID
    # (see _norm_id above), so a raw .isin() can silently drop real
    # matches.
    resident_procedure_ids = set(case_matrix["case_procedure_id"]) if not case_matrix.empty else set()
    procs = proc_df[
        (proc_df["specialty_id"] == specialty_id)
        & (proc_df["procedure_id"].astype(str).isin(resident_procedure_ids))
    ]
    proc_map = dict(zip(procs["procedure_name"], procs["procedure_id"]))
    _ALL_PROCS = "Choose Procedure for Heat Map"

    if not proc_map:
        # No procedure for this resident has step-level ratings data — the
        # dropdown would otherwise offer nothing but its own placeholder.
        with _proc_col:
            st.selectbox("Procedure (optional)", ["No heat maps available"], disabled=True)
        st.info(f"📊 No heat maps available for {resident_choice} yet — no step-level ratings recorded.")
        procedure_choice, procedure_id = _ALL_PROCS, None
    else:
        _proc_opts = [_ALL_PROCS] + _ordered_procedure_names(proc_map)
        # A procedure picked for the previous resident can fall outside this
        # resident's options — reset it rather than letting st.selectbox
        # raise on a default no longer in its options (same guard as the
        # Comments Dashboard's Procedure/Attending filters above).
        if st.session_state.get("att_dash_procedure") not in _proc_opts:
            st.session_state["att_dash_procedure"] = _ALL_PROCS
        with _proc_col:
            procedure_choice = st.selectbox(
                "Procedure (optional)",
                _proc_opts,
                key="att_dash_procedure",
            )
        procedure_id = proc_map.get(procedure_choice) if procedure_choice != _ALL_PROCS else None

    # Whether the Comments table is narrowed to the chosen procedure or
    # showing every procedure, independent of the dropdown itself — the
    # dropdown also drives the heatmap below, so switching it back to "All
    # Procedures" just to see every comment would lose the heatmap too.
    # The toggle always starts filtered whenever the resident/procedure
    # selection changes, rather than carrying over a stale "show all" from
    # a previous procedure.
    _comments_scope = f"{resident_email}|{procedure_choice}"
    if st.session_state.get("att_dash_comments_scope") != _comments_scope:
        st.session_state["att_dash_comments_scope"] = _comments_scope
        st.session_state["att_dash_show_all_comments"] = False
    show_all_comments = st.session_state["att_dash_show_all_comments"] or not procedure_id

    st.markdown("---")

    try:
        all_comments_df = _build_resident_comments_df(resident_email)
    except ConnectionError as exc:
        show_gs_error(exc)
        st.stop()

    if procedure_id:
        # case_matrix was already fetched above, and the dropdown only
        # ever offers a procedure_id that's in it, so it's never empty
        # here. Shown above the comments (per feedback) whenever a
        # procedure is chosen.
        _render_resident_heatmap(
            case_matrix, steps_df, procs_map, procedure_id,
            filename_stub=resident_choice.replace(" ", "_"),
            heading_suffix="Progress Heatmap and Comments",
        )
        st.markdown("---")

    if all_comments_df.empty:
        # Nothing to filter or toggle — skip the section header/button
        # entirely rather than showing controls over an empty table.
        st.info("💬 No comments recorded for this resident yet.")
    else:
        if not procedure_id:
            # Once a procedure is chosen, the heatmap's own heading above
            # already reads "{procedure} — Progress Heatmap and Comments",
            # covering this section too — no second heading needed here.
            _comments_heading = "All Comments" if show_all_comments else "Comments"
            st.markdown(f"### 💬 {_comments_heading} — {resident_choice}")
        if procedure_id:
            _toggle_label = "Show All Comments" if not show_all_comments else f"Show Only {procedure_choice} Comments"
            if st.button(_toggle_label, key="att_dash_comments_toggle"):
                st.session_state["att_dash_show_all_comments"] = not show_all_comments
                st.rerun()

        comments_df = all_comments_df
        if procedure_id and not show_all_comments:
            comments_df = comments_df[comments_df["Procedure"] == procedure_choice]

        if comments_df.empty:
            st.info("No comments recorded yet.")
        else:
            _render_comments_html_table(comments_df, show_proc=show_all_comments, show_att=True)

    st.markdown("---")
    if st.button("⬅️ Back to Home", key="att_dash_bottom_home"):
        go_to("attending_home")


# ════════════════════════════════════════════════════════════
# PAGE: EVALUATION HISTORY (attending) — every evaluation this
# attending has filled out (or was present for, via "Assessed
# Together"), across every resident, filterable by date range,
# resident, and procedure; each row opens the matching "view one
# evaluation" page (see _render_evaluation_history_list).
# ════════════════════════════════════════════════════════════
elif page == "attending_eval_history":
    mobile_tip("📱 On mobile: tap the >> icon at top left to view the sidebar.")
    page_header("📜 Complete Evaluation History")
    if st.button("🏠 Back to Home", key="att_eval_history_home_top"):
        go_to("attending_home")

    attending_id = st.session_state.get("attending_login_id")
    if not attending_id:
        st.error("Not logged in.")
        if st.button("⬅️ Back to Home"):
            go_to("attending_home")
        st.stop()

    # Defaults to just this attending's own evaluations; toggling this
    # on switches to every evaluation in the system, any attending —
    # same Resident/Procedure/date-range filters either way, same as
    # the resident login's own history page.
    _show_all_evals = st.toggle(
        "Show all evaluations (regardless of attending)", key="att_eval_hist_show_all",
    )
    _eval_scope = "all" if _show_all_evals else "mine"
    if st.session_state.get("att_eval_hist_scope") != _eval_scope:
        st.session_state["att_eval_hist_scope"] = _eval_scope
        # Which residents/procedures/dates are even available differs
        # between scopes — clear the filters (and force the date
        # pickers to remount via a fresh nonce) so a selection from one
        # scope doesn't linger as an invalid or out-of-range value in
        # the other, rather than risk st.date_input raising on a value
        # outside its new min/max.
        st.session_state.pop("att_eval_hist_proc_filter", None)
        st.session_state.pop("att_eval_hist_person_filter", None)
        st.session_state["att_eval_hist_date_nonce"] = st.session_state.get("att_eval_hist_date_nonce", 0) + 1

    try:
        _att_eval_history_df = _build_attending_evaluation_list(None if _show_all_evals else attending_id)
    except ConnectionError as exc:
        show_gs_error(exc)
        if st.button("⬅️ Back to Home"):
            go_to("attending_home")
        st.stop()

    if _att_eval_history_df.empty:
        st.info("No evaluations recorded yet.")
    else:
        _render_evaluation_history_list(
            _att_eval_history_df,
            person_col="Resident",
            person_noun="Resident",
            preposition="for",
            session_prefix="att_eval_hist",
            return_page="attending_eval_history",
        )

    st.markdown("---")
    if st.button("⬅️ Back to Home", key="att_eval_history_bottom_home"):
        go_to("attending_home")


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

    # Pre-fill from the resident's self-assessment draft, if this link carries one.
    draft_id = st.session_state.get("draft_id", "")
    try:
        _draft = load_draft(draft_id) if draft_id else None
    except ConnectionError as exc:
        show_gs_error(exc)
        st.stop()

    # A draft_id was provided but no longer resolves to anything — this
    # specific self-evaluation has already been reviewed and submitted
    # (via this same link, the "Pending self-evaluation" Home page
    # badge, or a second open tab). Block re-review here instead of
    # silently falling through to a blank form that could still be
    # submitted as a duplicate case for the same procedure.
    if draft_id and _draft is None:
        page_header("✅ Already Reviewed")
        st.success("This self-evaluation has already been reviewed and submitted. No further action is needed.")
        if st.session_state.get("role") == "attending" and st.session_state.get("attending_login_email"):
            if st.button("🏠 Back to Home", type="primary"):
                go_to("attending_home")
        else:
            st.markdown("_You may now close this window._")
        st.stop()

    try:
        _, proc_df_att, steps_df, _ = load_refs()
    except ConnectionError as exc:
        show_gs_error(exc)
        st.stop()

    # Resolve procedure name for display (Fix 3)
    _att_proc_rows = proc_df_att.loc[proc_df_att["procedure_id"] == procedure_id, "procedure_name"].values
    _att_proc_name = _att_proc_rows[0] if len(_att_proc_rows) else procedure_id

    # Resolve the resident's display name for the header, same "...
    # Assessment for {name}" phrasing the resident's own Assess
    # Together/Self-Assess page uses, so this page reads as the same
    # page. The magic link only carries the resident's email, not their
    # name, so look it up; fall back to the email if that fails.
    try:
        _residents_df = read_sheet_df(
            SHEET_RESIDENTS, expected_cols=RESIDENT_COLS
        )
        _resident_match = _residents_df.loc[
            _residents_df["email"].astype(str).str.strip().str.lower() == resident_email.strip().lower()
        ]
        _resident_display_name = (
            _resident_match["name"].values[0] if len(_resident_match) else resident_email
        )
    except ConnectionError:
        _resident_display_name = resident_email

    page_header(
        header_break_before(f"📝 {_att_proc_name}", f"Assessment for {_resident_display_name}"),
        tier_text=f"📝 {_att_proc_name} Assessment",
    )
    if _draft:
        # One deliberate break point (see header_break_before): the
        # script below tries the whole notice on one line first, only
        # falling back to two lines — starting with "Review and
        # adjust..." — once shrinking the font hits a floor.
        _notice_text = header_break_before(
            "📋 This form has been pre-filled from the resident's self-assessment.",
            "Review and adjust before submitting.",
        )
        st.markdown(
            '<div class="pp-prefill-notice-wrap" style="border: 2px solid #1E88E5; '
            'background-color: #FFFFFF; color: #000000; border-radius: 0.5rem; '
            'padding: 0.75rem 1rem; margin-bottom: 0.75rem;">'
            f'<span class="pp-prefill-notice-text">{html.escape(_notice_text)}</span>'
            "</div>",
            unsafe_allow_html=True,
        )
        st.iframe(
            """
            <script>
            (function() {
                var doc = window.parent.document;
                var wraps = doc.querySelectorAll('.pp-prefill-notice-wrap');
                var wrap = wraps[wraps.length - 1];
                if (!wrap) return;
                var el = wrap.querySelector('.pp-prefill-notice-text');
                if (!el) return;
                var oneLineMaxPx = 16;   // 1rem ceiling on one line
                var floorPx = 12;        // 0.75rem — below this, switch to two lines instead
                var twoLineMaxPx = 15;   // 0.9375rem ceiling once wrapped
                // Measures a string's rendered single-line width at a
                // given font size via a detached, invisible probe.
                function measureWidth(str, fontPx) {
                    var probe = doc.createElement('span');
                    probe.style.position = 'absolute';
                    probe.style.visibility = 'hidden';
                    probe.style.whiteSpace = 'nowrap';
                    probe.style.fontSize = fontPx + 'px';
                    var computed = window.parent.getComputedStyle(el);
                    probe.style.fontFamily = computed.fontFamily;
                    probe.style.fontWeight = computed.fontWeight;
                    probe.textContent = str;
                    doc.body.appendChild(probe);
                    var w = probe.scrollWidth;
                    doc.body.removeChild(probe);
                    return w;
                }
                function fit() {
                    var containerWidth = wrap.clientWidth;
                    if (!containerWidth) return;
                    var fullText = el.textContent;
                    // header_break_before() leaves exactly one regular
                    // space (everything else is nbsp) — that's the one
                    // deliberate wrap point, right before "Review...".
                    var breakIdx = fullText.indexOf(' ');
                    var oneLineWidth = measureWidth(fullText, oneLineMaxPx);
                    if (oneLineWidth <= containerWidth) {
                        el.style.whiteSpace = 'nowrap';
                        el.style.fontSize = oneLineMaxPx + 'px';
                        return;
                    }
                    var oneLineFit = Math.max(1, oneLineMaxPx * (containerWidth / oneLineWidth));
                    if (oneLineFit >= floorPx) {
                        el.style.whiteSpace = 'nowrap';
                        el.style.fontSize = (oneLineFit * 0.96) + 'px';
                        return;
                    }
                    // Doesn't comfortably fit one line even shrunk to the
                    // floor — allow the wrap, sized so each of the two
                    // resulting lines fits its own width.
                    el.style.whiteSpace = 'normal';
                    var widest = breakIdx > -1
                        ? Math.max(
                            measureWidth(fullText.slice(0, breakIdx), twoLineMaxPx),
                            measureWidth(fullText.slice(breakIdx + 1), twoLineMaxPx)
                          )
                        : measureWidth(fullText, twoLineMaxPx);
                    var finalPx = widest <= containerWidth
                        ? twoLineMaxPx
                        : Math.max(1, twoLineMaxPx * (containerWidth / widest) * 0.9);
                    el.style.fontSize = finalPx + 'px';
                }
                fit();
                window.parent.addEventListener('resize', fit);
                if (window.parent.ResizeObserver) {
                    new window.parent.ResizeObserver(fit).observe(wrap);
                }
            })();
            </script>
            """,
            height=1,
        )
    assessment_instructions_note()

    if st.session_state.get("role") == "attending" and st.session_state.get("attending_login_email"):
        # Only for a logged-in attending's own "Start Assessment" flow —
        # an anonymous magic-link recipient (this same page, reached via
        # query params, no attending session at all) has no
        # attending_start/attending_home to go back to.
        with st.container(key="assess_top_nav"):
            _top_cols_att_assess = st.columns([1, 1, 4])
            with _top_cols_att_assess[0]:
                if st.button("⬅️ Back to Start", key="att_assess_back_top"):
                    go_to("attending_start")
            with _top_cols_att_assess[1]:
                if st.button("🏠 Home", key="att_assess_home_top"):
                    go_to("attending_home")

    if _is_robotic_procedure(_att_proc_name):
        robo_type = render_robo_type_picker("robo_type", default=(_draft or {}).get("robo_type", "Xi"))
    else:
        robo_type = None

    st.markdown("---")

    steps = steps_df[steps_df["procedure_id"] == procedure_id].sort_values("step_order")
    if steps.empty:
        st.error("This procedure has no defined steps. Please contact the program coordinator.")
        st.stop()

    # Defaults sourced from the draft when this link was pre-filled, else
    # the usual blanks — same fallback pattern the resident's own page uses.
    _d = _draft or {}
    # Resident:/Procedure:/Attending:/Date of Procedure used to be shown
    # here — dropped so this page matches the resident's own assessment
    # page, which doesn't show them either. The date is no longer
    # editable by the attending: it comes from the draft (self-assess
    # flow) or, for a blank link, the date the resident actually chose on
    # the Start page before generating it (attending_link_date, carried
    # by the link itself) — falling back to today only if neither is
    # present (e.g. an old link from before this was added).
    _date_source = _d.get("date") or st.session_state.get("attending_link_date")
    case_date = datetime.date.today()
    if _date_source:
        try:
            case_date = datetime.date.fromisoformat(str(_date_source)[:10])
        except ValueError:
            pass

    with st.container(key="assess_improve_how"):
        _att_imp_label_col, _att_imp_input_col = st.columns([2, 6])
        with _att_imp_label_col:
            st.markdown(
                '<p style="text-align: left;">In order to improve this:</p>',
                unsafe_allow_html=True,
            )
        with _att_imp_input_col:
            improve = st.text_input(
                "What to improve",
                value=_d.get("improve", ""),
                key="assess_improve",
                label_visibility="collapsed",
                placeholder="e.g., suture technique",
            )
        _att_how_label_col, _att_how_input_col = st.columns([2, 6])
        with _att_how_label_col:
            st.markdown("Do this:")
        with _att_how_input_col:
            how = st.text_input(
                "How to improve it",
                value=_d.get("how", ""),
                key="assess_how",
                label_visibility="collapsed",
                placeholder="e.g., practice two-handed knots",
            )
    sync_improve_how_label_width()

    st.markdown("---")

    with st.container(key="assess_ratings_row"):
        _att_overall_col, _att_prep_col = st.columns(2)
        with _att_overall_col:
            _att_o_default = _d.get("overall_performance", O_SCORE_OPTIONS[0])
            _att_o_idx = O_SCORE_OPTIONS.index(_att_o_default) if _att_o_default in O_SCORE_OPTIONS else 0
            o_score = st.selectbox("Overall Performance Rating", O_SCORE_OPTIONS, index=_att_o_idx, key="assess_overall_performance")
        with _att_prep_col:
            _att_cp_opts = ["Not Assessed", "Unprepared", "Poorly Prepared",
                            "Adequately Prepared", "Well Prepared", "Highly Prepared"]
            _att_cp_default = _d.get("case_preparation", "Not Assessed")
            _att_cp_idx = _att_cp_opts.index(_att_cp_default) if _att_cp_default in _att_cp_opts else 0
            case_preparation = st.selectbox("Daily Preparation", _att_cp_opts, index=_att_cp_idx, key="assess_preparation")

    _att_cc_opts = ["— Select complexity —", "Straight Forward", "Moderate", "Complex"]
    _att_cc_default = _d.get("case_complexity", "— Select complexity —")
    _att_cc_idx = _att_cc_opts.index(_att_cc_default) if _att_cc_default in _att_cc_opts else 0

    # The resolved (index-0-fallback-aware) starting value for each field —
    # used below to check whether "Changes As Made Above" is actually true,
    # not just checked. Comparing against these instead of the raw draft
    # dict avoids false "changed" positives from e.g. a blank/NaN draft
    # value resolving to the same displayed default the widget already
    # falls back to on its own.
    _draft_resolved_o           = O_SCORE_OPTIONS[_att_o_idx]
    _draft_resolved_preparation = _att_cp_opts[_att_cp_idx]
    _draft_resolved_complexity  = _att_cc_opts[_att_cc_idx]

    scores: dict = {}
    _draft_scores = _d.get("scores") or {}
    _draft_resolved_scores: dict = {}
    with st.expander(
        header_break_before("Step-Level Ratings for", _att_proc_name),
        expanded=True,
        key="step_ratings_expander_attending",
    ):
        # Case Complexity leads the Step-Level Ratings section, then each
        # procedure step in order.
        case_complexity = st.selectbox(
            "Case Complexity", _att_cc_opts, index=_att_cc_idx, key="assess_case_complexity"
        )
        for _, row in steps.iterrows():
            step_id   = row["step_id"]
            step_name = row["step_name"]
            _step_default = _draft_scores.get(step_id, "Not Assessed")
            _step_idx = RATING_OPTIONS.index(_step_default) if _step_default in RATING_OPTIONS else 0
            _draft_resolved_scores[step_id] = RATING_OPTIONS[_step_idx]
            scores[step_id] = st.selectbox(
                step_name, RATING_OPTIONS, index=_step_idx, key=f"att_score_{step_id}"
            )

    st.markdown("---")

    render_rating_legend(key="rating_legend_attending")
    render_prep_legend(key="prep_legend_attending")
    render_complexity_legend(key="complexity_legend_attending")

    st.markdown("---")

    notes = st.text_area(
        "Development / Improvement / Feed-Forward (optional)",
        value=_d.get("notes", ""),
        key="assess_notes",
    )

    _accept_no_changes   = False
    _accept_with_changes = False
    if _draft:
        st.markdown("---")
        st.markdown("**This form was pre-filled from the resident's self-assessment, please confirm:**")
        _accept_no_changes = st.checkbox(
            "No changes. Accept Resident Self-Assessment", key="assess_accept_no_changes"
        )
        _accept_with_changes = st.checkbox(
            "Changes As Made Above", key="assess_accept_with_changes"
        )

    st.markdown("---")
    if st.button("✅ Submit Evaluation", type="primary", width="stretch"):
        # Re-check right at submit time, not just at page load — closes
        # the gap where someone else (another tab, or the resident's
        # own badge/link opened twice) already reviewed and consumed
        # this same draft in between this page loading and this click.
        _draft_still_pending = True
        if draft_id:
            try:
                _draft_still_pending = load_draft(draft_id) is not None
            except ConnectionError as exc:
                show_gs_error(exc)
                st.stop()
        _has_value = (
            case_complexity != "— Select complexity —"
            or case_preparation != "Not Assessed"
            or o_score != O_SCORE_OPTIONS[0]
            or any(v != "Not Assessed" for v in scores.values())
            or notes.strip() != ""
            or improve.strip() != ""
            or how.strip() != ""
        )
        _matches_draft = (
            case_complexity == _draft_resolved_complexity
            and case_preparation == _draft_resolved_preparation
            and o_score == _draft_resolved_o
            and notes == _d.get("notes", "")
            and improve == _d.get("improve", "")
            and how == _d.get("how", "")
            and all(scores.get(sid) == _draft_resolved_scores.get(sid) for sid in scores)
        )
        if not _has_value:
            st.warning("Please provide at least one rating or comment before submitting.")
        elif draft_id and not _draft_still_pending:
            st.warning("This self-evaluation has already been reviewed and submitted — "
                       "someone (possibly you, in another tab) already submitted it. "
                       "Please refresh; no further action is needed.")
        elif _draft and not (_accept_no_changes or _accept_with_changes):
            st.warning("Please check one of the two boxes above before submitting.")
        elif _draft and _accept_no_changes and _accept_with_changes:
            st.warning("Please check only one of the two boxes above, not both.")
        elif _draft and _accept_with_changes and _matches_draft:
            st.warning("You checked “Changes As Made Above,” but nothing was actually "
                       "changed from the resident's self-assessment. Please make a change, "
                       "or check “No changes. Accept Resident Self-Assessment” instead.")
        elif _draft and _accept_no_changes and not _matches_draft:
            st.warning("You checked “No changes. Accept Resident Self-Assessment,” but the "
                       "form no longer matches the resident's original self-assessment. "
                       "Please check “Changes As Made Above” instead, or revert your edits "
                       "back to what the resident originally entered.")
        else:
            if _draft:
                _assessment_type = ("Attending Evaluation (Accepted Self-Assessment, No Changes)"
                                     if _accept_no_changes else
                                     "Attending Evaluation (Pre-filled, Changes Made)")
            else:
                _assessment_type = "Attending Evaluation (Blank)"
            # A logged-in attending has a real attending_id — use it so the
            # case is attributed properly (filters/exports by Attending, a
            # future "my submissions" view, etc.). The anonymous magic-link
            # flow has no such account, so it keeps the decodable magic_
            # prefix instead.
            _attending_id_for_save = (
                st.session_state.get("attending_login_id")
                if st.session_state.get("role") == "attending" and st.session_state.get("attending_login_id")
                else f"magic_{attending_name}"
            )
            # Field-by-field diff against the resident's original
            # self-assessment — only meaningful when there was a draft
            # to compare against at all (a blank assessment has no
            # "before" to diff against). Computed before save_case() so
            # it can be persisted onto the case row itself (as JSON) —
            # not just kept in session_state — so the same "what
            # changed" view can be shown later to the resident, in a
            # different session, from load_case_detail().
            _changes: list = []
            if _draft:
                if case_complexity != _draft_resolved_complexity:
                    _changes.append(("Case Complexity", _draft_resolved_complexity, case_complexity))
                if case_preparation != _draft_resolved_preparation:
                    _changes.append(("Daily Preparation", _draft_resolved_preparation, case_preparation))
                if o_score != _draft_resolved_o:
                    _changes.append(("Overall Performance", _draft_resolved_o, o_score))
                if notes != _d.get("notes", ""):
                    _changes.append(("Comments", _d.get("notes", "") or "(blank)", notes or "(blank)"))
                if improve != _d.get("improve", ""):
                    _changes.append(("In order to improve this", _d.get("improve", "") or "(blank)", improve or "(blank)"))
                if how != _d.get("how", ""):
                    _changes.append(("Do this", _d.get("how", "") or "(blank)", how or "(blank)"))
                _step_name_lookup = dict(zip(steps["step_id"], steps["step_name"]))
                for _sid, _new_val in scores.items():
                    _old_val = _draft_resolved_scores.get(_sid, "Not Assessed")
                    if _new_val != _old_val:
                        _changes.append((_step_name_lookup.get(_sid, _sid), _old_val, _new_val))
            _self_assessment_diff = (
                json.dumps({"had_draft": True, "changes": _changes}) if _draft else ""
            )
            try:
                case_id = save_case(
                    resident_email=resident_email,
                    date=case_date,
                    specialty_id=specialty_id,
                    procedure_id=procedure_id,
                    attending_id=_attending_id_for_save,
                    scores_dict=scores,
                    notes=notes,
                    case_complexity=case_complexity,
                    case_preparation=case_preparation,
                    overall_performance=o_score,
                    robo_type=robo_type,
                    improve=improve,
                    how=how,
                    assessment_type=_assessment_type,
                    self_assessment_diff=_self_assessment_diff,
                )
                if draft_id:
                    delete_draft(draft_id)
                # Store submission summary for the confirmation page
                st.session_state["attending_submission"] = {
                    "had_draft":           bool(_draft),
                    "changes":             _changes,
                    "case_id":             case_id,
                    "resident_email":      resident_email,
                    "resident_name":       _resident_display_name,
                    "procedure_id":        procedure_id,
                    "procedure_name":      _att_proc_name,
                    "attending_name":      display_attending,
                    "date":                str(case_date),
                    "case_complexity":     case_complexity,
                    "case_preparation":    case_preparation,
                    "overall_performance": o_score,
                    "robo_type":           robo_type,
                    "notes":               notes,
                    "improve":             improve,
                    "how":                 how,
                    "assessment_type":     _assessment_type,
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

    page_header("✅ Evaluation Submitted")
    st.success("Thank you! Your evaluation has been recorded.")

    _render_self_assessment_diff(sub)

    _render_evaluation_card(sub)

    st.markdown("---")
    if st.session_state.get("role") == "attending" and st.session_state.get("attending_login_email"):
        st.markdown("_The resident can view this evaluation in their dashboard._")
        _att_confirm_cols = st.columns(2)
        with _att_confirm_cols[0]:
            if st.button("➕ Start Another Assessment", type="primary", width="stretch", key="att_confirm_another"):
                go_to("attending_start")
        with _att_confirm_cols[1]:
            if st.button("🏠 Back to Home", width="stretch", key="att_confirm_home"):
                go_to("attending_home")
    else:
        st.markdown("_You may now close this window. The resident can view the evaluation in their dashboard._")


# Runs after every page render, regardless of which page/branch above
# executed, so it always fits whatever buttons ended up on screen.
fit_all_button_labels()
suppress_picker_keyboards()
