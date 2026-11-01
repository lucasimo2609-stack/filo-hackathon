"""Importazione LOCALE dei documenti originali: niente di questo modulo parla con il cloud.

Per ogni file:
1. impronta SHA-256 (serve a riconoscere i doppioni e a dimostrare che l'originale non è cambiato);
2. copia nell'archivio di Filo, in sola lettura;
3. lettura del testo: PDF con testo, scansioni e foto con OCR locale (tesseract, se installato), testo semplice, CDA2 in XML;
4. riconoscimento del paziente dal CODICE FISCALE in testata (con controllo del carattere finale);
   se il paziente non c'è lo crea, se il codice manca o non torna il file va tra i "da smistare" e decide una persona;
5. tipo, reparto, data, autore dalla testata.
"""
from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from . import cf as cfmod
from . import db
from .config import DATA, TODAY

ARCHIVE = DATA / "archivio"
MAX_BYTES = 20 * 1024 * 1024
ALLOWED = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
           ".txt": "text/plain", ".xml": "application/xml"}
KIND_BY_TITLE = [("LETTERA DI DIMISSIONE", "lettera_dimissione"), ("REFERTO DI LABORATORIO", "esami_laboratorio"),
                 ("RICHIESTA DI PRESTAZIONE", "richiesta"), ("CONTATTO TELEFONICO", "telefonata"), ("DIARIO DI REPARTO", "nota_reparto"),
                 ("SCHEDA SOCIALE", "nota_sociale"), ("PRENOTAZIONE CUP", "prenotazione"), ("PRONTO SOCCORSO", "verbale_ps"), ("REFERTO", "referto")]


# ------------------------------------------------------------------ lettura

def _ocr_available() -> bool:
    return shutil.which("tesseract") is not None


def _ocr_image(path: Path) -> str:
    langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout
    lang = "ita" if "\nita" in langs else "eng"
    r = subprocess.run(["tesseract", str(path), "-", "-l", lang], capture_output=True, text=True, timeout=120)
    return r.stdout


def read_text(data: bytes, ext: str) -> tuple[str, str]:
    """Ritorna (testo, come è stato letto)."""
    if ext == ".pdf":
        from pypdf import PdfReader
        try:
            text = "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(data)).pages)
        except Exception:
            text = ""
        if len(text.strip()) > 40:
            return text, "testo del PDF"
        if _ocr_available() and shutil.which("pdftoppm"):
            with tempfile.TemporaryDirectory() as td:
                src = Path(td) / "d.pdf"; src.write_bytes(data)
                subprocess.run(["pdftoppm", "-r", "300", "-png", str(src), str(Path(td) / "p")], check=True, timeout=120)
                pages = sorted(Path(td).glob("p*.png"))
                return "\n".join(_ocr_image(p) for p in pages), "scansione letta con OCR locale"
        return "", "scansione senza testo (OCR non installato)"
    if ext in (".png", ".jpg", ".jpeg"):
        if not _ocr_available():
            return "", "immagine senza testo (OCR non installato)"
        with tempfile.NamedTemporaryFile(suffix=ext) as f:
            f.write(data); f.flush()
            return _ocr_image(Path(f.name)), "foto o scansione letta con OCR locale"
    if ext == ".xml":
        return _cda_text(data), "CDA2 (XML)"
    return data.decode("utf-8", errors="replace"), "testo semplice"


CF_ROOT = "2.16.840.1.113883.2.9.4.3.2"  # OID del codice fiscale (Ministero dell'Economia) nei documenti CDA2 italiani


def _cda_text(data: bytes) -> str:
    """CDA2 essenziale: codice fiscale, nome, nascita, titolo, data, testo delle sezioni. Lo riscrive nella forma della testata.
    Niente DTD né entità (difesa dalle "bombe" XML) e il codice fiscale vale solo se l'ente che lo assegna è quello giusto."""
    ns = {"h": "urn:hl7-org:v3"}
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        return "XML rifiutato: contiene DTD o entità"
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return data.decode("utf-8", errors="replace")
    g = lambda path, attr=None: (root.find(path, ns).get(attr) if attr else "".join(root.find(path, ns).itertext())) if root.find(path, ns) is not None else ""
    cfv = next((i.get("extension") for i in root.iterfind(".//h:recordTarget/h:patientRole/h:id", ns) if i.get("root") == CF_ROOT), "")
    given, family = g(".//h:patient/h:name/h:given"), g(".//h:patient/h:name/h:family")
    birth = g(".//h:patient/h:birthTime", "value")
    eff = g("./h:effectiveTime", "value")
    body = "\n".join("".join(t.itertext()).strip() for t in root.iterfind(".//h:section/h:text", ns))
    fmt = lambda v: f"{v[6:8]}/{v[4:6]}/{v[:4]}" if len(v) >= 8 else ""
    return (f"{g('./h:title').upper()}\nTipo documento: {g('./h:title')}\nPaziente: {given} {family}\nNato/a il: {fmt(birth)}\n"
            f"C.F.: {cfv}\nData: {fmt(eff)}\n{body}\nFirmato digitalmente da: {g('.//h:legalAuthenticator//h:name')}")


