import csv
import io
import logging
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from rapidfuzz import fuzz
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from . import config, otp
from .db import Base, SessionLocal, engine, get_db
from .extract import extract_pdf
from .models import (Assembly, AuditLog, Consent, District, Division, ExtractionLog,
                     MobileClient, MobileVerification, PollingStation, SearchLog, Upload, User,
                     Voter, VoterMark, Volunteer, VolunteerBooth)
from .security import check_password, current_admin, current_volunteer, encrypt, hash_password, make_token

app = FastAPI(title="Voter Roll Verification", version="1.0")
logger = logging.getLogger("voter.upload")
EPIC_FORMAT = re.compile(r"^[A-Z]{3}\d{7}$")


def _ensure_upload_columns():
    if not str(config.DATABASE_URL).startswith("sqlite"):
        return
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(electoral_roll_uploads)")}
        if "station_name" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE electoral_roll_uploads ADD COLUMN station_name VARCHAR(200) DEFAULT ''")
        if "station_number" not in cols:
            conn.exec_driver_sql(
                "ALTER TABLE electoral_roll_uploads ADD COLUMN station_number VARCHAR(20) DEFAULT ''")


def _ensure_area_columns():
    from sqlalchemy import inspect
    tables = set(inspect(engine).get_table_names())
    if "divisions" not in tables:
        return
    cols = {c["name"] for c in inspect(engine).get_columns("divisions")}
    if "assembly_id" not in cols:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE divisions ADD COLUMN assembly_id INTEGER")


def _link_existing_areas(db: Session):
    for div in db.scalars(select(Division).where(Division.assembly_id.is_(None))):
        dname = (div.district or "").strip()
        aname = (div.ac_name or "").strip()
        if not dname and not aname:
            continue
        dname = dname or "Unassigned"
        aname = aname or "Assembly"
        anum = (div.ac_number or "").strip()[:10]
        dist = db.scalar(select(District).where(func.lower(District.name) == dname.lower()))
        if not dist:
            dist = District(name=_clip(dname, 120))
            db.add(dist)
            db.flush()
        asm = db.scalar(select(Assembly).where(
            Assembly.district_id == dist.id, Assembly.number == anum,
            func.lower(Assembly.name) == aname.lower()))
        if not asm:
            asm = Assembly(district_id=dist.id, number=anum, name=_clip(aname, 120))
            db.add(asm)
            db.flush()
        div.assembly_id = asm.id
        div.district = dist.name
        div.ac_number = asm.number
        div.ac_name = asm.name


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    _ensure_upload_columns()
    _ensure_area_columns()
    config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        if not db.scalar(select(User).limit(1)):
            db.add(User(username=config.ADMIN_USERNAME, password_hash=hash_password(config.ADMIN_PASSWORD)))
            db.commit()
        _link_existing_areas(db)
        db.commit()
        # retention policy for search logs
        cutoff = datetime.utcnow() - timedelta(days=config.RETENTION_DAYS)
        db.execute(delete(SearchLog).where(SearchLog.at < cutoff))
        for up in db.scalars(select(Upload)):
            full = (up.station_number or "").strip()
            if not up.part_number or not full:
                continue
            st = db.scalar(select(PollingStation).where(
                PollingStation.division_id == up.division_id, PollingStation.part_number == up.part_number))
            if st and full.startswith(st.number or "") and len(full) > len(st.number or ""):
                st.number = full[:40]
        db.commit()


def audit(db: Session, admin: dict, action: str, detail: str = ""):
    db.add(AuditLog(username=admin["sub"], action=action, detail=detail[:500]))


# ---------------------------------------------------------------- auth
class Login(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(body: Login, db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.username == body.username))
    if not u or not check_password(body.password, u.password_hash):
        raise HTTPException(401, "Invalid credentials")
    return {"token": make_token(u.username, u.role)}


# ---------------------------------------------------------------- district / assembly / division
class DistrictIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)


class AssemblyIn(BaseModel):
    district_id: int
    number: str = ""
    name: str = Field(min_length=2, max_length=120)


def _district_row(d: District):
    return {"id": d.id, "name": d.name}


def _assembly_row(a: Assembly):
    return {"id": a.id, "district_id": a.district_id, "number": a.number, "name": a.name}


@app.get("/api/admin/districts")
def list_districts(db: Session = Depends(get_db), _=Depends(current_admin)):
    return [_district_row(d) for d in db.scalars(select(District).order_by(District.name))]


@app.post("/api/admin/districts")
def create_district(body: DistrictIn, db: Session = Depends(get_db), a=Depends(current_admin)):
    name = body.name.strip()
    if db.scalar(select(District).where(func.lower(District.name) == name.lower())):
        raise HTTPException(409, "District already exists")
    row = District(name=name)
    db.add(row)
    audit(db, a, "district.create", name)
    db.commit()
    return _district_row(row)


@app.delete("/api/admin/districts/{did}")
def delete_district(did: int, db: Session = Depends(get_db), a=Depends(current_admin)):
    row = db.get(District, did)
    if not row:
        raise HTTPException(404, "Not found")
    if db.scalar(select(func.count()).select_from(Assembly).where(Assembly.district_id == did)):
        raise HTTPException(409, "District has assemblies; remove them first")
    audit(db, a, "district.delete", row.name)
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.get("/api/admin/assemblies")
def list_assemblies(district_id: int = 0, db: Session = Depends(get_db), _=Depends(current_admin)):
    q = select(Assembly).order_by(Assembly.number, Assembly.name)
    if district_id:
        q = q.where(Assembly.district_id == district_id)
    return [_assembly_row(row) for row in db.scalars(q)]


@app.post("/api/admin/assemblies")
def create_assembly(body: AssemblyIn, db: Session = Depends(get_db), a=Depends(current_admin)):
    dist = db.get(District, body.district_id)
    if not dist:
        raise HTTPException(404, "District not found")
    number, name = body.number.strip()[:10], body.name.strip()
    taken = db.scalar(select(Assembly).where(
        Assembly.district_id == dist.id, Assembly.number == number, func.lower(Assembly.name) == name.lower()))
    if taken:
        raise HTTPException(409, "Assembly already exists")
    row = Assembly(district_id=dist.id, number=number, name=name)
    db.add(row)
    audit(db, a, "assembly.create", f"{dist.name}/{number} {name}")
    db.commit()
    return _assembly_row(row)


@app.delete("/api/admin/assemblies/{aid}")
def delete_assembly(aid: int, db: Session = Depends(get_db), a=Depends(current_admin)):
    row = db.get(Assembly, aid)
    if not row:
        raise HTTPException(404, "Not found")
    if db.scalar(select(func.count()).select_from(Division).where(Division.assembly_id == aid)):
        raise HTTPException(409, "Assembly has mandals; remove them first")
    audit(db, a, "assembly.delete", row.name)
    db.delete(row)
    db.commit()
    return {"ok": True}


@app.get("/api/admin/areas")
def list_areas(db: Session = Depends(get_db), _=Depends(current_admin)):
    return {
        "districts": list_districts(db),
        "assemblies": list_assemblies(0, db),
        "divisions": [_d(d) for d in db.scalars(select(Division).order_by(Division.name))],
    }


# ---------------------------------------------------------------- divisions / stations
class DivisionIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    assembly_id: int | None = None
    ac_number: str = ""
    ac_name: str = ""
    district: str = ""
    description: str = ""
    status: str = "active"


def _d(d: Division):
    return {c.name: getattr(d, c.name) for c in d.__table__.columns}


