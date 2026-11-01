"""Il kit in ambulatorio: la visita accessibile mentre si svolge.

Il kit arriva in ambulatorio con kit.py (prenotazione e mail al coordinamento). Qui c'è quello che succede dopo:
1. il medico apre la visita dal suo cruscotto e mette il microfono a clip; la persona mette le cuffie;
2. la voce del medico passa, nel browser del tablet, da un filtro (rumore ridotto, volume alzato) alle cuffie;
3. ogni frase va a trascrivere e torna sullo schermo del tablet in caratteri grandi. L'audio non si salva;
4. a fine visita la trascrizione entra nella cartella del paziente come documento;
5. se la persona la vuole, un tasto la manda per mail: all'indirizzo già in anagrafica o a uno scritto lì.

Trascrizione: Whisper sulla AI Factory Fastweb se c'è la chiave (FW_KEY_WHISPER), altrimenti ElevenLabs Scribe.
La pagina dice sempre quale dei due sta lavorando.
"""
from __future__ import annotations

import os
import re
import secrets
import uuid

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS kit_visite (
  id TEXT PRIMARY KEY, booking_id TEXT, patient_id TEXT, appointment_id TEXT, doctor TEXT, token TEXT UNIQUE,
  status TEXT, started_at TEXT, ended_at TEXT, note_id TEXT, backend TEXT, sent_to TEXT, sent_at TEXT, invio_a TEXT
);
CREATE TABLE IF NOT EXISTS kit_segmenti (visita_id TEXT, seq INTEGER, ts TEXT, text TEXT, PRIMARY KEY (visita_id, seq));
"""

WHISPER_MODEL = "openai/whisper-large-v3-turbo"
MAX_AUDIO = 3 * 1024 * 1024  # un pezzo è una frase (al massimo ~15 secondi): oltre è un errore
RE_MAIL = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$")
# frasi che i modelli di trascrizione inventano sul silenzio o sul rumore: non sono del medico
_INVENTATE = re.compile(r"sottotitoli|amara\.org|grazie per la visione|iscriviti al canale|\(musica\)|\(rumore\)|^\W*$", re.I)


class NonConfigurata(RuntimeError):
    pass


def backend() -> str | None:
    if (os.getenv("FW_KEY_WHISPER") or "").strip():
        return "fastweb"
    if (os.getenv("ELEVENLABS_API_KEY") or "").strip():
        return "elevenlabs"
    return None


BACKEND_LABEL = {"fastweb": "Whisper su AI Factory Fastweb (UE)", "elevenlabs": "ElevenLabs Scribe (prova: l'audio esce verso ElevenLabs)"}


def trascrivi(audio: bytes, mime: str) -> str:
    """Una frase di audio -> testo. Niente resta su disco. Le frasi inventate sul silenzio si scartano."""
    testo = " ".join((_chiedi(audio, mime) or "").split())
    return "" if _INVENTATE.search(testo) else testo


def _chiedi(audio: bytes, mime: str) -> str:
    b = backend()
    ext = "mp4" if "mp4" in mime else "ogg" if "ogg" in mime else "wav" if "wav" in mime else "webm"
    if b == "fastweb":
        from openai import OpenAI
        cl = OpenAI(base_url=os.getenv("FW_BASE_URL", "https://spod.ai-factory.fastweb.it/v1"), api_key=os.getenv("FW_KEY_WHISPER"), timeout=30)
        r = cl.audio.transcriptions.create(model=WHISPER_MODEL, file=(f"frase.{ext}", audio, mime), language="it")
        testo = r.text
    elif b == "elevenlabs":
        import httpx
        r = httpx.post("https://api.elevenlabs.io/v1/speech-to-text", headers={"xi-api-key": os.getenv("ELEVENLABS_API_KEY")},
                       data={"model_id": "scribe_v1", "language_code": "ita", "tag_audio_events": "false"},
                       files={"file": (f"frase.{ext}", audio, mime)}, timeout=30)
        if r.status_code == 400 and "invalid_audio" in r.text:
            return ""  # pezzo vuoto o troppo corto (il registratore del browser a volte ne chiude uno così): è silenzio
        r.raise_for_status()
        testo = r.json().get("text", "")
    else:
        raise NonConfigurata("Trascrizione non configurata: serve FW_KEY_WHISPER oppure ELEVENLABS_API_KEY nel .env.")
    return testo


# ------------------------------------------------------------------ visita

def _prenotazione(con, booking_id: str) -> dict | None:
    r = con.execute("SELECT b.*, k.label, a.service, a.specialty, a.place, p.name AS patient, p.email "
                    "FROM kit_bookings b JOIN kits k ON k.id=b.kit_id JOIN appointments a ON a.id=b.appointment_id "
                    "JOIN patients p ON p.id=b.patient_id WHERE b.id=? AND b.status='riservato'", (booking_id,)).fetchone()
    return dict(r) if r else None


def apri(con, booking_id: str, user: dict) -> str:
    """Apre (o riprende) la visita col kit di questa prenotazione. Una sola visita aperta per prenotazione."""
    b = _prenotazione(con, booking_id)
    if not b:
        raise ValueError("Nessun kit riservato con questo codice.")
    aperta = con.execute("SELECT id FROM kit_visite WHERE booking_id=? AND status='in_corso'", (booking_id,)).fetchone()
    if aperta:
        return aperta["id"]
    vid = "KV-" + uuid.uuid4().hex[:6]
    con.execute("INSERT INTO kit_visite (id, booking_id, patient_id, appointment_id, doctor, token, status, started_at, backend) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (vid, booking_id, b["patient_id"], b["appointment_id"], user["username"],
                                               secrets.token_urlsafe(16), "in_corso", db.now(), backend()))
    con.execute("INSERT INTO action_log VALUES (?,?,?,?)", (db.now(), user["username"], vid,
                f"{b['patient']}: visita con {b['label']} iniziata ({b['service']})"))
    return vid


def carica(con, vid: str) -> dict | None:
    r = con.execute("SELECT v.*, p.name AS patient, p.email, a.service, a.at, a.place, k.label FROM kit_visite v "
                    "JOIN patients p ON p.id=v.patient_id JOIN appointments a ON a.id=v.appointment_id "
                    "JOIN kit_bookings b ON b.id=v.booking_id LEFT JOIN kits k ON k.id=b.kit_id WHERE v.id=?", (vid,)).fetchone()
    return dict(r) if r else None


def per_token(con, token: str) -> dict | None:
    """Lo schermo del tablet entra con il codice della visita, finché la visita è in corso."""
    if not token:
        return None
    r = con.execute("SELECT id FROM kit_visite WHERE token=? AND status='in_corso'", (token,)).fetchone()
    return carica(con, r["id"]) if r else None


def aggiungi(con, vid: str, seq: int, testo: str) -> None:
    if testo:
        con.execute("INSERT OR REPLACE INTO kit_segmenti VALUES (?,?,?,?)", (vid, seq, db.now(), testo))


def segmenti(con, vid: str, dopo: int = -1) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT seq, ts, text FROM kit_segmenti WHERE visita_id=? AND seq>? ORDER BY seq", (vid, dopo))]


def testo(con, vid: str) -> str:
    return "\n".join(f"[{s['ts'][11:16]}] {s['text']}" for s in segmenti(con, vid))


def chiudi(con, vid: str, user: dict) -> str:
    """Fine visita: la trascrizione va nella cartella come documento. Il link del tablet smette di funzionare."""
    v = carica(con, vid)
    if not v:
        raise ValueError("Visita non trovata.")
    if v["status"] == "chiusa":
        return v["note_id"]
    corpo = testo(con, vid) or "(nessuna frase trascritta)"
    nid = "D-KV-" + vid[3:]
    medico = con.execute("SELECT name FROM users WHERE username=?", (v["doctor"],)).fetchone()
    con.execute("INSERT INTO notes (id,patient_id,kind,source,date,text,title,author,origin,imported_at,imported_by,read_with) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (nid, v["patient_id"], "trascrizione_visita", v["service"], v["at"][:10],
                 f"Trascrizione automatica della visita ({v['service']}, {v['at']}). Può contenere errori.\n\n{corpo}",
                 f"Trascrizione della visita accessibile ({v['label']})", medico["name"] if medico else v["doctor"],
                 "kit visita accessibile", db.now(), user["username"], BACKEND_LABEL.get(v["backend"] or "", "trascrizione automatica")))
    con.execute("UPDATE kit_visite SET status='chiusa', ended_at=?, note_id=?, token=NULL WHERE id=?", (db.now(), nid, vid))
    con.execute("INSERT INTO action_log VALUES (?,?,?,?)", (db.now(), user["username"], vid,
                f"{v['patient']}: visita con {v['label']} conclusa, trascrizione nella cartella ({nid})"))
    return nid


def invia(con, vid: str, email: str, salva: bool, user: dict) -> str:
    """Il tasto del medico. Solo a visita chiusa: si manda quello che è nella cartella. L'indirizzo resta in ospedale;
    il testo lo rende leggibile l'agente kit sulla piattaforma, se il flusso è acceso (app/flussi_kit.py)."""
    from . import flussi_kit
    v = carica(con, vid)
    if not v or v["status"] != "chiusa":
        raise ValueError("Prima termina la visita: la trascrizione si manda quando è completa.")
    email = (email or "").strip()
    if not RE_MAIL.match(email):
        raise ValueError("Indirizzo email non valido.")
    if salva and email.lower() != (v["email"] or "").lower():
        con.execute("UPDATE patients SET email=? WHERE id=?", (email, v["patient_id"]))
    con.execute("UPDATE kit_visite SET invio_a=? WHERE id=?", (email, vid))
    con.execute("INSERT INTO action_log VALUES (?,?,?,?)", (db.now(), user["username"], vid,
                f"{v['patient']}: chiesto l'invio della trascrizione a {email}"))
    return flussi_kit.invia(con, vid)


