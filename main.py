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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def parse_origins() -> list[str]:
    raw = os.environ.get("CORS_ORIGINS", "http://localhost:3000")
    return [o.strip() for o in raw.split(",") if o.strip()]

# ---------------------------------------------------------------------------
# SSE broadcast
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Smart Punch-In API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ToggleRequest(BaseModel):
    employee_id: int | None = None
    email: str | None = None
    name: str | None = None
    confidence: float | None = None

class UpdateEmployeeRequest(BaseModel):
    name: str | None = None
    email: str | None = None
    is_admin: bool | None = None

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
def startup():
    init_db(os.environ.get("DB_PATH", "attendance.db"))

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Employee (self) endpoints
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Device endpoint: Raspberry Pi face recognition
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# SSE: live updates for admin / dashboard
# ---------------------------------------------------------------------------

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
            "X-Accel-Buffering": "no",  # Nginx: disable proxy buffering
        },
    )

# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

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