@app.get("/api/admin/divisions")
def list_divisions(db: Session = Depends(get_db), _=Depends(current_admin)):
    return [_d(d) for d in db.scalars(select(Division).order_by(Division.name))]


def _fill_division_area(db: Session, row: Division, assembly_id: int | None):
    if not assembly_id:
        return
    asm = db.get(Assembly, assembly_id)
    if not asm:
        raise HTTPException(404, "Assembly not found")
    dist = db.get(District, asm.district_id)
    row.assembly_id = asm.id
    row.ac_number = asm.number
    row.ac_name = asm.name
    row.district = dist.name if dist else ""


@app.post("/api/admin/divisions")
def create_division(body: DivisionIn, db: Session = Depends(get_db), a=Depends(current_admin)):
    if db.scalar(select(Division).where(func.lower(Division.name) == body.name.strip().lower())):
        raise HTTPException(409, "Mandal / division / ward already exists")
    d = Division(**body.model_dump())
    d.name = body.name.strip()
    _fill_division_area(db, d, body.assembly_id)
    db.add(d)
    audit(db, a, "division.create", d.name)
    db.commit()
    return _d(d)


@app.put("/api/admin/divisions/{did}")
def update_division(did: int, body: DivisionIn, db: Session = Depends(get_db), a=Depends(current_admin)):
    d = db.get(Division, did)
    if not d:
        raise HTTPException(404, "Not found")
    for k, v in body.model_dump().items():
        setattr(d, k, v)
    _fill_division_area(db, d, body.assembly_id)
    audit(db, a, "division.update", d.name)
    db.commit()
    return _d(d)


@app.delete("/api/admin/divisions/{did}")
def delete_division(did: int, db: Session = Depends(get_db), a=Depends(current_admin)):
    d = db.get(Division, did)
    if not d:
        raise HTTPException(404, "Not found")
    if db.scalar(select(func.count()).select_from(Upload).where(Upload.division_id == did)) or \
            db.scalar(select(func.count()).select_from(Voter).where(Voter.division_id == did)):
        raise HTTPException(409, "Division has uploads or voters; remove them first")
    db.execute(delete(PollingStation).where(PollingStation.division_id == did))
    audit(db, a, "division.delete", d.name)
    db.delete(d)
    db.commit()
    return {"ok": True}


class StationIn(BaseModel):
    part_number: str
    number: str = ""
    name: str = ""
    address: str = ""
    locality: str = ""
    mandal: str = ""
    map_url: str = ""


@app.get("/api/admin/divisions/{did}/stations")
def list_stations(did: int, db: Session = Depends(get_db), _=Depends(current_admin)):
    return [{c.name: getattr(s, c.name) for c in s.__table__.columns}
            for s in db.scalars(select(PollingStation).where(PollingStation.division_id == did))]


@app.put("/api/admin/divisions/{did}/stations")
def upsert_station(did: int, body: StationIn, db: Session = Depends(get_db), a=Depends(current_admin)):
    s = db.scalar(select(PollingStation).where(PollingStation.division_id == did,
                                                PollingStation.part_number == body.part_number))
    if not s:
        s = PollingStation(division_id=did, part_number=body.part_number)
        db.add(s)
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    audit(db, a, "station.upsert", f"{did}/{body.part_number}")
    db.commit()
    return {"id": s.id}


# ---------------------------------------------------------------- upload + processing
def _clip(value, n):
    return (value or "")[:n]


def _voter_row(up: Upload, record: dict) -> Voter:
    return Voter(division_id=up.division_id, upload_id=up.id, part_number=up.part_number,
                 record_status="draft", ocr_confidence=record["confidence"],
                 serial_number=record["serial_number"], epic_number=_clip(record["epic_number"], 20),
                 voter_name=_clip(record["voter_name"], 200), relative_name=_clip(record["relative_name"], 200),
                 relationship_type=_clip(record["relationship_type"], 20),
                 house_number=_clip(record["house_number"], 40), age=record["age"],
                 gender=_clip(record["gender"], 20), pdf_page_number=record["pdf_page_number"],
                 flags=_clip(record.get("flags", ""), 120))


def _refresh_duplicate_flags(db: Session, upload_id: int):
    rows = db.scalars(select(Voter).where(Voter.upload_id == upload_id)).all()
    counts = {}
    for row in rows:
        if row.epic_number:
            counts[row.epic_number] = counts.get(row.epic_number, 0) + 1
    for row in rows:
        flags = [f for f in (row.flags or "").split(",") if f and f != "duplicate"]
        if row.epic_number and counts.get(row.epic_number, 0) > 1:
            flags.append("duplicate")
        row.flags = ",".join(flags)[:120]


def _split_ac(ac: str) -> tuple[str, str]:
    m = re.match(r"\s*(\d+)\s*[-–]?\s*(.*)$", ac or "")
    if not m:
        return "", (ac or "").strip()
    return m.group(1), m.group(2).strip(" -")


def _apply_roll_meta(up: Upload, meta: dict):
    if not up.part_number and meta.get("part_number"):
        up.part_number = _clip(meta["part_number"], 10)
    if meta.get("qualifying_date"):
        up.qualifying_date = _clip(meta["qualifying_date"], 30)
    if meta.get("publication_date"):
        up.publication_date = _clip(meta["publication_date"], 30)


def _apply_hierarchy(db: Session, up: Upload, meta: dict):
    """Keep district, assembly and division filled from the roll cover when the admin left them blank."""
    div = db.get(Division, up.division_id)
    if not div or not meta:
        return
    if not (div.district or "").strip() and meta.get("district"):
        div.district = _clip(meta["district"], 120)
    number, name = _split_ac(meta.get("ac") or "")
    if number and not (div.ac_number or "").strip():
        div.ac_number = _clip(number, 10)
    if name and not (div.ac_name or "").strip():
        div.ac_name = _clip(name, 120)


