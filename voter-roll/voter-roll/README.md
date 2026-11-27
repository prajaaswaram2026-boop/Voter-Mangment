# Voter Roll Verification

FastAPI + SQLAlchemy backend, single-page HTML frontend. This is the folder to push to GitHub and deploy on Render.

## Deploy on Render

1. Push this folder to GitHub. Do not commit `.env`, `storage/*.xlsx`, `voter.db`, or `.venv`.
2. In Render, create a Blueprint from `render.yaml`.
3. Set `ADMIN_PASSWORD` in the Render service environment when it asks.
4. Render creates the PostgreSQL database and starts `uvicorn` on `$PORT`.

The free Render disk is temporary. Voter rows stay in PostgreSQL. Uploaded Excel files do not survive a new deploy.

## Run

```
python -m venv .venv
.venv\Scripts\activate          (Linux/Mac: . .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          (edit SECRET_KEY and ADMIN_PASSWORD)
uvicorn app.main:app --reload
```
Open http://localhost:8000. API docs at /docs. Default admin: values of ADMIN_USERNAME / ADMIN_PASSWORD in `.env`.

SQLite is the default. For PostgreSQL set `DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/voterdb`
(tables are created automatically on startup).

## Workflow
1. Admin → Divisions: add a division.
2. Admin → Upload PDFs: upload one or many roll PDFs. Status goes Queued → Processing → Validation Required.
3. Review: low-confidence, duplicate and missing-EPIC records are listed; edit and Confirm each one.
4. Publish: only possible when nothing is left to review. Only published records are searchable.
5. Citizens search by EPIC or name (fuzzy). EPICs are masked in results.

## Tests
`python -m pytest -q`

## What is included / not included
Included: JWT admin login (bcrypt), divisions with dependency-checked delete, polling-station editing, PDF upload
validation, text-PDF extraction in 3-column layout, optional Tesseract fallback, draft-then-publish review flow,
extraction logs, EPIC/name/fuzzy search with pagination, disclaimer + ECI link, consent-gated OTP (hashed OTP,
5-minute expiry, 3 attempts, resend delay, IP rate limit, only verified numbers stored, encrypted, not linked to
voters), search log without personal data and with retention, audit log, aggregate CSV reports.

Not included (to keep it small): separate roles/sections/supplements tables, Excel export, WhatsApp provider,
Docker, backup scripts, HTTPS (put it behind a reverse proxy), malware scanning.

## Needed before real use
* **Sample PDF:** none was attached, so the parser in `app/extract.py` is built for the standard ECI layout and
  tested on synthetic card text only. Send the PDF and tune the regexes against it.
* **OTP:** `OTP_PROVIDER=console` prints the OTP in the server log. Implement `send_sms()` in `app/otp.py` with an
  approved SMS/WhatsApp provider.
* Set a long random `SECRET_KEY` (32+ chars) and change the admin password.
* Telugu OCR: install Tesseract with the `tel` language data and `pip install pytesseract`.
