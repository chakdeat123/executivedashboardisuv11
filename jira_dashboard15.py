"""
================================================================================
ISUV EXECUTIVE DASHBOARD - SECURE VERSION
================================================================================
Project: ISUV (Image SLA - Upgrades + Vulnerabilities)
All credentials stored in Streamlit Secrets
================================================================================
"""

import streamlit as st
import pandas as pd
import requests
from requests.auth import HTTPBasicAuth
from collections import defaultdict
import time
import hmac

# ============== PAGE CONFIG ==============
st.set_page_config(page_title="ISUV Executive Dashboard", page_icon="📊", layout="wide")

# ============== AUTHENTICATION ==============
def check_password():
    """Returns True if user has correct password."""
    
    def login_form():
        st.markdown("### 🔒 ISUV Executive Dashboard")
        st.markdown("#### Please login to continue")
        
        with st.form("login_form"):
            username = st.text_input("Username", placeholder="Enter username")
            password = st.text_input("Password", type="password", placeholder="Enter password")
            submitted = st.form_submit_button("Login", use_container_width=True)
            
            if submitted:
                if validate_credentials(username, password):
                    st.session_state["authenticated"] = True
                    st.session_state["username"] = username
                    st.rerun()
                else:
                    st.error("❌ Invalid username or password")
    
    def validate_credentials(username, password):
        try:
            users = st.secrets["auth"]["users"]
            if username in users:
                stored_password = users[username]["password"]
                return hmac.compare_digest(password, stored_password)
            return False
        except Exception as e:
            st.error(f"Authentication error: {e}")
            return False
    
    if st.session_state.get("authenticated", False):
        return True
    
    login_form()
    return False

def logout():
    st.session_state["authenticated"] = False
    st.session_state["username"] = None
    st.rerun()

# ============== CHECK AUTHENTICATION ==============
if not check_password():
    st.stop()

# ============== JIRA CONFIGURATION ==============
try:
    JIRA_URL = st.secrets["jira"]["url"]
    EMAIL = st.secrets["jira"]["email"]
    API_TOKEN = st.secrets["jira"]["api_token"]
    #PROJECT_KEY = st.secrets["jira"]["api_token"]
except:
    st.error("⚠️ JIRA credentials not configured!")
    st.stop()

PROJECT_KEY = "ISUV"
SAMPLE_SIZE = 15000

# ============== STATUS MAPPING ==============
# Based on your requirements:
# In Progress = Package Build + Package QA + Package Vulnerability + Image Queue + Image Build + Image QA + Image Delivery
# Done = Customer Delivered
# Todo = Thrt Research + Package Queue

STATUS_MAPPING = {
    # In Progress statuses
    "packagebuild": "In Progress",
    "package build": "In Progress",
    "package qa": "In Progress",
    "package vulnerability": "In Progress",
    "image queue": "In Progress",
    "image build": "In Progress",
    "ct2 - image build": "In Progress",
    "community - image build": "In Progress",
    "image qa": "In Progress",
    "community - image qa": "In Progress",
    "image delivery": "In Progress",
    "image consumption": "In Progress",
    
    # Done statuses
    "customer delivered": "Done",
    
    # To Do statuses
    "thrt research": "To Do",
    "package queue": "To Do",
    
    # Special statuses (shown as columns)
    "archived": "Archived",
    "verified": "Verified",
    
    # Other statuses
    "unsupported": "Unsupported",
    "duplicate": "Duplicate",
    "image vulnerability": "Image Vulnerability"
}

def get_status_category(status_raw):
    """Map raw status to category"""
    status_lower = status_raw.lower().strip()
    return STATUS_MAPPING.get(status_lower, "Other")

# ============== SIDEBAR ==============
with st.sidebar:
    st.markdown(f"👤 **{st.session_state.get('username', 'User')}**")
    st.markdown("---")
    if st.button("🚪 Logout", use_container_width=True):
        logout()
    st.markdown("---")
    st.caption("🔒 Secure Dashboard")

# ============== JIRA FUNCTIONS ==============
def get_auth():
    return HTTPBasicAuth(EMAIL, API_TOKEN)

