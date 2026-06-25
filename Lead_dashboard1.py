"""
================================================================================
LEAD PROJECT — EPIC & ALL CHILDREN DASHBOARD  —  Response ID: 280526-17
(fixes 280526-16: now shows stories/tasks/bugs AND their sub-tasks under each Epic)
================================================================================
Project: LEAD

How children are resolved under each Epic:
  1. Fetch every issue in the project in one paginated sweep.
  2. For each issue, record both:
     - parent_key  -> from the 'parent' field (modern Jira)
     - epic_link   -> from the legacy Epic Link custom field (auto-detected)
  3. Walk the parent chain upward until we hit an Epic. That Epic becomes
     the issue's "owning epic". Catches:
        * direct children (Story / Task / Bug under Epic)
        * grandchildren (Sub-task under Story under Epic)
        * legacy Epic Link references

Tables shown:
  1. Epics
  2. Per-Epic children (all types, grouped by Epic)
  3. Orphans (children not under any Epic)
  4. Progress by Assignee — Day / Week / Month / Quarter

Run:
  pip install streamlit pandas requests
  streamlit run lead_dashboard.py
================================================================================
"""

import json
import time
import requests
import pandas as pd
import streamlit as st
from requests.auth import HTTPBasicAuth
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ============== PAGE CONFIG ==============
st.set_page_config(
    page_title="LEAD Project Dashboard",
    page_icon="🎯",
    layout="wide",
)


# ============== YOUR CREDENTIALS ==============
JIRA_URL = st.secrets["jira"]["url"]
EMAIL = st.secrets["jira"]["email"]
API_TOKEN = st.secrets["jira"]["api_token"]
PROJECT_KEY = "LEAD"



AGED_DAYS_THRESHOLD = 30   # open issue with no due-date older than this = Aged

PAGE_SIZE = 100
MAX_PAGES = 1000


# -------------------------------------------------- connection
def get_auth():
    return HTTPBasicAuth(EMAIL, API_TOKEN)


def get_headers():
    return {"Accept": "application/json", "Content-Type": "application/json"}


# -------------------------------------------------- detect Epic Link custom field
@st.cache_data(ttl=3600, show_spinner=False)
def detect_epic_link_field():
    """
    Legacy Jira used a custom field named 'Epic Link' (often customfield_10014).
    Returns the field id or None.
    """
    r = requests.get(
        f"{JIRA_URL}/rest/api/3/field",
        auth=get_auth(), headers={"Accept": "application/json"}, timeout=30,
    )
    if r.status_code != 200:
        return None
    for f in r.json():
        if f.get("name", "").strip().lower() == "epic link":
            return f.get("id")
    return None


# -------------------------------------------------- API
def get_total_count(jql):
    r = requests.post(
        f"{JIRA_URL}/rest/api/3/search/approximate-count",
        auth=get_auth(), headers=get_headers(),
        data=json.dumps({"jql": jql}), timeout=30,
    )
    r.raise_for_status()
    return r.json().get("count", 0)


def fetch_issues(jql, fields, progress_cb=None):
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

        if data.get("isLast"):
            break
        next_token = data.get("nextPageToken")
        if not next_token or not batch:
            break
        if next_token in seen or pages >= MAX_PAGES:
            break
        seen.add(next_token)
    return issues


# -------------------------------------------------- parsing helpers
def parse_jira_dt(s):
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


def parse_jira_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def extract_basic(issue, epic_link_field):
    f = issue.get("fields", {})

    a = f.get("assignee") or {}
    assignee = a.get("displayName") or a.get("emailAddress") or "Unassigned"

    status_obj = f.get("status") or {}
    status     = status_obj.get("name", "Unknown")
    status_cat = (status_obj.get("statusCategory") or {}).get("key", "")

    issuetype  = (f.get("issuetype") or {}).get("name", "")
    parent_obj = f.get("parent") or {}
    parent_key = parent_obj.get("key")

    epic_link = None
    if epic_link_field:
        epic_link = f.get(epic_link_field)

    created   = parse_jira_dt(f.get("created"))
    duedate   = parse_jira_date(f.get("duedate"))
    resolved  = parse_jira_dt(f.get("resolutiondate"))

    return {
        "Key":       issue.get("key"),
        "Summary":   (f.get("summary") or "")[:120],
        "Type":      issuetype,
        "Assignee":  assignee,
        "Status":    status,
        "StatusCat": status_cat,
        "Created":   created,
        "DueDate":   duedate,
        "Resolved":  resolved,
        "ParentKey": parent_key,
        "EpicLink":  epic_link,
    }


