import sqlite3
from datetime import datetime

DB = "attendance.db"

def get_connection():
    return sqlite3.connect(DB)


# people table, Create or find a person

def get_or_create_person(name):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM people WHERE name=?", (name,))
    result = cur.fetchone()

    if result:
        person_id = result[0]
    else:
        cur.execute("INSERT INTO people (name) VALUES (?)", (name,))
        person_id = cur.lastrowid

    conn.commit()
    conn.close()
    return person_id


# image path storage

def store_image_path(person_id, image_path):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO face_images (person_id, image_path)
        VALUES (?, ?)
    """, (person_id, image_path))

    conn.commit()
    conn.close()


# attendance logging

def log_attendance(person_id):
    conn = get_connection()
    cur = conn.cursor()

    now = datetime.now()
    date = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")

    # Check if person already clocked in today
    cur.execute("""
        SELECT status FROM attendance
        WHERE person_id=? AND date=?
        ORDER BY id DESC LIMIT 1
    """, (person_id, date))
    
    last = cur.fetchone()

    # Determine status
    if last is None or last[0] == "OUT":
        status = "IN"
    else:
        status = "OUT"

    # Insert new record
    cur.execute("""
        INSERT INTO attendance (person_id, date, time, status)
        VALUES (?, ?, ?, ?)
    """, (person_id, date, time_str, status))

    conn.commit()
    conn.close()

    print(f"[DB] {person_id} punched {status} at {date} {time_str}")

def calculate_daily_hours(person_id, date):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT time, status
        FROM attendance
        WHERE person_id=? AND date=?
        ORDER BY id ASC
    """, (person_id, date))

    records = cur.fetchall()
    conn.close()

    total_seconds = 0
    last_in_time = None

    for t, status in records:
        if status == "IN":
            last_in_time = datetime.strptime(t, "%H:%M:%S")
        elif status == "OUT" and last_in_time:
            out_time = datetime.strptime(t, "%H:%M:%S")
            diff = out_time - last_in_time
            total_seconds += diff.total_seconds()
            last_in_time = None

    # Format as HH:MM:SS
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
