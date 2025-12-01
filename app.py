import streamlit as st
import pandas as pd
import sqlite3
from datetime import datetime
from io import StringIO

DB_PATH = "chemicals.db"

# -------------------------
# Database helpers
# -------------------------
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # chemicals master list (private)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS chemicals (
        serial_no INTEGER,
        chemical TEXT PRIMARY KEY,
        amount_total REAL,
        amount_remaining REAL,
        issued_total REAL,
        unit TEXT,
        cas_no TEXT
    )""")

    # users (simple registry for audit; roles are chosen at login in this demo)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        full_name TEXT
    )""")

    # requests made by users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        chemical TEXT NOT NULL,
        amount REAL NOT NULL,
        note TEXT,
        status TEXT NOT NULL DEFAULT 'Pending',   -- Pending / Approved / Rejected / Issued
        supervisor TEXT,
        lab_incharge TEXT,
        created_at TEXT,
        updated_at TEXT
    )""")

    # issued records (per user)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS issued (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        chemical TEXT NOT NULL,
        amount REAL NOT NULL,
        issued_by TEXT,
        issued_at TEXT
    )""")

    # notifications for users / lab / supervisor
    cur.execute("""
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recipient TEXT NOT NULL,
        message TEXT NOT NULL,
        seen INTEGER NOT NULL DEFAULT 0,
        created_at TEXT
    )""")

    conn.commit()
    conn.close()

# -------------------------
# Utility operations
# -------------------------
def safe_query_df(query, params=()):
    conn = get_conn()
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()
    return df