def compute_health(row, now_utc):
    age_days = None
    if row["Created"]:
        age_days = (now_utc - row["Created"]).days

    lateness = None
    if row["DueDate"]:
        ref = row["Resolved"] or now_utc
        diff = (ref - row["DueDate"]).days
        if diff > 0:
            lateness = diff

    if row["StatusCat"] == "done":
        health = "Done"
    elif row["DueDate"] and now_utc > row["DueDate"]:
        health = "Delayed"
    elif (row["DueDate"] is None) and age_days is not None and age_days > AGED_DAYS_THRESHOLD:
        health = "Aged"
    else:
        health = "On Track"

    return {**row, "AgeDays": age_days, "LatenessDays": lateness, "Health": health}


def fmt_dt(d):
    return d.strftime("%Y-%m-%d") if d else ""


# -------------------------------------------------- owning-epic resolution (NEW)
def resolve_owning_epic(item, by_key, epic_keys, max_depth=6):
    """
    Walk up the parent chain (and the legacy Epic Link) until we reach an Epic.
    Returns the Epic key, or None.
    """
    visited = set()
    cur = item

    for _ in range(max_depth):
        # Direct hit via Epic Link
        if cur.get("EpicLink") and cur["EpicLink"] in epic_keys:
            return cur["EpicLink"]

        # Direct parent IS an epic
        pk = cur.get("ParentKey")
        if pk and pk in epic_keys:
            return pk

        # Climb one level via parent
        if not pk or pk in visited or pk not in by_key:
            break
        visited.add(pk)
        cur = by_key[pk]

    return None


# -------------------------------------------------- progress windows
def in_window(dt, start, end):
    return dt is not None and start <= dt < end


def build_progress_table(rows, now_utc, window):
    if window == "day":
        start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        end   = start + timedelta(days=1)
        label = f"Today ({start.date()})"
    elif window == "week":
        today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        start = today - timedelta(days=today.weekday())
        end   = start + timedelta(days=7)
        label = f"This week (from {start.date()})"
    elif window == "month":
        start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        label = f"This month ({start.strftime('%b %Y')})"
    elif window == "quarter":
        q_start_month = ((now_utc.month - 1) // 3) * 3 + 1
        start = now_utc.replace(month=q_start_month, day=1,
                                hour=0, minute=0, second=0, microsecond=0)
        end_month = q_start_month + 3
        if end_month > 12:
            end = start.replace(year=start.year + 1, month=end_month - 12)
        else:
            end = start.replace(month=end_month)
        q_num = (q_start_month - 1) // 3 + 1
        label = f"This quarter (Q{q_num} {start.year})"

    # Counts split by type (Epic vs everything else)
    counts = defaultdict(lambda: {"Epics": 0, "Stories/Tasks/Bugs": 0, "Sub-tasks": 0})
    for r in rows:
        if r["StatusCat"] != "done":
            continue
        if not in_window(r["Resolved"], start, end):
            continue
        t = r["Type"]
        if t == "Epic":
            counts[r["Assignee"]]["Epics"] += 1
        elif t in ("Sub-task", "Subtask", "Sub Task", "Sub-Task"):
            counts[r["Assignee"]]["Sub-tasks"] += 1
        else:
            counts[r["Assignee"]]["Stories/Tasks/Bugs"] += 1

    out = []
    for a, c in counts.items():
        total = c["Epics"] + c["Stories/Tasks/Bugs"] + c["Sub-tasks"]
        out.append({
            "Assignee":            a,
            "Epics":               c["Epics"],
            "Stories/Tasks/Bugs":  c["Stories/Tasks/Bugs"],
            "Sub-tasks":           c["Sub-tasks"],
            "Total Completed":     total,
        })

    cols = ["Assignee", "Epics", "Stories/Tasks/Bugs", "Sub-tasks", "Total Completed"]
    df = pd.DataFrame(out, columns=cols)

    if not df.empty:
        df = df.sort_values("Total Completed", ascending=False).reset_index(drop=True)
        total_row = {
            "Assignee":            "TOTAL",
            "Epics":               int(df["Epics"].sum()),
            "Stories/Tasks/Bugs":  int(df["Stories/Tasks/Bugs"].sum()),
            "Sub-tasks":           int(df["Sub-tasks"].sum()),
            "Total Completed":     int(df["Total Completed"].sum()),
        }
        df = pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)

    return df, label


