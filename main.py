from dotenv import load_dotenv
load_dotenv()

import os
import asyncio
import json
from datetime import datetime, timezone, date
from typing import AsyncGenerator

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from db import get_conn
from models import init_db
from auth_firebase import get_current_user, get_current_user_optional
from auth_device import require_device_key
import httpx
from firebase_admin import auth as firebase_auth

# Helpers

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def parse_origins() -> list[str]:
    raw = os.environ.get("CORS_ORIGINS", "http://localhost:3000")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    return origins
# SSE broadcast
# A simple in-process queue list. Each connected SSE client registers a queue.
# When a clock event fires we push to every queue.

_sse_clients: list[asyncio.Queue] = []

async def _broadcast(payload: dict):
    dead = []
    for q in _sse_clients:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _sse_clients.remove(q)

# App
app = FastAPI(title="Smart Punch-In API")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.ngrok-free\.app|https://.*\.ngrok-free\.dev|https://.*\.ngrok\.io|http://localhost:\d+|http://192\.168\.\d+\.\d+:\d+|http://10\.\d+\.\d+\.\d+:\d+|http://172\.\d+\.\d+\.\d+:\d+",    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic models
class ToggleRequest(BaseModel):
    employee_id: int | None = None
    email: str | None = None
    name: str | None = None
    confidence: float | None = None

class UpdateEmployeeRequest(BaseModel):
    name: str | None = None
    email: str | None = None
    is_admin: bool | None = None

class CreateEmployeeRequest(BaseModel):
    name: str
    email: str

# Startup
@app.on_event("startup")
def startup():
    init_db(os.environ.get("DB_PATH", "attendance.db"))

# Internal helpers

def _get_employee_by_uid(firebase_uid: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM employees WHERE firebase_uid=?", (firebase_uid,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def get_or_link_employee(firebase_uid: str, email: str | None, name: str | None = None) -> dict:
    """
    Find employee by firebase_uid.
    If not found, try matching by email (user existed before Firebase link).
    If still not found, create a new record.
    """
    conn = get_conn()
    cur = conn.cursor()

    # 1. Exact UID match
    cur.execute("SELECT * FROM employees WHERE firebase_uid=?", (firebase_uid,))
    row = cur.fetchone()
    if row:
        # Opportunistically back-fill missing email/name
        emp = dict(row)
        updates, params = [], []
        if email and not emp.get("email"):
            updates.append("email=?"); params.append(email)
        if name and not emp.get("name"):
            updates.append("name=?"); params.append(name)
        if updates:
            params.append(emp["id"])
            cur.execute(f"UPDATE employees SET {', '.join(updates)} WHERE id=?", params)
            conn.commit()
            cur.execute("SELECT * FROM employees WHERE id=?", (emp["id"],))
            row = cur.fetchone()
        conn.close()
        return dict(row)

    # 2. Try linking by email (employee pre-created without Firebase UID)
    if email:
        cur.execute("SELECT * FROM employees WHERE email=? AND (firebase_uid IS NULL OR firebase_uid='')", (email,))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE employees SET firebase_uid=? WHERE id=?", (firebase_uid, row["id"]))
            conn.commit()
            cur.execute("SELECT * FROM employees WHERE id=?", (row["id"],))
            row = cur.fetchone()
            conn.close()
            return dict(row)

    # 3. Create new
    display_name = name or (email.split("@")[0] if email else firebase_uid)
    cur.execute(
        "INSERT INTO employees (firebase_uid, email, name) VALUES (?, ?, ?)",
        (firebase_uid, email, display_name),
    )
    conn.commit()
    cur.execute("SELECT * FROM employees WHERE firebase_uid=?", (firebase_uid,))
    row = cur.fetchone()
    conn.close()
    return dict(row)

def get_or_create_employee_by_name(name: str) -> dict:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM employees WHERE name=?", (name,))
    row = cur.fetchone()
    if row:
        conn.close()
        return dict(row)
    cur.execute(
        "INSERT INTO employees (firebase_uid, email, name) VALUES (?, ?, ?)",
        (f"local:{name}", None, name),
    )
    conn.commit()
    cur.execute("SELECT * FROM employees WHERE name=?", (name,))
    row = cur.fetchone()
    conn.close()
    return dict(row)

def get_current_status(employee_id: int) -> dict:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT status, ts_utc FROM time_entries
        WHERE employee_id=? ORDER BY id DESC LIMIT 1
    """, (employee_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"status": "OUT", "last_event": None}
    return {"status": row["status"], "last_event": row["ts_utc"]}

def toggle_status(employee_id: int, source: str, confidence: float | None = None) -> dict:
    current = get_current_status(employee_id)
    next_status = "IN" if current["status"] != "IN" else "OUT"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO time_entries (employee_id, ts_utc, status, source, confidence)
        VALUES (?, ?, ?, ?, ?)
    """, (employee_id, utc_now_iso(), next_status, source, confidence))
    conn.commit()
    cur.execute("""
        SELECT id, employee_id, ts_utc, status, source, confidence
        FROM time_entries WHERE employee_id=? ORDER BY id DESC LIMIT 1
    """, (employee_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row)

def require_admin(user: dict) -> dict:
    emp = _get_employee_by_uid(user["uid"])
    if not emp or not emp.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return emp

async def send_password_reset_email(email: str):
    """Triggers Firebase to send a password reset / invite email via REST API."""
    api_key = os.environ.get("FIREBASE_WEB_API_KEY")
    if not api_key:
        print("[Firebase] FIREBASE_WEB_API_KEY not set — skipping invite email")
        return
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={api_key}"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json={"requestType": "PASSWORD_RESET", "email": email})
        if r.status_code != 200:
            print(f"[Firebase] Email send failed: {r.text}")

def calculate_daily_hours(employee_id: int, target_date: str) -> float:
    """
    Returns total hours worked on target_date (YYYY-MM-DD, UTC).
    Pairs IN→OUT entries; an open IN at end-of-day counts until midnight.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT ts_utc, status FROM time_entries
        WHERE employee_id=? AND substr(ts_utc, 1, 10)=?
        ORDER BY ts_utc ASC
    """, (employee_id, target_date))
    rows = cur.fetchall()
    conn.close()

    total_seconds = 0
    last_in = None
    for r in rows:
        ts = datetime.fromisoformat(r["ts_utc"])
        if r["status"] == "IN":
            last_in = ts
        elif r["status"] == "OUT" and last_in:
            total_seconds += (ts - last_in).total_seconds()
            last_in = None
    return round(total_seconds / 3600, 2)