def push_notification(recipient, message):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO notifications(recipient,message,created_at) VALUES (?,?,?)",
        (recipient, message, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def get_unseen_notifications(user):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, message, created_at FROM notifications WHERE recipient=? AND seen=0 ORDER BY created_at DESC", (user,))
    rows = cur.fetchall()
    conn.close()
    return rows

def mark_notifications_seen(ids):
    if not ids:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.executemany("UPDATE notifications SET seen=1 WHERE id=?", [(i,) for i in ids])
    conn.commit()
    conn.close()

# -------------------------
# Chemical master list ops
# -------------------------
def load_chemicals():
    return safe_query_df("SELECT serial_no,chemical,amount_total,amount_remaining,issued_total,unit,cas_no FROM chemicals ORDER BY serial_no")

def find_chemical_row(chemical_name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT serial_no,chemical,amount_total,amount_remaining,issued_total,unit,cas_no FROM chemicals WHERE chemical = ?", (chemical_name,))
    row = cur.fetchone()
    conn.close()
    return row  # None or tuple

def adjust_stock(chemical_name, delta):
    """
    delta negative to reduce, positive to add. Returns (ok, message_or_new_remaining)
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT amount_remaining, issued_total FROM chemicals WHERE chemical = ?", (chemical_name,))
    r = cur.fetchone()
    if not r:
        conn.close()
        return False, "Chemical not found in master list"
    remaining, issued = r
    new_remaining = remaining + delta
    if new_remaining < 0:
        conn.close()
        return False, "Insufficient stock"
    new_issued = issued - delta if delta < 0 else issued  # if reducing stock, issued increases
    if delta < 0:
        new_issued = issued + (-delta)
    cur.execute("UPDATE chemicals SET amount_remaining=?, issued_total=? WHERE chemical=?", (new_remaining, new_issued, chemical_name))
    conn.commit()
    conn.close()
    return True, new_remaining

def upload_master_from_excel(uploaded_file):
    # read excel and expect the columns given by user: S.NO., Names, Quantity, Units, Q.Issued, Q.Remaining, CAS.No.
    df = pd.read_excel(uploaded_file)
    df.columns = df.columns.str.strip()
    required = ["S.NO.", "Names", "Quantity", "Units", "Q.Issued", "Q.Remaining", "CAS.No."]
    if not all(col in df.columns for col in required):
        raise ValueError("Excel must contain columns: " + ", ".join(required))
    conn = get_conn()
    cur = conn.cursor()
    # delete existing master list (user requested ability to permanently replace)
    cur.execute("DELETE FROM chemicals")
    # insert rows
    for _, r in df.iterrows():
        serial = int(r["S.NO."]) if not pd.isna(r["S.NO."]) else None
        name = str(r["Names"]).strip()
        qty = float(r["Quantity"]) if not pd.isna(r["Quantity"]) else 0.0
        unit = str(r["Units"]).strip() if "Units" in r and not pd.isna(r["Units"]) else ""
        issued_total = float(r["Q.Issued"]) if not pd.isna(r["Q.Issued"]) else 0.0
        remaining = float(r["Q.Remaining"]) if not pd.isna(r["Q.Remaining"]) else qty - issued_total if qty else 0.0
        cas = str(r["CAS.No."]).strip() if not pd.isna(r["CAS.No."]) else ""
        # upsert
        cur.execute("""
            INSERT INTO chemicals(serial_no,chemical,amount_total,amount_remaining,issued_total,unit,cas_no)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(chemical) DO UPDATE SET
                serial_no=excluded.serial_no,
                amount_total=excluded.amount_total,
                amount_remaining=excluded.amount_remaining,
                issued_total=excluded.issued_total,
                unit=excluded.unit,
                cas_no=excluded.cas_no
        """, (serial, name, qty, remaining, issued_total, unit, cas))
    conn.commit()
    conn.close()
    return True

# -------------------------
# Requests and issuance
# -------------------------
def create_request(username, chemical, amount, note=""):
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    cur = conn.cursor()

    # Check master list if chemical exists and enforce amount <= remaining
    cur.execute("SELECT amount_remaining FROM chemicals WHERE chemical = ?", (chemical,))
    r = cur.fetchone()
    if r:
        amt_remain = r[0]
        if float(amount) > float(amt_remain):
            conn.close()
            return False, f"Requested amount ({amount}) exceeds remaining stock ({amt_remain})."
    # create request
    cur.execute("""INSERT INTO requests(username,chemical,amount,note,status,created_at,updated_at)
                   VALUES (?,?,?,?, 'Pending',?,?)""", (username, chemical, float(amount), note, now, now))
    conn.commit()
    conn.close()
    return True, "Request created"

def list_requests(filters=None):
    # filters is dict where keys match column names
    base = "SELECT id,username,chemical,amount,note,status,supervisor,lab_incharge,created_at,updated_at FROM requests"
    params = []
    if filters:
        clauses = []
        for k, v in filters.items():
            clauses.append(f"{k} = ?")
            params.append(v)
        base += " WHERE " + " AND ".join(clauses)
    base += " ORDER BY created_at DESC"
    return safe_query_df(base, params)

def update_request_status(rid, status, supervisor=None, lab_incharge=None):
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT status, username, chemical, amount FROM requests WHERE id = ?", (rid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False, "Request not found"
    old_status, req_user, chem, amt = row
    if status == "Approved":
        cur.execute("UPDATE requests SET status=?, supervisor=?, updated_at=? WHERE id=?", (status, supervisor, now, rid))
        conn.commit()
        conn.close()
        # notify user and lab_incharge
        push_notification(req_user, f"Your request #{rid} for {amt} {chem} was APPROVED by {supervisor}.")
        push_notification("lab_incharge", f"Request #{rid} for {amt} {chem} by {req_user} approved by {supervisor}.")
        return True, "Approved"
    elif status == "Rejected":
        cur.execute("UPDATE requests SET status=?, supervisor=?, updated_at=? WHERE id=?", (status, supervisor, now, rid))
        conn.commit()
        conn.close()
        push_notification(req_user, f"Your request #{rid} for {amt} {chem} was REJECTED by {supervisor}.")
        return True, "Rejected"
    elif status == "Issued":
        # ensure enough stock
        cur.execute("SELECT amount_remaining FROM chemicals WHERE chemical = ?", (chem,))
        r2 = cur.fetchone()
        if not r2:
            conn.close()
            return False, "Chemical not found in master list — cannot issue from stock"
        remaining = r2[0]
        if float(amt) > float(remaining):
            conn.close()
            return False, f"Insufficient stock. Remaining: {remaining}"
        # deduct and record
        new_remaining = remaining - float(amt)
        # update chemicals
        cur.execute("UPDATE chemicals SET amount_remaining = amount_remaining - ?, issued_total = issued_total + ? WHERE chemical = ?", (float(amt), float(amt), chem))
        # update requests
        cur.execute("UPDATE requests SET status = ?, lab_incharge = ?, updated_at = ? WHERE id = ?", (status, lab_incharge, now, rid))
        # insert into issued
        cur.execute("INSERT INTO issued(username,chemical,amount,issued_by,issued_at) VALUES (?,?,?,?,?)",
                    (req_user, chem, float(amt), lab_incharge, now))
        conn.commit()
        conn.close()
        push_notification(req_user, f"Your request #{rid} for {amt} {chem} has been ISSUED by {lab_incharge}.")
        return True, "Issued"
    else:
        conn.close()
        return False, "Unsupported status"

def list_issued(filters=None):
    base = "SELECT id,username,chemical,amount,issued_by,issued_at FROM issued"
    params = []
    if filters:
        clauses = []
        for k, v in filters.items():
            clauses.append(f"{k} = ?")
            params.append(v)
        base += " WHERE " + " AND ".join(clauses)
    base += " ORDER BY issued_at DESC"
    return safe_query_df(base, params)

# -------------------------
# UI sections
# -------------------------
def login_area():
    st.sidebar.title("Login")
    st.sidebar.info("This demo uses a simple role-selection login. Use the role 'Lab' for Lab Incharge, 'Supervisor' for Supervisor, 'User' for regular users.")
    username = st.sidebar.text_input("Username")
    role = st.sidebar.selectbox("Role", ["User", "Supervisor", "Lab"])
    if st.sidebar.button("Login"):
        if not username.strip():
            st.sidebar.error("Enter a username")
            return None
        # register user (non-sensitive)
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute("INSERT OR IGNORE INTO users(username, full_name) VALUES (?, ?)", (username.strip(), username.strip()))
            conn.commit()
        finally:
            conn.close()
        st.session_state['user'] = {"username": username.strip(), "role": role}
    return None

def show_notifications():
    if 'user' not in st.session_state:
        return
    u = st.session_state['user']['username']
    rows = get_unseen_notifications(u)
    if rows:
        st.info(f"You have {len(rows)} new notification(s).")
        for nid, msg, c_at in rows:
            st.write(f"- {c_at[:19]} — {msg}")
        if st.button("Mark notifications seen"):
            ids = [r[0] for r in rows]
            mark_notifications_seen(ids)
            st.experimental_rerun()

def user_dashboard(user):
    st.title("Chemical Record Keeper — User")
    show_notifications()
    st.header("Request a Chemical")
    with st.form("request_form", clear_on_submit=True):
        chem = st.text_input("Chemical name (type freely)")
        amount = st.number_input("Amount required", min_value=0.01, format="%.3f")
        note = st.text_area("Note (optional)")
        submitted = st.form_submit_button("Submit Request")
        if submitted:
            ok, msg = create_request(user['username'], chem.strip(), amount, note.strip())
            if ok:
                st.success("Request submitted.")
            else:
                st.error(msg)

    st.subheader("My Requests")
    df = safe_query_df("SELECT id,chemical,amount,status,created_at,updated_at FROM requests WHERE username = ? ORDER BY created_at DESC", (user['username'],))
    st.dataframe(df)

    st.subheader("My Issued Records")
    df2 = list_issued(filters={"username": user['username']})
    st.dataframe(df2)

def supervisor_dashboard(user):
    st.title("Chemical Record Keeper — Supervisor")
    show_notifications()
    st.header("Pending Requests")
    df = list_requests(filters={"status": "Pending"})
    st.dataframe(df)

    st.subheader("Approve / Reject Request")
    cols = st.columns([1,1,2])
    rid = cols[0].number_input("Request ID", min_value=1, step=1)
    if cols[1].button("Approve"):
        ok, msg = update_request_status(rid, "Approved", supervisor=user['username'])
        if ok:
            st.success("Approved.")
        else:
            st.error(msg)
    if cols[1].button("Reject"):
        ok, msg = update_request_status(rid, "Rejected", supervisor=user['username'])
        if ok:
            st.info("Rejected.")
        else:
            st.error(msg)

    st.subheader("Master Chemical List (PRIVATE)")
    chems = load_chemicals()
    st.dataframe(chems)

    st.subheader("Downloads (Supervisor)")
    csv_chems = chems.to_csv(index=False)
    st.download_button("Download Chemical List (CSV)", csv_chems, "chemical_list.csv")

    issued = list_issued()
    st.download_button("Download Issued Log (CSV)", issued.to_csv(index=False), "issued_log.csv")

def lab_dashboard(user):
    st.title("Chemical Record Keeper — Lab Incharge")
    show_notifications()

    st.header("Requests Awaiting Issuance (Approved)")
    df = list_requests(filters={"status":"Approved"})
    st.dataframe(df)

    st.subheader("Issue a Request")
    cols = st.columns([1,1,1,1])
    rid = cols[0].number_input("Request ID", min_value=1, step=1)
    btn_issue = cols[1].button("Issue Request")
    if btn_issue:
        # find request details
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT username, chemical, amount, status FROM requests WHERE id = ?", (rid,))
        r = cur.fetchone()
        conn.close()
        if not r:
            st.error("Request not found.")
        else:
            req_user, chem, amt, status = r
            if status != "Approved":
                st.error("Request is not in Approved state.")
            else:
                # try to issue
                ok, msg = update_request_status(rid, "Issued", lab_incharge=user['username'])
                if ok:
                    st.success("Issued successfully.")
                else:
                    st.error(msg)

    st.subheader("Master Chemical List (PRIVATE)")
    chems = load_chemicals()
    st.dataframe(chems)

    st.subheader("Upload / Replace Master Chemical List (Excel)")
    uploaded = st.file_uploader("Upload .xlsx file (must include S.NO., Names, Quantity, Units, Q.Issued, Q.Remaining, CAS.No.)", type=["xlsx"])
    if uploaded is not None:
        try:
            upload_master_from_excel(uploaded)
            st.success("Master list uploaded.")
        except Exception as e:
            st.error("Upload failed: " + str(e))

    if st.button("Delete Master Chemical List (PERMANENT)"):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM chemicals")
        conn.commit()
        conn.close()
        st.warning("Master chemical list deleted permanently.")

    st.subheader("Issued Records (All Users)")
    issued = list_issued()
    st.dataframe(issued)

    st.subheader("Downloads (Lab)")
    st.download_button("Download Chemical List (CSV)", chems.to_csv(index=False), "chemical_list.csv")
    st.download_button("Download Issued Log (CSV)", issued.to_csv(index=False), "issued_log.csv")

# -------------------------
# Main
# -------------------------
def main():
    st.set_page_config(page_title="Chemical Record Keeper", layout="wide")
    init_db()

    if 'user' not in st.session_state:
        st.session_state['user'] = None

    login_area()

    if 'user' not in st.session_state or st.session_state['user'] is None:
        st.info("Please login (sidebar) to continue.")
        return

    user = st.session_state['user']
    role = user['role']
    # show appropriate dashboard
    if role == "User":
        user_dashboard(user)
    elif role == "Supervisor":
        supervisor_dashboard(user)
    elif role == "Lab":
        lab_dashboard(user)
    else:
        st.error("Unknown role selected.")

if __name__ == "__main__":
    main()
