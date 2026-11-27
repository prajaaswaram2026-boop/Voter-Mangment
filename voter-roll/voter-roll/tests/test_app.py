import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/t.db"
os.environ["STORAGE_DIR"] = _tmp
os.environ["ADMIN_PASSWORD"] = "TestPass!1"

from fastapi.testclient import TestClient  # noqa: E402

from app import otp  # noqa: E402
from app.extract import parse_cards  # noqa: E402
from app.main import app  # noqa: E402

SAMPLE = """1 ABC1234567
Name : RAMESH KUMAR
Father's Name : SURESH KUMAR
House Number : 12-3
Age : 34 Gender : Male
2 XYZ7654321
Name : LAKSHMI DEVI
Husband's Name : RAMESH KUMAR
House Number : 12-3
Age : 30 Gender : Female
3
Name : UNKNOWN PERSON
Age : 50 Gender : Male
"""


def test_parse_cards():
    r = parse_cards(SAMPLE)
    assert len(r) == 3
    assert r[0]["epic_number"] == "ABC1234567" and r[0]["relative_name"] == "SURESH KUMAR"
    assert r[1]["relationship_type"] == "Husband" and r[1]["age"] == 30
    assert r[2]["epic_number"] == "" and r[2]["confidence"] < 0.8
    scanned = """1 WPK4431136
Name Haidar Ali Shaik
Fathers Name: Ali Subhahan Shaik
House Number : #204, A Block
Age : 25 Gender : Male
7 WPK4362612
Name : Muralidhar Kolusu Kolusu
Fathers Name: Janardhana Rao Kolusu
Kolusu
House Number : 1-13/7/B
Age : 49 Gender : Maje
"""
    s = parse_cards(scanned)
    assert s[0]["voter_name"] == "Haidar Ali Shaik" and s[0]["epic_number"] == "WPK4431136"
    misread = parse_cards("1 WPR4A429429\nMame = Jhansi Karry\nHusbands Name: Seshagiri Rao\n")
    assert misread[0]["voter_name"] == "Jhansi Karry"
    assert misread[0]["epic_number"] == "WPR4442942"
    assert s[1]["relative_name"] == "Janardhana Rao Kolusu Kolusu" and s[1]["gender"] == "Male"


def test_mobile_validation():
    assert otp.normalize("+91 98765 43210") == "9876543210"
    assert otp.normalize("1234567890") is None
    assert otp.mask("9876543210") == "XXXXXX3210"


def test_end_to_end():
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "bad"}).status_code == 401
        tok = c.post("/api/auth/login", json={"username": "admin", "password": "TestPass!1"}).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        assert c.get("/api/admin/stats").status_code == 401
        d = c.post("/api/admin/divisions", json={"name": "Manikonda", "ac_number": "1", "ac_name": "Test"}, headers=h).json()

        # non-PDF rejected
        r = c.post("/api/admin/uploads", data={"division_id": d["id"], "roll_year": 2026},
                   files={"files": ("x.pdf", b"hello", "application/pdf")}, headers=h)
        assert r.status_code == 400

        # publish voters directly through the DB to test search/masking
        from app.db import SessionLocal
        from app.models import Upload, Voter
        with SessionLocal() as db:
            up = Upload(division_id=d["id"], filename="a.pdf", path="x", roll_year=2026, part_number="5")
            db.add(up)
            db.flush()
            db.add(Voter(division_id=d["id"], upload_id=up.id, epic_number="ABC1234567", serial_number=1,
                         voter_name="RAMESH KUMAR", relative_name="SURESH KUMAR", age=34, gender="Male",
                         part_number="5", record_status="published", ocr_confidence=1.0))
            db.commit()

        r = c.get("/api/public/search", params={"type": "epic", "epic": "abc1234567"}).json()
        assert r["status"] == "Record Found" and r["results"][0]["epic_number"] == "ABC1234567"
        assert r["results"][0]["booth_no"] == "5" and r["results"][0]["address"] == ""
        assert c.get("/api/public/search", params={"type": "epic", "epic": "bad"}).status_code == 422
        r = c.get("/api/public/search", params={"type": "name", "name": "Ramesh Kumr"}).json()  # typo -> fuzzy
        assert r["total"] == 1
        r = c.get("/api/public/search", params={"type": "name", "name": "Nobody Here"}).json()
        assert r["status"] == "No Matching Record Found"

        # division with data cannot be deleted
        assert c.delete(f"/api/admin/divisions/{d['id']}", headers=h).status_code == 409
        assert c.get("/api/admin/stats", headers=h).json()["searches"] == 3

        # OTP flow
        sid = "s" * 20
        assert c.post("/api/public/otp/send", json={"session_id": sid, "mobile": "9876543210", "consent": False}).status_code == 400
        assert c.post("/api/public/otp/send", json={"session_id": sid, "mobile": "9876543210", "consent": True}).status_code == 200
        for _ in range(3):
            assert c.post("/api/public/otp/verify", json={"session_id": sid, "otp": "000000"}).status_code == 400
        assert c.post("/api/public/otp/verify", json={"session_id": sid, "otp": "000000"}).json()["detail"] == "Too many attempts"


