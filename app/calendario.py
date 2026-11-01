"""Calendario delle agende: lo strumento con cui l'agente Agende legge e cambia le prenotazioni. Resta in ospedale.

Un'agenda è fatta di posti (esame, luogo, giorno e ora, durata). Ogni posto è:
  libero     - si può prenotare
  tenuto     - proposto a una persona, si aspetta la sua risposta (prenotazione "da confermare")
  prenotato  - occupato: da una prenotazione di Filo (appointment_id) o da una prenotazione sintetica di prova
  bloccato   - chiuso (ambulatorio non disponibile)

Regole che decide il codice, non chi chiama:
- ogni operazione rilegge il posto nella stessa transazione in cui lo cambia, e lo cambia solo se è ancora come
  l'aveva letto (numero di versione): un dato vecchio viene rifiutato, non sovrascritto;
- nessun doppione: una persona non ha due prenotazioni attive per lo stesso esame (salvo quella che sta sostituendo);
- nessuna sovrapposizione: una persona non ha due visite che si accavallano, di qualunque esame;
- due indici unici: un posto per esame, luogo e orario; una prenotazione in un posto solo;
- dopo ogni modifica lo strumento rilegge il database con una connessione nuova e dice se il salvataggio torna;
- ogni operazione, riuscita o no, finisce in cal_log.

Tutti i dati sono SINTETICI e marcati: test=1 e una "fonte" che dice da dove vengono.
"""
from __future__ import annotations

import hashlib
import json
import random
import uuid
from datetime import date, datetime, timedelta

from . import db
from .config import TODAY

