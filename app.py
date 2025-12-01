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
        push_notification(req_user, f