# ------------------------------------------------------------------ testata

def _grab(pattern, text, flags=0):
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def _date(s):
    if not s:
        return None
    m = re.search(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", s)
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else None


def parse(text: str) -> dict:
    up = text.upper()
    kind = next((k for t, k in KIND_BY_TITLE if t in up), "altro")
    body = text
    m1 = re.search(r"N\.\s*documento:.*\n", text)
    m2 = re.search(r"Firmato digitalmente da:", text)
    if m1:
        body = text[m1.end():m2.start() if m2 else None]
    body = re.sub(r"Documento SINTETICO.*", "", body).strip()
    body = re.sub(r"[ \t]+\n", "\n", body)
    return {
        "cf": (cfmod.find(text) or [None])[0],
        "cf_raw": _grab(r"C\.?F\.?:\s*(\S+)", text),
        "name": _grab(r"Paziente:\s*([^\n]+?)(?:\s{2,}|\s+Nato|\n|$)", text),
        "birth": _date(_grab(r"Nat[oa/]+ il:\s*([0-9/.-]+)", text)),
        "address": _grab(r"Residenza:\s*([^\n]+)", text),
        "source": (_grab(r"Reparto:\s*([^\n]+)", text) or "non indicato").lower(),
        "title": _grab(r"Tipo documento:\s*([^\n]+)", text),
        "date": _date(_grab(r"\bData:\s*([0-9/.-]+)", text)),
        "author": _grab(r"Firmato digitalmente da:\s*([^\n]+)", text),
        "number": _grab(r"N\.\s*documento:\s*(\S+)", text),
        "kind": kind,
        "body": body,
    }


# ------------------------------------------------------------------ archivio

def store(data: bytes, ext: str) -> tuple[str, str]:
    sha = hashlib.sha256(data).hexdigest()
    rel = f"{sha[:2]}/{sha}{ext}"
    dest = ARCHIVE / rel
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        dest.chmod(0o440)
    return sha, rel


def verify(rel: str, sha: str) -> bool:
    p = ARCHIVE / rel
    return p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == sha


PREVIEWS = DATA / "anteprime"


def page_count(rel: str) -> int:
    if not rel.endswith(".pdf"):
        return 1
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(ARCHIVE / rel)).pages)
    except Exception:  # pypdf mancante o file illeggibile: si mostra il PDF nel browser
        return 0


def preview(rel: str, page: int) -> Path | None:
    """Immagine PNG della pagina (per vedere l'originale anche dove il browser non mostra i PDF). Fatta in locale e tenuta in cache."""
    src = ARCHIVE / rel
    if not src.exists():
        return None
    if src.suffix.lower() in (".png", ".jpg", ".jpeg"):
        return src
    if src.suffix.lower() != ".pdf" or not shutil.which("pdftoppm"):
        return None
    out = PREVIEWS / f"{src.stem}-{page}.png"
    if not out.exists():  # si scrive in una cartella temporanea e si sposta alla fine: mai un'immagine a metà
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=out.parent) as td:
            base = Path(td) / "p"
            subprocess.run(["pdftoppm", "-r", "110", "-png", "-singlefile", "-f", str(page), "-l", str(page), str(src), str(base)],
                           check=True, timeout=60)
            if (Path(td) / "p.png").exists():
                os.replace(Path(td) / "p.png", out)
    return out if out.exists() else None


def can_preview() -> bool:
    return shutil.which("pdftoppm") is not None


def _title_name(n: str) -> str:
    return " ".join(w.capitalize() if not w.lower().startswith("dal") else w.capitalize() for w in n.split())


def _cf_birth(cf: str) -> str:
    b = cfmod.base(cf)
    yy, mm, dd = int(b[6:8]), cfmod.MESI.index(b[8]) + 1, int(b[9:11]) % 40
    cur = int(TODAY[2:4])
    return f"{1900 + yy if yy > cur else 2000 + yy}-{mm:02d}-{dd:02d}"


