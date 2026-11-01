"""Kit per le visite accessibili (persone con difficoltà di udito).

Come funziona, ricalcato su esperienze reali (es. tablet predisposti dell'ospedale, richiesta con almeno 24 ore di anticipo):
1. al CUP la persona chiede supporto per l'udito: la richiesta finisce nel fascicolo, la vedono accettazione e medico;
2. il giorno prima della visita, con il via libera dell'accettazione, Filo riserva uno dei kit liberi in quell'orario;
3. scrive al coordinamento infermieristico degli ambulatori, che custodisce i kit e lo porta in ambulatorio;
4. il medico trova la nota sull'appuntamento; l'accettazione trova l'appunto.

Nella mail va solo quello che serve a preparare il kit: nessuna diagnosi.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta

from . import db

KITS = [
    ("K1", "Kit 1", "tablet con sottotitoli in tempo reale, cuffie, microfono a clip"),
    ("K2", "Kit 2", "tablet con sottotitoli in tempo reale, cuffie, microfono a clip"),
    ("K3", "Kit 3", "amplificatore personale con cuffie, lavagnetta"),
]
DURATA = timedelta(minutes=60)  # un kit resta in ambulatorio per tutta la visita più il tempo di consegna
COORDINAMENTO = "coordinamento infermieristico ambulatori"


def destinatario() -> str:
    m = (os.getenv("MAIL_AMBULATORI_PROVA") or "").strip()
    return f"{COORDINAMENTO} <{m}>" if m else COORDINAMENTO


def assicura(con):
    if not con.execute("SELECT 1 FROM kits").fetchone():
        con.executemany("INSERT INTO kits VALUES (?,?,?)", KITS)


def liberi(con, at: str) -> list[dict]:
    t = datetime.fromisoformat(at)
    out = []
    for k in con.execute("SELECT * FROM kits ORDER BY id"):
        occupato = any(abs(datetime.fromisoformat(b["at"]) - t) < DURATA
                       for b in con.execute("SELECT at FROM kit_bookings WHERE kit_id=? AND status='riservato'", (k["id"],)))
        if not occupato:
            out.append(dict(k))
    return out


def libero(con, at: str) -> dict | None:
    lib = liberi(con, at)
    return lib[0] if lib else None


def riserva(con, case_id: str, p: dict, a: dict, user: dict, kit_id: str | None = None, consiglio: str | None = None) -> list[str]:
    """Riserva un kit e scrive al coordinamento. Ritorna le righe per il registro.
    kit_id e consiglio arrivano dall'agente kit sulla piattaforma (app/flussi_kit.py), già controllati: senza, il primo kit libero."""
    from .actions import _mail
    assicura(con)
    gia = con.execute("SELECT b.*, k.label FROM kit_bookings b JOIN kits k ON k.id=b.kit_id "
                      "WHERE appointment_id=? AND status='riservato'", (a["id"],)).fetchone()
    if gia:
        return [f"{gia['label']} già riservato per questa visita"]
    lib = liberi(con, a["at"])
    k = next((x for x in lib if x["id"] == kit_id), lib[0] if lib else None)
    if not k:
        nota = f"Nessun kit libero per la visita del {a['at']}: serve l'accettazione (chiedere al coordinamento ambulatori un kit in prestito)."
        con.execute("INSERT INTO kit_bookings VALUES (?,?,?,?,?,?,?,?)",
                    ("KB-" + uuid.uuid4().hex[:5], None, a["id"], p["id"], a["at"], "nessun_kit", db.now(), nota))
        return [nota]
    bid = "KB-" + uuid.uuid4().hex[:5]
    quando = datetime.fromisoformat(a["at"])
    entro = (quando - timedelta(minutes=30)).strftime("%H:%M")
    nota = (f"{k['label']} pronto in {a['place']} per la visita delle {a['at'][11:]}: usare il microfono a clip, "
            "parlare di fronte alla persona, controllare che i sottotitoli seguano la conversazione."
            + (f" {consiglio}" if consiglio else ""))
    con.execute("INSERT INTO kit_bookings VALUES (?,?,?,?,?,?,?,?)", (bid, k["id"], a["id"], p["id"], a["at"], "riservato", db.now(), nota))
    righe = [f"{k['label']} riservato per la visita del {a['at']}"]
    righe.append(_mail(con, case_id, "segreteria", destinatario(),
                       f"Preparare {k['label']} - {a['specialty']}, {a['place']}, {quando.strftime('%d/%m')} ore {a['at'][11:]} [{bid}]",
                       f"Buongiorno,\nper la visita di domani serve un kit per la comunicazione con una persona che ha chiesto supporto per l'udito.\n\n"
                       f"- kit: {k['label']} ({k['contenuto']})\n"
                       f"- dove: ambulatorio di {a['specialty']}, {a['place']}\n"
                       f"- visita: {a['service']}, {quando.strftime('%d/%m/%Y')} alle {a['at'][11:]}\n"
                       f"- paziente: {p['name']}\n"
                       f"- consegna: in ambulatorio entro le {entro}, carico e con i sottotitoli attivi\n\n"
                       "Il medico trova la nota sull'appuntamento. Se il kit non è disponibile rispondete a questa mail: "
                       "la Centrale Operativa ne riserva un altro.\n\nCentrale Operativa Territoriale"))
    con.execute("INSERT INTO action_log VALUES (?,?,?,?)", (db.now(), "agente", case_id,
                f"{p['name']}: {k['label']} riservato e richiesto al coordinamento ambulatori per la visita del {a['at']}"))
    return righe


def per_appuntamenti(con, ids) -> dict:
    ids = list(ids)
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {r["appointment_id"]: dict(r) for r in con.execute(
        f"SELECT b.*, k.label FROM kit_bookings b LEFT JOIN kits k ON k.id=b.kit_id WHERE b.appointment_id IN ({q})", ids)}
