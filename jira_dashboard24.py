

"""
================================================================================
ISUV EXECUTIVE DASHBOARD  —  Response ID: 280526-14
(builds on 280526-12; rewrites the Label x Group table only)
================================================================================
Filters (server-side JQL):
  * status NOT IN (ARCHIVED, UNSUPPORTED, Community - Image Build,
                   Duplicate, Image Consumption, Community - Image QA)
  * labels IN  (Upgrade, "SEC FIX", "NEW IMAGE")

Existing tables (unchanged):
  - Group Summary
  - Assignee x Status (Group + L1..L4 + each status)

REWRITTEN — Label x Group (3 rows x 8 columns):

  Rows: UPGRADE, SEC_FIXES, NEW_IMAGE

  Columns:
    CT1_Group_Count       CT1_Group_SLA%
    CT2_Group_Count       CT2_Group_SLA%
    QA_Group_Count        QA_Group_SLA%
    Delivery_Group_Count  Delivery_Group_SLA%

  Logic per cell:
    Count = # of issues where
              status IN <group's statuses>
              AND label IN <row's label values>

    SLA%  = within_sla / count * 100
            where an issue is "within SLA" if:
              today <= created + allotted_days

  Allotted days matrix:
                 UPGRADE  SEC_FIXES  NEW_IMAGE
    CT1_Group       2         2          8
    CT2_Group       3         3          9
    QA_Group        5         5         12
    Delivery_Group  6         6         13

Run:
  pip install streamlit pandas requests
  streamlit run isuv_dashboard.py
================================================================================
"""

import json
import time
import requests
import pandas as pd
import streamlit as st
from requests.auth import HTTPBasicAuth
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# ============== PAGE CONFIG ==============
st.set_page_config(
    page_title="ISUV Executive Dashboard",
    page_icon="📊",
    layout="wide",
)

# ============== YOUR CREDENTIALS ==============
JIRA_URL = st.secrets["jira"]["url"]
EMAIL = st.secrets["jira"]["email"]
API_TOKEN = st.secrets["jira"]["api_token"]
PROJECT_KEY = "ISUV"
SAMPLE_SIZE = 5000

# Custom field id for "Level" — change if your instance uses a different one.
# Common pattern: customfield_10XXX. Set to None to auto-detect from issue fields.
LEVEL_FIELD = None  # e.g. "customfield_10050"  — leave None to auto-detect

PAGE_SIZE = 100  # Jira max per page for /rest/api/3/search is 100
# ==================================================


# If auto-detect can't find the Level field, set its id here, e.g. "customfield_10050"
LEVEL_FIELD_OVERRIDE = None
# ==============================================


EXCLUDED_STATUSES = [
    "ARCHIVED", "UNSUPPORTED", "Community - Image Build",
    "Duplicate", "Image Consumption", "Community - Image QA",
]

ALLOWED_LABELS = ["UPGRADE", "SEC_FIXES", "New_Image"]

GROUP_DEFINITIONS = [
    ("CT1_Group",      ["Package Queue", "Package QA", "PackageBuild"]),
    ("CT2_Group",      ["CT2 - IMAGE BUILD", "Image Queue"]),
    ("QA_Group",       ["Image QA"]),
    ("Delivery_Group", ["Image Delivery", "Customer Delivered", "VERIFIED"]),
    ("Threat_Group", ["PACKAGE VULNERABILITY", "THRT RESEARCH", "Image Vulnerability"]),
]

# Map display row name -> the lowercased Jira label values that count for that row.
# Tolerates "SEC FIX"/"SEC_FIX" and "NEW IMAGE"/"NEW_IMAGE" spellings, just in case.
LABEL_ROWS = [
    ("UPGRADE",   ["Upgrade","upgrade","UPGRADE","UPGRADES","upgrade-only"]),
    ("SEC_FIXES", ["sec fix", "sec_fix", "sec_fixes", "sec-fix","SEC_FIXES"]),
    ("NEW_IMAGE", ["new image", "new_image", "newimage","New_Image"]),
]

# Allotted SLA days [group][label_row_display_name]
SLA_DAYS = {
    "CT1_Group":      {"UPGRADE": 2, "SEC_FIXES": 2, "NEW_IMAGE": 8},
    "CT2_Group":      {"UPGRADE": 3, "SEC_FIXES": 3, "NEW_IMAGE": 9},
    "QA_Group":       {"UPGRADE": 5, "SEC_FIXES": 5, "NEW_IMAGE": 12},
    "Delivery_Group": {"UPGRADE": 6, "SEC_FIXES": 6, "NEW_IMAGE": 13},
    "Threat_Group": {"UPGRADE": 0, "SEC_FIXES": 0, "NEW_IMAGE": 0},
}