def process_upload(upload_id: int):
    started = time.perf_counter()
    with SessionLocal() as db:
        up = db.get(Upload, upload_id)
        if not up:
            return
        path = up.path
        done = {row.page for row in db.scalars(select(ExtractionLog).where(
            ExtractionLog.upload_id == upload_id, ExtractionLog.method.in_(("text", "ocr"))))}
        if done:
            db.execute(delete(Voter).where(Voter.upload_id == upload_id, Voter.pdf_page_number.notin_(done)))
        else:
            db.execute(delete(Voter).where(Voter.upload_id == upload_id))
        db.execute(delete(ExtractionLog).where(
            ExtractionLog.upload_id == upload_id, ExtractionLog.method.notin_(("text", "ocr"))))
        up.status = "Processing"
        up.error = ""
        db.commit()
        if done:
            logger.info("resume upload=%s completed_pages=%s", upload_id, sorted(done))

    pending_recs, pending_logs = [], []
    state = {"meta": {}, "pages": 0, "saved": 0}

    def _flush():
        if not pending_recs and not pending_logs:
            return
        with SessionLocal() as db:
            up = db.get(Upload, upload_id)
            if not up:
                pending_recs.clear()
                pending_logs.clear()
                return
            _apply_roll_meta(up, state["meta"])
            existing = set(db.execute(select(Voter.serial_number, Voter.epic_number).where(
                Voter.upload_id == upload_id)).all())
            fresh = []
            for record in pending_recs:
                key = (record["serial_number"], record["epic_number"])
                if key in existing:
                    continue
                existing.add(key)
                fresh.append(_voter_row(up, record))
            step = config.OCR_INSERT_BATCH
            for start in range(0, len(fresh), step):
                db.add_all(fresh[start:start + step])
            for page, method, n, note in pending_logs:
                db.add(ExtractionLog(upload_id=upload_id, page=page, method=method, records=n, note=note))
            db.commit()
            state["saved"] += len(fresh)
        pending_recs.clear()
        pending_logs.clear()

    def log(page, method, n, note):
        pending_logs.append((page, method, n, note))

    def on_page(page, method, recs, note, elapsed, meta):
        state["meta"] = meta or {}
        pending_recs.extend(recs)
        if len(pending_recs) >= config.OCR_INSERT_BATCH or method in ("error", "none"):
            _flush()

    try:
        meta, recs, pages = extract_pdf(path, log, skip_pages=done, on_page=on_page)
        _flush()
        state["meta"] = meta or state["meta"]
        state["pages"] = pages
        with SessionLocal() as db:
            saved = db.scalar(select(func.count()).select_from(Voter).where(Voter.upload_id == upload_id)) or 0
        status = "Validation Required" if saved else "Failed"
        error = "" if saved else "No voter records found. Is the PDF scanned or unreadable?"
    except Exception as e:
        logger.exception("upload %s failed", upload_id)
        _flush()
        meta, pages, status, error = state["meta"], state["pages"], "Failed", str(e)[:500]

    with SessionLocal() as db:
        up = db.get(Upload, upload_id)
        if not up:
            return
        _apply_roll_meta(up, state["meta"])
        _apply_hierarchy(db, up, state["meta"])
        if state["pages"]:
            up.pages = state["pages"]
        _refresh_duplicate_flags(db, upload_id)
        if up.part_number:
            st = db.scalar(select(PollingStation).where(
                PollingStation.division_id == up.division_id, PollingStation.part_number == up.part_number))
            if not st:
                st = PollingStation(division_id=up.division_id, part_number=up.part_number)
                db.add(st)
            if up.station_number:
                st.number = _clip(up.station_number, 40)
            elif not st.number:
                st.number = up.part_number
            if up.station_name:
                st.name = _clip(up.station_name, 200)
            elif state["meta"].get("station") and not st.name:
                st.name = _clip(state["meta"]["station"], 200)
            up.station_id = st.id
            if state["meta"].get("station_address") and not st.address:
                st.address = state["meta"]["station_address"][:500]
        up.status, up.error = status, error
        db.commit()
        total = db.scalar(select(func.count()).select_from(Voter).where(Voter.upload_id == upload_id)) or 0
    try:
        write_roll_workbook()
    except Exception:
        logger.exception("excel export failed")
    logger.info("upload %s status=%s records=%s seconds=%.2f", upload_id, status, total, time.perf_counter() - started)


def _missing_identity():
    epic = func.trim(Voter.epic_number)
    name = func.trim(Voter.voter_name)
    return (epic == "") | (epic.is_(None)) | (name == "") | (name.is_(None))


@app.post("/api/admin/uploads")
async def upload_pdfs(bg: BackgroundTasks, division_id: int = Form(...), roll_year: int = Form(...),
                      revision_type: str = Form("main"), part_number: str = Form(""),
                      station_name: str = Form(""), station_number: str = Form(""),
                      description: str = Form(""),                       files: list[UploadFile] | None = File(None),
                      db: Session = Depends(get_db), a=Depends(current_admin)):
    station_name, station_number = station_name.strip(), station_number.strip()
    if not station_name or not station_number:
        raise HTTPException(400, "Enter the polling station name and polling station number")
    if not files:
        raise HTTPException(400, "Choose at least one PDF")
    if not db.get(Division, division_id):
        raise HTTPException(404, "Division not found")
    if revision_type not in ("main", "supplement", "deletions"):
        raise HTTPException(400, "Bad revision type")
    ids = []
    for f in files:
        data = await f.read()
        if not data.startswith(b"%PDF"):
            raise HTTPException(400, f"{f.filename} is not a PDF")
        if len(data) > config.MAX_PDF_MB * 1024 * 1024:
            raise HTTPException(413, f"{f.filename} exceeds {config.MAX_PDF_MB} MB")
        path = config.STORAGE_DIR / f"{uuid.uuid4().hex}.pdf"
        path.write_bytes(data)
        up = Upload(division_id=division_id, filename=Path(f.filename).name[:250], path=str(path),
                    roll_year=roll_year, revision_type=revision_type, part_number=part_number.strip(),
                    station_name=_clip(station_name, 200), station_number=_clip(station_number, 20),
                    description=description, status="Queued")
        db.add(up)
        db.flush()
        ids.append(up.id)
        audit(db, a, "upload.create", f.filename)
    db.commit()
    for i in ids:
        bg.add_task(process_upload, i)
    return {"upload_ids": ids}


_EXCEL_COLUMNS = {
    "serial": {"serial no", "serial", "sl no", "s no", "sno", "serial number"},
    "epic": {"epic no", "epic", "epic number", "voter id"},
    "name": {"name", "voter name", "elector name"},
    "relative": {"relative name", "relative", "father name", "fathers name", "husband name"},
    "relation": {"relation", "relationship", "relationship type"},
    "house": {"house no", "house number", "house", "address"},
    "age": {"age"},
    "gender": {"gender", "sex"},
    "mobile": {"mobile no", "mobile", "mobile number", "phone", "phone no", "phone number", "contact no"},
}


def _header_key(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _excel_rows(data: bytes):
    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheet = book.active
        rows = sheet.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            raise HTTPException(400, "The Excel file is empty")
        found = {}
        for index, cell in enumerate(header):
            key = _header_key(cell)
            for name, aliases in _EXCEL_COLUMNS.items():
                if key in aliases and name not in found:
                    found[name] = index
        missing = [label for name, label in (("serial", "serial no"), ("epic", "epic no"), ("name", "name")) if name not in found]
        if missing:
            raise HTTPException(400, "Excel must have columns: serial no, epic no, name, relative name, relation, house no, age, gender and mobile no")
        out = []
        for row in rows:
            if not row or all(cell is None or str(cell).strip() == "" for cell in row):
                continue

            def cell(name):
                idx = found.get(name)
                if idx is None or idx >= len(row) or row[idx] is None:
                    return ""
                value = row[idx]
                if isinstance(value, float) and value.is_integer():
                    return str(int(value))
                if isinstance(value, int):
                    return str(value)
                return str(value).strip()

            serial_text = cell("serial")
            serial = int(float(serial_text)) if re.fullmatch(r"\d+(\.0+)?", serial_text) else 0
            age_text = cell("age")
            age = int(float(age_text)) if re.fullmatch(r"\d+(\.0+)?", age_text) else None
            if age is not None and not 1 <= age <= 120:
                age = None
            out.append(dict(serial_number=serial, epic_number=cell("epic").upper().replace(" ", ""),
                            voter_name=cell("name"), relative_name=cell("relative"),
                            relationship_type=cell("relation"), house_number=cell("house"),
                            age=age, gender=cell("gender"), mobile=_roll_mobile(cell("mobile"))))
        if not out:
            raise HTTPException(400, "The Excel file has no voter rows")
        return out
    finally:
        book.close()


def _roll_mobile(value) -> str:
    """Keep the digits from the roll. Sheets often store 8–13 digit contact numbers."""
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("91") and len(digits) > 10:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) > 10:
        digits = digits[1:]
    return digits if 8 <= len(digits) <= 13 else ""