def _name_matches_cf(name: str | None, cf: str) -> bool:
    """Le sei lettere iniziali del codice fiscale tornano con il nome letto (nell'ordine nome cognome o cognome nome)?"""
    parts = (name or "").split()
    if len(parts) < 2:
        return False
    for i in range(1, len(parts)):
        a, b = " ".join(parts[:i]), " ".join(parts[i:])
        if cf[:6] in (cfmod.name_code(b, a), cfmod.name_code(a, b)):
            return True
    return False


def _new_patient(con, h, origin):
    """Paziente nuovo solo se nome, data di nascita e codice fiscale tornano tra loro: un codice letto male dall'OCR
    che per caso passa il controllo finale non deve creare un paziente fantasma."""
    pid = "P-" + uuid.uuid4().hex[:8].upper()
    sex = "F" if int(cfmod.base(h["cf"])[9:11]) > 40 else "M"
    birth = h["birth"] or _cf_birth(h["cf"])
    name = _title_name(h["name"])
    # vive solo e auto: NON NOTI finché un documento non lo dice (prima "ha l'auto" era il valore di partenza)
    con.execute("INSERT INTO patients (id,name,birth_year,address,phone,quartiere,distretto,lives_alone,has_car,hearing_support,mmg,"
                "birth_date,sex,cf,consenso_fse,origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, name, int(birth[:4]), (h["address"] or "").replace(", Mestre (VE)", ""), "", None, "non indicato", None, None, 0,
                 "non indicato", birth, sex, h["cf"], 0, origin))
    return pid


def ingest(con, data: bytes, original_name: str, origin: str, user: str, forced_pid: str | None = None) -> dict:
    ext = Path(original_name).suffix.lower()
    res = {"file": original_name}
    if ext not in ALLOWED:
        return {**res, "esito": "ignorato", "motivo": "formato non gestito"}
    if len(data) > MAX_BYTES:
        return {**res, "esito": "ignorato", "motivo": "file oltre 20 MB"}
    sha = hashlib.sha256(data).hexdigest()
    dup = con.execute("SELECT id, patient_id FROM notes WHERE sha256=?", (sha,)).fetchone() or \
        con.execute("SELECT id, NULL FROM inbox WHERE sha256=? AND status='da smistare'", (sha,)).fetchone()
    if dup:
        return {**res, "esito": "doppione", "motivo": f"già presente come {dup[0]}"}
    sha, rel = store(data, ext)
    text, how = read_text(data, ext)
    h = parse(text)
    now = db.now()

    def to_inbox(reason):
        iid = "S-" + sha[:12]  # 12 caratteri dell'impronta: niente collisioni anche con decine di migliaia di file
        con.execute("INSERT OR REPLACE INTO inbox (id,file,sha256,mime,original_name,text,reason,origin,imported_at,imported_by,read_with) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (iid, rel, sha, ALLOWED[ext], original_name, text, reason, origin, now, user, how))
        return {**res, "esito": "da smistare", "motivo": reason, "id": iid}

    pid, created = None, False
    if forced_pid:
        pat = con.execute("SELECT * FROM patients WHERE id=?", (forced_pid,)).fetchone()
        if h["cf"] and pat["cf"] and cfmod.base(h["cf"]) != cfmod.base(pat["cf"]):
            return to_inbox(f"il codice fiscale nel documento ({h['cf']}) non è quello di {pat['name']}")
        if not h["cf"] and h["name"] and pat["name"].split(" ", 1)[-1].upper() not in h["name"].upper():
            return to_inbox(f"documento senza codice fiscale con un nome ({h['name']}) diverso da {pat['name']}")
        pid = forced_pid
    elif h["cf"]:
        row = next((r for r in con.execute("SELECT id, name, cf FROM patients WHERE cf IS NOT NULL") if cfmod.base(r["cf"]) == cfmod.base(h["cf"])), None)
        if row:
            surname = row["name"].split(" ", 1)[-1].upper()
            if h["name"] and surname not in h["name"].upper():
                return to_inbox(f"codice fiscale di {row['name']} ma nome diverso nel documento ({h['name']})")
            pid = row["id"]
        elif not _name_matches_cf(h["name"], cfmod.base(h["cf"])):
            return to_inbox(f"codice fiscale {h['cf']} e nome letto ({h['name'] or 'non leggibile'}) non tornano: forse un errore di lettura")
        elif h["birth"] and _cf_birth(h["cf"]) != h["birth"]:
            return to_inbox(f"la data di nascita nel documento ({h['birth']}) non torna con il codice fiscale {h['cf']}")
        else:
            pid, created = _new_patient(con, h, origin), True
    else:
        looks_cf = h["cf_raw"] and len(re.sub(r"\W", "", h["cf_raw"])) >= 11
        why = f"codice fiscale non valido ({h['cf_raw']}): forse letto male dall'OCR" if looks_cf else "codice fiscale non trovato nel documento"
        if not text.strip():
            why = "nessun testo leggibile: " + how
        return to_inbox(why)

    nid = h["number"] if h["number"] and not con.execute("SELECT 1 FROM notes WHERE id=?", (h["number"],)).fetchone() else "D-" + sha[:12]
    if not h["date"]:
        how += " · data del documento non letta: usata quella di importazione"
    con.execute("INSERT INTO notes (id,patient_id,kind,source,date,text,title,author,file,sha256,mime,origin,imported_at,imported_by,read_with) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (nid, pid, h["kind"], h["source"], h["date"] or now[:10], h["body"] or text, h["title"] or h["kind"].replace("_", " ").capitalize(),
                 h["author"] or "non indicato", rel, sha, ALLOWED[ext], origin, now, user, how))
    return {**res, "esito": "nuovo paziente" if created else "aggiunto", "id": nid, "patient_id": pid}