PAGE_SIZE = 100
MAX_PAGES = 1000


# -------------------------------------------------- connection
def get_auth():
    return HTTPBasicAuth(EMAIL, API_TOKEN)


def get_headers():
    return {"Accept": "application/json", "Content-Type": "application/json"}


# -------------------------------------------------- JQL
def build_jql():
    excluded = ", ".join(f'"{s}"' for s in EXCLUDED_STATUSES)
    labels   = ", ".join(f'"{l}"' for l in ALLOWED_LABELS)
    return (
        f'project = {PROJECT_KEY} '
        f'AND status NOT IN ({excluded}) '
        f'AND labels IN ({labels})'
    )


# -------------------------------------------------- detect Level field
@st.cache_data(ttl=3600, show_spinner=False)
def detect_level_field():
    if LEVEL_FIELD_OVERRIDE:
        return LEVEL_FIELD_OVERRIDE
    r = requests.get(
        f"{JIRA_URL}/rest/api/3/field",
        auth=get_auth(), headers={"Accept": "application/json"}, timeout=30,
    )
    if r.status_code != 200:
        return None
    for f in r.json():
        if f.get("name", "").strip().lower() == "level":
            return f.get("id")
    return None


# -------------------------------------------------- API: count
def get_filtered_count(jql):
    r = requests.post(
        f"{JIRA_URL}/rest/api/3/search/approximate-count",
        auth=get_auth(), headers=get_headers(),
        data=json.dumps({"jql": jql}), timeout=30,
    )
    r.raise_for_status()
    return r.json().get("count", 0)


# -------------------------------------------------- API: paginated fetch
def fetch_filtered_issues(jql, level_field, progress_cb=None):
    fields = ["assignee", "status", "labels", "created"]
    if level_field:
        fields.append(level_field)

    issues, next_token, pages, seen = [], None, 0, set()
    while True:
        payload = {"jql": jql, "fields": fields, "maxResults": PAGE_SIZE}
        if next_token is not None:
            payload["nextPageToken"] = next_token

        r = requests.post(
            f"{JIRA_URL}/rest/api/3/search/jql",
            auth=get_auth(), headers=get_headers(),
            data=json.dumps(payload), timeout=60,
        )
        r.raise_for_status()
        data = r.json()

        batch = data.get("issues", [])
        issues.extend(batch)
        pages += 1
        if progress_cb:
            progress_cb(len(issues), pages)

        is_last = data.get("isLast", False)
        next_token = data.get("nextPageToken")
        if is_last or not next_token or not batch:
            break
        if next_token in seen or pages >= MAX_PAGES:
            break
        seen.add(next_token)
    return issues


# -------------------------------------------------- field extraction
def extract_assignee(f):
    a = f.get("assignee")
    if not a:
        return "Unassigned"
    return a.get("displayName") or a.get("emailAddress") or "Unassigned"


def extract_status(f):
    return (f.get("status") or {}).get("name", "Unknown")


def extract_level(f, level_field_id):
    if not level_field_id:
        return None
    v = f.get(level_field_id)
    if v is None:
        return None
    if isinstance(v, dict):
        val = v.get("value") or v.get("name")
    elif isinstance(v, list) and v:
        first = v[0]
        val = first.get("value") or first.get("name") if isinstance(first, dict) else str(first)
    else:
        val = str(v)
    val = (val or "").strip().upper()
    return val if val in ("L1", "L2", "L3", "L4") else None


def extract_labels_lower(f):
    return [str(l).strip().lower() for l in (f.get("labels") or []) if l]


def extract_created(f):
    s = f.get("created")
    if not s:
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        if len(s) >= 5 and (s[-5] in "+-") and s[-3] != ":":
            s = s[:-2] + ":" + s[-2:]
        return datetime.fromisoformat(s)
    except Exception:
        return None


# -------------------------------------------------- group helpers
def determine_group_for_assignee(assignee_status_counts):
    """Used by the existing Assignee table only."""
    best_group, best_count = None, 0
    for group_name, statuses in GROUP_DEFINITIONS:
        count = sum(assignee_status_counts.get(s, 0) for s in statuses)
        if count > best_count:
            best_count = count
            best_group = group_name
    return best_group if best_group else "—"