def _part_from_station(station_number: str) -> str:
    match = re.match(r"\s*(\d+)", station_number or "")
    return match.group(1)[:10] if match else _clip(station_number, 10)


@app.post("/api/admin/uploads/excel")
async def upload_excel(division_id: int = Form(...), station_number: str = Form(...),
                       station_name: str = Form(...), station_address: str = Form(...),
                       roll_year: int = Form(0), revision_type: str = Form("main"),
                       file: UploadFile | None = File(None),
                       db: Session = Depends(get_db), a=Depends(current_admin)):
    station_number, station_name, station_address = station_number.strip(), station_name.strip(), station_address.strip()
    if not all([station_number, station_name, station_address]):
        raise HTTPException(400, "Enter polling station number, name and address")
    if revision_type not in ("main", "supplement", "deletions"):
        raise HTTPException(400, "Bad revision type")
    div = db.get(Division, division_id)
    if not div:
        raise HTTPException(400, "Select a mandal, division or ward")
    if file is None or not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(400, "Choose an Excel .xlsx file")
    data = await file.read()
    if len(data) > config.MAX_PDF_MB * 1024 * 1024:
        raise HTTPException(413, f"{file.filename} exceeds {config.MAX_PDF_MB} MB")
    records = _excel_rows(data)
    part = _part_from_station(station_number)
    station = db.scalar(select(PollingStation).where(
        PollingStation.division_id == div.id, PollingStation.part_number == part))
    if not station:
        station = PollingStation(division_id=div.id, part_number=part)
        db.add(station)
        db.flush()
    station.number = _clip(station_number, 40)
    station.name = _clip(station_name, 200)
    station.address = station_address[:500]
    path = config.STORAGE_DIR / f"{uuid.uuid4().hex}.xlsx"
    path.write_bytes(data)
    year = roll_year or datetime.utcnow().year
    up = Upload(division_id=div.id, station_id=station.id, filename=Path(file.filename).name[:250],
                path=str(path), roll_year=year, revision_type=revision_type, part_number=part,
                station_name=station.name, station_number=_clip(station_number, 20),
                status="Validation Required", pages=1)
    db.add(up)
    db.flush()
    seen = set()
    saved = 0
    for record in records:
        key = (record["serial_number"], record["epic_number"])
        if key in seen:
            continue
        seen.add(key)
        flags = []
        if not record["epic_number"]:
            flags.append("missing_epic")
        if not record["voter_name"]:
            flags.append("missing_name")
        voter = Voter(division_id=div.id, upload_id=up.id, part_number=part, record_status="draft",
                      ocr_confidence=1.0 if not flags else 0.5,
                      serial_number=record["serial_number"], epic_number=_clip(record["epic_number"], 20),
                      voter_name=_clip(record["voter_name"], 200), relative_name=_clip(record["relative_name"], 200),
                      relationship_type=_clip(record["relationship_type"], 20),
                      house_number=_clip(record["house_number"], 40), age=record["age"],
                      gender=_clip(record["gender"], 20), flags=",".join(flags))
        db.add(voter)
        db.flush()
        if record.get("mobile"):
            db.add(VoterMark(voter_id=voter.id, mobile=record["mobile"], updated_by=a["sub"]))
        saved += 1
    audit(db, a, "upload.excel", file.filename)
    db.commit()
    try:
        write_roll_workbook()
    except Exception:
        logger.exception("excel export failed")
    return {"upload_id": up.id, "records": saved}


@app.get("/api/admin/uploads")
def list_uploads(db: Session = Depends(get_db), _=Depends(current_admin)):
    out = []
    for u in db.scalars(select(Upload).order_by(Upload.id.desc())):
        n = db.scalar(select(func.count()).select_from(Voter).where(Voter.upload_id == u.id))
        low = db.scalar(select(func.count()).select_from(Voter).where(
            Voter.upload_id == u.id, _missing_identity()))
        out.append(dict(id=u.id, filename=u.filename, status=u.status, error=u.error, pages=u.pages,
                        part_number=u.part_number, station_name=u.station_name, station_number=u.station_number,
                        roll_year=u.roll_year, revision_type=u.revision_type,
                        division_id=u.division_id, records=n, needs_review=low))
    return out


@app.get("/api/admin/uploads/{uid}/records")
def upload_records(uid: int, only_review: bool = False, page: int = 1, db: Session = Depends(get_db),
                   _=Depends(current_admin)):
    q = select(Voter).where(Voter.upload_id == uid).order_by(Voter.pdf_page_number, Voter.serial_number)
    if only_review:
        q = q.where(_missing_identity())
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.offset((page - 1) * 50).limit(50)).all()
    records = [{c.name: getattr(v, c.name) for c in v.__table__.columns if c.name != "updated_at"} for v in rows]
    return {"total": total, "records": records}


class VoterEdit(BaseModel):
    epic_number: str | None = None
    voter_name: str | None = None
    relative_name: str | None = None
    relationship_type: str | None = None
    house_number: str | None = None
    age: int | None = None
    gender: str | None = None


@app.patch("/api/admin/records/{vid}")
def edit_record(vid: str, body: VoterEdit, db: Session = Depends(get_db), a=Depends(current_admin)):
    v = db.get(Voter, vid)
    if not v:
        raise HTTPException(404, "Not found")
    for k, val in body.model_dump(exclude_none=True).items():
        setattr(v, k, val)
    missing = []
    if not (v.epic_number or "").strip():
        missing.append("missing_epic")
    if not (v.voter_name or "").strip():
        missing.append("missing_name")
    v.flags = ",".join(missing)
    if not missing:
        v.ocr_confidence = 1.0
    audit(db, a, "record.edit", vid)
    db.commit()
    return {"ok": True}


@app.post("/api/admin/uploads/{uid}/publish")
def publish(uid: int, db: Session = Depends(get_db), a=Depends(current_admin)):
    up = db.get(Upload, uid)
    if not up:
        raise HTTPException(404, "Not found")
    pending = db.scalar(select(func.count()).select_from(Voter).where(
        Voter.upload_id == uid, _missing_identity()))
    if pending:
        raise HTTPException(409, f"{pending} records are missing an EPIC number or a name")
    for v in db.scalars(select(Voter).where(Voter.upload_id == uid)):
        v.record_status = "published"
    up.status = "Completed"
    audit(db, a, "upload.publish", str(uid))
    db.commit()
    return {"ok": True}


@app.delete("/api/admin/uploads/{uid}")
def delete_upload(uid: int, db: Session = Depends(get_db), a=Depends(current_admin)):
    up = db.get(Upload, uid)
    if not up:
        raise HTTPException(404, "Not found")
    db.execute(delete(ExtractionLog).where(ExtractionLog.upload_id == uid))
    db.execute(delete(Voter).where(Voter.upload_id == uid))
    path = Path(up.path)
    if path.is_file():
        path.unlink()
    audit(db, a, "upload.delete", up.filename)
    db.delete(up)
    db.commit()
    return {"ok": True}


class VolunteerIn(BaseModel):
    name: str
    mobile: str


class BoothAssign(BaseModel):
    station_ids: list[int] = []


def _booth_row(st: PollingStation, division: str):
    return {"id": st.id, "division_id": st.division_id, "division": division,
            "part_number": st.part_number, "number": st.number or st.part_number, "name": st.name}