def test_review_only_missing_epic_or_name_and_delete():
    with TestClient(app) as c:
        tok = c.post("/api/auth/login", json={"username": "admin", "password": "TestPass!1"}).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        d = c.post("/api/admin/divisions", json={"name": "Review Area", "ac_number": "2", "ac_name": "Test"}, headers=h).json()
        missing = c.post("/api/admin/uploads", data={"division_id": d["id"], "roll_year": 2026, "station_name": "Hall"},
                         files={"files": ("x.pdf", b"%PDF-1.4", "application/pdf")}, headers=h)
        assert missing.status_code == 400
        bad = c.post("/api/admin/uploads", data={"division_id": d["id"], "roll_year": 2026,
                     "station_name": "Hall", "station_number": "12"},
                     files={"files": ("x.pdf", b"hello", "application/pdf")}, headers=h)
        assert bad.status_code == 400 and "not a PDF" in bad.json()["detail"]

        from app.db import SessionLocal
        from app.models import Upload, Voter
        with SessionLocal() as db:
            up = Upload(division_id=d["id"], filename="roll.pdf", path="missing.pdf", roll_year=2026,
                        part_number="70", station_name="Manikonda Jagir", station_number="70")
            db.add(up)
            db.flush()
            db.add(Voter(division_id=d["id"], upload_id=up.id, epic_number="", serial_number=1,
                         voter_name="NO EPIC", part_number="70"))
            db.add(Voter(division_id=d["id"], upload_id=up.id, epic_number="WPK1111111", serial_number=2,
                         voter_name="", part_number="70"))
            db.add(Voter(division_id=d["id"], upload_id=up.id, epic_number="WPK2222222", serial_number=3,
                         voter_name="COMPLETE NAME", part_number="70"))
            db.commit()
            uid = up.id
        pack = c.get(f"/api/admin/uploads/{uid}/records", params={"only_review": "true"}, headers=h).json()
        assert pack["total"] == 2
        shown = {(row["epic_number"], row["voter_name"]) for row in pack["records"]}
        assert ("", "NO EPIC") in shown and ("WPK1111111", "") in shown
        assert c.post(f"/api/admin/uploads/{uid}/publish", headers=h).status_code == 409
        assert c.delete(f"/api/admin/uploads/{uid}", headers=h).status_code == 200
        assert all(u["id"] != uid for u in c.get("/api/admin/uploads", headers=h).json())


