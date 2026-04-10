import subprocess
from database import get_or_create_person, store_image_path, log_attendance
import sqlite3

DB = "attendance.db"

def show_recent_attendance(limit=10):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.execute("""
    SELECT people.name, attendance.date, attendance.time, attendance.status
    FROM attendance
    JOIN people ON people.id = attendance.person_id
    ORDER BY attendance.id DESC
    LIMIT ?
    """, (limit,))


    rows = cur.fetchall()
    conn.close()
    
def show_daily_hours():
    from database import get_or_create_person, calculate_daily_hours

    name = input("Enter name: ")
    date = input("Enter date (YYYY-MM-DD): ")

    # Get or create person
    person_id = get_or_create_person(name)

    # Calculate hours
    hours = calculate_daily_hours(person_id, date)

    print(f"{name} worked {hours} on {date}")


while True:
    print("\n=== FACE PUNCH-IN SYSTEM ===")
    print("1. Register new user")
    print("2. Train model")
    print("3. Start recognition")
    print("4. Exit")

    choice = input("Choose an option: ")

    if choice == "1":
        name = input("Enter name: ")
        subprocess.run(["python3", "image_capture.py", name])

    elif choice == "2":
        subprocess.run(["python3", "model_training.py"])

    elif choice == "3":
        subprocess.run(["python3", "face_rec_test.py"])

    elif choice == "4":
        print("Goodbye!")
        break

    else:
        print("Invalid option")
