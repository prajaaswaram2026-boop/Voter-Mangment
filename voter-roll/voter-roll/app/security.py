import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import config

_bearer = HTTPBearer(auto_error=False)
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(config.SECRET_KEY.encode()).digest()))


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def check_password(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())


def make_token(username: str, role: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    return jwt.encode({"sub": username, "role": role, "exp": exp}, config.SECRET_KEY, algorithm="HS256")


def _decode(cred: HTTPAuthorizationCredentials | None) -> dict:
    if not cred:
        raise HTTPException(401, "Login required")
    try:
        return jwt.decode(cred.credentials, config.SECRET_KEY, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")


def current_admin(cred: HTTPAuthorizationCredentials = Depends(_bearer)) -> dict:
    data = _decode(cred)
    if data.get("role") != "admin":
        raise HTTPException(401, "Login required")
    return data


def current_volunteer(cred: HTTPAuthorizationCredentials = Depends(_bearer)) -> dict:
    data = _decode(cred)
    if data.get("role") != "volunteer":
        raise HTTPException(401, "Login required")
    return data


def encrypt(text: str) -> str:
    return _fernet.encrypt(text.encode()).decode()


def hash_otp(session_id: str, otp: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), f"{session_id}:{otp}".encode(), hashlib.sha256).hexdigest()