@app.get("/api/admin/volunteers")
def list_volunteers(db: Session = Depends(get_db), _=Depends(current_admin)):
    divs = {d.id: d.name for d in db.scalars(select(Division))}
    stations = db.scalars(select(PollingStation).order_by(PollingStation.division_id, PollingStation.part_number)).all()
    booths = [_booth_row(s, divs.get(s.division_id, "")) for s in stations]
    by_id = {b["id"]: b for b in booths}
    out = []
    for v in db.scalars(select(Volunteer).order_by(Volunteer.name)):
        ids = db.scalars(select(VolunteerBooth.station_id).where(VolunteerBooth.volunteer_id == v.id)).all()
        out.append({"id": v.id, "name": v.name, "mobile": v.mobile,
                    "booths": [by_id[i] for i in ids if i in by_id]})
    return {"volunteers": out, "booths": booths}


@app.post("/api/admin/volunteers")
def create_volunteer(body: VolunteerIn, db: Session = Depends(get_db), a=Depends(current_admin)):
    name = body.name.strip()
    mobile = re.sub(r"\D", "", body.mobile)
    if mobile.startswith("91") and len(mobile) == 12:
        mobile = mobile[2:]
    if not name:
        raise HTTPException(400, "Enter the volunteer name")
    if not re.fullmatch(r"[6-9]\d{9}", mobile):
        raise HTTPException(422, "Enter a valid 10-digit mobile number")
    if db.scalar(select(Volunteer).where(Volunteer.mobile == mobile)):
        raise HTTPException(409, "A volunteer with this mobile number already exists")
    v = Volunteer(name=name[:120], mobile=mobile)
    db.add(v)
    db.flush()
    audit(db, a, "volunteer.create", f"{name} {mobile}")
    db.commit()
    return {"id": v.id}


@app.put("/api/admin/volunteers/{vid}/booths")
def assign_booths(vid: int, body: BoothAssign, db: Session = Depends(get_db), a=Depends(current_admin)):
    v = db.get(Volunteer, vid)
    if not v:
        raise HTTPException(404, "Volunteer not found")
    ids = list(dict.fromkeys(body.station_ids))
    for sid in ids:
        if not db.get(PollingStation, sid):
            raise HTTPException(404, "Booth not found")
    db.execute(delete(VolunteerBooth).where(VolunteerBooth.volunteer_id == vid))
    for sid in ids:
        db.add(VolunteerBooth(volunteer_id=vid, station_id=sid))
    audit(db, a, "volunteer.assign", f"{vid}:{','.join(map(str, ids))}")
    db.commit()
    return {"ok": True, "booths": len(ids)}


@app.delete("/api/admin/volunteers/{vid}")
def delete_volunteer(vid: int, db: Session = Depends(get_db), a=Depends(current_admin)):
    v = db.get(Volunteer, vid)
    if not v:
        raise HTTPException(404, "Volunteer not found")
    db.execute(delete(VolunteerBooth).where(VolunteerBooth.volunteer_id == vid))
    audit(db, a, "volunteer.delete", v.name)
    db.delete(v)
    db.commit()
    return {"ok": True}


@app.get("/api/admin/stats")
def stats(db: Session = Depends(get_db), _=Depends(current_admin)):
    c = lambda m, *w: db.scalar(select(func.count()).select_from(m).where(*w))
    voters = _dedupe(db.scalars(select(Voter).where(
        Voter.record_status.in_(("published", "draft")))).all())
    marks = _marks_for(db, [v.id for v in voters])
    moods = {mood: 0 for mood in MOODS}
    mobiles = 0
    for voter in voters:
        mark = marks.get(voter.id)
        if not mark:
            continue
        if (mark.mobile or "").strip():
            mobiles += 1
        if mark.mood in moods:
            moods[mark.mood] += 1
    _set_not_met(moods, len(voters))
    booths = c(PollingStation)
    return {"polling_booths": booths, "polling_stations": booths, "voters": len(voters),
            "mobile_mapped": mobiles, "volunteers": c(Volunteer),
            "favour": moods["favour"], "unfavour": moods["unfavour"], "neutral": moods["neutral"],
            "undecided": moods["undecided"], "not_met": moods["not_met"],
            "divisions": c(Division), "uploads": c(Upload),
            "processed_ok": c(Upload, Upload.status.in_(["Completed", "Validation Required"])),
            "failed": c(Upload, Upload.status == "Failed"),
            "searches": c(SearchLog), "verified_mobiles": c(MobileVerification)}


@app.get("/api/admin/reports/{name}")
def report(name: str, fmt: str = "json", db: Session = Depends(get_db), a=Depends(current_admin)):
    """Aggregate counts only; no personal voter data is exported."""
    q = {"division": select(Division.name, func.count(Voter.id)).join(Voter, Voter.division_id == Division.id)
         .where(Voter.record_status == "published").group_by(Division.name),
         "part": select(Voter.part_number, func.count()).where(Voter.record_status == "published")
         .group_by(Voter.part_number),
         "uploads": select(Upload.status, func.count()).group_by(Upload.status),
         "searches": select(SearchLog.outcome, func.count()).group_by(SearchLog.outcome)}.get(name)
    if q is None:
        raise HTTPException(404, "Unknown report")
    rows = [list(r) for r in db.execute(q)]
    audit(db, a, "report.view", name)
    db.commit()
    if fmt == "csv":
        buf = io.StringIO()
        csv.writer(buf).writerows([["key", "count"], *rows])
        return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": f"attachment; filename={name}.csv"})
    return rows


def write_roll_workbook(db: Session | None = None) -> bytes:
    """One row per voter, with district, assembly, division and booth filled in."""
    from openpyxl import Workbook
    own = db is None
    if db is None:
        db = SessionLocal()
    try:
        stations = {(s.division_id, s.part_number): s for s in db.scalars(select(PollingStation))}
        divisions = {d.id: d for d in db.scalars(select(Division))}
        voters = db.scalars(select(Voter).order_by(Voter.division_id, Voter.part_number, Voter.serial_number)).all()
        mobiles = {m.voter_id: m.mobile for m in db.scalars(select(VoterMark)) if m.mobile}
        book = Workbook()
        sheet = book.active
        sheet.title = "Voters"
        headers = ["District", "Assembly no", "Assembly name", "Division", "Part no", "Booth no",
                   "Booth name", "Serial", "EPIC", "Name", "Relative name", "Relationship",
                   "House number", "Age", "Gender", "Mobile no", "Status"]
        sheet.append(headers)
        counts = {}
        for v in voters:
            div = divisions.get(v.division_id)
            booth = stations.get((v.division_id, v.part_number))
            booth_no = (booth.number if booth and booth.number else "") or v.part_number
            booth_name = booth.name if booth else ""
            sheet.append([
                div.district if div else "", div.ac_number if div else "", div.ac_name if div else "",
                div.name if div else "", v.part_number, booth_no, booth_name, v.serial_number or "",
                v.epic_number, v.voter_name, v.relative_name, v.relationship_type, v.house_number,
                v.age if v.age is not None else "", v.gender, mobiles.get(v.id, ""), v.record_status,
            ])
            key = (div.district if div else "", div.ac_name if div else "", div.name if div else "",
                   v.part_number, booth_no, booth_name)
            counts[key] = counts.get(key, 0) + 1
        summary = book.create_sheet("Booths")
        summary.append(["District", "Assembly", "Division", "Part no", "Booth no", "Booth name", "Voters"])
        for key, n in sorted(counts.items()):
            summary.append([*key, n])
        for ws in (sheet, summary):
            ws.auto_filter.ref = ws.dimensions
            ws.freeze_panes = "A2"
        buf = io.BytesIO()
        book.save(buf)
        data = buf.getvalue()
        (config.STORAGE_DIR / "electoral-roll.xlsx").write_bytes(data)
        return data
    finally:
        if own:
            db.close()


