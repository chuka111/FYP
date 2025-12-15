import sqlite3

conn = sqlite3.connect("attendance.db")
cur = conn.cursor()


# people table

cur.execute("""
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
)
""")


# image path table

cur.execute("""
CREATE TABLE IF NOT EXISTS face_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    image_path TEXT NOT NULL,
    FOREIGN KEY (person_id) REFERENCES people(id)
)
""")


# attendance table

cur.execute("""
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY (person_id) REFERENCES people(id)
)
""")


conn.commit()
conn.close()

print("Database setup complete.")
