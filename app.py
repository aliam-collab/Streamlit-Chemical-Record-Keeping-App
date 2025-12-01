import streamlit as st
import pandas as pd
import sqlite3
import os
from datetime import datetime

DB = "chemicals.db"

# ----------------------------
# Database Initialization
# ----------------------------

def init_db():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # Chemical master list
    cur.execute("""
    CREATE TABLE IF NOT EXISTS chemicals (
        serial_no INTEGER,
        chemical TEXT PRIMARY KEY,
        amount_total REAL,
        amount_remaining REAL,
        issued_total REAL,
        unit TEXT,
        cas_no TEXT
    )
    """)

    # Users database
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        role TEXT
    )
    """)

    # Requests table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        chemical TEXT,
        amount REAL,
        status TEXT,
        request_time TEXT,
        approve_time TEXT,
        issue_time TEXT
    )
    """)

    conn.commit()
    conn.close()


# ----------------------------
# Helper Functions
# ----------------------------

def load_chemical_list():
    conn = sqlite3.connect(DB)
    df = pd.read_sql_query("SELECT * FROM chemicals", conn)
    conn.close()
    return df

def load_requests():
    conn = sqlite3.connect(DB)
    df = pd.read_sql_query("SELECT * FROM requests", conn)
    conn.close()
    return df

def load_user_requests(username):
    conn = sqlite3.connect(DB)
    df = pd.read_sql_query(f"SELECT * FROM requests WHERE username='{username}'", conn)
    conn.close()
    return df

def add_request(username, chem, amount):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO requests (username, chemical, amount, status, request_time)
        VALUES (?, ?, ?, 'Pending', ?)
    """, (username, chem, amount, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def approve_request(req_id):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
        UPDATE requests SET status='Approved', approve_time=?
        WHERE id=?
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), req_id))
    conn.commit()
    conn.close()

def issue_request(req_id, chem, amount):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # deduct stock
    cur.execute("SELECT amount_remaining, issued_total FROM chemicals WHERE chemical=?", (chem,))
    stock = cur.fetchone()

    if stock:
        remaining, issued = stock
        new_remaining = remaining - amount
        new_issued = issued + amount

        cur.execute("""
            UPDATE chemicals
            SET amount_remaining=?, issued_total=?
            WHERE chemical=?
        """, (new_remaining, new_issued, chem))

    # update request status
    cur.execute("""
        UPDATE requests SET status='Issued', issue_time=?
        WHERE id=?
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), req_id))

    conn.commit()
    conn.close()


# ----------------------------
# Upload Chemical List
# ----------------------------

def upload_chemical_list(file):
    df = pd.read_excel(file)

    # Expected columns
    df.columns = df.columns.str.strip()

    required_cols = ["S.NO.", "Names", "Quantity", "Units", "Q.Issued", "Q.Remaining", "CAS.No."]
    if not all(col in df.columns for col in required_cols):
        st.error("Uploaded sheet does not match required format.")
        return

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM chemicals")  # delete old list

    for _, row in df.iterrows():
        cur.execute("""
        INSERT INTO chemicals (serial_no, chemical, amount_total, unit, issued_total, amount_remaining, cas_no)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            row["S.NO."],
            row["Names"],
            row["Quantity"],
            row["Units"],
            row["Q.Issued"],
            row["Q.Remaining"],
            row["CAS.No."]
        ))

    conn.commit()
    conn.close()
    st.success("Chemical list uploaded successfully.")


# ----------------------------
# Authentication Mock
# ----------------------------

def login():
    st.sidebar.title("Login")
    username = st.sidebar.text_input("Username")
    role = st.sidebar.selectbox("Role", ["User", "Supervisor", "Lab Incharge"])
    if st.sidebar.button("Login"):
        return username, role
    return None, None


# ----------------------------
# Main App
# ----------------------------

def user_panel(username):
    st.header("🔹 Request Chemical")

    chem = st.text_input("Chemical Name")
    amount = st.number_input("Amount Required", min_value=0.01)

    if st.button("Submit Request"):
        add_request(username, chem, amount)
        st.success("Request Submitted!")

    st.subheader("Your Chemical Request History")
    df = load_user_requests(username)
    st.dataframe(df)


def supervisor_panel():
    st.header("📌 Supervisor Panel")

    st.subheader("Pending Requests")
    df = load_requests()
    pending = df[df["status"] == "Pending"]
    st.dataframe(pending)

    req_id = st.number_input("Enter Request ID to Approve", min_value=1)
    if st.button("Approve"):
        approve_request(req_id)
        st.success("Request Approved!")

    st.subheader("Chemical List")
    st.dataframe(load_chemical_list())

    if st.button("Download Chemical List"):
        st.download_button("Download", load_chemical_list().to_csv(), "chemical_list.csv")

    if st.button("Download Issuance Log"):
        st.download_button("Download", load_requests().to_csv(), "issued_log.csv")


def lab_incharge_panel():
    st.header("🧪 Lab Incharge Panel")

    st.subheader("Approved Requests (Pending Issuance)")
    df = load_requests()
    approved = df[df["status"] == "Approved"]
    st.dataframe(approved)

    req_id = st.number_input("Request ID for Issuing", min_value=1)
    chemical = st.text_input("Chemical Name for Issuing")
    amount = st.number_input("Amount to Issue", min_value=0.01)

    if st.button("Issue Chemical"):
        issue_request(req_id, chemical, amount)
        st.success("Chemical Issued!")

    st.subheader("Chemical List (Private)")
    st.dataframe(load_chemical_list())

    st.subheader("Upload New Chemical List")
    file = st.file_uploader("Upload Excel File", type=["xlsx"])
    if file:
        upload_chemical_list(file)

    if st.button("Delete Current Chemical List"):
        conn = sqlite3.connect(DB)
        cur = conn.cursor()
        cur.execute("DELETE FROM chemicals")
        conn.commit()
        conn.close()
        st.warning("Chemical List Deleted Permanently.")


# ----------------------------
# Run App
# ----------------------------

def main():
    init_db()

    username, role = login()
    if not username:
        st.stop()

    st.sidebar.success(f"Logged in as {username} ({role})")

    if role == "User":
        user_panel(username)
    elif role == "Supervisor":
        supervisor_panel()
    elif role == "Lab Incharge":
        lab_incharge_panel()


if __name__ == "__main__":
    main()
