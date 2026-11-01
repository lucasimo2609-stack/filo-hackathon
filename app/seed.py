"""Genera dati SINTETICI: utenti di prova, pazienti, note, agende, popolazione del distretto.

    python -m app.seed            # crea data/filo.db e i due utenti di prova
    python -m app.seed --reset    # ricomincia da zero con i dati (gli utenti restano)
    python -m app.seed --reset --nuovi-utenti   # rigenera anche utenti, password e QR

Le note contengono apposta dati identificativi (nomi, vie, telefoni): sono le "spie"
che il test del cancello verifica non escano mai verso il cloud.
"""
from __future__ import annotations

import random
import secrets
import sys

import pyotp

from . import db
from .auth import hash_password
from .config import DATA, DB_PATH, ROOT as ROOT_DIR

PATIENTS = [
    # id, nome, anno, indirizzo, telefono, quartiere, distretto, solo, auto, udito, mmg
    ("P01", "Giuseppe Rossato", 1942, "via dei Mille 14", "041 555 0101", "Piave", "Nord", 1, 0, 0, "dott. Ferro"),
    ("P02", "Anna Zanella", 1947, "via Fratelli Bandiera 8", "041 555 0102", "Porto", "Sud", 0, 0, 0, "dott.ssa Rizzo"),
    ("P03", "Teresa Furlan", 1944, "via Torre Belfredo 3", "041 555 0103", "Centro", "Nord", 1, 0, 0, "dott. Ferro"),
    ("P04", "Lucia Pavan", 1958, "via Cappuccina 40", "041 555 0104", "Stazione", "Nord", 0, 1, 0, "dott.ssa Moro"),
    ("P05", "Bruno Tonin", 1935, "piazza Ferretto 2", "041 555 0105", "Borgo Est", "Nord", 1, 0, 1, "dott. Ferro"),
    ("P06", "Sergio Vianello", 1951, "via Giustizia 21", "041 555 0106", "Porto", "Sud", 0, 1, 0, "dott.ssa Rizzo"),
    ("P07", "Giovanna Boscolo", 1939, "via Pio X 5", "041 555 0107", "Centro", "Nord", 1, 0, 0, "dott.ssa Moro"),
    ("P08", "Franco De Marchi", 1949, "via Ca' Rossa 77", "041 555 0108", "Lido", "Sud", 0, 1, 0, "dott.ssa Rizzo"),
    ("P09", "Elisa Carraro", 1962, "via Miranese 120", "041 555 0109", "Stazione", "Nord", 0, 1, 0, "dott.ssa Moro"),
    ("P10", "Renato Scarpa", 1940, "via Cecchini 9", "041 555 0110", "Lido", "Sud", 1, 0, 0, "dott.ssa Rizzo"),
]

from .documenti import DOCS  # noqa: E402

# data di nascita, sesso e codice fiscale (calcolato, sintetico)
_BIRTH = {"P01": ("1942-03-14", "M"), "P02": ("1947-07-02", "F"), "P03": ("1944-11-23", "F"), "P04": ("1958-01-30", "F"),
          "P05": ("1935-05-09", "M"), "P06": ("1951-09-17", "M"), "P07": ("1939-12-05", "F"), "P08": ("1949-04-21", "M"),
          "P09": ("1962-08-11", "F"), "P10": ("1940-02-27", "M")}


def _extra():
    from .cf import compute
    out = {}
    for p in PATIENTS:
        nome, cognome = p[1].split(" ", 1)
        b, sx = _BIRTH[p[0]]
        out[p[0]] = {"birth": b, "sex": sx, "cf": compute(cognome, nome, b, sx), "consenso_fse": int(p[0] not in ("P04", "P08"))}
    return out


PATIENT_EXTRA = _extra()

