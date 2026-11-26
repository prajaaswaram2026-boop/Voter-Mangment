"""OTP sending. Replace send_sms() with your approved SMS / WhatsApp Business provider."""
import logging
import re
import secrets
import time

from . import config
from .security import hash_otp

log = logging.getLogger("otp")
MOBILE_RE = re.compile(r"^(?:\+91|91|0)?([6-9]\d{9})$")
_pending: dict[str, dict] = {}   # session_id -> state (in-memory; pending numbers are never persisted)
_ip_hits: dict[str, list] = {}


def normalize(mobile: str) -> str | None:
    m = MOBILE_RE.match(re.sub(r"[\s-]", "", mobile or ""))
    return m.group(1) if m else None


def mask(mobile10: str) -> str:
    return "XXXXXX" + mobile10[-4:]


def rate_limited(ip: str, limit=5, window=3600) -> bool:
    now = time.time()
    hits = [t for t in _ip_hits.get(ip, []) if now - t < window]
    _ip_hits[ip] = hits
    if len(hits) >= limit:
        return True
    hits.append(now)
    return False


def send_sms(mobile10: str, otp: str):
    if config.OTP_PROVIDER == "console":
        log.warning("DEV OTP for %s: %s", mask(mobile10), otp)
        return
    raise NotImplementedError("Configure an approved SMS/WhatsApp provider here")


def start(session_id: str, mobile10: str) -> None:
    st = _pending.get(session_id)
    if st and time.time() - st["sent"] < 45:
        raise ValueError("Please wait before requesting another OTP")
    otp = f"{secrets.randbelow(10**6):06d}"
    _pending[session_id] = dict(mobile=mobile10, hash=hash_otp(session_id, otp),
                                sent=time.time(), attempts=0)
    send_sms(mobile10, otp)


def check(session_id: str, otp: str) -> str | None:
    """Returns the verified 10-digit number, or None. Raises ValueError on expiry/attempt limits."""
    st = _pending.get(session_id)
    if not st:
        raise ValueError("No OTP requested")
    if time.time() - st["sent"] > 300:
        _pending.pop(session_id, None)
        raise ValueError("OTP expired")
    st["attempts"] += 1
    if st["attempts"] > 3:
        _pending.pop(session_id, None)
        raise ValueError("Too many attempts")
    if secrets.compare_digest(st["hash"], hash_otp(session_id, otp)):
        _pending.pop(session_id, None)
        return st["mobile"]
    return None
