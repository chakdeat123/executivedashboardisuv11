"""
================================================================================
JIRA EXECUTIVE DASHBOARD - FULLY SECURE VERSION
================================================================================
All credentials stored in Streamlit Secrets - Nothing exposed in code
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
st.set_page_config(page_title="JIRA Executive Dashboard", page_icon="📊", layout="wide")

# ============== AUTHENTICATION FUNCTION ==============
def check_password():
    """Returns True if user has correct password."""
    
    def login_form():
        """Display login form."""
        st.markdown("""
        <style>
        .login-container {
            max-width: 400px;
            margin: auto;
            padding: 40px;
            border-radius: 10px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }
        </style>
        """, unsafe_allow_html=True)
        
        st.markdown("### 🔒 JIRA Executive Dashboard")
        st.markdown("#### Please login to continue")
        
        with st.form("login_form"):
            username = st.text_input("Username", placeholder="Enter username")
            password = st.text_input("Password", type="password", placeholder="Enter password")
            submitted = st.form_submit_button("Login", use_container_width=True)
            print("46 username:",username)
            print("46 password:",password)
            print("46 submitted:",submitted)
            
            if submitted:
                if validate_credentials(username, password):
                    st.session_state["authenticated"] = True
                    st.session_state["username"] = username
                    st.rerun()
                else:
                    st.error("❌ Invalid username or password")
    
    def validate_credentials(username, password):
        """Validate credentials against secrets."""
        try:
            # Get users from secrets
            users = st.secrets["auth"]["users"]
            print("46 users:",users)
            
            if username in users:
                stored_password = users[username]["password"]
                print("46 stored_password:",stored_password)
                # Secure comparison to prevent timing attacks
                return hmac.compare_digest(password, stored_password)
            return False
        except Exception as e:
            st.error(f"Authentication error: {e}")
            return False
    
    # Check if already authenticated
    if st.session_state.get("authenticated", False):
        return True
    
    # Show login form
    login_form()
    return False

# ============== LOGOUT FUNCTION ==============
def logout():
    """Logout user."""
    st.session_state["authenticated"] = False
    st.session_state["username"] = None
    st.rerun()

# ============== CHECK AUTHENTICATION ==============
if not check_password():
    st.stop()

# ============== USER IS AUTHENTICATED - SHOW DASHBOARD ==============

# Get JIRA credentials from secrets
try:
    JIRA_URL = st.secrets["jira"]["url"]
    EMAIL = st.secrets["jira"]["email"]
    API_TOKEN = st.secrets["jira"]["api_token"]
except Exception as e:
    st.error("⚠️ JIRA credentials not configured in secrets!")
    st.info("Please add JIRA credentials to Streamlit secrets.")
    st.stop()

# Get projects from secrets or use defaults
try:
    PROJECTS = dict(st.secrets["projects"])
except:
    PROJECTS = {
        "ISUV": {"name": "Image SLA - Upgrades + Vulnerabilities", "sample_size": 500},
        "CPKV1": {"name": "Clean-Package-V1", "sample_size": 500},
        "CIV": {"name": "Clean-Image-V1", "sample_size": 500}
    }

# ============== SIDEBAR ==============
with st.sidebar:
    st.markdown(f"👤 **Logged in as:** {st.session_state.get('username', 'User')}")
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
    assignees = defaultdict(lambda: {
        "Total": 0, "Done": 0, "Invalid": 0, "QA Passed": 0, "To Do": 0,
        "In Progress": 0, "Other": 0, "L1": 0, "L2": 0, "L3": 0, "L4": 0, "L0": 0
    })
    all_statuses = defaultdict(int)
    
    for issue in issues:
        fields = issue.get("fields", {})
        a = fields.get("assignee")
        name = a.get("displayName") if a else "Unassigned"
        
        status_raw = fields.get("status", {}).get("name", "Unknown")
        status = status_raw.lower()
        
        all_statuses[status_raw] += 1
        assignees[name]["Total"] += 1
        
        if "done" in status or "closed" in status or "resolved" in status:
            assignees[name]["Done"] += 1
        elif "invalid" in status:
            assignees[name]["Invalid"] += 1
        elif "qa" in status or "passed" in status:
            assignees[name]["QA Passed"] += 1
        elif "to do" in status or "todo" in status or "open" in status or "new" in status:
            assignees[name]["To Do"] += 1
        elif "progress" in status or "review" in status:
            assignees[name]["In Progress"] += 1
        else:
            assignees[name]["Other"] += 1
        
        level_found = False
        labels = fields.get("labels") or []
        for label in labels:
            label_upper = label.upper().strip()
            if label_upper in ["L1", "L2", "L3", "L4"]:
                assignees[name][label_upper] += 1
                level_found = True
                break
        
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
    total = data["Total"]
    done = data["Done"]
    todo = data["To Do"]
    invalid = data["Invalid"]
    qa_passed = data["QA Passed"]
    in_progress = data.get("In Progress", 0)
    
    if total == 0:
        return 0, 0, 0, 0, 0, 0, "N/A"
    
    qa_pct = round(((done + qa_passed) / total) * 100, 1)
    cve_pct = round((done / total) * 100, 1)
    
    completed_or_active = done + qa_passed + in_progress
    sla_pct = min(round((completed_or_active / total) * 100, 1), 100.0)
    
    valid_total = total - invalid
    wos_pct = min(round((completed_or_active / valid_total) * 100, 1), 100.0) if valid_total > 0 else 0
    
    completed = done + qa_passed
    qs_pct = round((qa_passed / completed) * 100, 1) if completed > 0 else 0
    
    actionable = done + todo
    ps_pct = round((done / actionable) * 100, 1) if actionable > 0 else 0
    
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
    rows = []
    for name, data in assignees.items():
        qa, cve, sla, wos, qs, ps, tier = calculate_metrics(data)
        rows.append({
            "Assignee": name, "Total": data["Total"], "Done": data["Done"], "To Do": data["To Do"],
            "Invalid": data["Invalid"], "QA Passed": data["QA Passed"],
            "L1": data["L1"], "L2": data["L2"], "L3": data["L3"], "L4": data["L4"],
            "QA%": qa, "CVE%": cve, "SLA%": sla, "WOS%": wos, "QS%": qs, "PS%": ps, "Tier": tier
        })
    
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Total", ascending=False).reset_index(drop=True)
        df.index = df.index + 1
    return df

def style_dataframe(df):
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

def render_project_tab(project_key, project_config):
    # Handle both dict and AttrDict from secrets
    if hasattr(project_config, 'get'):
        name = project_config.get('name', project_key)
        sample_size = project_config.get('sample_size', 500)
    else:
        name = project_key
        sample_size = 500
    
    st.subheader(f"📊 {name} ({project_key})")
    
    with st.spinner("Finding board..."):
        board_id = find_board(project_key)
    
    if not board_id:
        st.error(f"❌ Board not found for {project_key}")
        return
    
    total_available = get_total_count(board_id)
    
    st.info(f"📋 Total: **{total_available:,}** | Fetching: **{min(sample_size, total_available):,}**")
    
    progress_bar = st.progress(0, "Starting...")
    start_time = time.time()
    issues = fetch_issues_paginated(board_id, sample_size, progress_bar)
    elapsed = round(time.time() - start_time, 1)
    progress_bar.empty()
    
    if not issues:
        st.error("❌ No issues found")
        return
    
    st.success(f"✅ Loaded {len(issues):,} items in {elapsed}s")
    
    with st.spinner("Processing..."):
        assignees, all_statuses = process_issues(issues)
        df = build_dataframe(assignees)
    
    if df.empty:
        st.warning("No data to display")
        return
    
    st.markdown("---")
    
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    total_all = df["Total"].sum()
    done_all = df["Done"].sum()
    
    c1.metric("📋 Total", f"{total_all:,}")
    c2.metric("✅ Done", f"{done_all:,}")
    c3.metric("📝 To Do", f"{df['To Do'].sum():,}")
    c4.metric("🔍 QA Passed", f"{df['QA Passed'].sum():,}")
    c5.metric("📈 Completion", f"{round(done_all/total_all*100,1) if total_all > 0 else 0}%")
    c6.metric("👥 Assignees", len(df))
    
    st.markdown("---")
    st.subheader("👥 Team Performance")
    st.dataframe(style_dataframe(df), use_container_width=True, height=500)
    
    st.download_button("📥 Download CSV", df.to_csv(index=True), f"{project_key}_dashboard.csv", "text/csv")
    
    with st.expander("📊 Status Breakdown"):
        status_df = pd.DataFrame([{"Status": s, "Count": c} for s, c in sorted(all_statuses.items(), key=lambda x: x[1], reverse=True)])
        st.dataframe(status_df, use_container_width=True)

# ============== MAIN DASHBOARD ==============
st.title("📊 JIRA Executive Dashboard")
st.caption(f"🔒 Secure Access | Source: {JIRA_URL}")

if st.button("🔄 Refresh Data"):
    st.rerun()

st.markdown("---")

# Create tabs
tab1, tab2, tab3 = st.tabs(["📊 Package_Image (ISUV)", "📦 Package (CPKV1)", "🔐 Image (CIV)"])

with tab1:
    render_project_tab("ISUV", PROJECTS.get("ISUV", {"name": "ISUV", "sample_size": 500}))

with tab2:
    render_project_tab("CPKV1", PROJECTS.get("CPKV1", {"name": "CPKV1", "sample_size": 500}))

with tab3:
    render_project_tab("CIV", PROJECTS.get("CIV", {"name": "CIV", "sample_size": 500}))

st.markdown("---")
st.caption("🔒 Secure Dashboard | 💡 Click column headers to sort")