@app.get("/api/admin/export/voters.xlsx")
def export_voters(_: object = Depends(current_admin)):
    data = write_roll_workbook()
    return StreamingResponse(io.BytesIO(data),
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=electoral-roll.xlsx"})


@app.get("/api/admin/audit")
def audit_logs(db: Session = Depends(get_db), _=Depends(current_admin)):
    return [dict(at=str(r.at), user=r.username, action=r.action, detail=r.detail)
            for r in db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(200))]


# ---------------------------------------------------------------- public search
def mask_epic(e: str) -> str:
    return e[:3] + "XXXX" + e[-3:] if len(e) >= 8 else e


def card(v: Voter, db: Session, reveal: bool):
    d = db.get(Division, v.division_id)
    st = db.scalar(select(PollingStation).where(PollingStation.division_id == v.division_id,
                                                PollingStation.part_number == v.part_number))
    up = db.get(Upload, v.upload_id)
    stored = (st.number if st and st.number else "") or ""
    entered = ((up.station_number or "").strip() if up else "")
    if entered and (not stored or entered.startswith(stored)):
        station_no = entered
    else:
        station_no = stored or entered or v.part_number
    return dict(voter_name=v.voter_name, relative_name=v.relative_name, relationship_type=v.relationship_type,
                epic_number=v.epic_number if reveal else mask_epic(v.epic_number), age=v.age, gender=v.gender,
                house_number=v.house_number, address=v.house_number, serial_number=v.serial_number,
                part_number=v.part_number, booth_no=station_no, polling_station_no=station_no,
                division=d.name if d else "", assembly_constituency=(f"{d.ac_number} {d.ac_name}".strip() if d else ""),
                polling_station=st.name if st and st.name else "", polling_address=st.address if st else "",
                roll_year=up.roll_year if up else None, revision=up.revision_type if up else "",
                qualifying_date=up.qualifying_date if up else "")


def _dedupe(rows):
    best = {}
    for v in rows:
        key = (v.epic_number, v.part_number, v.serial_number, (v.voter_name or "").strip().lower())
        cur = best.get(key)
        if cur is None or (v.ocr_confidence or 0) >= (cur.ocr_confidence or 0):
            best[key] = v
    return list(best.values())


class CitizenLogin(BaseModel):
    mobile: str


def _volunteer_profile(db: Session, mobile: str):
    v = db.scalar(select(Volunteer).where(Volunteer.mobile == mobile))
    if not v:
        return None
    divs = {d.id: d.name for d in db.scalars(select(Division))}
    booths = []
    for sid in db.scalars(select(VolunteerBooth.station_id).where(VolunteerBooth.volunteer_id == v.id)):
        st = db.get(PollingStation, sid)
        if st:
            booths.append({"id": st.id, "part_number": st.part_number, "number": st.number or st.part_number,
                           "name": st.name, "division": divs.get(st.division_id, "")})
    return {"name": v.name, "mobile": otp.mask(v.mobile), "booths": booths,
            "summary": _volunteer_summary(db, booths)}


@app.post("/api/volunteer/login")
def volunteer_login(body: CitizenLogin, db: Session = Depends(get_db)):
    mobile = otp.normalize(body.mobile)
    if not mobile:
        raise HTTPException(422, "Enter a valid 10-digit mobile number")
    profile = _volunteer_profile(db, mobile)
    if not profile:
        raise HTTPException(401, "This mobile number is not registered")
    return {"token": make_token(mobile, "volunteer"), **profile}


@app.get("/api/volunteer/me")
def volunteer_me(db: Session = Depends(get_db), who=Depends(current_volunteer)):
    profile = _volunteer_profile(db, who["sub"])
    if not profile:
        raise HTTPException(401, "This mobile number is not registered")
    return profile


MOODS = ("favour", "unfavour", "neutral", "undecided", "not_met")


def _assigned_booth(db: Session, who: dict, sid: int) -> tuple[Volunteer, PollingStation]:
    vol = db.scalar(select(Volunteer).where(Volunteer.mobile == who["sub"]))
    if not vol:
        raise HTTPException(401, "This mobile number is not registered")
    st = db.get(PollingStation, sid)
    assigned = db.scalar(select(VolunteerBooth).where(
        VolunteerBooth.volunteer_id == vol.id, VolunteerBooth.station_id == sid))
    if not st or not assigned:
        raise HTTPException(403, "This polling station is not assigned to you")
    return vol, st


def _booth_rows(db: Session, st: PollingStation):
    rows = _dedupe(db.scalars(select(Voter).where(
        Voter.division_id == st.division_id, Voter.part_number == st.part_number,
        Voter.record_status.in_(("published", "draft")))).all())
    rows.sort(key=lambda v: (v.serial_number or 0, v.voter_name or ""))
    return rows


def _marks_for(db: Session, voter_ids):
    if not voter_ids:
        return {}
    return {m.voter_id: m for m in db.scalars(select(VoterMark).where(VoterMark.voter_id.in_(voter_ids)))}


def _volunteer_voter(db: Session, voter: Voter, mark: VoterMark | None):
    row = card(voter, db, True)
    row["id"] = voter.id
    row["mobile"] = mark.mobile if mark else ""
    row["mood"] = mark.mood if mark else ""
    return row


def _mood_report(db: Session, voters):
    marks = _marks_for(db, [v.id for v in voters])
    counts = {mood: 0 for mood in MOODS}
    report = []
    for voter in voters:
        mark = marks.get(voter.id)
        if not mark or not (mark.mood or mark.mobile):
            continue
        if mark.mood in counts:
            counts[mark.mood] += 1
        report.append({"id": voter.id, "epic_number": voter.epic_number, "voter_name": voter.voter_name,
                       "address": voter.house_number, "mobile": mark.mobile, "mood": mark.mood,
                       "updated_by": mark.updated_by, "updated_at": str(mark.updated_at)})
    _set_not_met(counts, len(voters))
    return counts, report


def _set_not_met(counts, total):
    """Not met starts as every voter, then drops by one for each other mood."""
    marked = sum(counts[mood] for mood in MOODS if mood != "not_met")
    counts["not_met"] = max(total - marked, 0)


def _volunteer_summary(db: Session, booths):
    """Counts for the booths assigned to one volunteer."""
    seen = set()
    voters = []
    for booth in booths:
        st = db.get(PollingStation, booth["id"])
        if not st:
            continue
        for voter in _booth_rows(db, st):
            if voter.id in seen:
                continue
            seen.add(voter.id)
            voters.append(voter)
    marks = _marks_for(db, [v.id for v in voters])
    moods = {mood: 0 for mood in MOODS}
    mobiles = 0
    for voter in voters:
        mark = marks.get(voter.id)
        if not mark:
            continue
        if (mark.mobile or "").strip():
            mobiles += 1
        if mark.mood in moods:
            moods[mark.mood] += 1
    _set_not_met(moods, len(voters))
    return {"booths": len(booths), "voters": len(voters), "mobile_mapped": mobiles,
            "favour": moods["favour"], "unfavour": moods["unfavour"], "neutral": moods["neutral"],
            "undecided": moods["undecided"], "not_met": moods["not_met"]}