def _iter_files(path: Path):
    for p in sorted(path.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            if p.suffix.lower() == ".zip":
                yield from _iter_zip(p.read_bytes())
            else:
                yield str(p.relative_to(path)), p.read_bytes()


MAX_ZIP_FILES = 2000
MAX_ZIP_TOTAL = 2 * 1024 * 1024 * 1024  # 2 GB decompressi al massimo per pacchetto
MAX_RATIO = 100  # un file che si espande più di 100 volte è sospetto (bomba zip)


def iter_zip_file(fileobj):
    """Legge un pacchetto zip un file alla volta, senza tenerlo tutto in memoria e con limiti contro le bombe zip."""
    with zipfile.ZipFile(fileobj) as z:
        total, n = 0, 0
        for info in z.infolist():
            if info.is_dir() or Path(info.filename).name.startswith(".") or info.file_size > MAX_BYTES:
                continue
            if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
                continue
            n += 1
            total += info.file_size
            if n > MAX_ZIP_FILES or total > MAX_ZIP_TOTAL:
                break
            with z.open(info) as f:
                yield info.filename, f.read(MAX_BYTES + 1)


def _iter_zip(data: bytes):
    yield from iter_zip_file(io.BytesIO(data))


def run_job(files, origin: str, user: str) -> dict:
    """files: iterabile di (nome, bytes). Una transazione per file: un file rotto non ferma gli altri."""
    jid = "IMP-" + datetime.now().strftime("%H%M%S") + uuid.uuid4().hex[:3]
    t0 = datetime.now()
    with db.tx() as con:
        con.execute("INSERT INTO import_jobs (id, started_at, started_by, origin) VALUES (?,?,?,?)", (jid, db.now(), user, origin))
    results = []
    for name, data in files:
        try:
            with db.tx() as con:
                results.append(ingest(con, data, name, origin, user))
        except Exception as e:  # noqa: BLE001
            results.append({"file": name, "esito": "errore", "motivo": str(e)[:200]})
    count = lambda k: sum(r["esito"] == k for r in results)
    summary = {"file": len(results), "aggiunti": count("aggiunto") + count("nuovo paziente"),
               "pazienti_nuovi": len({r["patient_id"] for r in results if r["esito"] == "nuovo paziente"}),
               "da_smistare": count("da smistare"), "doppioni": count("doppione"), "ignorati": count("ignorato"), "errori": count("errore"),
               "secondi": round((datetime.now() - t0).total_seconds(), 1), "dettaglio": results[:2000]}
    with db.tx() as con:
        con.execute("UPDATE import_jobs SET finished_at=?, summary=? WHERE id=?", (db.now(), json.dumps(summary, ensure_ascii=False), jid))
    return {"id": jid, **summary}


def import_folder(path: Path, user: str) -> dict:
    return run_job(_iter_files(path), f"cartella condivisa ({path.name})", user)


def import_zip(data: bytes, name: str, user: str) -> dict:
    return run_job(_iter_zip(data), f"pacchetto caricato ({name})", user)


def guess_mime(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"
