"""Valutazione del medico a fine visita: con che urgenza va considerato il paziente, e perché.

La decide il medico, a mano: un livello (bassa, media, alta); i motivi da un elenco fisso e la nota sono facoltativi. Resta in ospedale: niente di tutto questo va al cloud.

Effetto: nei casi del MEDICO per quel paziente il punteggio prende una voce "Valutazione del medico", visibile in
"Perché questa priorità". I casi dell'accettazione non la ricevono: sapere che una persona è "urgente" è già un'informazione
clinica. Il peso è una scelta di prototipo, da far rivedere a un medico come le altre soglie del punteggio.
"""
from __future__ import annotations

import json
import uuid

from . import db

MIN_FLAGS = 0  # i motivi sono facoltativi
URGENCY = {"bassa": "Bassa", "media": "Media", "alta": "Alta"}
POINTS = {"bassa": 0, "media": 10, "alta": 20}
PART = "Valutazione del medico"
FLAGS = {
    "peggioramento": "Peggioramento rispetto alla visita precedente",
    "parametri": "Parametri vitali o esami alterati",
    "dolore": "Dolore non controllato",
    "politerapia": "Terapia complessa (5 farmaci o più)",
    "terapia_cambiata": "Terapia appena cambiata, da monitorare",
    "aderenza": "Non prende la terapia come prescritto",
    "ricovero_recente": "Ricovero o pronto soccorso negli ultimi 30 giorni",
    "cadute": "Rischio di caduta",
    "cognitivo": "Confusione o deficit cognitivo",
    "solo": "Vive solo o con rete familiare debole",
    "accesso": "Difficoltà a raggiungere i servizi",
    "comunicazione": "Difficoltà di comunicazione (udito, lingua)",
}


def latest(con, pid: str) -> dict | None:
    r = con.execute("SELECT * FROM visit_assessments WHERE patient_id=? ORDER BY at DESC, rowid DESC LIMIT 1", (pid,)).fetchone()
    return _row(r) if r else None


def history(con, pid: str, n: int = 10) -> list[dict]:
    return [_row(r) for r in con.execute("SELECT * FROM visit_assessments WHERE patient_id=? ORDER BY at DESC, rowid DESC LIMIT ?", (pid, n))]


def _row(r) -> dict:
    d = dict(r)
    d["flags"] = json.loads(d["flags"])
    d["labels"] = [FLAGS[f] for f in d["flags"] if f in FLAGS]
    return d


def apply_to_parts(parts: list, v: dict | None) -> list:
    """Toglie la voce vecchia e, se c'è una valutazione, aggiunge quella nuova."""
    parts = [p for p in parts if p[0] != PART]
    if v:
        parts.append([PART, POINTS[v["urgency"]]])
    return parts


def save(con, pid: str, doctor: str, urgency: str, flags: list[str], note: str = "", appointment_id: str | None = None) -> dict:
    """Controlla e registra. ValueError con un messaggio leggibile se qualcosa non va."""
    if urgency not in URGENCY:
        raise ValueError("Scegli l'urgenza: bassa, media o alta.")
    flags = list(dict.fromkeys(f for f in flags if f))
    unknown = [f for f in flags if f not in FLAGS]
    if unknown:
        raise ValueError("Motivo non previsto: " + ", ".join(unknown))
    if len(flags) < MIN_FLAGS:
        raise ValueError(f"Segna almeno {MIN_FLAGS} motivi che giustificano l'urgenza (ne hai segnati {len(flags)}).")
    if not con.execute("SELECT 1 FROM patients WHERE id=?", (pid,)).fetchone():
        raise ValueError("Paziente non trovato.")
    if appointment_id and not con.execute("SELECT 1 FROM appointments WHERE id=? AND patient_id=?", (appointment_id, pid)).fetchone():
        raise ValueError("La visita scelta non è di questo paziente.")
    v = {"id": "V-" + uuid.uuid4().hex[:8], "patient_id": pid, "at": db.now(), "doctor": doctor, "urgency": urgency,
         "flags": json.dumps(flags), "note": (note or "").strip()[:500] or None, "appointment_id": appointment_id or None}
    con.execute("INSERT INTO visit_assessments (id, patient_id, at, doctor, urgency, flags, note, appointment_id) VALUES (?,?,?,?,?,?,?,?)",
                tuple(v.values()))
    v = _row(v)
    # subito nei casi del medico ancora in attesa: chi apre "Da firmare" vede già il nuovo ordine
    for c in con.execute("SELECT id, parts FROM cases WHERE patient_id=? AND role='medico' AND status='pending'", (pid,)).fetchall():
        parts = apply_to_parts(json.loads(c["parts"] or "[]"), v)
        con.execute("UPDATE cases SET parts=?, score=? WHERE id=?", (json.dumps(parts), min(100, sum(x[1] for x in parts)), c["id"]))
    return v