# Employee (self) endpoints
@app.get("/me")
async def me(user=Depends(get_current_user)):
    employee = get_or_link_employee(user["uid"], user.get("email"), user.get("name"))
    return {"employee": employee}

@app.get("/me/status")
async def me_status(user=Depends(get_current_user)):
    employee = get_or_link_employee(user["uid"], user.get("email"))
    return get_current_status(employee["id"])

@app.get("/me/history")
async def me_history(limit: int = 50, user=Depends(get_current_user)):
    employee = get_or_link_employee(user["uid"], user.get("email"))
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, ts_utc, status, source, confidence
        FROM time_entries WHERE employee_id=? ORDER BY id DESC LIMIT ?
    """, (employee["id"], limit))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"employee_id": employee["id"], "entries": rows}

@app.get("/me/hours")
async def me_hours(target_date: str = Query(default=None), user=Depends(get_current_user)):
    employee = get_or_link_employee(user["uid"], user.get("email"))
    d = target_date or date.today().isoformat()
    hours = calculate_daily_hours(employee["id"], d)
    return {"employee_id": employee["id"], "date": d, "hours": hours}

# Device endpoint: Raspberry Pi face recognition
@app.post("/clock/toggle")
async def clock_toggle(payload: ToggleRequest, _=Depends(require_device_key)):
    employee_id = payload.employee_id

    if not employee_id and payload.email:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM employees WHERE email=?", (payload.email,))
        row = cur.fetchone()
        conn.close()
        if row:
            employee_id = row["id"]

    if not employee_id and payload.name:
        emp = get_or_create_employee_by_name(payload.name)
        employee_id = emp["id"]

    if not employee_id:
        raise HTTPException(status_code=400, detail="employee_id, email, or name is required")

    inserted = toggle_status(employee_id, source="face", confidence=payload.confidence)
    current = get_current_status(employee_id)

    # Broadcast to all SSE listeners
    await _broadcast({
        "type": "clock_event",
        "employee_id": employee_id,
        "event": inserted,
        "current": current,
    })

    return {"ok": True, "event": inserted, "current": current}

# SSE: live updates for admin / dashboard
@app.get("/events")
async def sse_stream(user=Depends(get_current_user)):
    """
    Server-Sent Events stream. Any clock event is pushed to all connected clients.
    Works for both employees (their own events) and admins (all events).
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    _sse_clients.append(queue)

    async def generator() -> AsyncGenerator[str, None]:
        try:
            # Send a heartbeat comment every 20 s to keep proxies alive
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                _sse_clients.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  
        },
    )

