import sqlite3

def init_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS employees (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        firebase_uid TEXT UNIQUE NOT NULL,
        email       TEXT UNIQUE,
        name        TEXT,
        is_admin    INTEGER NOT NULL DEFAULT 0  -- 1 = admin
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS time_entries (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        employee_id INTEGER NOT NULL,
        ts_utc      TEXT NOT NULL,
        status      TEXT NOT NULL CHECK(status IN ('IN','OUT')),
        source      TEXT NOT NULL DEFAULT 'face',
        confidence  REAL,
        FOREIGN KEY(employee_id) REFERENCES employees(id)
    );
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_time_entries_emp_ts ON time_entries(employee_id, ts_utc);")

    # add is_admin if upgrading from older schema
    try:
        cur.execute("ALTER TABLE employees ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # Column already exists

    conn.commit()
    conn.close()