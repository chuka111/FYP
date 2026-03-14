import os
import firebase_admin
from firebase_admin import credentials, auth
from fastapi import Header, HTTPException

_service_account = os.environ.get(
    "FIREBASE_SERVICE_ACCOUNT",
    "smart-punch-in-firebase-adminsdk-fbsvc-c4c6354377.json",
)

if not firebase_admin._apps:
    cred = credentials.Certificate(_service_account)
    firebase_admin.initialize_app(cred)


async def get_current_user(authorization: str = Header(None)) -> dict:
    """
    Requires a valid Firebase ID token.
    Returns decoded token payload: uid, email, name, etc.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.split(" ", 1)[1].strip()
    try:
        decoded = auth.verify_id_token(token)
        return decoded
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired Firebase token")


async def get_current_user_optional(authorization: str = Header(None)) -> dict | None:
    """Same as get_current_user but returns None instead of raising on failure."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    try:
        return auth.verify_id_token(token)
    except Exception:
        return None