# Admin endpoints
@app.get("/admin/employees")
async def admin_list_employees(user=Depends(get_current_user)):
    require_admin(user)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, firebase_uid, email, name, is_admin FROM employees ORDER BY name")
    employees = [dict(r) for r in cur.fetchall()]
    conn.close()

    # Attach current status to each employee
    result = []
    for emp in employees:
        status = get_current_status(emp["id"])
        result.append({**emp, **status})
    return {"employees": result}

@app.get("/admin/employees/unlinked")
async def admin_unlinked_employees(user=Depends(get_current_user)):
    """Returns employees who have no email linked yet — used by the Link Employee modal."""
    require_admin(user)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name FROM employees
        WHERE email IS NULL
        ORDER BY name ASC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"employees": rows}

@app.post("/admin/employees/link")
async def admin_link_employee(
    body: CreateEmployeeRequest,
    user=Depends(get_current_user),
):
    """Link a Pi-recognised employee to a Firebase account and send a password invite."""
    require_admin(user)

    conn = get_conn()
    cur = conn.cursor()

    # Find the existing Pi-created employee by name (case insensitive)
    cur.execute(
        "SELECT * FROM employees WHERE LOWER(name)=LOWER(?)",
        (body.name,)
    )
    existing = cur.fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail=f"No employee named '{body.name}' found")

    existing = dict(existing)

    # Block if already linked
    if existing.get("email"):
        conn.close()
        raise HTTPException(status_code=400, detail=f"{body.name} already has an email linked: {existing['email']}")

    # Check email not already taken
    cur.execute("SELECT id FROM employees WHERE email=?", (body.email,))
    if cur.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="This email is already linked to another employee")

    conn.close()

    # Create Firebase account
    try:
        firebase_user = firebase_auth.create_user(
            email=body.email,
            display_name=existing["name"],
        )
    except firebase_auth.EmailAlreadyExistsError:
        raise HTTPException(status_code=400, detail="This email already exists in Firebase")

    # Update the existing employee record — single connection
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE employees SET firebase_uid=?, email=? WHERE id=?",
            (firebase_user.uid, body.email, existing["id"])
        )
        conn.commit()
        cur.execute("SELECT * FROM employees WHERE id=?", (existing["id"],))
        row = dict(cur.fetchone())
    finally:
        conn.close()

    # Send password invite email
    await send_password_reset_email(body.email)

    return {
        "ok": True,
        "employee": row,
        "message": f"Invite sent to {body.email}"
    }

@app.get("/admin/employees/{employee_id}/history")
async def admin_employee_history(
    employee_id: int,
    limit: int = 100,
    user=Depends(get_current_user),
):
    require_admin(user)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM employees WHERE id=?", (employee_id,))
    emp = cur.fetchone()
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    cur.execute("""
        SELECT id, ts_utc, status, source, confidence
        FROM time_entries WHERE employee_id=? ORDER BY id DESC LIMIT ?
    """, (employee_id, limit))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"employee": dict(emp), "entries": rows}

@app.get("/admin/employees/{employee_id}/hours")
async def admin_employee_hours(
    employee_id: int,
    target_date: str = Query(default=None),
    user=Depends(get_current_user),
):
    require_admin(user)
    d = target_date or date.today().isoformat()
    hours = calculate_daily_hours(employee_id, d)
    return {"employee_id": employee_id, "date": d, "hours": hours}