@app.get("/api/volunteer/booths/{sid}/voters")
def volunteer_booth_voters(sid: int, page: int = 1, q: str = "", db: Session = Depends(get_db),
                           who=Depends(current_volunteer)):
    _, st = _assigned_booth(db, who, sid)
    rows = _booth_rows(db, st)
    counts, report = _mood_report(db, rows)
    needle = q.strip()
    if needle:
        folded = needle.lower()
        epic = needle.upper().replace(" ", "")
        rows = [v for v in rows if epic in (v.epic_number or "").upper()
                or folded in (v.voter_name or "").lower()
                or folded in (v.house_number or "").lower()]
    page = max(page, 1)
    chunk = rows[(page - 1) * 10: page * 10]
    marks = _marks_for(db, [v.id for v in chunk])
    return {"total": len(rows), "page": page,
            "station": {"id": st.id, "number": st.number or st.part_number, "name": st.name},
            "counts": counts, "report": report,
            "voters": [_volunteer_voter(db, v, marks.get(v.id)) for v in chunk]}


class VoterMarkIn(BaseModel):
    mobile: str = ""
    mood: str = ""


@app.put("/api/volunteer/booths/{sid}/voters/{voter_id}")
def volunteer_mark_voter(sid: int, voter_id: str, body: VoterMarkIn, db: Session = Depends(get_db),
                         who=Depends(current_volunteer)):
    vol, st = _assigned_booth(db, who, sid)
    voter = db.get(Voter, voter_id)
    if not voter or voter.division_id != st.division_id or voter.part_number != st.part_number:
        raise HTTPException(404, "Voter not found on this polling station")
    mood = body.mood.strip().lower()
    if mood not in ("", *MOODS):
        raise HTTPException(400, "Choose favour, unfavour, neutral, undecided or not met")
    raw_mobile = body.mobile.strip()
    mobile = _roll_mobile(raw_mobile) if raw_mobile else ""
    if raw_mobile and not mobile:
        raise HTTPException(422, "Mobile must be 8 to 13 digits. The mood was not saved.")
    mark = db.scalar(select(VoterMark).where(VoterMark.voter_id == voter.id))
    if not mobile and not mood:
        if mark:
            db.delete(mark)
        db.commit()
        return {"ok": True, "mobile": "", "mood": ""}
    if not mark:
        mark = VoterMark(voter_id=voter.id)
        db.add(mark)
    mark.mobile = mobile
    mark.mood = mood
    mark.updated_by = vol.name
    mark.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "mobile": mark.mobile, "mood": mark.mood}


@app.get("/api/admin/voter-mood")
def admin_voter_mood(db: Session = Depends(get_db), _=Depends(current_admin)):
    marks = db.scalars(select(VoterMark).order_by(VoterMark.updated_at.desc())).all()
    counts = {mood: 0 for mood in MOODS}
    rows = []
    divisions = {d.id: d.name for d in db.scalars(select(Division))}
    for mark in marks:
        if not (mark.mood or mark.mobile):
            continue
        voter = db.get(Voter, mark.voter_id)
        if not voter:
            continue
        if mark.mood in counts:
            counts[mark.mood] += 1
        station = db.scalar(select(PollingStation).where(
            PollingStation.division_id == voter.division_id, PollingStation.part_number == voter.part_number))
        rows.append({"epic_number": voter.epic_number, "voter_name": voter.voter_name,
                     "relative_name": voter.relative_name, "address": voter.house_number,
                     "division": divisions.get(voter.division_id, ""),
                     "booth": (station.number if station and station.number else voter.part_number),
                     "booth_name": station.name if station else "",
                     "mobile": mark.mobile, "mood": mark.mood,
                     "updated_by": mark.updated_by, "updated_at": str(mark.updated_at)})
    total = len(_dedupe(db.scalars(select(Voter).where(
        Voter.record_status.in_(("published", "draft")))).all()))
    _set_not_met(counts, total)
    return {"counts": counts, "rows": rows}


@app.get("/api/public/divisions")
def public_divisions(db: Session = Depends(get_db)):
    return [dict(id=d.id, name=d.name, ac=f"{d.ac_number} {d.ac_name}".strip())
            for d in db.scalars(select(Division).where(Division.status == "active"))]


@app.get("/api/public/search")
def search(request: Request, type: str = "name", epic: str = "", name: str = "", division_id: int | None = None,
           relative: str = "", house: str = "", age: int | None = None, page: int = 1,
           session_id: str = "", db: Session = Depends(get_db)):
    base = select(Voter).where(Voter.record_status.in_(("published", "draft")))
    if type == "epic":
        e = epic.strip().upper()
        if not EPIC_FORMAT.match(e):
            raise HTTPException(422, "EPIC must be 3 letters followed by 7 digits, e.g. ABC1234567")
        rows = _dedupe(db.scalars(base.where(Voter.epic_number == e)).all())
        total = len(rows)
    else:
        n = name.strip()
        if len(n) < 3:
            raise HTTPException(422, "Enter at least 3 letters of the name")
        if division_id:
            base = base.where(Voter.division_id == division_id)
        if relative.strip():
            base = base.where(Voter.relative_name.ilike(f"%{relative.strip()}%"))
        if house.strip():
            base = base.where(Voter.house_number == house.strip())
        if age:
            base = base.where(Voter.age.between(age - 2, age + 2))
        first = n.split()[0]
        cands = _dedupe(db.scalars(base.where(or_(Voter.voter_name.ilike(f"%{n}%"), Voter.voter_name.ilike(f"%{first[:3]}%")))
                           .limit(3000)).all())
        scored = sorted(((fuzz.token_set_ratio(n.lower(), v.voter_name.lower()), v) for v in cands),
                        key=lambda t: -t[0])
        hits = [v for s, v in scored if s >= 80]
        total = len(hits)
        rows = hits[(page - 1) * 10: page * 10]
    outcome = "not_found" if total == 0 else ("found" if total == 1 else "multiple")
    db.add(SearchLog(search_type=type, outcome=outcome, session_id=session_id[:40]))
    db.commit()
    status = {"found": "Record Found", "multiple": "Multiple Matching Records Found",
              "not_found": "No Matching Record Found"}[outcome]
    return dict(status=status, total=total, page=page, results=[card(v, db, True) for v in rows],
                disclaimer="Results reflect the uploaded electoral roll and its revision date. They do not "
                           "independently confirm current voter eligibility.", official_url=config.ECI_URL)


# ---------------------------------------------------------------- mobile verification
class OtpReq(BaseModel):
    session_id: str
    mobile: str
    consent: bool


class OtpCheck(BaseModel):
    session_id: str
    otp: str


@app.get("/api/public/consent-text")
def consent_text():
    return {"text": "Your mobile number is used only to send a one-time code to confirm you are a real person "
                    "using this service. It is not linked to any voter record, is not shared, and is deleted "
                    f"after {config.RETENTION_DAYS} days. Providing it is optional."}


@app.post("/api/public/otp/send")
def otp_send(body: OtpReq, request: Request, db: Session = Depends(get_db)):
    if not body.consent:
        raise HTTPException(400, "Consent is required")
    m = otp.normalize(body.mobile)
    if not m:
        raise HTTPException(422, "Enter a valid 10-digit Indian mobile number")
    if otp.rate_limited(request.client.host if request.client else "?"):
        raise HTTPException(429, "Too many requests, try later")
    try:
        otp.start(body.session_id, m)
    except ValueError as e:
        raise HTTPException(429, str(e))
    db.add(Consent(session_id=body.session_id[:40], purpose="mobile_otp"))
    db.commit()
    return {"sent": True, "expires_in": 300}