STATI = ("libero", "tenuto", "prenotato", "bloccato")
STATO_TESTO = {"libero": "Libero", "tenuto": "Tenuto (da confermare)", "prenotato": "Prenotato", "bloccato": "Bloccato"}
ATTIVE = ("prenotata", "da confermare")  # stati di una prenotazione che occupa un posto
SISTEMA = {"username": "calendario", "name": "strumento Calendario"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cal_slots (
  id TEXT PRIMARY KEY, service TEXT, specialty TEXT, place TEXT, at TEXT, minutes INTEGER,
  status TEXT CHECK(status IN ('libero','tenuto','prenotato','bloccato')),
  appointment_id TEXT, occupant TEXT, fonte TEXT, test INTEGER DEFAULT 1, version INTEGER DEFAULT 0, updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS cal_un_posto_per_orario ON cal_slots(service, place, at);
CREATE UNIQUE INDEX IF NOT EXISTS cal_una_prenotazione_un_posto ON cal_slots(appointment_id) WHERE appointment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS cal_per_giorno ON cal_slots(service, at);
CREATE TABLE IF NOT EXISTS cal_log (ts TEXT, who TEXT, op TEXT, slot_id TEXT, appointment_id TEXT, esito TEXT, text TEXT);
"""

# Agende sintetiche, costruite sulle visite del dataset (seed.py): stessi esami, luoghi e orari delle prenotazioni esistenti.
# esame -> (specialità, luogo, giorni della settimana 0=lun, primo orario, ultimo orario, minuti, quota occupata dalle prenotazioni di prova)
AGENDE = {
    "ecocardiogramma": ("cardiologia", "Ospedale Laguna, pad. C", range(0, 5), "08:00", "12:40", 20, 1.0),  # c'è la lista d'attesa: tutto pieno
    "visita cardiologica": ("cardiologia", "Ospedale Laguna, pad. B", range(0, 5), "08:40", "12:00", 50, 0.75),
    "visita cardiologica di controllo": ("cardiologia", "Ospedale Laguna, pad. B", range(0, 6), "08:30", "12:00", 30, 0.7),
    "visita diabetologica": ("diabetologia", "Poliambulatorio Sud", range(0, 5), "08:00", "11:20", 40, 0.7),
    "visita pneumologica di controllo": ("pneumologia", "Ospedale Laguna, pad. A", range(0, 5), "14:00", "16:30", 30, 0.7),
    "visita oculistica": ("oculistica", "Poliambulatorio Sud", range(0, 5), "14:00", "17:00", 30, 0.7),
    "tac_torace": ("radiologia", "Ospedale Laguna, pad. A", range(0, 5), "08:00", "12:40", 20, 0.8),
    "rmn_ginocchio": ("radiologia", "Ospedale Laguna, pad. A", range(0, 5), "13:00", "17:00", 30, 0.8),
}
GIORNI_AGENDA = 45


class Errore(Exception):
    """Operazione rifiutata dai controlli: la transazione si annulla e niente cambia."""


def _today() -> date:
    return date.fromisoformat(TODAY)


def _slot_id(service: str, place: str, at: str) -> str:
    return "CS-" + hashlib.sha256(f"{service}|{place}|{at}".encode()).hexdigest()[:10]


def _log(con, who, op, slot_id, appt, esito, text):
    con.execute("INSERT INTO cal_log VALUES (?,?,?,?,?,?,?)", (db.now(), (who or SISTEMA)["username"], op, slot_id, appt, esito, text[:500]))


def _row(r) -> dict | None:
    return dict(r) if r else None


# ------------------------------------------------------------------ lettura (sempre dal database, mai da una copia)

def consulta(con, service: str | None = None, dal: str | None = None, al: str | None = None, place: str | None = None,
             stato: str | None = None, limite: int = 500) -> list[dict]:
    q, a = ("SELECT s.*, ap.patient_id, ap.status AS appointment_status, p.name AS patient "
            "FROM cal_slots s LEFT JOIN appointments ap ON ap.id=s.appointment_id LEFT JOIN patients p ON p.id=ap.patient_id WHERE 1=1"), []
    for cond, val in (("s.service=?", service), ("s.at>=?", dal), ("s.at<=?", al), ("s.place=?", place), ("s.status=?", stato)):
        if val:
            q += f" AND {cond}"
            a.append(val)
    return [dict(r) for r in con.execute(q + " ORDER BY s.at, s.service LIMIT ?", (*a, limite))]


def posto(con, slot_id: str | None = None, service: str | None = None, at: str | None = None, place: str | None = None) -> dict | None:
    if slot_id:
        return _row(con.execute("SELECT * FROM cal_slots WHERE id=?", (slot_id,)).fetchone())
    q, a = "SELECT * FROM cal_slots WHERE service=? AND at=?", [service, at]
    if place:
        q += " AND place=?"
        a.append(place)
    return _row(con.execute(q, a).fetchone())


def e_libero(con, **k) -> dict:
    s = posto(con, **k)
    if not s:
        return {"libero": False, "motivo": "posto inesistente nel calendario"}
    if s["status"] != "libero":
        return {"libero": False, "motivo": f"posto {STATO_TESTO[s['status']].lower()}", "posto": s}
    if datetime.fromisoformat(s["at"]).date() < _today():
        return {"libero": False, "motivo": "posto già passato", "posto": s}
    return {"libero": True, "posto": s}


def disponibilita(con, service: str, dal: str | None = None, al: str | None = None, limite: int = 20) -> list[dict]:
    dal = dal or _today().isoformat()
    return consulta(con, service=service, dal=dal, al=al, stato="libero", limite=limite)


def prenotazioni_paziente(con, patient_id: str, service: str | None = None) -> list[dict]:
    q, a = "SELECT * FROM appointments WHERE patient_id=? AND status IN ('prenotata','da confermare')", [patient_id]
    if service:
        q += " AND service=?"
        a.append(service)
    return [dict(r) for r in con.execute(q + " ORDER BY at", a)]


def posti_liberati(con, service: str | None = None) -> list[dict]:
    """Posti disdetti che la lista d'attesa può usare: non assegnati, non passati, e ancora liberi nel calendario."""
    out = []
    for f in con.execute("SELECT * FROM freed_slots WHERE assigned_to IS NULL ORDER BY at"):
        if service and f["service"] != service:
            continue
        s = posto(con, service=f["service"], at=f["at"], place=f["place"])
        out.append({**dict(f), "cal": s, "libero_nel_calendario": bool(s and s["status"] in ("libero", "tenuto")),
                    "passato": datetime.fromisoformat(f["at"]).date() < _today()})
    return out


def _sovrapposizioni(con, patient_id: str, s: dict, escludi=()) -> list[dict]:
    """Altre visite attive della stessa persona che si accavallano con il posto s."""
    start = datetime.fromisoformat(s["at"])
    end = start + timedelta(minutes=s["minutes"] or 30)
    out = []
    for a in prenotazioni_paziente(con, patient_id):
        if a["id"] in escludi:
            continue
        cs = posto(con, service=a["service"], at=a["at"], place=a["place"])
        a_start = datetime.fromisoformat(a["at"])
        a_end = a_start + timedelta(minutes=(cs or {}).get("minutes") or 30)
        if a_start < end and start < a_end:
            out.append(a)
    return out


# ------------------------------------------------------------------ scrittura (dentro la transazione di chi chiama)

def _prendi(con, s: dict, da: tuple, a: str, appt: str | None, occupant: str | None, versione: int | None):
    """Cambia lo stato del posto solo se è ancora quello letto: altrimenti qualcuno l'ha cambiato nel frattempo."""
    if versione is not None and versione != s["version"]:
        raise Errore(f"dato non aggiornato: il posto è cambiato dopo la lettura (versione {versione}, ora {s['version']}). Rileggi il calendario.")
    n = con.execute(f"UPDATE cal_slots SET status=?, appointment_id=?, occupant=?, version=version+1, updated_at=? "
                    f"WHERE id=? AND version=? AND status IN ({','.join('?' * len(da))})",
                    (a, appt, occupant, db.now(), s["id"], s["version"], *da)).rowcount
    if n != 1:
        raise Errore("il posto è stato cambiato da un'altra operazione nello stesso momento: rileggi il calendario")


def prenota(con, patient_id: str, slot_id: str, who: dict = SISTEMA, stato: str = "prenotata", sostituisce: str | None = None,
            versione: int | None = None, specialty: str | None = None) -> str:
    """Nuova prenotazione in un posto libero. stato='da confermare' tiene il posto in attesa della risposta.
    sostituisce: la prenotazione della stessa persona per lo stesso esame che questa prenderà il posto (non conta come doppione)."""
    if stato not in ATTIVE:
        raise Errore(f"stato non valido: {stato}")
    if not con.execute("SELECT 1 FROM patients WHERE id=?", (patient_id,)).fetchone():
        raise Errore("paziente sconosciuto")
    ok = e_libero(con, slot_id=slot_id)
    if not ok["libero"]:
        raise Errore(f"posto non disponibile: {ok['motivo']}")
    s = ok["posto"]
    doppie = [a for a in prenotazioni_paziente(con, patient_id, s["service"]) if a["id"] != sostituisce]
    if doppie:
        raise Errore(f"doppione: la persona ha già {s['service']} il {doppie[0]['at']} ({doppie[0]['status']})")
    sopra = _sovrapposizioni(con, patient_id, s, escludi={sostituisce} if sostituisce else set())
    if sopra:
        raise Errore(f"sovrapposizione: alla stessa ora la persona ha {sopra[0]['service']} il {sopra[0]['at']}")
    appt = "A-" + uuid.uuid4().hex[:8]
    con.execute("INSERT INTO appointments VALUES (?,?,?,?,?,?,?)",
                (appt, patient_id, s["service"], specialty or s["specialty"], s["at"], s["place"], stato))
    _prendi(con, s, ("libero",), "tenuto" if stato == "da confermare" else "prenotato", appt, "paziente", versione)
    _log(con, who, "prenota", s["id"], appt, "ok", f"{s['service']} {s['at']} per {patient_id} ({stato})")
    return appt


def _posto_di(con, appointment_id: str) -> tuple[dict, dict]:
    a = _row(con.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone())
    if not a:
        raise Errore("prenotazione sconosciuta")
    s = _row(con.execute("SELECT * FROM cal_slots WHERE appointment_id=?", (appointment_id,)).fetchone())
    if not s:
        raise Errore("la prenotazione non ha un posto nel calendario: dati non coerenti, da controllare a mano")
    return a, s


def conferma(con, appointment_id: str, who: dict = SISTEMA):
    a, s = _posto_di(con, appointment_id)
    if a["status"] != "da confermare" or s["status"] != "tenuto":
        raise Errore(f"niente da confermare (prenotazione {a['status']}, posto {s['status']})")
    if con.execute("UPDATE appointments SET status='prenotata' WHERE id=? AND status='da confermare'", (appointment_id,)).rowcount != 1:
        raise Errore("la prenotazione è cambiata nel frattempo")
    _prendi(con, s, ("tenuto",), "prenotato", appointment_id, "paziente", None)
    _log(con, who, "conferma", s["id"], appointment_id, "ok", f"{a['service']} {a['at']} confermata")


def cancella(con, appointment_id: str, who: dict = SISTEMA, motivo: str = "", alla_lista: bool = True) -> str | None:
    """Annulla la prenotazione e libera il posto. alla_lista: se era una prenotazione vera (non una proposta) e il posto è
    nel futuro, diventa un "posto liberato" per la lista d'attesa. Ritorna l'id del posto liberato, se c'è."""
    a, s = _posto_di(con, appointment_id)
    if a["status"] not in ATTIVE:
        raise Errore(f"prenotazione già {a['status']}")
    if con.execute("UPDATE appointments SET status='annullata' WHERE id=? AND status=?", (appointment_id, a["status"])).rowcount != 1:
        raise Errore("la prenotazione è cambiata nel frattempo")
    _prendi(con, s, ("tenuto", "prenotato"), "libero", None, None, None)
    fs = None
    if alla_lista and a["status"] == "prenotata" and datetime.fromisoformat(s["at"]).date() >= _today():
        old = con.execute("SELECT id FROM freed_slots WHERE service=? AND at=? AND place=?", (s["service"], s["at"], s["place"])).fetchone()
        if old:  # lo stesso posto era già stato liberato e poi assegnato: torna disponibile
            fs = old[0]
            con.execute("UPDATE freed_slots SET assigned_to=NULL, freed_at=? WHERE id=?", (db.now(), fs))
        else:
            fs = "FS-" + uuid.uuid4().hex[:6]
            con.execute("INSERT INTO freed_slots VALUES (?,?,?,?,?,NULL)", (fs, s["service"], s["at"], s["place"], db.now()))
    _log(con, who, "cancella", s["id"], appointment_id, "ok", f"{a['service']} {a['at']} annullata ({motivo or 'nessun motivo'})"
         + (f"; il posto passa alla lista d'attesa ({fs})" if fs else ""))
    return fs


def sposta(con, appointment_id: str, slot_id: str, who: dict = SISTEMA, versione: int | None = None, libera_per_lista: bool = True) -> str | None:
    """Sposta una prenotazione in un altro posto libero dello stesso esame. Il posto lasciato passa alla lista d'attesa
    (libera_per_lista=False quando l'ambulatorio lo chiude: allora resta a chi lo chiude)."""
    a, old = _posto_di(con, appointment_id)
    if a["status"] not in ATTIVE:
        raise Errore(f"prenotazione già {a['status']}")
    ok = e_libero(con, slot_id=slot_id)
    if not ok["libero"]:
        raise Errore(f"posto di arrivo non disponibile: {ok['motivo']}")
    s = ok["posto"]
    if s["service"] != a["service"]:
        raise Errore(f"il posto di arrivo è per {s['service']}, non per {a['service']}")
    sopra = _sovrapposizioni(con, a["patient_id"], s, escludi={appointment_id})
    if sopra:
        raise Errore(f"sovrapposizione: alla nuova ora la persona ha {sopra[0]['service']} il {sopra[0]['at']}")
    if versione is not None and versione != s["version"]:
        raise Errore(f"dato non aggiornato: il posto è cambiato dopo la lettura (versione {versione}, ora {s['version']}). Rileggi il calendario.")
    # prima si lascia il posto vecchio, poi si prende il nuovo: l'indice unico vieta una prenotazione in due posti
    _prendi(con, old, ("tenuto", "prenotato"), "libero", None, None, None)
    _prendi(con, s, ("libero",), old["status"], appointment_id, "paziente", None)
    con.execute("UPDATE appointments SET at=?, place=? WHERE id=?", (s["at"], s["place"], appointment_id))
    fs = None
    if libera_per_lista and a["status"] == "prenotata" and datetime.fromisoformat(old["at"]).date() >= _today():
        fs = "FS-" + uuid.uuid4().hex[:6]
        con.execute("INSERT INTO freed_slots VALUES (?,?,?,?,?,NULL)", (fs, old["service"], old["at"], old["place"], db.now()))
    _log(con, who, "sposta", s["id"], appointment_id, "ok", f"{a['service']} da {a['at']} a {s['at']}" + (f"; il posto lasciato va alla lista ({fs})" if fs else ""))
    return fs


def tieni(con, slot_id: str, who: dict = SISTEMA, versione: int | None = None, per: str = "modifica") -> dict:
    """Tiene un posto libero per una modifica proposta (si aspetta la conferma del paziente): nessuno lo prende nel frattempo."""
    ok = e_libero(con, slot_id=slot_id)
    if not ok["libero"]:
        raise Errore(f"posto non disponibile: {ok['motivo']}")
    _prendi(con, ok["posto"], ("libero",), "tenuto", None, per, versione)
    _log(con, who, "tieni", slot_id, None, "ok", f"{ok['posto']['service']} {ok['posto']['at']} tenuto per una {per} in attesa di conferma")
    return posto(con, slot_id=slot_id)


def rilascia(con, slot_id: str, who: dict = SISTEMA, per: str = "modifica"):
    """Libera un posto tenuto per una modifica che non si fa più."""
    s = posto(con, slot_id=slot_id)
    if not s or s["status"] != "tenuto" or s["appointment_id"] or s["occupant"] != per:
        raise Errore("il posto non è tenuto per questa modifica")
    _prendi(con, s, ("tenuto",), "libero", None, None, None)
    _log(con, who, "rilascia", slot_id, None, "ok", f"{s['service']} {s['at']} di nuovo libero")


def chiudi_posto(con, slot_id: str, who: dict = SISTEMA, motivo: str = ""):
    """Ambulatorio non disponibile: il posto (libero, o occupato da una prenotazione di prova) diventa bloccato.
    Un posto con la prenotazione di una persona non si chiude qui: prima va spostata (modifiche.blocca)."""
    s = posto(con, slot_id=slot_id)
    if not s:
        raise Errore("posto inesistente")
    if s["appointment_id"] or s["status"] == "tenuto":
        raise Errore("nel posto c'è una prenotazione o una proposta: va prima spostata")
    if s["status"] == "bloccato":
        return
    _prendi(con, s, ("libero", "prenotato"), "bloccato", None, None, None)
    _log(con, who, "blocca", slot_id, None, "ok", f"{s['service']} {s['at']} bloccato ({motivo or 'nessun motivo'})"
         + (" ; tolta la prenotazione di prova" if s["occupant"] == "prova" else ""))


# ------------------------------------------------------------------ verifica dopo il salvataggio

def verifica_prenotazione(con, appointment_id: str, stato: str | None = None) -> list[str]:
    """Rilegge: la prenotazione c'è nello stato atteso, occupa esattamente un posto coerente, niente doppioni o sovrapposizioni."""
    a = _row(con.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone())
    if not a:
        return ["prenotazione non trovata"]
    prob = []
    if stato and a["status"] != stato:
        prob.append(f"prenotazione in stato '{a['status']}', attesa '{stato}'")
    slots = [dict(r) for r in con.execute("SELECT * FROM cal_slots WHERE appointment_id=?", (appointment_id,))]
    if a["status"] in ATTIVE:
        if len(slots) != 1:
            prob.append(f"la prenotazione occupa {len(slots)} posti nel calendario")
        else:
            s = slots[0]
            want = "tenuto" if a["status"] == "da confermare" else "prenotato"
            if s["status"] != want or s["at"] != a["at"] or s["service"] != a["service"] or s["place"] != a["place"]:
                prob.append(f"il posto nel calendario non corrisponde (posto {s['status']} {s['at']}, prenotazione {a['status']} {a['at']})")
        n = len(prenotazioni_paziente(con, a["patient_id"], a["service"]))
        if n != 1:
            prob.append(f"la persona risulta prenotata {n} volte per {a['service']}")
    elif slots:
        prob.append("la prenotazione annullata occupa ancora un posto")
    return prob


def coerenza(con) -> list[str]:
    """Controllo di tutto il calendario: ogni prenotazione attiva ha il suo posto, ogni posto occupato da Filo ha la sua prenotazione."""
    prob = []
    for a in con.execute("SELECT id FROM appointments WHERE status IN ('prenotata','da confermare')"):
        prob += [f"{a['id']}: {p}" for p in verifica_prenotazione(con, a["id"])]
    for s in con.execute("SELECT s.id, s.status, a.status AS ast FROM cal_slots s LEFT JOIN appointments a ON a.id=s.appointment_id "
                         "WHERE s.appointment_id IS NOT NULL"):
        if s["ast"] not in ATTIVE:
            prob.append(f"{s['id']}: posto {s['status']} legato a una prenotazione {s['ast'] or 'inesistente'}")
    for s in con.execute("SELECT id, occupant FROM cal_slots WHERE status IN ('tenuto') AND appointment_id IS NULL"):
        aperta = s["occupant"] == "modifica" and con.execute(
            "SELECT 1 FROM cal_changes WHERE to_slot=? AND status IN ('da_approvare','in_chiamata','esito','senza_esito','errore')", (s["id"],)).fetchone()
        if not aperta:
            prob.append(f"{s['id']}: posto tenuto senza prenotazione né modifica aperta")
    return prob


# ------------------------------------------------------------------ lo strumento per l'agente

def _schema(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": props, "required": req}}}


STRUMENTI = [
    _schema("calendario_consulta", "Legge i posti del calendario (con stato e versione). Va chiamato prima di ogni modifica.",
            {"esame": {"type": "string"}, "dal": {"type": "string", "description": "AAAA-MM-GG"}, "al": {"type": "string"},
             "stato": {"type": "string", "enum": list(STATI)}}, []),
    _schema("calendario_posto_libero", "Dice se un posto è libero adesso.", {"posto": {"type": "string"}}, ["posto"]),
    _schema("calendario_disponibilita", "I prossimi posti liberi per un esame.", {"esame": {"type": "string"}, "dal": {"type": "string"}}, ["esame"]),
    _schema("calendario_prenota", "Prenota un posto libero per una persona. Passa la versione letta.",
            {"paziente": {"type": "string"}, "posto": {"type": "string"}, "versione": {"type": "integer"},
             "da_confermare": {"type": "boolean"}, "sostituisce": {"type": "string"}}, ["paziente", "posto", "versione"]),
    _schema("calendario_conferma", "Conferma una prenotazione 'da confermare'.", {"prenotazione": {"type": "string"}}, ["prenotazione"]),
    _schema("calendario_sposta", "Sposta una prenotazione in un altro posto libero dello stesso esame. Se non lo chiede il paziente, "
            "la modifica NON si applica subito: si tiene il posto, si avvisa l'ambulatorio e si chiama il paziente per la conferma "
            "(chiamata da approvare). Senza 'posto', Filo propone il primo posto libero adatto.",
            {"prenotazione": {"type": "string"}, "posto": {"type": "string"}, "versione": {"type": "integer"}, "motivo": {"type": "string"},
             "richiesta_dal_paziente": {"type": "boolean"}}, ["prenotazione"]),
    _schema("calendario_cancella", "Annulla una prenotazione. Se non lo chiede il paziente, serve prima la sua conferma al telefono "
            "(come per lo spostamento). Il posto, se futuro, passa alla lista d'attesa.",
            {"prenotazione": {"type": "string"}, "motivo": {"type": "string"}, "richiesta_dal_paziente": {"type": "boolean"}}, ["prenotazione"]),
    _schema("calendario_blocca", "Chiude un posto (ambulatorio non disponibile). Se c'è la prenotazione di una persona, propone lo "
            "spostamento al primo posto libero e chiede la conferma al paziente.",
            {"posto": {"type": "string"}, "motivo": {"type": "string"}}, ["posto"]),
    _schema("calendario_posti_liberati", "Posti disdetti disponibili per la lista d'attesa.", {"esame": {"type": "string"}}, []),
]


def esegui(nome: str, args: dict, who: dict = SISTEMA) -> dict:
    """Esegue uno strumento: transazione propria, poi rilettura con una connessione nuova. Non dichiara mai riuscita
    un'operazione che non lo è: {"ok": False, "errore": ...} se è stata rifiutata, {"ok": False, "verifica": [...]} se
    il salvataggio non torna."""
    args = args or {}
    letture = {"calendario_consulta": lambda c: consulta(c, args.get("esame"), args.get("dal"), args.get("al"), stato=args.get("stato"), limite=200),
               "calendario_posto_libero": lambda c: e_libero(c, slot_id=args.get("posto")),
               "calendario_disponibilita": lambda c: disponibilita(c, args.get("esame"), args.get("dal")),
               "calendario_posti_liberati": lambda c: posti_liberati(c, args.get("esame"))}
    if nome in letture:
        with db.tx() as con:
            return {"ok": True, "dati": letture[nome](con)}
    if nome in ("calendario_sposta", "calendario_cancella", "calendario_blocca") and not args.get("richiesta_dal_paziente"):
        # una prenotazione fissata non si cambia senza il paziente: si apre la procedura di modifica (app/modifiche.py)
        from . import modifiche
        try:
            with db.tx() as con:
                if nome == "calendario_blocca":
                    r = modifiche.blocca(con, args.get("posto"), args.get("motivo") or "ambulatorio non disponibile", who)
                else:
                    r = modifiche.richiedi(con, args.get("prenotazione"), "sposta" if nome == "calendario_sposta" else "annulla",
                                           args.get("motivo") or "", who, args.get("posto"), versione=args.get("versione"))
        except Exception as e:
            with db.tx() as con:
                _log(con, who, nome, args.get("posto"), args.get("prenotazione"), "rifiutata", f"{type(e).__name__}: {e}")
            return {"ok": False, "errore": str(e)}
        if r is None:
            return {"ok": True, "verificato": True, "fatto": "posto chiuso"}
        with db.tx() as con:
            prob = modifiche.verifica_richiesta(con, r["id"])
        if prob:
            return {"ok": False, "verifica": prob, "richiesta": r["id"]}
        return {"ok": True, "in_attesa": True, "richiesta": r["id"], "posto_tenuto": r["to_slot"],
                "nota": "modifica proposta: posto tenuto, ambulatorio avvisato, chiamata al paziente da approvare"}
    paziente = bool(args.get("richiesta_dal_paziente"))
    try:
        with db.tx() as con:
            if nome == "calendario_prenota":
                appt = prenota(con, args.get("paziente"), args.get("posto"), who, "da confermare" if args.get("da_confermare") else "prenotata",
                               args.get("sostituisce"), args.get("versione"))
                atteso, extra = ("da confermare" if args.get("da_confermare") else "prenotata"), {"prenotazione": appt}
            elif nome == "calendario_conferma":
                appt = args.get("prenotazione"); conferma(con, appt, who); atteso, extra = "prenotata", {}
            elif nome == "calendario_sposta":
                appt = args.get("prenotazione"); fs = sposta(con, appt, args.get("posto"), who, args.get("versione"))
                atteso, extra = None, {"posto_liberato": fs}
            elif nome == "calendario_cancella":
                appt = args.get("prenotazione"); fs = cancella(con, appt, who, args.get("motivo") or "")
                atteso, extra = "annullata", {"posto_liberato": fs}
            else:
                return {"ok": False, "errore": f"strumento sconosciuto: {nome}"}
    except Exception as e:  # rifiutata dai controlli o errore del database: la transazione non è stata confermata
        with db.tx() as con:
            _log(con, who, nome, args.get("posto"), args.get("prenotazione"), "rifiutata", f"{type(e).__name__}: {e}")
        return {"ok": False, "errore": str(e)}
    with db.tx() as con:  # rilettura con una connessione nuova: è salvato davvero?
        prob = verifica_prenotazione(con, appt, atteso)
        _log(con, who, nome, None, appt, "verificata" if not prob else "verifica non superata", "; ".join(prob) or "salvataggio riletto: torna")
    if prob:
        return {"ok": False, "verifica": prob, **extra}
    if paziente and nome in ("calendario_sposta", "calendario_cancella"):  # la chiede il paziente: si applica, e si avvisa l'ambulatorio
        from . import modifiche
        with db.tx() as con:
            modifiche.avvisa_richiesta_paziente(con, appt, nome, who)
    return {"ok": True, "verificato": True, **extra}


# ------------------------------------------------------------------ popolamento sintetico

def _orari(primo: str, ultimo: str, minuti: int) -> list[str]:
    t, end, out = datetime.strptime(primo, "%H:%M"), datetime.strptime(ultimo, "%H:%M"), []
    while t <= end:
        out.append(t.strftime("%H:%M"))
        t += timedelta(minutes=minuti)
    return out


def popola(con, giorni: int = GIORNI_AGENDA, seme: int = 22102026) -> dict:
    """Costruisce le agende dai dati del dataset sintetico. Si può rifare: gli id dei posti sono fissi (esame, luogo, ora),
    quindi niente doppioni. Ordine: griglia delle agende, poi le prenotazioni del dataset nei loro posti, poi i posti
    disdetti (liberi), infine le prenotazioni di prova di altre persone (occupant='prova') con un seme fisso."""
    start = _today()
    rnd = random.Random(seme)
    rows = []
    for service, (spec, place, wd, primo, ultimo, minuti, _) in AGENDE.items():
        for i in range(giorni + 1):
            d = start + timedelta(days=i)
            if d.weekday() not in wd:
                continue
            for h in _orari(primo, ultimo, minuti):
                at = f"{d.isoformat()} {h}"
                rows.append((_slot_id(service, place, at), service, spec, place, at, minuti, "libero", None, None,
                             "sintetico: agenda generata da calendario.AGENDE", 1, 0, db.now()))
    before = con.execute("SELECT COUNT(*) FROM cal_slots").fetchone()[0]
    con.executemany("INSERT OR IGNORE INTO cal_slots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    # le prenotazioni del dataset: ognuna nel suo posto (se l'orario non è nella griglia, il posto si aggiunge)
    for a in [dict(r) for r in con.execute("SELECT * FROM appointments WHERE status IN ('prenotata','da confermare') ORDER BY at")]:
        sid = _slot_id(a["service"], a["place"], a["at"])
        if not posto(con, slot_id=sid):
            m = AGENDE.get(a["service"], (None, None, None, None, None, 30))[5]
            con.execute("INSERT INTO cal_slots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (sid, a["service"], a["specialty"], a["place"], a["at"], m,
                        "libero", None, None, "sintetico: posto della prenotazione del dataset (seed.py)", 1, 0, db.now()))
        s = posto(con, slot_id=sid)
        if s["appointment_id"] == a["id"]:
            continue
        if s["appointment_id"] or s["status"] != "libero":
            if s["occupant"] == "prova":  # una prova occupava l'orario di una prenotazione vera: la prova si toglie
                con.execute("UPDATE cal_slots SET status='libero', occupant=NULL WHERE id=?", (sid,))
            else:
                raise Errore(f"dataset incoerente: {a['id']} e {s['appointment_id']} nello stesso posto {sid}")
        con.execute("UPDATE cal_slots SET status=?, appointment_id=?, occupant='paziente', fonte=?, version=version+1, updated_at=? WHERE id=?",
                    ("tenuto" if a["status"] == "da confermare" else "prenotato", a["id"], "sintetico: prenotazione del dataset (seed.py)", db.now(), sid))
    # posti disdetti: liberi nel calendario
    freed = set()
    for f in [dict(r) for r in con.execute("SELECT * FROM freed_slots")]:
        sid = _slot_id(f["service"], f["place"], f["at"])
        freed.add(sid)
        if not posto(con, slot_id=sid):
            m = AGENDE.get(f["service"], (None, None, None, None, None, 30))[5]
            spec = AGENDE.get(f["service"], ("da indicare",))[0]
            con.execute("INSERT INTO cal_slots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (sid, f["service"], spec, f["place"], f["at"], m,
                        "libero", None, None, "sintetico: posto disdetto del dataset (seed.py)", 1, 0, db.now()))
        elif not f["assigned_to"]:
            con.execute("UPDATE cal_slots SET fonte='sintetico: posto disdetto del dataset (seed.py)' WHERE id=? AND appointment_id IS NULL", (sid,))
    # prenotazioni di prova di altre persone (nessun paziente inventato: il posto risulta solo occupato)
    occ = 0
    for s in [dict(r) for r in con.execute("SELECT id, service, status, appointment_id, occupant, version FROM cal_slots ORDER BY id")]:
        quota = AGENDE.get(s["service"], (0,) * 7)[6]
        r = rnd.random()  # un numero per posto, sempre nello stesso ordine: stesso risultato a ogni popolamento
        # solo posti mai toccati (versione 0): un posto liberato da una disdetta vera non torna occupato da una prova
        if s["status"] == "libero" and s["version"] == 0 and s["occupant"] is None and s["id"] not in freed and r < quota:
            con.execute("UPDATE cal_slots SET status='prenotato', occupant='prova', fonte=?, version=version+1, updated_at=? WHERE id=?",
                        ("sintetico: prenotazione di prova (nessuna persona reale)", db.now(), s["id"]))
            occ += 1
    after = con.execute("SELECT COUNT(*) FROM cal_slots").fetchone()[0]
    _log(con, SISTEMA, "popola", None, None, "ok", f"posti nuovi {after - before}, prenotazioni di prova nuove {occ}")
    return {"posti": after, "nuovi": after - before, "prove": occ, "incoerenze": coerenza(con)}