def find_board(project_key):
    try:
        r = requests.get(
            f"{JIRA_URL}/rest/agile/1.0/board",
            params={"projectKeyOrId": project_key},
            auth=get_auth(),
            headers={"Accept": "application/json"},
            timeout=30
        )
        if r.status_code == 200:
            boards = r.json().get("values", [])
            if boards:
                return boards[0].get("id")
    except:
        pass
    return None

def get_total_count(board_id):
    try:
        r = requests.get(
            f"{JIRA_URL}/rest/agile/1.0/board/{board_id}/issue",
            params={"maxResults": 1},
            auth=get_auth(),
            headers={"Accept": "application/json"},
            timeout=30
        )
        if r.status_code == 200:
            return r.json().get("total", 0)
    except:
        pass
    return 0

def fetch_issues_paginated(board_id, sample_size, progress_bar):
    all_issues = []
    start_at = 0
    batch_size = 100
    
    while len(all_issues) < sample_size:
        remaining = sample_size - len(all_issues)
        fetch_size = min(batch_size, remaining)
        
        try:
            r = requests.get(
                f"{JIRA_URL}/rest/agile/1.0/board/{board_id}/issue",
                params={
                    "startAt": start_at,
                    "maxResults": fetch_size,
                    "fields": "assignee,status,labels,priority"
                },
                auth=get_auth(),
                headers={"Accept": "application/json"},
                timeout=60
            )
            
            if r.status_code != 200:
                break
            
            data = r.json()
            issues = data.get("issues", [])
            total_available = data.get("total", 0)
            
            if not issues:
                break
            
            all_issues.extend(issues)
            progress = min(len(all_issues) / sample_size, 1.0)
            progress_bar.progress(progress, f"Loading: {len(all_issues):,} / {min(sample_size, total_available):,}")
            
            if len(all_issues) >= total_available:
                break
            
            start_at += fetch_size
        except Exception as e:
            st.error(f"Error: {e}")
            break
    
    return all_issues

def process_issues(issues):
    """Process issues with ISUV-specific status mapping"""
    assignees = defaultdict(lambda: {
        "Total": 0,
        "Done": 0,           # Customer Delivered
        "In Progress": 0,    # Package Build, QA, Image Queue, Build, QA, Delivery
        "To Do": 0,          # Thrt Research, Package Queue
        "Archived": 0,       # ARCHIVED
        "Verified": 0,       # VERIFIED
        "Customer Delivered": 0,  # Same as Done, shown as column
        "Other": 0,
        "L1": 0, "L2": 0, "L3": 0, "L4": 0, "L0": 0
    })
    all_statuses = defaultdict(int)
    
    for issue in issues:
        fields = issue.get("fields", {})
        
        # Assignee
        a = fields.get("assignee")
        name = a.get("displayName") if a else "Unassigned"
        
        # Status
        status_raw = fields.get("status", {}).get("name", "Unknown")
        status_category = get_status_category(status_raw)
        
        all_statuses[status_raw] += 1
        assignees[name]["Total"] += 1
        
        # Map to categories
        if status_category == "Done":
            assignees[name]["Done"] += 1
            assignees[name]["Customer Delivered"] += 1
        elif status_category == "In Progress":
            assignees[name]["In Progress"] += 1
        elif status_category == "To Do":
            assignees[name]["To Do"] += 1
        elif status_category == "Archived":
            assignees[name]["Archived"] += 1
        elif status_category == "Verified":
            assignees[name]["Verified"] += 1
        else:
            assignees[name]["Other"] += 1
        
        # Level from labels
        level_found = False
        labels = fields.get("labels") or []
        for label in labels:
            label_upper = label.upper().strip()
            if label_upper in ["L1", "L2", "L3", "L4"]:
                assignees[name][label_upper] += 1
                level_found = True
                break
        
        # Level from custom fields
        if not level_found:
            for key, value in fields.items():
                if value and "customfield" in key:
                    level_val = ""
                    if isinstance(value, dict):
                        level_val = str(value.get("value", "")).upper().strip()
                    elif isinstance(value, str):
                        level_val = value.upper().strip()
                    elif isinstance(value, list) and len(value) > 0:
                        first = value[0]
                        if isinstance(first, dict):
                            level_val = str(first.get("value", "")).upper().strip()
                        else:
                            level_val = str(first).upper().strip()
                    
                    if level_val in ["L1", "L2", "L3", "L4"]:
                        assignees[name][level_val] += 1
                        level_found = True
                        break
        
        if not level_found:
            assignees[name]["L0"] += 1
    
    return assignees, all_statuses