@app.patch("/admin/employees/{employee_id}")
async def admin_update_employee(
    employee_id: int,
    body: UpdateEmployeeRequest,
    user=Depends(get_current_user),
):
    require_admin(user)
    conn = get_conn()
    cur = conn.cursor()
    updates, params = [], []
    if body.name is not None:
        updates.append("name=?"); params.append(body.name)
    if body.email is not None:
        updates.append("email=?"); params.append(body.email)
    if body.is_admin is not None:
        updates.append("is_admin=?"); params.append(1 if body.is_admin else 0)
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")
    params.append(employee_id)
    cur.execute(f"UPDATE employees SET {', '.join(updates)} WHERE id=?", params)
    conn.commit()
    cur.execute("SELECT * FROM employees WHERE id=?", (employee_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Employee not found")
    return {"employee": dict(row)}

@app.post("/admin/employees/{employee_id}/clock")
async def admin_manual_clock(
    employee_id: int,
    user=Depends(get_current_user),
):
    """Admin can manually toggle clock for any employee."""
    require_admin(user)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM employees WHERE id=?", (employee_id,))
    if not cur.fetchone():
        raise HTTPException(status_code=404, detail="Employee not found")
    conn.close()

    inserted = toggle_status(employee_id, source="manual")
    current = get_current_status(employee_id)
    await _broadcast({"type": "clock_event", "employee_id": employee_id, "event": inserted, "current": current})
    return {"ok": True, "event": inserted, "current": current}

@app.get("/admin/summary")
async def admin_summary(target_date: str = Query(default=None), user=Depends(get_current_user)):
    """Returns all employees with their status and hours for a given date."""
    require_admin(user)
    d = target_date or date.today().isoformat()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, email, is_admin FROM employees ORDER BY name")
    employees = [dict(r) for r in cur.fetchall()]
    conn.close()

    result = []
    for emp in employees:
        status = get_current_status(emp["id"])
        hours = calculate_daily_hours(emp["id"], d)
        result.append({**emp, **status, "hours_today": hours})

    total_in = sum(1 for e in result if e["status"] == "IN")
    return {
        "date": d,
        "total_employees": len(result),
        "currently_in": total_in,
        "currently_out": len(result) - total_in,
        "employees": result,
    }

# Roster / Shifts Pydantic models

class ShiftCreate(BaseModel):
    name: str
    start_time: str   # "HH:MM"
    end_time: str     # "HH:MM"

class RosterAssign(BaseModel):
    employee_id: int
    shift_id: int
    date: str         # "YYYY-MM-DD"

# Shift templates
@app.get("/admin/shifts")
async def list_shifts(user=Depends(get_current_user)):
    require_admin(user)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM shifts ORDER BY start_time")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"shifts": rows}

@app.post("/admin/shifts")
async def create_shift(body: ShiftCreate, user=Depends(get_current_user)):
    require_admin(user)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO shifts (name, start_time, end_time) VALUES (?, ?, ?)",
        (body.name, body.start_time, body.end_time),
    )
    conn.commit()
    cur.execute("SELECT * FROM shifts WHERE id=?", (cur.lastrowid,))
    row = dict(cur.fetchone())
    conn.close()
    return {"shift": row}

@app.delete("/admin/shifts/{shift_id}")
async def delete_shift(shift_id: int, user=Depends(get_current_user)):
    require_admin(user)
    conn = get_conn()
    cur = conn.cursor()
    # Remove any roster entries using this shift first
    cur.execute("DELETE FROM roster WHERE shift_id=?", (shift_id,))
    cur.execute("DELETE FROM shifts WHERE id=?", (shift_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# Roster
@app.get("/admin/roster")
async def admin_get_roster(week: str = Query(default=None), user=Depends(get_current_user)):
    """
    Returns the full roster for a week.
    week = any YYYY-MM-DD date within the desired week (defaults to current week).
    Response includes all employees as rows and Mon-Sun as columns.
    """
    require_admin(user)

    # Calculate Monday of the requested week
    from datetime import timedelta
    ref = date.fromisoformat(week) if week else date.today()
    monday = ref - timedelta(days=ref.weekday())
    days = [(monday + timedelta(days=i)).isoformat() for i in range(7)]

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id, name FROM employees ORDER BY name")
    employees = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM shifts ORDER BY start_time")
    shifts = [dict(r) for r in cur.fetchall()]

    # Fetch all roster entries for the week
    cur.execute("""
        SELECT r.id, r.employee_id, r.shift_id, r.date,
               s.name as shift_name, s.start_time, s.end_time
        FROM roster r
        JOIN shifts s ON s.id = r.shift_id
        WHERE r.date >= ? AND r.date <= ?
    """, (days[0], days[6]))
    entries = [dict(r) for r in cur.fetchall()]
    conn.close()

    # Build a lookup: {employee_id: {date: entry}}
    lookup = {}
    for e in entries:
        lookup.setdefault(e["employee_id"], {})[e["date"]] = e

    # Build grid rows
    rows = []
    for emp in employees:
        row = {"employee_id": emp["id"], "name": emp["name"], "days": {}}
        for d in days:
            row["days"][d] = lookup.get(emp["id"], {}).get(d, None)
        rows.append(row)

    return {
        "week_start": days[0],
        "week_end":   days[6],
        "days":       days,
        "shifts":     shifts,
        "rows":       rows,
    }

@app.post("/admin/roster")
async def assign_roster(body: RosterAssign, user=Depends(get_current_user)):
    """Assign or replace a shift for an employee on a date."""
    require_admin(user)
    conn = get_conn()
    cur = conn.cursor()
    # UPSERT — replace existing entry if same employee+date
    cur.execute("""
        INSERT INTO roster (employee_id, shift_id, date)
        VALUES (?, ?, ?)
        ON CONFLICT(employee_id, date) DO UPDATE SET shift_id=excluded.shift_id
    """, (body.employee_id, body.shift_id, body.date))
    conn.commit()
    cur.execute("""
        SELECT r.id, r.employee_id, r.shift_id, r.date,
               s.name as shift_name, s.start_time, s.end_time
        FROM roster r JOIN shifts s ON s.id = r.shift_id
        WHERE r.employee_id=? AND r.date=?
    """, (body.employee_id, body.date))
    row = dict(cur.fetchone())
    conn.close()
    return {"ok": True, "entry": row}

@app.delete("/admin/roster/{roster_id}")
async def delete_roster_entry(roster_id: int, user=Depends(get_current_user)):
    require_admin(user)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM roster WHERE id=?", (roster_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# Employee: view own roster
@app.get("/me/roster")
async def me_roster(user=Depends(get_current_user)):
    """Returns the employee's roster for the next 14 days."""
    employee = get_or_link_employee(user["uid"], user.get("email"))
    from datetime import timedelta
    today = date.today()
    future = (today + timedelta(days=13)).isoformat()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.id, r.date, s.name as shift_name, s.start_time, s.end_time
        FROM roster r
        JOIN shifts s ON s.id = r.shift_id
        WHERE r.employee_id=? AND r.date >= ? AND r.date <= ?
        ORDER BY r.date ASC
    """, (employee["id"], today.isoformat(), future))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return {"roster": rows}

# Attendance Report — compares clock times against roster shifts
GRACE_MINUTES = 10  # minutes of tolerance before flagging late/early

@app.get("/admin/attendance-report")
async def attendance_report(
    target_date: str = Query(default=None),
    user=Depends(get_current_user),
):
    require_admin(user)
    from datetime import timedelta as td

    d = target_date or date.today().isoformat()

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT r.employee_id, r.id as roster_id,
               s.start_time, s.end_time, s.name as shift_name,
               e.name as employee_name
        FROM roster r
        JOIN shifts s ON s.id = r.shift_id
        JOIN employees e ON e.id = r.employee_id
        WHERE r.date = ?
        ORDER BY e.name ASC
    """, (d,))
    scheduled = [dict(r) for r in cur.fetchall()]

    results = []

    for row in scheduled:
        emp_id = row["employee_id"]

        cur.execute("""
            SELECT status, ts_utc FROM time_entries
            WHERE employee_id=? AND substr(ts_utc,1,10)=?
            ORDER BY ts_utc ASC
        """, (emp_id, d))
        entries = [dict(e) for e in cur.fetchall()]

        first_in  = next((e["ts_utc"] for e in entries if e["status"] == "IN"),  None)
        last_out  = next((e["ts_utc"] for e in reversed(entries) if e["status"] == "OUT"), None)

        # Build naive local datetimes from shift times
        shift_start = datetime.fromisoformat(f"{d}T{row['start_time']}:00")
        shift_end   = datetime.fromisoformat(f"{d}T{row['end_time']}:00")

        flags      = []
        late_by    = None
        early_by   = None
        hours      = calculate_daily_hours(emp_id, d)

        if not first_in:
            flags.append("no_show")
        else:
            # Normalise UTC timestamp to naive for comparison
            ci = datetime.fromisoformat(first_in.replace("+00:00", "").replace("Z", ""))
            diff_in = (ci - shift_start).total_seconds() / 60
            if diff_in > GRACE_MINUTES:
                flags.append("late_in")
                late_by = round(diff_in)

        if first_in and not last_out:
            flags.append("no_clock_out")
        elif last_out:
            co = datetime.fromisoformat(last_out.replace("+00:00", "").replace("Z", ""))
            diff_out = (shift_end - co).total_seconds() / 60
            if diff_out > GRACE_MINUTES:
                flags.append("early_out")
                early_by = round(diff_out)

        results.append({
            "employee_id":   emp_id,
            "employee_name": row["employee_name"],
            "shift_name":    row["shift_name"],
            "shift_start":   row["start_time"],
            "shift_end":     row["end_time"],
            "clocked_in":    first_in,
            "clocked_out":   last_out,
            "hours_worked":  hours,
            "flags":         flags,
            "late_by_mins":  late_by,
            "early_by_mins": early_by,
            "on_time":       len(flags) == 0,
        })

    conn.close()

    no_show   = sum(1 for r in results if "no_show"   in r["flags"])
    late      = sum(1 for r in results if "late_in"   in r["flags"])
    early_out = sum(1 for r in results if "early_out" in r["flags"])
    on_time   = sum(1 for r in results if r["on_time"])

    return {
        "date":           d,
        "grace_minutes":  GRACE_MINUTES,
        "total_scheduled": len(results),
        "on_time":        on_time,
        "late":           late,
        "early_out":      early_out,
        "no_show":        no_show,
        "report":         results,
    }
