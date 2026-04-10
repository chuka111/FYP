import os
import secrets
from fastapi import Header, HTTPException

def require_device_key(x_device_key: str = Header(None)):
    device_api_key = os.environ.get("DEVICE_API_KEY")
    if not device_api_key:
        raise HTTPException(status_code=500, detail="DEVICE_API_KEY not configured on server")
    if not x_device_key:
        raise HTTPException(status_code=401, detail="X-Device-Key header missing")
    # Constant time comparison to prevent timing attacks
    if not secrets.compare_digest(x_device_key, device_api_key):
        raise HTTPException(status_code=401, detail="Invalid device key")
    return True