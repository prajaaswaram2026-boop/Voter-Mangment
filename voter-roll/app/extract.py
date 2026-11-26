"""Electoral-roll PDF extraction.

Designed around the standard ECI layout: page 1 = metadata, following pages =
3 columns of voter cards (serial no., EPIC, Name, Father's/Husband's/... Name,
House Number, Age, Gender). Text PDFs use pdfplumber; image-only pages fall back
to Tesseract (optional). Everything is returned as DRAFT data for admin review.
"""
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter

from . import config

logger = logging.getLogger("voter.extract")

EPIC_RE = re.compile(r"\b([A-Z]{2,3}/?\d{2}/?\d{3}/?\d{6,7}|[A-Z]{3}\d{7})\b")
REL_RE = re.compile(r"\b(Father|Husband|Mother|Wife|Other)'?s?\s+(\S+)\s*[:;\-]?\s*(.*)", re.I)
HOUSE_RE = re.compile(r"House\s*(?:N[A-Za-z.]{1,12}|No\.?)?\s*[:;\-]?\s*([^\s].*?)(?:\s{2,}|$)", re.I)
AGE_RE = re.compile(r"\b(?:Age|Ago)\s*[:;\-]?\s*([0-9OIl]{1,3})", re.I)
GENDER_RE = re.compile(r"\b(?:Gender|Gander|Condor|Gendor)\s*[:;\-]?\s*([A-Za-z]+)", re.I)
_FIELD = re.compile(r"^(?:Elector|House|Age|Ago|Gender|Gander|Photo|Father|Husband|Mother|Wife|Other)\b", re.I)
START_RE = re.compile(r"^\s*(\d{1,4})\s*(?:[A-Z]{2,3}/?\d{2}/?\d{3}/?\d{6,7}|[A-Z]{3}\d{7})?\s*$|"
                      r"^\s*(\d{1,4})\s+([A-Z]{3}\d{7})\b")


def parse_cards(text: str):
    """Split one column of text into voter cards and parse each."""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    chunks, cur = [], []
    for line in lines:
        if START_RE.match(line) and (cur and any("name" in c.lower() for c in cur) or not cur):
            if cur:
                chunks.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        chunks.append(cur)
    out = []
    for ch in chunks:
        rec = parse_card("\n".join(ch))
        if rec:
            out.append(rec)
    return out


def _fold(text: str) -> str:
    table = str.maketrans("åäàáâãéèêëöóôü", "aaaaaaeeeeooou")
    return "".join(c for c in (text or "").lower().translate(table) if "a" <= c <= "z")


def _name_label(word: str) -> bool:
    w = _fold(word)
    if w in {"name", "nama", "narne", "narna", "namo", "naame", "mame", "hame", "nme", "nam",
             "nara", "marne", "narre", "mamne"}:
        return True
    # OCR often reads Name as Mame, Narne, Narnc: one letter off.
    if len(w) == 4 and sum(a == b for a, b in zip(w, "name")) >= 3:
        return True
    return w.startswith("na") and 4 <= len(w) <= 5 and ("m" in w or "rn" in w or "nm" in w)