def test_volunteer_booth_assignment():
    with TestClient(app) as c:
        tok = c.post("/api/auth/login", json={"username": "admin", "password": "TestPass!1"}).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        d = c.post("/api/admin/divisions", json={"name": "Volunteer Area", "ac_number": "9", "ac_name": "Test"}, headers=h).json()
        st = c.put(f"/api/admin/divisions/{d['id']}/stations",
                   json={"part_number": "12", "number": "12", "name": "School Booth"}, headers=h).json()
        bad = c.post("/api/admin/volunteers", json={"name": "Ravi", "mobile": "12345"}, headers=h)
        assert bad.status_code == 422
        vid = c.post("/api/admin/volunteers", json={"name": "Ravi", "mobile": "9876501234"}, headers=h).json()["id"]
        assert c.put(f"/api/admin/volunteers/{vid}/booths", json={"station_ids": [st["id"]]}, headers=h).status_code == 200
        pack = c.get("/api/admin/volunteers", headers=h).json()
        row = next(v for v in pack["volunteers"] if v["id"] == vid)
        assert row["booths"][0]["name"] == "School Booth" and row["booths"][0]["part_number"] == "12"
        assert c.post("/api/volunteer/login", json={"mobile": "9000000000"}).status_code == 401
        logged = c.post("/api/volunteer/login", json={"mobile": "9876501234"}).json()
        assert logged["name"] == "Ravi" and logged["booths"][0]["part_number"] == "12"
        vh = {"Authorization": "Bearer " + logged["token"]}
        assert c.get("/api/admin/stats", headers=vh).status_code == 401
        assert c.get("/api/volunteer/me", headers=vh).json()["mobile"].endswith("1234")
        from app.db import SessionLocal
        from app.models import Upload, Voter
        with SessionLocal() as db:
            up = Upload(division_id=d["id"], filename="booth.pdf", path="b", roll_year=2026, part_number="12")
            db.add(up)
            db.flush()
            db.add(Voter(division_id=d["id"], upload_id=up.id, epic_number="WPK0000012", serial_number=4,
                         voter_name="BOOTH VOTER", relative_name="BOOTH RELATIVE", house_number="12-A",
                         part_number="12", record_status="published"))
            db.add(Voter(division_id=d["id"], upload_id=up.id, epic_number="WPK0000099", serial_number=5,
                         voter_name="OTHER PART", part_number="99", record_status="published"))
            db.commit()
        other = c.put(f"/api/admin/divisions/{d['id']}/stations",
                      json={"part_number": "99", "number": "99", "name": "Other"}, headers=h).json()
        roll = c.get(f"/api/volunteer/booths/{st['id']}/voters", headers=vh).json()
        assert roll["total"] == 1
        voter = roll["voters"][0]
        assert voter["epic_number"] == "WPK0000012" and voter["voter_name"] == "BOOTH VOTER"
        assert voter["relative_name"] == "BOOTH RELATIVE" and voter["address"] == "12-A"
        found = c.get(f"/api/volunteer/booths/{st['id']}/voters?q=12-A", headers=vh).json()
        assert found["total"] == 1 and found["voters"][0]["id"] == voter["id"]
        saved = c.put(f"/api/volunteer/booths/{st['id']}/voters/{voter['id']}",
                      json={"mobile": "9876511111", "mood": "favour"}, headers=vh)
        assert saved.status_code == 200
        report = c.get("/api/admin/voter-mood", headers=h).json()
        assert report["counts"]["favour"] == 1 and report["rows"][0]["mobile"] == "9876511111"
        summary = c.get("/api/admin/stats", headers=h).json()
        assert summary["favour"] == 1 and summary["mobile_mapped"] >= 1
        assert summary["not_met"] == summary["voters"] - summary["favour"] - summary["unfavour"] - summary["neutral"] - summary["undecided"]
        assert c.get("/api/admin/voter-mood", headers=vh).status_code == 401
        again = c.get(f"/api/volunteer/booths/{st['id']}/voters", headers=vh).json()
        assert again["report"][0]["mood"] == "favour"
        assert again["counts"]["not_met"] == again["total"] - 1
        assert c.get(f"/api/volunteer/booths/{other['id']}/voters", headers=vh).status_code == 403
        assert c.delete(f"/api/admin/volunteers/{vid}", headers=h).status_code == 200
        assert all(v["id"] != vid for v in c.get("/api/admin/volunteers", headers=h).json()["volunteers"])


def _write_text_pdf(path, pages):
    """Minimal text PDF. pages is a list of line lists."""
    objects = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    content_ids = []
    for lines in pages:
        commands = ["BT /F1 11 Tf"]
        y = 760
        for line in lines:
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"1 0 0 1 48 {y} Tm ({safe}) Tj")
            y -= 16
        commands.append("ET")
        stream = "\n".join(commands).encode()
        content_ids.append(add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"))
        page_ids.append(add(b""))  # filled after the page tree id is known
    pages_id = add(b"")
    catalog_id = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[pages_id - 1] = f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>".encode()
    for pid, cid in zip(page_ids, content_ids):
        objects[pid - 1] = (
            f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
            f"/Contents {cid} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>"
        ).encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects)+1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer << /Size {len(objects)+1} /Root {catalog_id} 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    with open(path, "wb") as handle:
        handle.write(out)


