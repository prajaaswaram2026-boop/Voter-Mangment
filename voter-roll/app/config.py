import os
from pathlib import Path

try:  # optional .env loader, no extra dependency required
    for line in Path(".env").read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
except FileNotFoundError:
    pass

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./voter.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len("postgresql://"):]
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-secret-change-me")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ChangeMe!123")
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", "./storage"))
MAX_PDF_MB = int(os.getenv("MAX_PDF_MB", "50"))
OTP_PROVIDER = os.getenv("OTP_PROVIDER", "console")
RETENTION_DAYS = int(os.getenv("SEARCH_LOG_RETENTION_DAYS", "90"))
LOW_CONFIDENCE = 0.8
ECI_URL = "https://voters.eci.gov.in/"
# Scanned-page OCR. Render Free runs one page at a time with one Tesseract thread.
OCR_DPI = int(os.getenv("OCR_DPI", "220"))
OCR_LANG = os.getenv("OCR_LANG", "eng")
OCR_THREADS = max(1, int(os.getenv("OCR_THREADS", "1")))
OCR_INSERT_BATCH = min(500, max(200, int(os.getenv("OCR_INSERT_BATCH", "300"))))