# -------------------------------------------------- cached loader
@st.cache_data(ttl=300, show_spinner=False)
def load_dashboard_data():
    epic_link_field = detect_epic_link_field()

    fields = [
        "summary", "status", "assignee", "issuetype",
        "created", "duedate", "resolutiondate", "parent",
    ]
    if epic_link_field:
        fields.append(epic_link_field)

    # Single sweep: every issue in the project
    jql_all = f'project = {PROJECT_KEY}'
    total_count = get_total_count(jql_all)

    progress_text = st.empty()
    def _progress(n, p):
        progress_text.caption(f"📥 Fetched {n}/{total_count} issues across {p} page(s)...")
    raw = fetch_issues(jql_all, fields, progress_cb=_progress)
    progress_text.empty()

    now_utc = datetime.now(timezone.utc)
    items = [compute_health(extract_basic(i, epic_link_field), now_utc) for i in raw]

    # Build lookups
    by_key    = {it["Key"]: it for it in items}
    epic_keys = {it["Key"] for it in items if it["Type"] == "Epic"}

    epics    = [it for it in items if it["Type"] == "Epic"]
    children = [it for it in items if it["Type"] != "Epic"]

    # Resolve each non-epic item to its owning Epic (or None for orphans)
    children_by_epic = defaultdict(list)
    orphans = []
    for ch in children:
        owner = resolve_owning_epic(ch, by_key, epic_keys)
        if owner:
            children_by_epic[owner].append(ch)
        else:
            orphans.append(ch)

    # Build flat DataFrame for epics table
    epics_df = pd.DataFrame([{
        "Key":          e["Key"],
        "Summary":      e["Summary"],
        "Assignee":     e["Assignee"],
        "Status":       e["Status"],
        "Health":       e["Health"],
        "Start":        fmt_dt(e["Created"]),
        "End (Due)":    fmt_dt(e["DueDate"]),
        "Resolved":     fmt_dt(e["Resolved"]),
        "Age (days)":   e["AgeDays"],
        "Late (days)":  e["LatenessDays"],
        "Children":     len(children_by_epic.get(e["Key"], [])),
    } for e in epics])

    return {
        "epic_link_field":  epic_link_field,
        "total_count":      total_count,
        "epics":            epics,
        "children":         children,
        "epics_df":         epics_df,
        "children_by_epic": dict(children_by_epic),
        "orphans":          orphans,
        "all_rows":         items,
        "now_utc":          now_utc,
    }


# -------------------------------------------------- styling
HEALTH_COLORS = {
    "Done":     "background-color: #d4edda; color: #155724",
    "On Track": "background-color: #d1ecf1; color: #0c5460",
    "Aged":     "background-color: #fff3cd; color: #856404",
    "Delayed":  "background-color: #f8d7da; color: #721c24",
}

def style_health(val):
    return HEALTH_COLORS.get(val, "")


def child_table(items):
    df = pd.DataFrame([{
        "Key":         c["Key"],
        "Type":        c["Type"],
        "Summary":     c["Summary"],
        "Assignee":    c["Assignee"],
        "Status":      c["Status"],
        "Health":      c["Health"],
        "Start":       fmt_dt(c["Created"]),
        "End (Due)":   fmt_dt(c["DueDate"]),
        "Resolved":    fmt_dt(c["Resolved"]),
        "Age (days)":  c["AgeDays"],
        "Late (days)": c["LatenessDays"],
        "Parent":      c["ParentKey"] or "",
    } for c in items])
    if df.empty:
        return df
    # Order: type (Story/Task/Bug first, then Sub-task), then health priority
    type_order = {"Story": 0, "Task": 1, "Bug": 2}
    health_order = {"Delayed": 0, "Aged": 1, "On Track": 2, "Done": 3}
    df["_t"] = df["Type"].map(lambda t: type_order.get(t, 5 if "Sub" in t else 3))
    df["_h"] = df["Health"].map(lambda h: health_order.get(h, 9))
    df = df.sort_values(["_t", "_h", "Key"]).drop(columns=["_t", "_h"]).reset_index(drop=True)
    return df


# -------------------------------------------------- UI
st.title("🎯 Leadership Project — Epics & Stories Dashboard")
#st.caption("Response 280526-17 · Now resolves Stories, Tasks, Bugs, and their Sub-tasks under each Epic")


c, _ = st.columns([1, 9])
with c:
    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

st.divider()

# Credential check
if "YOUR_EMAIL" in EMAIL or "YOUR_API_TOKEN" in API_TOKEN:
    st.error("⚠️ Please set EMAIL and API_TOKEN at the top of the script.")
    st.stop()