def calculate_metrics(data):
    """
    Calculate JIRA-standard metrics for ISUV
    
    Based on status mapping:
    - Done = Customer Delivered
    - In Progress = Package Build, QA, Image Queue, Build, QA, Delivery
    - To Do = Thrt Research, Package Queue
    """
    total = data["Total"]
    done = data["Done"]  # Customer Delivered
    in_progress = data["In Progress"]
    todo = data["To Do"]
    archived = data["Archived"]
    verified = data["Verified"]
    
    if total == 0:
        return 0, 0, 0, 0, 0, 0, "N/A"
    
    # QA% = Quality Assurance Rate
    # (Done + Verified) / Total - measures quality approved work
    qa_pct = round(((done + verified) / total) * 100, 1)
    
    # CVE% = Completion Rate
    # Done / Total - simple completion rate
    cve_pct = round((done / total) * 100, 1)
    
    # SLA% = SLA Compliance
    # (Done + In Progress) / (Total - Archived) - work being handled on time
    active_total = total - archived
    if active_total > 0:
        sla_pct = round(((done + in_progress) / active_total) * 100, 1)
        sla_pct = min(sla_pct, 100.0)
    else:
        sla_pct = 0
    
    # WOS% = Work on Schedule
    # (Done + In Progress + Verified) / (Total - Archived) - valid work progress
    if active_total > 0:
        wos_pct = round(((done + in_progress + verified) / active_total) * 100, 1)
        wos_pct = min(wos_pct, 100.0)
    else:
        wos_pct = 0
    
    # QS% = Quality Score
    # Verified / (Done + Verified) - percentage that passed verification
    verified_total = done + verified
    qs_pct = round((verified / verified_total) * 100, 1) if verified_total > 0 else 0
    
    # PS% = Productivity Score
    # Done / (Done + To Do) - efficiency metric
    actionable = done + todo
    ps_pct = round((done / actionable) * 100, 1) if actionable > 0 else 0
    
    # Tier Classification
    weighted_avg = (cve_pct * 0.30) + (sla_pct * 0.25) + (qa_pct * 0.20) + (wos_pct * 0.15) + (ps_pct * 0.10)
    
    if weighted_avg >= 80:
        tier = "🥇 Gold"
    elif weighted_avg >= 60:
        tier = "🥈 Silver"
    elif weighted_avg >= 40:
        tier = "🥉 Bronze"
    else:
        tier = "📈 Dev"
    
    return qa_pct, cve_pct, sla_pct, wos_pct, qs_pct, ps_pct, tier

def build_dataframe(assignees):
    """Build DataFrame with ISUV-specific columns"""
    rows = []
    
    for name, data in assignees.items():
        qa, cve, sla, wos, qs, ps, tier = calculate_metrics(data)
        
        rows.append({
            "Assignee": name,
            "Total": data["Total"],
            "Archived": data["Archived"],
            "Customer Delivered": data["Customer Delivered"],
            "Verified": data["Verified"],
            "L1": data["L1"],
            "L2": data["L2"],
            "L3": data["L3"],
            "L4": data["L4"],
            "QA%": qa,
            "CVE%": cve,
            "SLA%": sla,
            "WOS%": wos,
            "QS%": qs,
            "PS%": ps,
            "Tier": tier
        })
    
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Total", ascending=False).reset_index(drop=True)
        df.index = df.index + 1
    return df

def style_dataframe(df):
    """Apply styling to dataframe"""
    def color_tier(val):
        if "Gold" in str(val):
            return "background-color: #90EE90"
        elif "Silver" in str(val):
            return "background-color: #E8E8E8"
        elif "Bronze" in str(val):
            return "background-color: #FFDAB9"
        return "background-color: #FFE4E1"
    
    def color_pct(val):
        try:
            v = float(val)
            if v >= 80:
                return "background-color: #90EE90"
            elif v >= 60:
                return "background-color: #FFFACD"
            elif v >= 40:
                return "background-color: #FFDAB9"
            return "background-color: #FFB6C1"
        except:
            return ""
    
    pct_cols = ["QA%", "CVE%", "SLA%", "WOS%", "QS%", "PS%"]
    styled = df.style.map(color_tier, subset=["Tier"])
    styled = styled.map(color_pct, subset=pct_cols)
    return styled