def manda(con, vid: str, leggibile: str | None, user: dict) -> str:
    """Manda la mail all'indirizzo scelto dal medico. `leggibile`: il testo dell'agente, già controllato; senza, le frasi così come sono."""
    from .actions import _mail
    v = carica(con, vid)
    email = v["invio_a"]
    centrale = os.getenv("NUMERO_CENTRALE", "041 000 0000")
    corpo = (f"Buongiorno,\ncome richiesto in ambulatorio, ecco la trascrizione di quello che il medico ha detto durante la visita "
             f"({v['service']}, {v['at'][8:10]}/{v['at'][5:7]}/{v['at'][:4]} alle {v['at'][11:16]}).\n\n"
             "È una trascrizione automatica: può contenere errori e non sostituisce la lettera o il referto del medico.\n"
             f"Per qualunque dubbio chiami la Centrale Operativa al {centrale}.\n\n"
             f"{leggibile or testo(con, vid) or '(nessuna frase trascritta)'}\n\nCentrale Operativa Territoriale")
    esito = _mail(con, vid, "medico", email, f"Trascrizione della sua visita del {v['at'][8:10]}/{v['at'][5:7]}", corpo)
    con.execute("UPDATE kit_visite SET sent_to=?, sent_at=? WHERE id=?", (email, db.now(), vid))
    con.execute("INSERT INTO action_log VALUES (?,?,?,?)", (db.now(), user["username"], vid,
                f"{v['patient']}: trascrizione della visita mandata a {email}"))
    return esito
