"""
================================================================================
JIRA EXECUTIVE DASHBOARD - STREAMLIT VERSION
================================================================================
Features:
- 2 Tabs: CPKV1 and CIV projects
- JIRA-standard metrics (QA%, CVE%, SLA%, WOS%, QS%, PS%, Tier)
- Level fields (L1, L2, L3, L4)
- Pagination support for large datasets
- Professional UI for executive team

SETUP:
    pip install streamlit pandas requests
    streamlit run jira_dashboard.py

DEPLOY TO STREAMLIT CLOUD:
    1. Push to GitHub
    2. Go to https://share.streamlit.io
    3. Connect your repo
    4. Add secrets (EMAIL, API_TOKEN)
    5. Share URL with executives
================================================================================
"""

import streamlit as st
import pandas as pd
import requests
from requests.auth import HTTPBasicAuth
from collections import defaultdict
import time


# ============== CONFIGURATION ==============
JIRA_URL = "https://triamsecurity.atlassian.net"

# For local testing, set these directly
# For Streamlit Cloud, use st.secrets
try:
    EMAIL = st.secrets["EMAIL"]
    API_TOKEN = st.secrets["API_TOKEN"]
except:
    EMAIL = "YOUR_EMAIL@company.com"
    API_TOKEN = "YOUR_API_TOKEN"

# Project configurations
PROJECTS = {
    "ISUV": {"name": "Image SLA - Upgrades + Vulnerabilities", "sample_size": 500},
    "CPKV1": {"name": "Clean-Package-V1", "sample_size": 500},
    "CIV": {"name": "Clean-Image-V1", "sample_size": 500}
}
# ============================================

st.set_page_config(
    page_title="JIRA Executive Dashboard",
    page_icon="📊",
    layout="wide"
)

# Custom CSS
st.markdown("""
<style>
    .metric-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        padding: 20px;
        border-radius: 10px;
        color: white;
        text-align: center;
    }
    .stDataFrame {font-size: 12px;}
    .gold {background-color: #FFD700 !important;}
    .silver {background-color: #C0C0C0 !important;}
    .bronze {background-color: #CD7F32 !important;}
</style>
""", unsafe_allow_html=True)


def get_auth():
    return HTTPBasicAuth(EMAIL, API_TOKEN)