# ============== MAIN DASHBOARD ==============
st.title("📊 ISUV Executive Dashboard")
st.caption(f"🔒 Secure Access | Project: {PROJECT_KEY} | Source: {JIRA_URL}")

col1, col2 = st.columns([1, 8])
with col1:
    if st.button("🔄 Refresh"):
        st.rerun()

st.markdown("---")

# Find board
with st.spinner("Finding board..."):
    board_id = find_board(PROJECT_KEY)

if not board_id:
    st.error(f"❌ Board not found for {PROJECT_KEY}")
    st.stop()

# Get total count
total_available = get_total_count(board_id)
st.info(f"📋 Total Backlogs: **{total_available:,}** | Fetching: **{min(SAMPLE_SIZE, total_available):,}**")

# Fetch issues
progress_bar = st.progress(0, "Starting...")
start_time = time.time()
issues = fetch_issues_paginated(board_id, SAMPLE_SIZE, progress_bar)
elapsed = round(time.time() - start_time, 1)
progress_bar.empty()

if not issues:
    st.error("❌ No issues found")
    st.stop()

st.success(f"✅ Loaded {len(issues):,} items in {elapsed}s")

# Process data
with st.spinner("Processing data..."):
    assignees, all_statuses = process_issues(issues)
    df = build_dataframe(assignees)

if df.empty:
    st.warning("No data to display")
    st.stop()

# Summary metrics
st.markdown("---")
st.subheader("📈 Project Summary")

c1, c2, c3, c4, c5, c6 = st.columns(6)

total_all = df["Total"].sum()
delivered_all = df["Customer Delivered"].sum()
archived_all = df["Archived"].sum()
verified_all = df["Verified"].sum()
completion = round((delivered_all / total_all) * 100, 1) if total_all > 0 else 0

c1.metric("📋 Total", f"{total_all:,}")
c2.metric("✅ Delivered", f"{delivered_all:,}")
c3.metric("📦 Archived", f"{archived_all:,}")
c4.metric("✔️ Verified", f"{verified_all:,}")
c5.metric("📈 Completion", f"{completion}%")
c6.metric("👥 Assignees", len(df))

# Main table
st.markdown("---")
st.subheader("👥 Team Performance Dashboard")

styled_df = style_dataframe(df)
st.dataframe(styled_df, use_container_width=True, height=500)

# Download button
csv = df.to_csv(index=True)
st.download_button(
    label="📥 Download CSV Report",
    data=csv,
    file_name=f"ISUV_dashboard_{time.strftime('%Y%m%d')}.csv",
    mime="text/csv"
)

# Status breakdown
with st.expander("📊 Raw Status Breakdown"):
    status_df = pd.DataFrame([
        {"Status": s, "Count": c, "Category": get_status_category(s)} 
        for s, c in sorted(all_statuses.items(), key=lambda x: x[1], reverse=True)
    ])
    st.dataframe(status_df, use_container_width=True)

# Metrics legend
with st.expander("📖 Metrics Legend"):
    st.markdown("""
    ### Status Mapping
    | Category | Statuses Included |
    |----------|-------------------|
    | **In Progress** | Package Build, Package QA, Package Vulnerability, Image Queue, Image Build, Image QA, Image Delivery |
    | **Done** | Customer Delivered |
    | **To Do** | Thrt Research, Package Queue |
    
    ### Metrics Formulas
    | Metric | Formula | Description |
    |--------|---------|-------------|
    | **QA%** | (Delivered + Verified) / Total | Quality Assurance Rate |
    | **CVE%** | Delivered / Total | Completion Rate |
    | **SLA%** | (Delivered + In Progress) / (Total - Archived) | SLA Compliance |
    | **WOS%** | (Delivered + In Progress + Verified) / (Total - Archived) | Work on Schedule |
    | **QS%** | Verified / (Delivered + Verified) | Quality Score |
    | **PS%** | Delivered / (Delivered + To Do) | Productivity Score |
    | **Tier** | Weighted average | 🥇≥80% 🥈≥60% 🥉≥40% 📈<40% |
    """)

st.markdown("---")
st.caption("🔒 Secure Dashboard | 💡 Click column headers to sort | 📥 Download CSV for reports")
