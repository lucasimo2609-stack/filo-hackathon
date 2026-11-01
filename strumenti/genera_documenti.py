"""Genera i documenti ORIGINALI sintetici (PDF) per la demo. Serve solo a chi sviluppa: richiede reportlab.

    python strumenti/genera_documenti.py                 # archivio dei 10 pazienti + pacchetto di esempio (40 pazienti nuovi)
    python strumenti/genera_documenti.py --pazienti 500 --uscita /tmp/prova500   # pacchetto grande per misurare i tempi

Struttura e ospedale sono INVENTATI ("ASL Laguna Veneta", "Ospedale Laguna"); ogni pagina porta la filigrana
"DATI SINTETICI". Nessun dato reale.
"""
from __future__ import annotations

import argparse
import io
import random
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import cf  # noqa: E402
from app.documenti import DOCS  # noqa: E402
from app.seed import PATIENT_EXTRA, PATIENTS  # noqa: E402

KIND_TITLE = {"lettera_dimissione": "LETTERA DI DIMISSIONE", "referto": "REFERTO", "esami_laboratorio": "REFERTO DI LABORATORIO",
              "richiesta": "RICHIESTA DI PRESTAZIONE", "telefonata": "DIARIO INFERMIERISTICO - CONTATTO TELEFONICO",
              "nota_reparto": "DIARIO DI REPARTO", "nota_sociale": "SCHEDA SOCIALE", "prenotazione": "NOTA DI PRENOTAZIONE CUP",
              "verbale_ps": "VERBALE DI PRONTO SOCCORSO"}
SCANS = {"E-093", "E-171"}  # arrivano come scansione: solo immagine, senza testo


def _wrap(c, text, x, y, width, font="Helvetica", size=10.5, lead=15):
    for para in text.split("\n"):
        words, line = para.split(" "), ""
        for w in words:
            t = (line + " " + w).strip()
            if c.stringWidth(t, font, size) > width:
                c.drawString(x, y, line); y -= lead; line = w
            else:
                line = t
        c.drawString(x, y, line); y -= lead
    return y


def render(doc: dict) -> bytes:
    """doc: id, kind, source, date (AAAA-MM-GG), title, author, text, name, birth (AAAA-MM-GG), cf, address"""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    c.setTitle(f"{doc['title']} - {doc['id']}")
    c.setFillColor(HexColor("#1f5f4a")); c.rect(0, H - 22 * mm, W, 22 * mm, stroke=0, fill=1)
    c.setFillColor(HexColor("#ffffff")); c.setFont("Helvetica-Bold", 13)
    c.drawString(18 * mm, H - 12 * mm, "ASL LAGUNA VENETA  ·  Ospedale Laguna")
    c.setFont("Helvetica", 9); c.drawString(18 * mm, H - 17.5 * mm, "Struttura fittizia per prototipo - Mestre (VE)")
    c.setFillColor(HexColor("#222222")); c.setFont("Helvetica-Bold", 11)
    c.drawString(18 * mm, H - 32 * mm, f"Reparto: {doc['source'].capitalize()}")
    c.setFont("Helvetica-Bold", 14); c.drawString(18 * mm, H - 42 * mm, KIND_TITLE.get(doc["kind"], doc["kind"].upper()))
    c.setFont("Helvetica", 10.5); c.drawString(18 * mm, H - 49 * mm, f"Tipo documento: {doc['title']}")
    c.setStrokeColor(HexColor("#999999")); c.rect(18 * mm, H - 78 * mm, W - 36 * mm, 24 * mm, stroke=1, fill=0)
    d = doc["birth"]
    c.drawString(22 * mm, H - 61 * mm, f"Paziente: {doc['name'].upper()}")
    c.drawString(115 * mm, H - 61 * mm, f"Nato/a il: {d[8:10]}/{d[5:7]}/{d[:4]}")
    c.drawString(22 * mm, H - 68 * mm, f"C.F.: {doc['cf']}")
    c.drawString(22 * mm, H - 75 * mm, f"Residenza: {doc['address']}")
    dd = doc["date"]
    c.drawString(18 * mm, H - 88 * mm, f"Data: {dd[8:10]}/{dd[5:7]}/{dd[:4]}")
    c.drawRightString(W - 18 * mm, H - 88 * mm, f"N. documento: {doc['id']}")
    c.line(18 * mm, H - 92 * mm, W - 18 * mm, H - 92 * mm)
    y = _wrap(c, doc["text"], 18 * mm, H - 102 * mm, W - 36 * mm)
    y -= 14
    c.setFont("Helvetica-Oblique", 10.5); c.drawString(18 * mm, y, f"Firmato digitalmente da: {doc['author']}")
    c.setFont("Helvetica", 8); c.setFillColor(HexColor("#666666"))
    c.drawString(18 * mm, 12 * mm, "Documento SINTETICO generato per il prototipo Filo. Nessun valore clinico o legale.")
    c.saveState(); c.setFillColor(Color(0.75, 0.1, 0.1, alpha=0.10)); c.setFont("Helvetica-Bold", 60)
    c.translate(W / 2, H / 2); c.rotate(35); c.drawCentredString(0, 0, "DATI SINTETICI"); c.restoreState()
    c.showPage(); c.save()
    return buf.getvalue()