def _clean_person(value: str) -> str:
    value = re.sub(r"^[\s:=;+\-|<>.{}\]]+", "", value or "")
    value = re.sub(r"\s*(photo|available|ph[o0]t[o0a]|p\s*h\s*o\s*t\s*o)\b.*", "", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip(" ,;")


def _name_value(line: str) -> str:
    m = re.match(r"^(?:Elector'?s?\s*)?(\S+)\s*[:;\-]?\s*(.+)$", line.strip(), re.I)
    if not m or not _name_label(m.group(1)):
        return ""
    value = _clean_person(m.group(2))
    if not value or _name_label(value.split()[0]):
        return ""
    return value


def _norm_gender(token: str) -> str:
    t = "".join(c for c in (token or "").lower() if "a" <= c <= "z")
    if t.startswith("f"):
        return "Female"
    if t.startswith("m"):
        return "Male"
    if t.startswith("t"):
        return "Third"
    if t.startswith("o"):
        return "Other"
    return ""


def parse_card(block: str):
    lines = [l.strip() for l in block.splitlines() if l.strip()]
    if not lines:
        return None
    m = re.match(r"^\s*(\d{1,4})\b", lines[0])
    serial = int(m.group(1)) if m else 0
    epic_m = EPIC_RE.search(block)
    epic = epic_m.group(1).replace("/", "") if epic_m else ""
    if not epic:
        for token in re.findall(r"\b[A-Za-z0-9][A-Za-z0-9/\-]{7,13}\b", block):
            if got := normalize_epic(token):
                epic = got
                break
    name = rel = rel_type = house = gender = ""
    age = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if not rel and (r := REL_RE.search(line)) and _name_label(r.group(2)):
            rel_type, rel = r.group(1).title(), _clean_person(r.group(3))
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt and not _FIELD.match(nxt) and not _name_value(nxt) and not EPIC_RE.search(nxt) and not re.match(r"^\d{1,4}\b", nxt):
                i += 1
                rel = f"{rel} {lines[i]}".strip()
        elif not name and (n := _name_value(line)):
            name = n
        if not house and (h := HOUSE_RE.search(line)):
            house = re.sub(r"\s*(photo|available|p\s*h\s*o\s*t\s*o)\b.*", "", h.group(1), flags=re.I).strip(" ,;")
        if age is None and (a := AGE_RE.search(line)):
            age = int(a.group(1).upper().translate(str.maketrans("OIL", "011")))
            if not 1 <= age <= 120:
                age = None
        if not gender and (g := GENDER_RE.search(line)):
            gender = _norm_gender(g.group(1))
        i += 1
    if not name and not epic:
        return None
    found = [bool(epic), bool(name), bool(rel), age is not None, bool(gender), bool(house), serial > 0]
    weights = [2, 2, 1, 1, 1, 0.5, 0.5]
    conf = sum(w for f, w in zip(found, weights) if f) / sum(weights)
    return dict(serial_number=serial, epic_number=epic, voter_name=name, relative_name=rel,
                relationship_type=rel_type, house_number=house, age=age, gender=gender,
                confidence=round(conf, 2))


def parse_metadata(text: str) -> dict:
    def grab(pat):
        m = re.search(pat, text or "", re.I)
        return m.group(1).strip() if m else ""
    return dict(
        ac=grab(r"Assembly Constituency[^\n]{0,60}?[:\-]\s*(\d+\s*[-–]?\s*[A-Za-z(). ]{2,40})"),
        district=grab(r"District(?:\s+(?:No\.?|Number)(?:\s+and\s+Name)?)?\s*[:\-]\s*([A-Za-z][A-Za-z .]{1,60})"),
        part_number=grab(r"Part\s*(?:No\.?|Number)\s*[:\-]?\s*(\d+)"),
        qualifying_date=grab(r"Qualifying Date[\s\S]{0,40}?(\d{1,2}[./-]\d{1,2}[./-]\d{4})"),
        publication_date=grab(r"Date of Publication[\s\S]{0,40}?(\d{1,2}[./-]\d{1,2}[./-]\d{4})"),
        year=grab(r"Revision[^\n]{0,40}?(20\d{2})"),
        station=grab(r"Name of Polling Station\s*[:\-]\s*([^\n]{2,80})"),
        station_address=grab(r"Address of Polling Station\s*[:\-]\s*([^\n]{2,160})"),
    )


def _tesseract_cmd() -> str:
    cmd = os.getenv("TESSERACT_CMD", "").strip()
    if cmd and os.path.isfile(cmd):
        return cmd
    windows = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.isfile(windows):
        return windows
    return "tesseract"


def _tesseract_ready() -> bool:
    try:
        proc = subprocess.run([_tesseract_cmd(), "--version"], capture_output=True, timeout=15, check=False)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _ocr_backend() -> str:
    """Tesseract on Linux/Render. Windows OCR stays available when Tesseract is absent."""
    cached = getattr(_ocr_backend, "name", None)
    if cached:
        return cached
    choice = os.getenv("OCR_ENGINE", "auto").strip().lower()
    tess = _tesseract_ready()
    win = _ocr_images([]) is not None
    if choice == "tesseract" and tess:
        name = "tesseract"
    elif choice == "winocr" and win:
        name = "winocr"
    elif tess and not (win and os.name == "nt" and choice == "auto"):
        name = "tesseract"
    elif win:
        name = "winocr"
    else:
        name = "none"
    _ocr_backend.name = name
    logger.info("ocr backend=%s dpi=%s lang=%s threads=%s", name, config.OCR_DPI, config.OCR_LANG, config.OCR_THREADS)
    return name


def _preprocess(img):
    from PIL import ImageOps
    gray = ImageOps.grayscale(img)
    return ImageOps.autocontrast(gray, cutoff=1)


def _render_page(page):
    rendered = page.to_image(resolution=config.OCR_DPI).original
    try:
        return _preprocess(rendered)
    finally:
        rendered.close()


def _tesseract_env():
    env = os.environ.copy()
    env["OMP_THREAD_LIMIT"] = str(config.OCR_THREADS)
    return env


def _tesseract_words(img) -> list:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = _tesseract_cmd()
    os.environ["OMP_THREAD_LIMIT"] = str(config.OCR_THREADS)
    lang = config.OCR_LANG or "eng"
    try:
        # PSM 11 keeps each voter card's EPIC as its own word. PSM 6 merges them into the line.
        data = pytesseract.image_to_data(img, lang=lang, config="--psm 11", output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractError:
        if lang == "eng":
            raise
        logger.warning("OCR language %s unavailable; using eng", lang)
        data = pytesseract.image_to_data(img, lang="eng", config="--psm 11", output_type=pytesseract.Output.DICT)
    words = []
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = 0.0
        words.append({"text": text, "x": float(data["left"][i]), "y": float(data["top"][i]),
                       "w": float(data["width"][i]), "h": float(data["height"][i]),
                       "conf": 0.0 if conf < 0 else conf / 100.0})
    return words


def _tesseract_batch_text(images, psm: str = "7", whitelist: str = "") -> list:
    """One Tesseract process for every crop on the page. PSM 7 keeps single-line EPICs accurate."""
    if not images:
        return []
    folder = tempfile.mkdtemp(prefix="ocr-crops-")
    try:
        paths = []
        for i, img in enumerate(images):
            dest = os.path.join(folder, f"{i}.png")
            img.save(dest)
            paths.append(dest)
        listing = os.path.join(folder, "list.txt")
        with open(listing, "w", encoding="utf-8") as handle:
            handle.write("\n".join(paths))
        cmd = [_tesseract_cmd(), listing, "stdout", "--psm", psm, "-l", config.OCR_LANG or "eng"]
        if whitelist:
            cmd.extend(["-c", f"tessedit_char_whitelist={whitelist}"])
        proc = subprocess.run(cmd, capture_output=True, timeout=180, check=False, env=_tesseract_env())
        if proc.returncode != 0 and (config.OCR_LANG or "eng") != "eng":
            cmd[cmd.index("-l") + 1] = "eng"
            proc = subprocess.run(cmd, capture_output=True, timeout=180, check=False, env=_tesseract_env())
        parts = [p.strip() for p in proc.stdout.decode("utf-8", "replace").split("\f")]
        if len(parts) == len(images) + 1 and parts[-1] == "":
            parts.pop()
        if len(parts) < len(images):
            parts.extend([""] * (len(images) - len(parts)))
        return parts[:len(images)]
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _ocr_crop_texts(images, psm: str = "7", whitelist: str = "") -> list:
    if not images:
        return []
    if _ocr_backend() == "tesseract":
        return _tesseract_batch_text(images, psm=psm, whitelist=whitelist)
    results = _ocr_images(images) or []
    return [_ocr_text(result) for result in results]


def _full_page_image(page) -> bool:
    for im in page.images or []:
        if im.get("width", 0) >= page.width * 0.85 and im.get("height", 0) >= page.height * 0.85:
            return True
    return False


def _text_sufficient(page, text: str) -> bool:
    """True when pdfplumber already has the voter cards, so OCR would only add time."""
    if len(EPIC_RE.findall(text or "")) >= 3:
        return True
    return len((text or "").strip()) >= 40 and not _full_page_image(page)


def _ocr_images(images):
    """Windows OCR. Returns one result dict per image, or None if winocr is not installed."""
    try:
        import asyncio
        from winocr import picklify, recognize_pil
    except ImportError:
        return None

    async def run():
        return [picklify(await recognize_pil(img, "en")) for img in images]

    return asyncio.run(run())


def _page_image(page):
    import io
    from PIL import Image
    for im in page.images or []:
        if im.get("width", 0) < page.width * 0.85 or im.get("height", 0) < page.height * 0.85:
            continue
        try:
            return Image.open(io.BytesIO(im["stream"].get_data())).convert("RGB")
        except Exception:
            continue
    return page.to_image(resolution=110).original.convert("RGB")


def _words(result) -> list:
    out = []
    for ln in (result or {}).get("lines") or []:
        for w in ln.get("words") or []:
            text = (w.get("text") or "").strip()
            box = w.get("bounding_rect") or {}
            if not text:
                continue
            out.append({"text": text, "x": float(box.get("x") or 0), "y": float(box.get("y") or 0),
                         "w": float(box.get("width") or 0), "h": float(box.get("height") or 0)})
    return out


def _looks_epic(text: str) -> bool:
    raw = re.sub(r"[^A-Za-z0-9]", "", text or "")
    letters = sum(c.isalpha() for c in raw)
    digits = sum(c.isdigit() for c in raw)
    return letters >= 2 and digits >= 4 and 8 <= len(raw) <= 14


def normalize_epic(text: str) -> str:
    """Turn an OCR token into ABC1234567, fixing letter/digit mix-ups inside each half."""
    raw = re.sub(r"[^A-Za-z0-9]", "", (text or "").upper())
    letters_fix = str.maketrans("01258", "OIZSB")
    digits_fix = str.maketrans("OILSZBGQA", "011528604")
    for i in range(0, max(0, len(raw) - 9) + 1):
        chunk = raw[i:i + 10]
        if len(chunk) < 10:
            break
        epic = chunk[:3].translate(letters_fix) + chunk[3:].translate(digits_fix)
        if re.fullmatch(r"[A-Z]{3}\d{7}", epic):
            return epic
    return ""


def _column_lines(words, width, columns=3, y_tol=8):
    buckets = [[] for _ in range(columns)]
    col_w = width / columns
    for w in words:
        idx = min(columns - 1, max(0, int((w["x"] + w["w"] / 2) / col_w)))
        buckets[idx].append(w)
    grouped = []
    for bucket in buckets:
        bucket.sort(key=lambda w: (w["y"], w["x"]))
        lines = []
        for w in bucket:
            if lines and abs(w["y"] - lines[-1]["y"]) <= y_tol:
                lines[-1]["words"].append(w)
            else:
                lines.append({"y": w["y"], "words": [w]})
        for ln in lines:
            ln["words"].sort(key=lambda w: w["x"])
            ln["text"] = " ".join(w["text"] for w in ln["words"])
        grouped.append(lines)
    return grouped


def _is_noise(text: str) -> bool:
    t = _fold(text)
    return t.startswith("phot") or t.startswith("avail") or t in {"able", "avai", "phto"}


def _clean_lines(lines):
    cleaned = []
    for ln in lines:
        words = [w for w in ln["words"] if not _is_noise(w["text"])]
        if not words:
            continue
        cleaned.append({"y": ln["y"], "words": words, "text": " ".join(w["text"] for w in words)})
    return cleaned


def _is_anchor(ln) -> bool:
    if any(_looks_epic(w["text"]) for w in ln["words"]):
        return True
    return bool(ln["words"]) and all(re.fullmatch(r"\d{1,4}", w["text"]) for w in ln["words"])


def _split_cards(lines):
    lines = _clean_lines(lines)
    anchors = [i for i, ln in enumerate(lines) if _is_anchor(ln)]
    cards = []
    for n, start in enumerate(anchors):
        end = anchors[n + 1] if n + 1 < len(anchors) else len(lines)
        cards.append(lines[start:end])
    return cards


def _epic_word(card):
    found = [w for ln in card for w in ln["words"] if _looks_epic(w["text"])]
    if not found:
        return None
    found.sort(key=lambda w: (w["y"], -w["x"]))
    return found[0]


def _scale(img) -> float:
    """Pixel tolerances were tuned on ~1200px-wide scans. 220 DPI pages are about 3x that."""
    return max(1.0, getattr(img, "width", 1200) / 1200.0)


def _serial(card, scale: float = 1) -> int:
    top = min(ln["y"] for ln in card)
    for ln in card:
        if ln["y"] - top > 36 * scale:
            continue
        for w in ln["words"]:
            if re.fullmatch(r"\d{1,4}", w["text"]):
                n = int(w["text"])
                if 1 <= n <= 5000:
                    return n
    return 0


def _crop_box(img, x0, y0, x1, y1):
    from PIL import ImageOps
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(img.width, int(x1)), min(img.height, int(y1))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    crop = img.crop((x0, y0, x1, y1))
    factor = 4 if crop.height >= 18 else 5
    longest = max(crop.size)
    if longest * factor > 900:
        factor = max(1, int(900 / longest))
    if factor > 1:
        crop = crop.resize((crop.width * factor, crop.height * factor))
    return ImageOps.autocontrast(ImageOps.grayscale(crop)).convert("RGB")


def _epic_crop(img, word):
    s = _scale(img)
    return _crop_box(img, word["x"] - 14 * s, word["y"] - 8 * s,
                     word["x"] + max(word["w"], 40 * s) + 16 * s, word["y"] + max(word["h"], 12 * s) + 10 * s)


def _column_index(card, width):
    centers = [w["x"] + w["w"] / 2 for ln in card for w in ln["words"]]
    return min(2, max(0, int((sum(centers) / len(centers)) / (width / 3))))


def _serial_zone(img, card):
    s = _scale(img)
    col = _column_index(card, img.width)
    y = min(ln["y"] for ln in card)
    left = col * img.width / 3
    return _crop_box(img, left + 2 * s, y - 8 * s, left + 175 * s, y + 40 * s)


def _name_band(img, card):
    """The elector name sits under the EPIC row and above House Number."""
    s = _scale(img)
    col = _column_index(card, img.width)
    left = col * img.width / 3
    right = left + img.width / 3
    top = min(ln["y"] for ln in card)
    lower = [ln["y"] for ln in card if re.search(r"house|age|gender|photo", ln["text"], re.I)]
    bottom = min(lower) if lower else top + 80 * s
    if bottom - top < 24 * s:
        bottom = top + 80 * s
    return _crop_box(img, left + 6 * s, top + 16 * s, right - 6 * s, bottom - 2)


def _name_from_ocr(text: str) -> str:
    for line in (text or "").splitlines():
        if got := _name_value(line):
            return got
    line = _clean_person((text or "").replace("\n", " "))
    if not line or re.search(r"house|age|gender|father|husband|mother|wife|photo", line, re.I):
        return ""
    if sum(c.isalpha() for c in line) >= 3:
        return line
    return ""


def _epic_zone(img, card):
    s = _scale(img)
    col = _column_index(card, img.width)
    y = min(ln["y"] for ln in card)
    right = (col + 1) * img.width / 3
    return _crop_box(img, right - 220 * s, y - 8 * s, right - 2 * s, y + 40 * s)


def _card_block(card, serial, epic, scale: float = 1) -> str:
    body = []
    top = min(ln["y"] for ln in card)
    for ln in card:
        kept = []
        for w in ln["words"]:
            if _looks_epic(w["text"]):
                continue
            if ln["y"] - top <= 36 * scale and re.fullmatch(r"\d{1,4}", w["text"]):
                continue
            kept.append(w["text"])
        if kept:
            body.append(" ".join(kept))
    head = f"{serial} {epic}".strip() if serial or epic else ""
    return "\n".join(([head] if head else []) + body)


def _ocr_text(result) -> str:
    return " ".join(ln.get("text") or "" for ln in (result or {}).get("lines") or [])


def _needs_epic_reread(word) -> bool:
    """A second read is unnecessary when the token is already a confident EPIC."""
    if not word:
        return True
    if not normalize_epic(word.get("text") or ""):
        return True
    if "conf" not in word:
        return True
    return word["conf"] < 0.75


def _records_from_words(img, words):
    scale = _scale(img)
    cards = []
    for lines in _column_lines(words, img.width, y_tol=8 * scale):
        cards.extend(_split_cards(lines))
    jobs = []
    try:
        for i, card in enumerate(cards):
            word = _epic_word(card)
            if _needs_epic_reread(word):
                crop = _epic_crop(img, word) if word else None
                if crop is not None:
                    jobs.append((i, "epic", crop))
                elif (zone := _epic_zone(img, card)) is not None:
                    jobs.append((i, "epic", zone))
            if not _serial(card, scale) and (zone := _serial_zone(img, card)) is not None:
                jobs.append((i, "serial", zone))
        found = {}
        if jobs:
            texts = _ocr_crop_texts([crop for _, _, crop in jobs])
            for (i, kind, _), text in zip(jobs, texts):
                found.setdefault(i, {})[kind] = text
        out = []
        for i, card in enumerate(cards):
            guessed = (_epic_word(card) or {}).get("text", "")
            epic = normalize_epic(found.get(i, {}).get("epic", "")) or normalize_epic(guessed)
            serial = _serial(card, scale)
            if not serial:
                m = re.search(r"\b(\d{1,4})\b", found.get(i, {}).get("serial", ""))
                if m and 1 <= int(m.group(1)) <= 5000:
                    serial = int(m.group(1))
            rec = parse_card(_card_block(card, serial, epic, scale))
            if not rec:
                if not epic:
                    continue
                rec = dict(serial_number=serial or 0, epic_number=epic, voter_name="", relative_name="",
                           relationship_type="", house_number="", age=None, gender="", confidence=0.2)
            if epic:
                rec["epic_number"] = epic
            if serial:
                rec["serial_number"] = serial
            if not rec["epic_number"] and not rec["voter_name"]:
                continue
            confs = [w["conf"] for ln in card for w in ln["words"] if "conf" in w]
            if confs:
                rec["engine_confidence"] = round(sum(confs) / len(confs), 3)
            out.append((card, rec))
        name_jobs = []
        for i, (card, rec) in enumerate(out):
            if rec["voter_name"]:
                continue
            band = _name_band(img, card)
            if band is not None:
                name_jobs.append((i, band))
        if name_jobs:
            texts = _ocr_crop_texts([band for _, band in name_jobs])
            for (i, band), text in zip(name_jobs, texts):
                band.close()
                if got := _name_from_ocr(text):
                    out[i][1]["voter_name"] = got
        return [rec for _, rec in out]
    finally:
        for _, _, crop in jobs:
            crop.close()


def _plaintext(words) -> str:
    words = sorted(words, key=lambda w: (w["y"], w["x"]))
    lines, cur, y = [], [], None
    for w in words:
        if y is not None and abs(w["y"] - y) > 8:
            lines.append(" ".join(t["text"] for t in cur))
            cur = []
        cur.append(w)
        y = w["y"]
    if cur:
        lines.append(" ".join(t["text"] for t in cur))
    return "\n".join(lines)


def _clean_meta(meta: dict) -> dict:
    out = {}
    part = re.sub(r"\D", "", meta.get("part_number") or "")
    if 1 <= len(part) <= 4:
        out["part_number"] = part
    for key in ("qualifying_date", "publication_date"):
        if re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{4}", meta.get(key) or ""):
            out[key] = meta[key]
    ac = (meta.get("ac") or "").strip()
    if 3 <= len(ac) <= 60:
        out["ac"] = ac
    station = (meta.get("station") or "").strip()
    if 3 <= len(station) <= 80:
        out["station"] = station
    addr = (meta.get("station_address") or "").strip()
    if 3 <= len(addr) <= 160:
        out["station_address"] = addr
    if re.fullmatch(r"20\d{2}", meta.get("year") or ""):
        out["year"] = meta["year"]
    return out


def _extract_scanned_page(page):
    """OCR one image page. Returns (records, note, metadata)."""
    backend = _ocr_backend()
    if backend == "none":
        return [], "Scanned page; install Tesseract, or on Windows pip install winocr", {}
    img = _render_page(page) if backend == "tesseract" else _page_image(page)
    try:
        if backend == "tesseract":
            words = _tesseract_words(img)
        else:
            result = _ocr_images([img])[0]
            words = _words(result)
        meta = _clean_meta(parse_metadata(_plaintext(words)))
        epic_like = sum(_looks_epic(w["text"]) for w in words)
        if epic_like < 1 and len(words) < 40:
            return [], "", meta
        return _records_from_words(img, words), "", meta
    finally:
        img.close()


def _apply_ocr_confidence(recs):
    for r in recs:
        engine = r.get("engine_confidence")
        if engine is None:
            r["confidence"] = round(r["confidence"] * 0.85, 2)
        else:
            r["confidence"] = round(min(1.0, r["confidence"] * (0.75 + 0.25 * engine)), 2)


def _base_flags(record):
    flags = []
    if not record["epic_number"]:
        flags.append("missing_epic")
    if not record["voter_name"]:
        flags.append("missing_name")
    if record["confidence"] < config.LOW_CONFIDENCE:
        flags.append("low_confidence")
    record["flags"] = ",".join(flags)


def _flag_records(records):
    counts = Counter(r["epic_number"] for r in records if r["epic_number"])
    for r in records:
        flags = []
        if not r["epic_number"]:
            flags.append("missing_epic")
        elif counts[r["epic_number"]] > 1:
            flags.append("duplicate")
        if not r["voter_name"]:
            flags.append("missing_name")
        if r["confidence"] < config.LOW_CONFIDENCE:
            flags.append("low_confidence")
        r["flags"] = ",".join(flags)


def extract_pdf(path: str, log, skip_pages=None, on_page=None):
    """Returns (metadata, records, pages). log(page, method, n_records, note).

    Text pages are read with pdfplumber. OCR runs only when that text has no voter
    cards. skip_pages are pages already stored from an earlier attempt. on_page, when
    set, is called as on_page(page, method, records, note, seconds, metadata).
    """
    import pdfplumber
    skip = set(skip_pages or ())
    started = time.perf_counter()
    records, meta = [], {}
    ocr_pages = text_pages = 0
    with pdfplumber.open(path) as pdf:
        n_pages = len(pdf.pages)
        for i, page in enumerate(pdf.pages, start=1):
            pt = time.perf_counter()
            if i in skip:
                logger.info("page %s skipped; already completed", i)
                continue
            try:
                full = page.extract_text() or ""
                if i == 1 and full.strip():
                    meta = parse_metadata(full)
                if not _text_sufficient(page, full):
                    recs, note, page_meta = _extract_scanned_page(page)
                    method = "none" if note else "ocr"
                    for k, v in page_meta.items():
                        if v and not meta.get(k):
                            meta[k] = v
                    if method == "ocr":
                        _apply_ocr_confidence(recs)
                        ocr_pages += 1
                else:
                    method, note = "text", ""
                    text_pages += 1
                    width = page.width / 3
                    cols = [page.crop((c * width, 0, (c + 1) * width, page.height)).extract_text() or ""
                            for c in range(3)]
                    texts = cols if sum(len(EPIC_RE.findall(c)) for c in cols) else [full]
                    recs = [r for t in texts for r in parse_cards(t)]
                for r in recs:
                    r["pdf_page_number"] = i
                    _base_flags(r)
                records.extend(recs)
                elapsed = time.perf_counter() - pt
                timed = f"{note} [{elapsed:.2f}s]".strip()
                logger.info("page %s method=%s records=%s seconds=%.2f", i, method, len(recs), elapsed)
                log(i, method, len(recs), timed)
                if on_page:
                    on_page(i, method, recs, note, elapsed, meta)
            except Exception as e:  # one bad page must not kill the job
                elapsed = time.perf_counter() - pt
                note = str(e)[:300]
                logger.exception("page %s failed after %.2fs", i, elapsed)
                log(i, "error", 0, f"{note} [{elapsed:.2f}s]")
                if on_page:
                    on_page(i, "error", [], note, elapsed, meta)
            finally:
                flush = getattr(page, "flush_cache", None)
                if flush:
                    try:
                        flush()
                    except Exception:
                        pass
    _flag_records(records)
    logger.info(
        "pdf done pages=%s text_pages=%s ocr_pages=%s records=%s seconds=%.2f",
        n_pages, text_pages, ocr_pages, len(records), time.perf_counter() - started,
    )
    return meta, records, n_pages