def test_text_pdf_skips_ocr_and_resume_does_not_duplicate(tmp_path, monkeypatch):
    from app.db import SessionLocal
    from app.extract import extract_pdf
    from app.main import process_upload
    from app.models import Division, ExtractionLog, Upload, Voter

    pdf = tmp_path / "roll.pdf"
    _write_text_pdf(pdf, [
        ["1 ABC1234567", "Name : RAMESH KUMAR", "Father's Name : SURESH KUMAR",
         "House Number : 12-3", "Age : 34 Gender : Male",
         "2 XYZ7654321", "Name : LAKSHMI DEVI", "Husband's Name : RAMESH KUMAR",
         "House Number : 12-3", "Age : 30 Gender : Female"],
        ["3 DEF1234567", "Name : THIRD VOTER", "Father's Name : PARENT",
         "House Number : 9", "Age : 41 Gender : Male"],
    ])
    calls = {"ocr": 0}

    def boom(page):
        calls["ocr"] += 1
        raise AssertionError("text page must not be OCR'd")

    monkeypatch.setattr("app.extract._extract_scanned_page", boom)
    seen = []
    _meta, recs, pages = extract_pdf(str(pdf), lambda *args: seen.append(args))
    assert calls["ocr"] == 0
    assert pages == 2 and len(recs) == 3
    assert {r["epic_number"] for r in recs} == {"ABC1234567", "XYZ7654321", "DEF1234567"}
    assert all(row[1] == "text" for row in seen)
    assert recs[0]["voter_name"] == "RAMESH KUMAR" and recs[0]["age"] == 34

    from sqlalchemy import select
    with SessionLocal() as db:
        division = Division(name="Resume Division")
        db.add(division)
        db.flush()
        up = Upload(division_id=division.id, filename="roll.pdf", path=str(pdf),
                    roll_year=2026, part_number="7", status="Queued")
        db.add(up)
        db.commit()
        upload_id = up.id
    process_upload(upload_id)
    with SessionLocal() as db:
        first = db.scalars(select(Voter).where(Voter.upload_id == upload_id)).all()
        assert len(first) == 3
        assert all(v.record_status == "draft" for v in first)
        page2 = [v for v in first if v.pdf_page_number == 2]
        assert len(page2) == 1 and page2[0].epic_number == "DEF1234567"
        db.delete(page2[0])
        for row in db.scalars(select(ExtractionLog).where(
                ExtractionLog.upload_id == upload_id, ExtractionLog.page == 2)):
            db.delete(row)
        db.commit()
    process_upload(upload_id)
    with SessionLocal() as db:
        again = db.scalars(select(Voter).where(Voter.upload_id == upload_id)).all()
        assert sorted(v.epic_number for v in again) == ["ABC1234567", "DEF1234567", "XYZ7654321"]
        logs = db.scalars(select(ExtractionLog).where(ExtractionLog.upload_id == upload_id)).all()
        assert {row.page for row in logs if row.method == "text"} == {1, 2}