# A chi arrivano le conferme (indirizzi finti, dominio riservato agli esempi)
CONTATTI = {"P01": ("marco.rossato@example.com", "il figlio Marco"), "P02": ("anna.zanella47@example.com", None),
            "P03": ("teresa.furlan@example.com", None), "P04": ("lucia.pavan@example.com", None),
            "P05": ("paola.tonin@example.com", "la figlia Paola"), "P06": ("sergio.vianello@example.com", None),
            "P07": ("andrea.boscolo@example.com", "il nipote Andrea"), "P08": ("franco.demarchi@example.com", None),
            "P09": ("elisa.carraro@example.com", None), "P10": ("chiara.scarpa@example.com", "la figlia Chiara")}

NOTES = [(d[0], d[1], d[2], d[3], d[4], d[7]) for d in DOCS]  # forma breve usata dai test

APPOINTMENTS = [
    ("A-01", "P01", "visita cardiologica di controllo", "cardiologia", "2026-11-21 11:00", "Ospedale Laguna, pad. B", "prenotata"),
    ("A-02", "P02", "visita cardiologica di controllo", "cardiologia", "2026-10-23 09:30", "Ospedale Laguna, pad. B", "prenotata"),
    ("A-05", "P05", "visita cardiologica", "cardiologia", "2026-10-23 10:20", "Ospedale Laguna, pad. B", "prenotata"),
    ("A-06", "P06", "visita diabetologica", "diabetologia", "2026-11-03 08:40", "Poliambulatorio Sud", "prenotata"),
    ("A-09", "P09", "visita pneumologica di controllo", "pneumologia", "2026-11-12 14:30", "Ospedale Laguna, pad. A", "prenotata"),
    ("A-10", "P10", "visita oculistica", "oculistica", "2026-10-27 15:00", "Poliambulatorio Sud", "prenotata"),
]

WAITLIST = [
    ("W-01", "P03", "ecocardiogramma", "B", "2026-09-12", "in attesa"),
    ("W-02", "P07", "ecocardiogramma", "D", "2026-10-01", "in attesa"),
    ("W-03", "P08", "ecocardiogramma", "B", "2026-09-30", "in attesa"),
    # chi aspetta una visita cardiologica: se qualcuno disdice (per la demo, Bruno Tonin il 23/10), c'è chi può prendere il posto
    ("W-04", "P04", "visita cardiologica", "B", "2026-09-20", "in attesa"),
    ("W-05", "P06", "visita cardiologica", "D", "2026-10-05", "in attesa"),
]

FREED = [("FS-1", "ecocardiogramma", "2026-10-23 09:40", "Ospedale Laguna, pad. C", "2026-10-22 07:52", None)]

EXTERNAL = [
    ("X-03", "sciopero", "2026-10-23", "Sciopero dei mezzi pubblici dalle 8:30 alle 16:30."),
    ("X-04", "caldo", "2026-10-25", "Ondata di calore prevista da sabato."),
]

# Regole ricalcate su servizi reali di Venezia e Mestre (verificate sui loro siti a ottobre 2026), contatti finti.
# id, nome, distretti, regole, preavviso in giorni, email, come si prenota, tempi di risposta, costo
TRANSPORTS = [
    ("T1", "STACCO - trasporto sociale", "Nord,Sud",
     "Gratuito per persone fragili: anziani o disabili, chi vive solo, chi non ha mezzi. Richiesta almeno 3 giorni prima.",
     3, "segreteria@stacco.example", "telefono lun-ven 9-13", "conferma entro 48 ore", "gratuito"),
    ("T2", "Croce Verde - mezzo attrezzato", "Nord,Sud",
     "Accompagnamento con mezzo attrezzato e volontario, anche il giorno prima se c'è posto.",
     1, "servizi@croceverde.example", "telefono o email", "risposta in giornata", "offerta o tariffa convenzionata"),
    ("T3", "Associazione di quartiere (esempio)", "Nord",
     "Volontari con auto propria per visite in città. Richiesta 2 giorni prima.",
     2, "volontari@quartiere.example", "email", "risposta entro 24 ore", "gratuito"),
]