def status_to_group(status):
    """Map an issue's current status to a group. Returns None if no match."""
    for group_name, statuses in GROUP_DEFINITIONS:
        if status in statuses:
            return group_name
    return None


def issue_label_row(labels_lower):
    """Return the display row name (UPGRADE / SEC_FIXES / NEW_IMAGE) or None."""
    for row_name, accepted in LABEL_ROWS:
        for v in accepted:
            if v in labels_lower:
                return row_name
    return None


# -------------------------------------------------- NEW: Label x Group table
def build_label_group_table(issues, now_utc):
    """
    Rows = UPGRADE, SEC_FIXES, NEW_IMAGE
    Columns (per group, in GROUP_DEFINITIONS order):
        <Group>_Count   <Group>_SLA%
    Logic:
        - Bucket issues by (label_row, group-from-status).
        - Count = total issues in that bucket.
        - SLA%  = within_sla / count * 100.
                  Within-SLA means today <= created + allotted_days.
    """
    # buckets[(row, group)] = {"count": n, "within": n, "no_date": n}
    buckets = defaultdict(lambda: {"count": 0, "within": 0, "no_date": 0})

    for issue in issues:
        f = issue.get("fields", {})

        status = extract_status(f)
        group  = status_to_group(status)
        if not group:
            continue   # status not in any of our 4 buckets

        labels = extract_labels_lower(f)
        row    = issue_label_row(labels)
        if not row:
            continue   # label not one of UPGRADE/SEC_FIXES/NEW_IMAGE

        allotted = SLA_DAYS[group][row]

        b = buckets[(row, group)]
        b["count"] += 1

        created = extract_created(f)
        if not created:
            b["no_date"] += 1
            # cannot judge SLA without date — exclude from SLA denominator
            continue

        due_at = created + timedelta(days=allotted)
        if now_utc <= due_at:
            b["within"] += 1
        # else: breached -> contributes to denominator but not numerator

    # Build the dataframe
    rows_out = []
    group_names = [g for g, _ in GROUP_DEFINITIONS]

    for row_name, _ in LABEL_ROWS:
        row_data = {"Label": row_name}
        for g in group_names:
            b = buckets[(row_name, g)]
            count = b["count"]
            judged = count - b["no_date"]
            if judged > 0:
                sla_pct = b["within"] / judged * 100.0
                sla_val = round(sla_pct, 1)
            else:
                sla_val = None
            row_data[f"{g}_Count"] = count
            row_data[f"{g}_SLA%"]  = sla_val
        rows_out.append(row_data)

    cols = ["Label"]
    for g in group_names:
        cols.extend([f"{g}_Count", f"{g}_SLA%"])

    df = pd.DataFrame(rows_out, columns=cols)
    return df, buckets


# -------------------------------------------------- aggregation (existing table)
def aggregate(issues, level_field_id):
    matrix = defaultdict(lambda: defaultdict(int))
    levels = defaultdict(lambda: defaultdict(int))
    totals = defaultdict(int)
    statuses_seen = set()

    for issue in issues:
        f = issue.get("fields", {})
        assignee = extract_assignee(f)
        status   = extract_status(f)
        level    = extract_level(f, level_field_id)

        matrix[assignee][status] += 1
        totals[assignee] += 1
        statuses_seen.add(status)
        if level:
            levels[assignee][level] += 1

    groups = {a: determine_group_for_assignee(matrix[a]) for a in totals}
    return matrix, levels, totals, groups, sorted(statuses_seen)


def build_assignee_dataframe(matrix, levels, totals, groups, status_cols):
    level_cols = ["L1", "L2", "L3", "L4"]
    group_order = {name: i for i, (name, _) in enumerate(GROUP_DEFINITIONS)}
    group_order["—"] = len(GROUP_DEFINITIONS)
    sorted_assignees = sorted(
        totals.keys(),
        key=lambda a: (group_order.get(groups[a], 99), -totals[a], a.lower()),
    )
    rows = []
    for a in sorted_assignees:
        row = {"Assignee": a, "Group": groups[a], "Total": totals[a]}
        for lvl in level_cols:
            row[lvl] = levels[a].get(lvl, 0)
        for s in status_cols:
            row[s] = matrix[a].get(s, 0)
        rows.append(row)
    df = pd.DataFrame(rows, columns=["Assignee", "Group", "Total"] + level_cols + status_cols)

    grand = {"Assignee": "GRAND TOTAL", "Group": "", "Total": int(df["Total"].sum())}
    for lvl in level_cols:
        grand[lvl] = int(df[lvl].sum())
    for s in status_cols:
        grand[s] = int(df[s].sum())
    df = pd.concat([df, pd.DataFrame([grand])], ignore_index=True)
    return df