@app.post("/api/public/otp/verify")
def otp_verify(body: OtpCheck, db: Session = Depends(get_db)):
    try:
        m = otp.check(body.session_id, body.otp)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not m:
        raise HTTPException(400, "Incorrect OTP")
    db.add(MobileVerification(session_id=body.session_id[:40], mobile_masked=otp.mask(m), mobile_enc=encrypt(m)))
    db.commit()
    return {"verified": True, "mobile": otp.mask(m)}


# ---------------------------------------------------------------- mobile clients
class MobileClientIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    code: str = Field(min_length=4, max_length=40)
    division_id: int | None = None


def _client_by_code(db: Session, code: str) -> MobileClient:
    row = db.scalar(select(MobileClient).where(func.lower(MobileClient.code) == code.strip().lower()))
    if not row or not row.active:
        raise HTTPException(404, "This client code is not active")
    return row


@app.get("/api/admin/mobile-clients")
def list_mobile_clients(db: Session = Depends(get_db), _=Depends(current_admin)):
    divisions = {d.id: d.name for d in db.scalars(select(Division))}
    return [{"id": c.id, "name": c.name, "code": c.code, "division_id": c.division_id,
             "division": divisions.get(c.division_id, "All divisions"), "active": c.active}
            for c in db.scalars(select(MobileClient).order_by(MobileClient.name))]


@app.post("/api/admin/mobile-clients")
def create_mobile_client(body: MobileClientIn, db: Session = Depends(get_db), a=Depends(current_admin)):
    code = re.sub(r"\s+", "", body.code).upper()
    if db.scalar(select(MobileClient).where(func.lower(MobileClient.code) == code.lower())):
        raise HTTPException(409, "This client code already exists")
    if body.division_id and not db.get(Division, body.division_id):
        raise HTTPException(404, "Division not found")
    row = MobileClient(name=body.name.strip(), code=code, division_id=body.division_id)
    db.add(row)
    audit(db, a, "mobile.client", f"{row.name} {code}")
    db.commit()
    return {"id": row.id, "code": row.code}


@app.delete("/api/admin/mobile-clients/{cid}")
def delete_mobile_client(cid: int, db: Session = Depends(get_db), a=Depends(current_admin)):
    row = db.get(MobileClient, cid)
    if not row:
        raise HTTPException(404, "Not found")
    audit(db, a, "mobile.client.delete", row.code)
    db.delete(row)
    db.commit()
    return {"ok": True}


def _sync_pack(db: Session, client: MobileClient):
    voter_q = select(Voter).where(Voter.record_status.in_(("published", "draft")))
    station_q = select(PollingStation)
    if client.division_id:
        voter_q = voter_q.where(Voter.division_id == client.division_id)
        station_q = station_q.where(PollingStation.division_id == client.division_id)
    voters = _dedupe(db.scalars(voter_q).all())
    stations = list(db.scalars(station_q))
    station_by_part = {(s.division_id, s.part_number): s for s in stations}
    station_ids = {s.id for s in stations}
    marks = _marks_for(db, [v.id for v in voters])
    divisions = {d.id: d for d in db.scalars(select(Division))}
    people = []
    for voter in voters:
        station = station_by_part.get((voter.division_id, voter.part_number))
        mark = marks.get(voter.id)
        div = divisions.get(voter.division_id)
        people.append({
            "id": voter.id, "epic_number": voter.epic_number, "voter_name": voter.voter_name,
            "relative_name": voter.relative_name, "age": voter.age, "gender": voter.gender,
            "address": voter.house_number, "house_number": voter.house_number,
            "serial_number": voter.serial_number, "part_number": voter.part_number,
            "division_id": voter.division_id, "division": div.name if div else "",
            "station_id": station.id if station else None,
            "booth_no": (station.number if station and station.number else voter.part_number),
            "polling_station": station.name if station else "",
            "mobile": mark.mobile if mark else "", "mood": mark.mood if mark else "",
        })
    volunteers = []
    for volunteer in db.scalars(select(Volunteer).order_by(Volunteer.name)):
        booths = []
        for sid in db.scalars(select(VolunteerBooth.station_id).where(VolunteerBooth.volunteer_id == volunteer.id)):
            if sid not in station_ids:
                continue
            station = db.get(PollingStation, sid)
            if station:
                booths.append({"id": station.id, "part_number": station.part_number,
                               "number": station.number or station.part_number, "name": station.name,
                               "division": divisions.get(station.division_id).name if divisions.get(station.division_id) else ""})
        if booths:
            volunteers.append({"name": volunteer.name, "mobile": volunteer.mobile, "booths": booths})
    div = divisions.get(client.division_id) if client.division_id else None
    return {
        "client": {"name": client.name, "code": client.code,
                   "division_id": client.division_id, "division": div.name if div else "All divisions"},
        "updated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "divisions": [{"id": d.id, "name": d.name} for d in divisions.values()
                      if not client.division_id or d.id == client.division_id],
        "voters": people,
        "volunteers": volunteers,
    }


@app.get("/api/mobile/sync")
def mobile_sync(code: str, db: Session = Depends(get_db)):
    return _sync_pack(db, _client_by_code(db, code))


class MobileMarkIn(BaseModel):
    code: str
    volunteer_mobile: str
    voter_id: str
    mobile: str = ""
    mood: str = ""


@app.post("/api/mobile/marks")
def mobile_push_mark(body: MobileMarkIn, db: Session = Depends(get_db)):
    client = _client_by_code(db, body.code)
    volunteer = db.scalar(select(Volunteer).where(Volunteer.mobile == (otp.normalize(body.volunteer_mobile) or body.volunteer_mobile)))
    if not volunteer:
        raise HTTPException(401, "This mobile number is not registered")
    voter = db.get(Voter, body.voter_id)
    if not voter:
        raise HTTPException(404, "Voter not found")
    if client.division_id and voter.division_id != client.division_id:
        raise HTTPException(403, "This voter is outside the configured division")
    station = db.scalar(select(PollingStation).where(
        PollingStation.division_id == voter.division_id, PollingStation.part_number == voter.part_number))
    assigned = station and db.scalar(select(VolunteerBooth).where(
        VolunteerBooth.volunteer_id == volunteer.id, VolunteerBooth.station_id == station.id))
    if not assigned:
        raise HTTPException(403, "This polling station is not assigned to you")
    mood = body.mood.strip().lower()
    if mood not in ("", *MOODS):
        raise HTTPException(400, "Choose favour, unfavour, neutral, undecided or not met")
    mobile = _roll_mobile(body.mobile) if body.mobile.strip() else ""
    mark = db.scalar(select(VoterMark).where(VoterMark.voter_id == voter.id))
    if not mobile and not mood:
        if mark:
            db.delete(mark)
        db.commit()
        return {"ok": True}
    if not mark:
        mark = VoterMark(voter_id=voter.id)
        db.add(mark)
    mark.mobile = mobile
    mark.mood = mood
    mark.updated_by = volunteer.name
    mark.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "mobile": mobile, "mood": mood}


# ---------------------------------------------------------------- frontend
STATIC = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/m")
def mobile_app():
    """Citizen search and volunteer access for the Android app. Admin stays on /."""
    return FileResponse(STATIC / "mobile.html", headers={"Cache-Control": "no-store"})