CONDITIONS = ["nessuna", "ipertensione", "diabete", "fibrillazione atriale", "scompenso cardiaco", "BPCO",
              "artrosi", "ipotiroidismo", "amiloidosi cardiaca"]
COND_WEIGHTS = [30, 25, 12, 8, 6, 7, 8, 5, 0.15]
QUARTIERI = {"Piave": "Nord", "Centro": "Nord", "Stazione": "Nord", "Borgo Est": "Nord", "Porto": "Sud", "Lido": "Sud"}
PATIENT_CONDITION = {"P01": "fibrillazione atriale", "P02": "ipertensione", "P03": "scompenso cardiaco", "P04": "BPCO",
                     "P05": "amiloidosi cardiaca", "P06": "diabete", "P07": "ipertensione", "P08": "ipotiroidismo",
                     "P09": "BPCO", "P10": "nessuna"}


def residents(n=6000, seed=7):
    rnd = random.Random(seed)
    rows = []
    for _ in range(n):
        age = int(rnd.triangular(40, 100, 72))
        q = rnd.choice(list(QUARTIERI))
        cond = rnd.choices(CONDITIONS, COND_WEIGHTS)[0]
        alone = int(rnd.random() < (0.45 if age >= 80 else 0.25))
        car = int(rnd.random() < (0.25 if age >= 85 else 0.55 if age >= 75 else 0.85))
        rows.append((age, q, QUARTIERI[q], cond, alone, car))
    return rows


USER_COLS = "username,name,role,pw_salt,pw_hash,totp_secret,title,first_name,last_name,qualifica,struttura,reparto,theme"