def build_group_summary(groups, totals):
    head = defaultdict(int)
    issues_per_group = defaultdict(int)
    for a, g in groups.items():
        head[g] += 1
        issues_per_group[g] += totals[a]
    rows = []
    for name, _ in GROUP_DEFINITIONS:
        rows.append({"Group": name,
                     "Headcount": head.get(name, 0),
                     "Total Issues": issues_per_group.get(name, 0)})
    if head.get("—", 0):
        rows.append({"Group": "— (no matching statuses)",
                     "Headcount": head["—"],
                     "Total Issues": issues_per_group["—"]})
    return pd.DataFrame(rows)


# -------------------------------------------------- cached loader
@st.cache_data(ttl=300, show_spinner=False)
def load_dashboard_data():
    jql = build_jql()
    level_field = detect_level_field()
    total_count = get_filtered_count(jql)

    progress_text = st.empty()
    def _progress(n, p):
        progress_text.caption(f"📥 Fetched {n} issues across {p} page(s)...")
    issues = fetch_filtered_issues(jql, level_field, progress_cb=_progress)
    progress_text.empty()

    matrix, levels, totals, groups, status_cols = aggregate(issues, level_field)
    df = build_assignee_dataframe(matrix, levels, totals, groups, status_cols)
    group_summary = build_group_summary(groups, totals)

    # NEW Label x Group table
    now_utc = datetime.now(timezone.utc)
    lg_df, lg_buckets = build_label_group_table(issues, now_utc)

    # Detail rows for transparency
    detail_rows = []
    for (row_name, _), in [((rn, ax),) for rn, ax in LABEL_ROWS]:  # row order
        pass
    for row_name, _ in LABEL_ROWS:
        for g, _ in GROUP_DEFINITIONS:
            b = lg_buckets[(row_name, g)]
            judged = b["count"] - b["no_date"]
            detail_rows.append({
                "Label":           row_name,
                "Group":           g,
                "Allotted Days":   SLA_DAYS[g][row_name],
                "Total Issues":    b["count"],
                "Within SLA":      b["within"],
                "Breached":        max(0, judged - b["within"]),
                #"Missing Created": b["no_date"],
                "SLA %":           round(b["within"] / judged * 100, 1) if judged > 0 else None,
            })
    detail_df = pd.DataFrame(detail_rows)

    return {
        "jql":           jql,
        "level_field":   level_field,
        "approx_total":  total_count,
        "fetched":       len(issues),
        "assignees":     len(totals),
        "status_cols":   status_cols,
        "df":            df,
        "group_summary": group_summary,
        "lg_df":         lg_df,
        "lg_detail":     detail_df,
        "now_utc":       now_utc.isoformat(),
    }


# -------------------------------------------------- UI
st.title("📊 ISUV Executive Dashboard")
#st.caption("Response 280526-14 · Label × Group rewritten · Count + SLA% per group")

with st.expander("ℹ️ Group rules (status-based for Label × Group table)"):
    for name, statuses in GROUP_DEFINITIONS:
        st.markdown(f"- **{name}** ← issues with status in: {', '.join(statuses)}")

with st.expander("⏱️ SLA scoring rules"):
    st.markdown(
        "**Per issue:** breached if `today > created + allotted_days`.  \n"
        "**SLA%** for a cell = `within_sla_count / total_judged × 100`.  \n"
        #"Issues missing a creation date are excluded from the SLA denominator only.  \n"
        "**100%** = `nothing breached`.  \n"
        "**0%** = `everything breached`. \n"
    )
    sla_pretty = pd.DataFrame({
        "Group":     list(SLA_DAYS.keys()),
        "UPGRADE":   [SLA_DAYS[g]["UPGRADE"]   for g in SLA_DAYS],
        "SEC_FIXES": [SLA_DAYS[g]["SEC_FIXES"] for g in SLA_DAYS],
        "NEW_IMAGE": [SLA_DAYS[g]["NEW_IMAGE"] for g in SLA_DAYS],
    })
    st.dataframe(sla_pretty, hide_index=True, use_container_width=True)