def test_excel_upload_stores_area_and_voters():
    from io import BytesIO
    from openpyxl import Workbook
    from app.db import SessionLocal
    from app.models import Division, PollingStation, Voter
    book = Workbook()
    sheet = book.active
    sheet.append(["Serial No", "EPIC No", "Name", "Relative Name", "Relation", "House No", "Age", "Gender", "Mobile No"])
    sheet.append([1, "WPK1234567", "Ramesh Kumar", "Suresh Kumar", "Father", "12-3", 34, "Male", 9876543210])
    sheet.append([2, "", "No Epic", "Parent", "Mother", "9", 41, "Female", ""])
    buf = BytesIO()
    book.save(buf)
    with TestClient(app) as c:
        tok = c.post("/api/auth/login", json={"username": "admin", "password": "TestPass!1"}).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        dist = c.post("/api/admin/districts", json={"name": "Ranga Reddy"}, headers=h).json()
        asm = c.post("/api/admin/assemblies", json={"district_id": dist["id"], "number": "51", "name": "Rajendra Nagar"}, headers=h).json()
        div = c.post("/api/admin/divisions", json={"name": "Manikonda Ward", "assembly_id": asm["id"]}, headers=h).json()
        r = c.post("/api/admin/uploads/excel", data={
            "division_id": div["id"], "station_number": "70",
            "station_name": "Chaitanya School", "station_address": "Manikonda Jagir", "roll_year": 2026,
        }, files={"file": ("roll.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["records"] == 2
        with SessionLocal() as db:
            div = db.scalar(__import__("sqlalchemy").select(Division).where(Division.name == "Manikonda Ward"))
            assert div.district == "Ranga Reddy" and div.ac_number == "51" and div.ac_name == "Rajendra Nagar"
            station = db.scalar(__import__("sqlalchemy").select(PollingStation).where(PollingStation.division_id == div.id))
            assert station.number == "70" and station.name == "Chaitanya School" and station.address == "Manikonda Jagir"
            voters = db.scalars(__import__("sqlalchemy").select(Voter).where(Voter.division_id == div.id)).all()
            full = next(v for v in voters if v.epic_number == "WPK1234567")
            assert full.voter_name == "Ramesh Kumar" and full.relative_name == "Suresh Kumar"
            assert full.relationship_type == "Father" and full.house_number == "12-3" and full.age == 34
            from app.models import VoterMark
            mark = db.scalar(__import__("sqlalchemy").select(VoterMark).where(VoterMark.voter_id == full.id))
            assert mark is not None and mark.mobile == "9876543210"
            assert any(not v.epic_number for v in voters)


def test_mobile_client_sync_and_offline_mark():
    with TestClient(app) as c:
        tok = c.post("/api/auth/login", json={"username": "admin", "password": "TestPass!1"}).json()["token"]
        h = {"Authorization": f"Bearer {tok}"}
        d = c.post("/api/admin/divisions", json={"name": "Client Area"}, headers=h).json()
        st = c.put(f"/api/admin/divisions/{d['id']}/stations",
                   json={"part_number": "8", "number": "8", "name": "Client Booth"}, headers=h).json()
        vid = c.post("/api/admin/volunteers", json={"name": "Sita", "mobile": "9876502222"}, headers=h).json()["id"]
        c.put(f"/api/admin/volunteers/{vid}/booths", json={"station_ids": [st["id"]]}, headers=h)
        from app.db import SessionLocal
        from app.models import Upload, Voter, VoterMark
        with SessionLocal() as db:
            up = Upload(division_id=d["id"], filename="c.xlsx", path="c", roll_year=2026, part_number="8")
            db.add(up)
            db.flush()
            voter = Voter(division_id=d["id"], upload_id=up.id, epic_number="WPK0000088", serial_number=1,
                          voter_name="CLIENT VOTER", house_number="8-1", part_number="8", record_status="published")
            db.add(voter)
            db.commit()
            voter_id = voter.id
        made = c.post("/api/admin/mobile-clients", json={"name": "Booth phone", "code": "booth8", "division_id": d["id"]}, headers=h)
        assert made.status_code == 200 and made.json()["code"] == "BOOTH8"
        assert c.get("/api/mobile/sync?code=missing").status_code == 404
        pack = c.get("/api/mobile/sync?code=booth8").json()
        assert pack["client"]["division"] == "Client Area"
        assert pack["voters"][0]["epic_number"] == "WPK0000088"
        assert pack["volunteers"][0]["mobile"] == "9876502222"
        pushed = c.post("/api/mobile/marks", json={
            "code": "BOOTH8", "volunteer_mobile": "9876502222", "voter_id": voter_id,
            "mobile": "800865437", "mood": "favour"})
        assert pushed.status_code == 200 and pushed.json()["mobile"] == "800865437"
        with SessionLocal() as db:
            mark = db.scalar(__import__("sqlalchemy").select(VoterMark).where(VoterMark.voter_id == voter_id))
            assert mark.mood == "favour" and mark.mobile == "800865437"
            db.delete(mark)
            db.commit()