def main():
    keep_users, keep_sessions = [], []
    if "--reset" in sys.argv and DB_PATH.exists():
        # gli utenti restano (password, app di autenticazione, profilo, tema) e chi è dentro resta dentro
        if "--nuovi-utenti" not in sys.argv:
            import sqlite3
            old = sqlite3.connect(DB_PATH)
            for cols in (USER_COLS, "username,name,role,pw_salt,pw_hash,totp_secret"):  # database di prima: meno colonne
                try:
                    keep_users = [(cols, r) for r in old.execute(f"SELECT {cols} FROM users").fetchall()]
                    break
                except sqlite3.Error:
                    keep_users = []
            try:
                keep_sessions = old.execute("SELECT sid, username, created_at, last_seen FROM sessions").fetchall()
            except sqlite3.Error:
                keep_sessions = []
            old.close()
        DB_PATH.unlink()
        for ext in ("-wal", "-shm"):
            DB_PATH.with_name(DB_PATH.name + ext).unlink(missing_ok=True)
    db.init()
    if keep_users:
        with db.tx() as con:
            for cols, r in keep_users:
                con.execute(f"INSERT INTO users ({cols}) VALUES ({','.join('?' * len(r))})", r)
            con.executemany("INSERT OR IGNORE INTO sessions VALUES (?,?,?,?)", keep_sessions)
    with db.tx() as con:
        for t in ["inbox", "import_jobs", "transport_requests", "transport_events", "patients", "notes", "appointments", "waitlist", "freed_slots", "external_events", "transport_services", "residents",
                  "slot_offers", "slot_events", "cal_slots", "cal_log", "cal_changes", "cal_change_events"]:
            con.execute(f"DELETE FROM {t}")
        con.executemany("INSERT INTO patients VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [(*p, PATIENT_EXTRA[p[0]]["birth"], PATIENT_EXTRA[p[0]]["sex"], PATIENT_EXTRA[p[0]]["cf"],
                          PATIENT_EXTRA[p[0]]["consenso_fse"], "anagrafe", *CONTATTI[p[0]]) for p in PATIENTS])
        con.executemany("INSERT INTO notes (id,patient_id,kind,source,date,text,title,author) VALUES (?,?,?,?,?,?,?,?)", [(d[0], d[1], d[2], d[3], d[4], d[7], d[5], d[6]) for d in DOCS])
        con.executemany("INSERT INTO appointments VALUES (?,?,?,?,?,?,?)", APPOINTMENTS)
        con.executemany("INSERT INTO waitlist VALUES (?,?,?,?,?,?)", WAITLIST)
        con.executemany("INSERT INTO freed_slots VALUES (?,?,?,?,?,?)", FREED)
        con.executemany("INSERT INTO external_events VALUES (?,?,?,?)", EXTERNAL)
        con.executemany("INSERT INTO transport_services VALUES (?,?,?,?,?,?,?,?,?)", TRANSPORTS)
        con.executemany("INSERT INTO residents VALUES (?,?,?,?,?,?)", residents())
        from . import calendario  # agende sintetiche costruite su visite, lista d'attesa e posti disdetti qui sopra
        cal = calendario.popola(con)
        if cal["incoerenze"]:
            raise RuntimeError("Calendario non coerente con il dataset: " + "; ".join(cal["incoerenze"][:5]))
        for p in PATIENTS:  # anche i pazienti fanno parte della popolazione
            con.execute("INSERT INTO residents VALUES (?,?,?,?,?,?)",
                        (2026 - p[2], p[5], p[6], PATIENT_CONDITION[p[0]], p[7], p[8]))

        # originali dei documenti: archivio dell'ospedale, con impronta SHA-256
        from .importa import store
        originals = ROOT_DIR / "esempi" / "archivio_originali"
        n_files = 0
        for d in DOCS:
            f = originals / f"{d[0]}.pdf"
            if f.exists():
                sha, rel = store(f.read_bytes(), ".pdf")
                how = "scansione: testo dal sistema di origine" if d[0] in ("E-093", "E-171") else "testo del PDF"
                con.execute("UPDATE notes SET file=?, sha256=?, mime='application/pdf', origin='archivio ospedaliero', imported_at=?, "
                            "imported_by='sistema', read_with=? WHERE id=?", (rel, sha, db.now(), how, d[0]))
                n_files += 1

        existing = {r["username"] for r in con.execute("SELECT username FROM users")}
        created = []
        for username, name, role in [("m.bianchi", "Marta Bianchi", "segreteria"), ("e.gatti", "Dott.ssa Elena Gatti", "medico")]:
            if username in existing:
                continue
            password = secrets.token_urlsafe(9)
            salt, h = hash_password(password)
            secret = pyotp.random_base32()
            con.execute("INSERT INTO users (username,name,role,pw_salt,pw_hash,totp_secret) VALUES (?,?,?,?,?,?)",
                        (username, name, role, salt, h, secret))
            created.append((username, name, role, password, secret))

    if created:
        import qrcode
        lines = ["UTENTI DI PROVA DI FILO (dati sintetici). File riservato, non condividere.\n"]
        for username, name, role, password, secret in created:
            uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Filo demo")
            img = DB_PATH.parent / f"qr_{username}.png"  # accanto al database: un database di prova non tocca le credenziali vere
            qrcode.make(uri).save(img)
            img.chmod(0o600)
            lines.append(f"{name} ({role})\n  utente: {username}\n  password: {password}\n  QR per l'app di autenticazione: {img}\n")
        cred = DB_PATH.parent / "credenziali_demo.txt"
        cred.write_text("\n".join(lines), encoding="utf-8")
        cred.chmod(0o600)
        print("\n".join(lines))
        print("Inquadra i QR con Google Authenticator, Microsoft Authenticator o simili.")
    else:
        print("Utenti già presenti: credenziali in data/credenziali_demo.txt")
    from .posta import contatti_demo
    contatti_demo()  # caselle vere della prova al posto dei contatti finti, se nel .env
    print(f"Database pronto: {DB_PATH} ({n_files} documenti originali collegati)")


if __name__ == "__main__":
    main()