# Connection
try:
    t0 = time.time()
    r = requests.get(f"{JIRA_URL}/rest/api/3/myself",
                     auth=get_auth(), headers={"Accept": "application/json"},
                     timeout=10)
    r.raise_for_status()
    st.success(f"✅ Connected")
except Exception as e:
    st.error(f"❌ Connection error: {e}")
    st.stop()

with st.spinner("Loading LEAD data..."):
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

# ---------- summary ----------
st.subheader("📈 Summary")
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Total Issues", f"{data['total_count']:,}")
m2.metric("Epics", len(data["epics"]))
m3.metric("Stories Mapped",
          sum(len(v) for v in data["children_by_epic"].values()))
m4.metric("Orphans (Untagged Stories)", len(data["orphans"]))
m5.metric("Done",
          sum(1 for it in data["all_rows"] if it["StatusCat"] == "done"))

st.caption(
    f"⏱️ Loaded in {elapsed:.1f}s  ")

st.divider()

# ---------- 1. Epics table ----------
st.subheader("1️⃣ Epics")
if data["epics_df"].empty:
    st.info("No epics found in LEAD.")
else:
    epics_styled = data["epics_df"].style.map(style_health, subset=["Health"])
    st.dataframe(epics_styled, hide_index=True, use_container_width=True, height=400)
    st.download_button(
        "📥 Download Epics CSV",
        data["epics_df"].to_csv(index=False),
        file_name=f"LEAD_epics_{time.strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
        key="dl_epics",
    )

st.divider()

# ---------- 2. Stories under each Epic ----------
st.subheader("2️⃣ Stories under each Epic ")

if not data["epics"]:
    st.info("No epics, so no groupings to show.")
else:
    health_order = {"Delayed": 0, "Aged": 1, "On Track": 2, "Done": 3}
    sorted_epics = sorted(
        data["epics"],
        key=lambda e: (health_order.get(e["Health"], 99), e["Key"] or "")
    )

    for epic in sorted_epics:
        kids = data["children_by_epic"].get(epic["Key"], [])
        header = (
            f"**{epic['Key']}** — {epic['Summary']}  ·  "
            f"👤 {epic['Assignee']}  ·  📍 {epic['Status']}  ·  "
            f"🏷 {epic['Health']}  ·  {len(kids)} Stories"
        )
        with st.expander(header, expanded=False):
            if not kids:
                st.caption("No children linked to this Epic.")
            else:
                # Show a small per-type breakdown
                type_counts = defaultdict(int)
                for k in kids:
                    type_counts[k["Type"]] += 1
                bits = " · ".join(f"{t}: {n}" for t, n in sorted(type_counts.items()))
                st.caption(bits)

                df = child_table(kids)
                st.dataframe(
                    df.style.map(style_health, subset=["Health"]),
                    hide_index=True, use_container_width=True,
                )

    # Orphans
    if data["orphans"]:
        st.markdown("##### 📎 Orphans — Stories are not tagged to any Epic")
        df = child_table(data["orphans"])
        st.dataframe(
            df.style.map(style_health, subset=["Health"]),
            hide_index=True, use_container_width=True, height=300,
        )
        st.caption(
            f"{len(data['orphans'])} item(s). "
            "These are stories/tasks/bugs/sub-tasks whose parent chain doesn't lead to an Epic. "
            "Often expected (standalone tasks, project-level bugs, etc)."
        )

st.divider()

# ---------- 3. Progress by Assignee (Today/Week/Month/Quarter) ----------
st.subheader("3️⃣ Progress by Assignee — Completed Work (Today/Week/Month/Quarter)")

tabs = st.tabs(["📅 Today", "🗓 This Week", "📆 This Month", "🗂 This Quarter"])
for win, tab in zip(["day", "week", "month", "quarter"], tabs):
    with tab:
        df, label = build_progress_table(data["all_rows"], data["now_utc"], win)
        st.caption(label)
        if df.empty:
            st.info("Nothing completed in this window yet.")
        else:
            st.dataframe(df, hide_index=True, use_container_width=True, height=400)
            st.download_button(
                f"📥 Download {win.capitalize()} CSV",
                df.to_csv(index=False),
                file_name=f"LEAD_progress_{win}_{time.strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key=f"dl_{win}",
            )

st.divider()
st.caption(
    "💡 If an Epic shows 0 Stories but you expect more, check the Orphans section — "
    "those items likely lack a Epic linking back to the Epic in Jira. "
    "Resolution requires fixing the link in Jira itself."
)