def as_scan(pdf: bytes) -> bytes:
    """Trasforma il PDF in una 'scansione': immagine leggermente storta e grigia, senza testo."""
    from PIL import Image, ImageFilter, ImageOps
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "d.pdf"; src.write_bytes(pdf)
        subprocess.run(["pdftoppm", "-r", "150", "-png", "-singlefile", str(src), str(Path(td) / "p")], check=True)
        im = Image.open(Path(td) / "p.png").convert("L")
        im = im.rotate(-0.8, expand=True, fillcolor=245).filter(ImageFilter.GaussianBlur(0.5))
        im = ImageOps.autocontrast(im, cutoff=1).point(lambda v: min(255, v + 12))
        out = io.BytesIO(); im.convert("RGB").save(out, "PDF", resolution=150.0)
        return out.getvalue()


# ---------------------------------------------------------------- archivio dei 10 pazienti

def archivio(dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    pts = {p[0]: p for p in PATIENTS}
    for d in DOCS:
        p = pts[d[1]]; ex = PATIENT_EXTRA[p[0]]
        doc = dict(id=d[0], kind=d[2], source=d[3], date=d[4], title=d[5], author=d[6], text=d[7], name=p[1],
                   birth=ex["birth"], cf=ex["cf"], address=f"{p[3]}, Mestre (VE)")
        pdf = render(doc)
        (dest / f"{d[0]}.pdf").write_bytes(as_scan(pdf) if d[0] in SCANS else pdf)
    print(f"{len(DOCS)} documenti in {dest}")


# ---------------------------------------------------------------- pacchetto per l'importazione massiva

NOMI_M = ["Luigi", "Antonio", "Giovanni", "Mario", "Franco", "Pietro", "Angelo", "Carlo", "Gino", "Bruno", "Sergio", "Aldo", "Paolo", "Dino", "Ettore"]
NOMI_F = ["Maria", "Giuseppina", "Rosa", "Lucia", "Elena", "Carla", "Franca", "Rita", "Gabriella", "Silvana", "Luciana", "Bruna", "Ida", "Graziella", "Nives"]
COGNOMI = ["Bortolozzo", "Zanon", "Favaro", "Scattolin", "Cazzaro", "Tessaro", "Busato", "Gasparini", "Pagnin", "Marcon", "Zecchin", "Vianello",
           "Barbiero", "Bertoldo", "Dal Corso", "Gobbo", "Mion", "Trevisan", "Zuin", "Carraro", "Bellotto", "Pizzolato", "Simionato", "Baldin",
           "Fregonese", "Ballarin", "Rado", "Zago", "Michieletto", "Boato"]
VIE = ["via Piave", "via Carducci", "via Torino", "viale San Marco", "via Miranese", "via Castellana", "via Gazzera Alta", "via Bissuola", "corso del Popolo", "via Trieste"]
MEDICI = {"cardiologia": "dott. Paolo Zennaro, Cardiologia", "medicina": "dott.ssa Carla Venturini, Medicina generale",
          "pneumologia": "dott.ssa Elisa Cagnin, Pneumologia", "geriatria": "dott.ssa Francesca Barbieri, Geriatria",
          "diabetologia": "dott.ssa Laura Scarpa, Diabetologia", "ortopedia": "dott. Luca Bortolato, Ortopedia",
          "laboratorio": "Laboratorio analisi", "endocrinologia": "dott. Stefano Mazzon, Endocrinologia"}
MODELLI = [
    ("referto", "cardiologia", "Visita cardiologica", "Cardiologia",
     "Ipertensione arteriosa. PA {pa}/{pad}. ECG: ritmo sinusale. Prosegue amlodipina 5 mg. Controllo tra 6 mesi."),
    ("referto", "cardiologia", "Visita cardiologica", "Cardiologia",
     "Fibrillazione atriale parossistica. In terapia anticoagulante orale con apixaban 5 mg due volte al giorno."),
    ("esami_laboratorio", "laboratorio", "Esami del sangue", "Laboratorio",
     "Emoglobina {hb} g/dl\nCreatinina {cr} mg/dl\nGlicemia {gl} mg/dl\nPotassio 4,{k} mmol/l"),
    ("referto", "diabetologia", "Visita diabetologica", "Diabetologia",
     "Diabete tipo 2. Emoglobina glicata {hba}%. Prosegue metformina 500 mg due volte al giorno."),
    ("referto", "pneumologia", "Visita pneumologica", "Pneumologia",
     "BPCO moderata. Spirometria: VEMS {vems}% del teorico. Prosegue broncodilatatore a lunga durata d'azione."),
    ("referto", "ortopedia", "Visita ortopedica", "Ortopedia",
     "Coxartrosi destra. Si consiglia diclofenac 75 mg al bisogno per 7 giorni e fisioterapia."),
    ("lettera_dimissione", "medicina", "Lettera di dimissione", "Medicina generale",
     "Ricovero per scompenso cardiaco riacutizzato. Buona risposta alla terapia. Terapia alla dimissione: furosemide 25 mg al mattino."),
    ("referto", "geriatria", "Valutazione geriatrica", "Geriatria",
     "Valutazione multidimensionale: parzialmente autonomo. Vive solo, non guida più. Rischio di caduta moderato."),
]


def pacchetto(dest_zip: Path, n_new: int, seed: int = 11, with_existing: bool = True):
    rnd = random.Random(seed)
    files: list[tuple[str, bytes]] = []
    used = set()
    for i in range(n_new):
        sex = rnd.choice("MF")
        nome = rnd.choice(NOMI_M if sex == "M" else NOMI_F); cogn = rnd.choice(COGNOMI)
        while (nome, cogn) in used:
            nome = rnd.choice(NOMI_M if sex == "M" else NOMI_F); cogn = rnd.choice(COGNOMI)
        used.add((nome, cogn))
        birth = f"{rnd.randint(1930, 1965)}-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}"
        code = cf.compute(cogn, nome, birth, sex)
        addr = f"{rnd.choice(VIE)} {rnd.randint(1, 140)}, Mestre (VE)"
        for j, (kind, src, title, folder, text) in enumerate(rnd.sample(MODELLI, rnd.randint(2, 4))):
            date = f"2026-{rnd.randint(5, 10):02d}-{rnd.randint(1, 28):02d}"
            body = text.format(pa=rnd.randint(130, 170), pad=rnd.randint(75, 98), hb=f"{rnd.randint(110, 150) / 10:.1f}".replace(".", ","),
                               cr=f"{rnd.randint(7, 16) / 10:.1f}".replace(".", ","), gl=rnd.randint(80, 160), k=rnd.randint(0, 9),
                               hba=f"{rnd.randint(60, 85) / 10:.1f}".replace(".", ","), vems=rnd.randint(45, 80))
            doc = dict(id=f"X-{i:03d}{j}", kind=kind, source=src, date=date, title=title, author=MEDICI[src], text=body,
                       name=f"{nome} {cogn}", birth=birth, cf=code, address=addr)
            files.append((f"Archivio_ASL/{folder}/{date[:7]}/{doc['id']}_{cogn.replace(' ', '')}.pdf", render(doc)))
    if with_existing:  # documenti nuovi per pazienti già in Filo: vanno agganciati al codice fiscale
        p = next(x for x in PATIENTS if x[0] == "P06"); ex = PATIENT_EXTRA["P06"]
        doc = dict(id="X-P06A", kind="esami_laboratorio", source="laboratorio", date="2026-10-21", title="Esami del sangue",
                   author=MEDICI["laboratorio"], text="Emoglobina glicata 7,4% (sopra l'obiettivo)\nGlicemia a digiuno 151 mg/dl\nCreatinina 1,0 mg/dl",
                   name=p[1], birth=ex["birth"], cf=ex["cf"], address=f"{p[3]}, Mestre (VE)")
        files.append(("Archivio_ASL/Laboratorio/2026-10/X-P06A_Vianello.pdf", render(doc)))
        p = next(x for x in PATIENTS if x[0] == "P07"); ex = PATIENT_EXTRA["P07"]
        doc = dict(id="X-P07A", kind="referto", source="cardiologia", date="2026-10-20", title="Ecocardiogramma", author=MEDICI["cardiologia"],
                   text="Ecocardiogramma: stenosi aortica lieve, frazione di eiezione 60%. Controllo tra 12 mesi.",
                   name=p[1], birth=ex["birth"], cf=ex["cf"], address=f"{p[3]}, Mestre (VE)")
        files.append(("Archivio_ASL/Cardiologia/2026-10/X-P07A_Boscolo.pdf", render(doc)))
        files.append(("Archivio_ASL/Cardiologia/2026-10/X-P07A_Boscolo_copia.pdf", files[-1][1]))  # doppione: stesso file
        # senza codice fiscale leggibile: deve finire tra i documenti da smistare
        doc = dict(id="X-NOCF", kind="referto", source="geriatria", date="2026-10-19", title="Visita geriatrica", author=MEDICI["geriatria"],
                   text="Paziente accompagnata dalla figlia. Lieve deterioramento cognitivo. Da rivalutare tra 3 mesi.",
                   name="Teresa F.", birth="1944-03-02", cf="(non riportato)", address="(non riportata)")
        files.append(("Archivio_ASL/Da_scanner/scansione_0193.pdf", as_scan(render(doc))))
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            z.writestr(name, data)
    print(f"{len(files)} file per {n_new} pazienti nuovi in {dest_zip}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pazienti", type=int, default=40)
    ap.add_argument("--uscita", default=str(ROOT / "esempi"))
    a = ap.parse_args()
    out = Path(a.uscita)
    if a.pazienti == 40:
        if (out / "archivio_originali").exists():
            shutil.rmtree(out / "archivio_originali")
        archivio(out / "archivio_originali")
    pacchetto(out / f"importazione_esempio_{a.pazienti}_pazienti.zip", a.pazienti, with_existing=a.pazienti == 40)