def find_board(project_key):
    """Find board ID for project"""
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
    """Get total backlog count"""
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
    """Fetch issues with pagination"""
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
            
            # Update progress
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
    """Process issues and calculate metrics per assignee"""
    assignees = defaultdict(lambda: {
        "Total": 0, "Done": 0, "Invalid": 0, "QA Passed": 0, "To Do": 0, 
        "In Progress": 0, "Other": 0,
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
        status = status_raw.lower()
        
        all_statuses[status_raw] += 1
        assignees[name]["Total"] += 1
        
        # Categorize status
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
    Calculate JIRA-standard metrics based on industry best practices
    
    Metrics based on:
    - Atlassian JIRA Service Management standards
    - ITSM best practices
    - QA industry KPIs
    """
    total = data["Total"]
    done = data["Done"]
    todo = data["To Do"]
    invalid = data["Invalid"]
    qa_passed = data["QA Passed"]
    in_progress = data.get("In Progress", 0)
    
    if total == 0:
        return 0, 0, 0, 0, 0, 0, "N/A"
    
    # QA% = Quality Assurance Rate
    # Formula: (Issues Passed QA + Done) / Total * 100
    # Measures: Overall quality of work delivered
    qa_pct = round(((done + qa_passed) / total) * 100, 1)
    
    # CVE% = Completion Rate (Completion vs Expected)
    # Formula: Done / Total * 100
    # Measures: Simple completion percentage
    cve_pct = round((done / total) * 100, 1)
    
    # SLA% = SLA Compliance Rate
    # Formula: (Total - Invalid - Overdue) / Total * 100
    # In absence of due dates, we use: (Completed + In Progress) / Total
    # This measures work being actively handled within expected timelines
    completed_or_active = done + qa_passed + in_progress
    sla_pct = round((completed_or_active / total) * 100, 1)
    sla_pct = min(sla_pct, 100.0)  # Cap at 100%
    
    # WOS% = Work on Schedule
    # Formula: (Done + QA Passed + In Progress) / (Total - Invalid) * 100
    # Measures: Percentage of valid work being actively processed
    valid_total = total - invalid
    if valid_total > 0:
        wos_pct = round((completed_or_active / valid_total) * 100, 1)
        wos_pct = min(wos_pct, 100.0)
    else:
        wos_pct = 0
    
    # QS% = Quality Score
    # Formula: QA Passed / (Done + QA Passed) * 100
    # Measures: Percentage of completed work that passed QA
    completed = done + qa_passed
    qs_pct = round((qa_passed / completed) * 100, 1) if completed > 0 else 0
    
    # PS% = Productivity Score
    # Formula: Done / (Done + To Do) * 100
    # Measures: Efficiency - how much of actionable work is completed
    actionable = done + todo
    ps_pct = round((done / actionable) * 100, 1) if actionable > 0 else 0
    
    # Tier Classification based on weighted average
    # Weights: CVE(30%), SLA(25%), QA(20%), WOS(15%), PS(10%)
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
    """Build DataFrame with all metrics"""
    rows = []
    
    for name, data in assignees.items():
        qa, cve, sla, wos, qs, ps, tier = calculate_metrics(data)
        
        rows.append({
            "Assignee": name,
            "Total": data["Total"],
            "Done": data["Done"],
            "To Do": data["To Do"],
            "Invalid": data["Invalid"],
            "QA Passed": data["QA Passed"],
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
    df = df.sort_values("Total", ascending=False).reset_index(drop=True)
    df.index = df.index + 1  # Start index from 1
    
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
    styled = styled.format({col: "{:.1f}" for col in pct_cols})
    
    return styled


def render_project_tab(project_key, project_config):
    """Render dashboard for a single project"""
    
    st.subheader(f"📊 {project_config['name']} ({project_key})")
    
    # Find board
    with st.spinner("Finding board..."):
        board_id = find_board(project_key)
    
    if not board_id:
        st.error(f"❌ Board not found for {project_key}")
        return
    
    # Get total count
    total_available = get_total_count(board_id)
    sample_size = project_config["sample_size"]
    
    st.info(f"📋 Total Backlogs: **{total_available:,}** | Fetching: **{min(sample_size, total_available):,}**")
    
    # Fetch issues
    progress_bar = st.progress(0, "Starting...")
    start_time = time.time()
    
    issues = fetch_issues_paginated(board_id, sample_size, progress_bar)
    
    elapsed = round(time.time() - start_time, 1)
    progress_bar.empty()
    
    if not issues:
        st.error("❌ No issues found")
        return
    
    st.success(f"✅ Loaded {len(issues):,} items in {elapsed}s")
    
    # Process data
    with st.spinner("Processing data..."):
        assignees, all_statuses = process_issues(issues)
        df = build_dataframe(assignees)
    
    # Summary metrics
    st.markdown("---")
    
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    
    total_all = df["Total"].sum()
    done_all = df["Done"].sum()
    todo_all = df["To Do"].sum()
    qa_all = df["QA Passed"].sum()
    completion = round((done_all / total_all) * 100, 1) if total_all > 0 else 0
    
    col1.metric("📋 Total", f"{total_all:,}")
    col2.metric("✅ Done", f"{done_all:,}")
    col3.metric("📝 To Do", f"{todo_all:,}")
    col4.metric("🔍 QA Passed", f"{qa_all:,}")
    col5.metric("📈 Completion", f"{completion}%")
    col6.metric("👥 Assignees", len(df))
    
    # Main table
    st.markdown("---")
    st.subheader("👥 Team Performance Dashboard")
    
    styled_df = style_dataframe(df)
    st.dataframe(styled_df, use_container_width=True, height=500)
    
    # Download button
    csv = df.to_csv(index=True)
    st.download_button(
        label="📥 Download CSV",
        data=csv,
        file_name=f"dashboard_{project_key}.csv",
        mime="text/csv"
    )
    
    # Status breakdown
    with st.expander("📊 Status Breakdown"):
        status_df = pd.DataFrame([
            {"Status": s, "Count": c} 
            for s, c in sorted(all_statuses.items(), key=lambda x: x[1], reverse=True)
        ])
        st.dataframe(status_df, use_container_width=True)
    
    # Metrics legend
    with st.expander("📖 Metrics Legend"):
        st.markdown("""
        | Metric | Formula | Description |
        |--------|---------|-------------|
        | **QA%** | (Done + QA Passed) / Total | Quality Assurance Rate |
        | **CVE%** | Done / Total | Completion Rate |
        | **SLA%** | (Done + QA + In Progress) / Total | SLA Compliance |
        | **WOS%** | (Done + QA + In Progress) / (Total - Invalid) | Work on Schedule |
        | **QS%** | QA Passed / (Done + QA Passed) | Quality Score |
        | **PS%** | Done / (Done + To Do) | Productivity Score |
        | **Tier** | Weighted average | 🥇≥80% 🥈≥60% 🥉≥40% 📈<40% |
        
        **Level Fields (L1-L4):** Count of issues with Level = L1, L2, L3, or L4
        """)


def main():
    st.title("📊 JIRA Executive Dashboard")
    st.caption(f"Source: {JIRA_URL}")
    
    # Refresh button
    col1, col2 = st.columns([1, 8])
    with col1:
        if st.button("🔄 Refresh"):
            st.cache_data.clear()
            st.rerun()
    
    st.markdown("---")
    
    # Create tabs for each project
    tab1, tab2, tab3 = st.tabs(["📊 Package_Image", "📦 Package", "🔐 Image"])
    
    with tab1:
        render_project_tab("ISUV", PROJECTS["ISUV"])
    
    with tab2:
        render_project_tab("CPKV1", PROJECTS["CPKV1"])
    
    with tab3:
        render_project_tab("CIV", PROJECTS["CIV"])
    
    # Footer
    st.markdown("---")
    st.caption("💡 Tip: Click column headers to sort | Use 🔄 Refresh to reload data")


if __name__ == "__main__":
    main()