with st.expander("🔧 JQL used"):
    st.code(build_jql(), language="sql")

c, _ = st.columns([1, 9])
with c:
    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

st.divider()

# Credentials
if "YOUR_EMAIL" in EMAIL or "YOUR_API_TOKEN" in API_TOKEN:
    st.error("⚠️ Please set EMAIL and API_TOKEN at the top of the script.")
    st.stop()

# Connection
try:
    t0 = time.time()
    test = requests.get(f"{JIRA_URL}/rest/api/3/myself",
                        auth=get_auth(), headers={"Accept": "application/json"}, timeout=10)
    test.raise_for_status()
    #st.success(f"✅ Connected as **{test.json().get('displayName')}**")
    st.success(f"✅ Connected as **Amit Jain**")
except requests.HTTPError as e:
    st.error(f"❌ Connection failed: {e}")
    st.stop()
except Exception as e:
    st.error(f"❌ Connection error: {e}")
    st.stop()

# Load
with st.spinner("Loading ISUV data..."):
    try:
        data = load_dashboard_data()
    except requests.HTTPError as e:
        st.error(f"❌ Jira API error: {e}")
        if e.response is not None:
            st.code(e.response.text[:500])
        st.stop()
    except Exception as e:
        st.error(f"❌ Error: {e}")
        st.stop()

elapsed = time.time() - t0

# Summary
st.subheader("📈 Summary")
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Approx Total (filtered)", f"{data['approx_total']:,}")
m2.metric("Issues Fetched", f"{data['fetched']:,}")
m3.metric("Assignees", data["assignees"])
m4.metric("Status Columns", len(data["status_cols"]))
level_total = int(data["df"][["L1", "L2", "L3", "L4"]].iloc[-1].sum())
m5.metric("L1–L4 Tagged", f"{level_total:,}")
st.caption(f"⏱️ Loaded in {elapsed:.1f}s · Cached 5 min · "
           f"Level field: **{data['level_field'] or '⚠ NOT FOUND'}**")

# Group summary
st.subheader("👥 Group Summary")
st.dataframe(data["group_summary"], hide_index=True,
             use_container_width=True, height=210)

# NEW: Label x Group table
st.subheader("🏷️ Label × Group — Count + SLA%")

def _color_sla(val):
    if pd.isna(val) or val is None:
        return "color: #999"
    if val >= 90:
        return "background-color: #d4edda; color: #155724"
    if val >= 70:
        return "background-color: #fff3cd; color: #856404"
    if val >= 40:
        return "background-color: #ffe0b3; color: #663d00"
    return "background-color: #f8d7da; color: #721c24"

sla_cols = [c for c in data["lg_df"].columns if c.endswith("_SLA%")]
count_cols = [c for c in data["lg_df"].columns if c.endswith("_Count")]
fmt = {c: "{:.1f}%" for c in sla_cols}
fmt.update({c: "{:,}" for c in count_cols})

lg_styled = (
    data["lg_df"]
        .style
        .format(fmt, na_rep="—")
        .applymap(_color_sla, subset=sla_cols)
)
st.dataframe(lg_styled, hide_index=True, use_container_width=True, height=180)

with st.expander("🔎 Per-cell detail (counts behind the SLA %)"):
    st.dataframe(data["lg_detail"], hide_index=True, use_container_width=True)

st.download_button(
    "📥 Download Label × Group CSV",
    data["lg_df"].to_csv(index=False),
    file_name=f"ISUV_label_group_{time.strftime('%Y%m%d_%H%M%S')}.csv",
    mime="text/csv",
    key="dl_lg",
)

st.divider()

# Assignee table (unchanged)
st.subheader("📋 Assignee × Status (with Group + L1–L4)")
st.dataframe(data["df"], hide_index=True, use_container_width=True, height=600)

st.download_button(
    "📥 Download Assignee CSV",
    data["df"].to_csv(index=False),
    file_name=f"ISUV_dashboard_{time.strftime('%Y%m%d_%H%M%S')}.csv",
    mime="text/csv",
    key="dl_main",
)

st.divider()
st.caption(
    "💡 If a Count is 0 where you expected non-zero, check the per-cell detail expander "
    "and compare against the status spellings in the Assignee × Status table — the Group "
    "lookup is exact-match on status name."
)