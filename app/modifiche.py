"""Modifiche alle prenotazioni già fissate: spostamento o annullamento voluti dall'ospedale (ambulatorio chiuso,
medico assente, posto da liberare). Una prenotazione di una persona non si cambia mai senza di lei.

Passi, tutti in ospedale:
1. richiesta: Filo rilegge il calendario, sceglie (o riceve) il posto nuovo, lo TIENE (nessuno lo prende nel frattempo),
   manda una mail all'ambulatorio ("modifica proposta, in attesa della conferma del paziente") e prepara un caso per
   l'accettazione;
2. ogni chiamata al paziente parte solo con l'approvazione di una persona (come per i posti liberati);
3. il paziente conferma: Filo applica la modifica nel calendario, RILEGGE, e solo se torna manda la mail all'ambulatorio
   ("modifica confermata") e la conferma al paziente (nuova data, o annullamento);
4. il paziente non conferma (rifiuta, vuole un'altra data): resta la data di prima, posto nuovo liberato, mail
   all'ambulatorio. Nessuna risposta: si può richiamare (nuova approvazione); dopo MAX_CHIAMATE si chiude così;
5. errore di salvataggio o rilettura che non torna: nessuna mail di conferma, posto nuovo ancora tenuto, caso
   "da controllare a mano".
Un posto chiuso dall'ambulatorio con una prenotazione dentro (blocca) passa da qui: se il paziente non accetta, il
caso va a mano, perché la data di prima non c'è più.
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta

from . import calendario, db

ATTIVE = ("da_approvare", "in_chiamata", "esito", "senza_esito", "errore")
MAX_CHIAMATE = 2
STATO = {"da_approvare": "Chiamata al paziente da approvare", "in_chiamata": "Chiamata approvata: si aspetta la risposta",
         "esito": "Risposta in registrazione", "senza_esito": "Senza risposta: da decidere", "errore": "Errore: da controllare a mano",
         "confermata": "Confermata dal paziente", "rifiutata": "Non accettata: resta la data di prima",
         "altra_data": "Vuole un'altra data: resta la data di prima", "nessuna_risposta": "Nessuna risposta: resta la data di prima",
         "ritirata": "Ritirata", "scaduta": "Scaduta"}
AGENTE = {"username": "filo-modifiche", "name": "procedura delle modifiche"}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS cal_changes (
  id TEXT PRIMARY KEY, appointment_id TEXT, patient_id TEXT, tipo TEXT CHECK(tipo IN ('sposta','annulla')), from_slot TEXT, to_slot TEXT,
  motivo TEXT, blocca INTEGER DEFAULT 0, status TEXT, calls INTEGER DEFAULT 0, call_id TEXT, case_id TEXT, created_by TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS cal_changes_una_per_prenotazione ON cal_changes(appointment_id) WHERE status IN {ATTIVE};
CREATE UNIQUE INDEX IF NOT EXISTS cal_changes_un_posto_tenuto ON cal_changes(to_slot) WHERE to_slot IS NOT NULL AND status IN {ATTIVE};
CREATE TABLE IF NOT EXISTS cal_change_events (change_id TEXT, ts TEXT, who TEXT, step TEXT, text TEXT);
"""


class Errore(Exception):
    pass


def _today() -> date:
    return calendario._today()


def evento(con, cid: str, who: dict, step: str, text: str):
    con.execute("INSERT INTO cal_change_events VALUES (?,?,?,?,?)", (cid, db.now(), who["username"], step, text[:600]))


def _ch(con, cid: str) -> dict | None:
    r = con.execute("SELECT * FROM cal_changes WHERE id=?", (cid,)).fetchone()
    return dict(r) if r else None


def _appt(con, aid: str) -> dict:
    return dict(con.execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone())


def _patient(con, pid: str) -> dict:
    return dict(con.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone())


def _quando(at: str) -> str:
    from .cup_mail import data_estesa
    return data_estesa(at)


# ------------------------------------------------------------------ mail all'ospedale

def ambulatorio(a: dict) -> str:
    return f"ambulatorio {a.get('specialty') or 'da indicare'} ({a['place']})"


def mail_ospedale(con, ch: dict, oggetto: str, testo: str):
    """Avviso all'ambulatorio della visita. Resta in Posta in uscita come tutte le mail interne di Filo."""
    from .actions import _mail
    a = _appt(con, ch["appointment_id"])
    to = ambulatorio(a)
    _mail(con, ch["id"], "segreteria", to, oggetto, testo + "\n\nCentrale Operativa Territoriale (messaggio generato da Filo).")
    evento(con, ch["id"], AGENTE, "mail ospedale", f"Mail a {to}: {oggetto}.")


def _descrivi(con, ch: dict) -> tuple[dict, dict, dict | None, str]:
    a = _appt(con, ch["appointment_id"])
    p = _patient(con, ch["patient_id"])
    nuovo = calendario.posto(con, slot_id=ch["to_slot"]) if ch["to_slot"] else None
    if ch["tipo"] == "sposta":
        cosa = f"{a['service']} di {p['name']} da {_quando(a['at'])} a {_quando(nuovo['at'])} ({nuovo['place']})"
    else:
        cosa = f"annullamento della {a['service']} di {p['name']} del {_quando(a['at'])}"
    return a, p, nuovo, cosa


# ------------------------------------------------------------------ 1. richiesta

def primo_posto(con, a: dict, escludi=()) -> dict | None:
    """Il primo posto libero adatto: stesso esame, da domani, stesso luogo se possibile, senza sovrapposizioni per la persona."""
    dal = (_today() + timedelta(days=1)).isoformat()
    liberi = [s for s in calendario.disponibilita(con, a["service"], dal=dal, limite=500) if s["id"] not in escludi]
    liberi.sort(key=lambda s: (s["place"] != a["place"], s["at"]))
    for s in liberi:
        if not calendario._sovrapposizioni(con, a["patient_id"], s, escludi={a["id"]}):
            return s
    return None


def richiedi(con, appointment_id: str, tipo: str, motivo: str, who: dict, to_slot: str | None = None, blocca: bool = False,
             versione: int | None = None) -> dict:
    """Apre la richiesta di modifica (dentro la transazione di chi chiama). Non cambia la prenotazione."""
    if tipo not in ("sposta", "annulla"):
        raise Errore("tipo di modifica non valido")
    a = con.execute("SELECT * FROM appointments WHERE id=?", (appointment_id or "",)).fetchone()
    if not a:
        raise Errore("prenotazione sconosciuta")
    a = dict(a)
    if a["status"] != "prenotata":
        raise Errore(f"si modificano solo le prenotazioni fissate (questa è '{a['status']}')")
    if datetime.fromisoformat(a["at"]).date() < _today():
        raise Errore("la visita è già passata")
    _, da = calendario._posto_di(con, appointment_id)
    if con.execute(f"SELECT 1 FROM cal_changes WHERE appointment_id=? AND status IN {ATTIVE}", (appointment_id,)).fetchone():
        raise Errore("per questa prenotazione c'è già una modifica in corso")
    nuovo = None
    if tipo == "sposta":
        if to_slot:
            ok = calendario.e_libero(con, slot_id=to_slot)
            if not ok["libero"]:
                raise Errore(f"posto di arrivo non disponibile: {ok['motivo']}")
            nuovo = ok["posto"]
            if nuovo["service"] != a["service"]:
                raise Errore(f"il posto di arrivo è per {nuovo['service']}, non per {a['service']}")
            sopra = calendario._sovrapposizioni(con, a["patient_id"], nuovo, escludi={a["id"]})
            if sopra:
                raise Errore(f"sovrapposizione: alla nuova ora la persona ha {sopra[0]['service']} il {sopra[0]['at']}")
        else:
            nuovo = primo_posto(con, a, escludi={da["id"]})
            if not nuovo:
                raise Errore("nessun posto libero adatto per spostare la visita")
            versione = nuovo["version"]
        calendario.tieni(con, nuovo["id"], who, versione)
    cid = "MO-" + uuid.uuid4().hex[:8]
    con.execute("INSERT INTO cal_changes (id, appointment_id, patient_id, tipo, from_slot, to_slot, motivo, blocca, status, calls, "
                "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, a["id"], a["patient_id"], tipo, da["id"], nuovo["id"] if nuovo else None, (motivo or "").strip()[:300] or "non indicato",
                 int(blocca), "da_approvare", 0, who["username"], db.now(), db.now()))
    ch = _ch(con, cid)
    _, p, _, cosa = _descrivi(con, ch)
    evento(con, cid, who, "richiesta", f"Proposta di {cosa}. Motivo: {ch['motivo']}."
           + (f" Posto nuovo tenuto nel calendario ({nuovo['id']})." if nuovo else ""))
    mail_ospedale(con, ch, f"Modifica proposta - {p['name']} - {a['service']} del {a['at'][:10]}",
                  f"Proposta di {cosa}.\nMotivo: {ch['motivo']}.\n"
                  + (f"Il posto del {nuovo['at']} è tenuto nel calendario. " if nuovo else "")
                  + "La modifica NON è ancora valida: aspettiamo la conferma del paziente al telefono. Vi scriviamo di nuovo con l'esito.")
    _salva_caso(con, ch)
    return ch


def verifica_richiesta(con, cid: str) -> list[str]:
    ch = _ch(con, cid)
    if not ch:
        return ["richiesta non trovata"]
    prob = []
    a = _appt(con, ch["appointment_id"])
    if a["status"] != "prenotata":
        prob.append("la prenotazione non è più fissata")
    if ch["to_slot"]:
        s = calendario.posto(con, slot_id=ch["to_slot"])
        if not s or s["status"] != "tenuto" or s["occupant"] != "modifica":
            prob.append("il posto nuovo non risulta tenuto nel calendario")
    if not con.execute("SELECT 1 FROM cases WHERE kind='modifica' AND status='pending' AND json_extract(payload,'$.change')=?", (cid,)).fetchone():
        prob.append("manca il caso da approvare per la chiamata")
    return prob


def _caso(con, ch: dict) -> dict:
    from . import gateway
    a, p, nuovo, cosa = _descrivi(con, ch)
    pr = gateway.profile(con, p["id"])
    d = gateway._days(a["at"])
    s, parts = gateway.score(pr, gateway._urg(d), 10, 16)
    retry = ch["status"] == "senza_esito"
    return dict(role="segreteria", kind="modifica", patient=pr, score=s, parts=parts, conf="alta",
                tag="Modifica prenotazione: nuovo tentativo" if retry else "Modifica prenotazione",
                summary=f"Proposta di {cosa}. Motivo: {ch['motivo']}. L'ambulatorio è stato avvisato."
                        + (f" La chiamata precedente non ha avuto risposta ({ch['calls']} di {MAX_CHIAMATE})." if retry else ""),
                action="Approvando autorizzi UNA chiamata dell'agente vocale per chiedere al paziente se conferma la modifica. "
                       "Finché non risponde la data di prima resta valida" + (" e il posto nuovo resta tenuto" if nuovo else "")
                       + ". Se conferma, Filo cambia il calendario, lo rilegge e scrive all'ambulatorio e al paziente. "
                       + ("Scartando, si registra che non ha risposto e la modifica si chiude." if retry else
                          "Scartando, la modifica si ritira: resta la data di prima e l'ambulatorio viene avvisato."),
                evidence=[[a["id"], "Calendario · prenotazione attuale", f"{a['service']} {a['at']}, {a['place']}"]]
                         + ([[nuovo["id"], "Calendario · posto proposto (tenuto)", f"{nuovo['service']} {nuovo['at']}, {nuovo['place']}"]] if nuovo else []),
                role_note=None, ckey=f"modifica|{ch['id']}|{ch['calls']}",
                payload={"change": ch["id"], "tentativo": ch["calls"] + 1})


def _salva_caso(con, ch: dict):
    c = _caso(con, ch)
    if con.execute("SELECT 1 FROM cases WHERE ckey=?", (c["ckey"],)).fetchone():
        return
    cid = "MC-" + uuid.uuid4().hex[:8]
    con.execute("INSERT INTO cases (id,run_id,patient_id,role,kind,score,parts,confidence,tag,summary,action,evidence,role_note,payload,ckey) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, "calendario", c["patient"]["id"], c["role"], c["kind"], c["score"], json.dumps(c["parts"]), c["conf"], c["tag"],
                 c["summary"], c["action"], json.dumps(c["evidence"], ensure_ascii=False), None, json.dumps(c["payload"]), c["ckey"]))


def _caso_a_mano(con, ch: dict, perche: str):
    from . import gateway
    a, p, nuovo, cosa = _descrivi(con, ch)
    pr = gateway.profile(con, p["id"])
    s, parts = gateway.score(pr, 30, 14, 10)
    con.execute("INSERT INTO cases (id,run_id,patient_id,role,kind,score,parts,confidence,tag,summary,action,evidence,role_note,payload,ckey) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("MC-" + uuid.uuid4().hex[:8], "calendario", p["id"], "segreteria", "modifica_errore", s, json.dumps(parts), "bassa",
                 "Da controllare a mano", f"Modifica: {cosa}. {perche}",
                 "Controllare il calendario e la prenotazione dalla pagina Calendario (sezione Modifiche): riprovare o ritirare la modifica.",
                 json.dumps([[a["id"], "Calendario · prenotazione", f"{a['service']} {a['at']} ({a['status']})"]], ensure_ascii=False),
                 None, json.dumps({"change": ch["id"]}), f"modifica_errore|{ch['id']}|{uuid.uuid4().hex[:4]}"))


# ------------------------------------------------------------------ 2. approvazione = via libera a UNA chiamata

def approva(con, case: dict, p: dict, user: dict) -> list[str]:
    from .voce import chiamata as chv
    pl = json.loads(case["payload"])
    ch = _ch(con, pl.get("change") or "")
    if not ch or ch["status"] not in ("da_approvare", "senza_esito"):
        raise ValueError("Questa modifica non è più aperta.")
    prob = [x for x in verifica_richiesta(con, ch["id"]) if not x.startswith("manca il caso")]
    if prob:  # il calendario si rilegge adesso: se nel frattempo è cambiato, niente chiamata
        raise ValueError("Il calendario non torna con la modifica: " + "; ".join(prob))
    call = chv.crea(con, p["id"], "modifica", ch["id"], user)
    con.execute("UPDATE cal_changes SET status='in_chiamata', call_id=?, case_id=?, calls=calls+1, updated_at=? WHERE id=?",
                (call, case["id"], db.now(), ch["id"]))
    con.execute("UPDATE cases SET payload=? WHERE id=?", (json.dumps({**pl, "call": call}), case["id"]))
    evento(con, ch["id"], user, "approvata", f"{user['name']} approva la chiamata {call} a {p['name']} (tentativo {ch['calls'] + 1}).")
    return [f"chiamata {call} a {p['name']} approvata: si avvia dalla pagina della chiamata"]


def _supera_casi(con, ch: dict, who: dict):
    con.execute("UPDATE cases SET status='superato', decided_by=?, decided_at=? WHERE status='pending' AND kind='modifica' "
                "AND json_extract(payload,'$.change')=?", (who["username"], db.now(), ch["id"]))


def _chiudi(con, ch: dict, status: str, who: dict, text: str, mail: bool = True):
    """Chiude senza applicare: la data di prima resta, il posto nuovo torna libero."""
    if ch["to_slot"]:
        s = calendario.posto(con, slot_id=ch["to_slot"])
        if s and s["status"] == "tenuto" and s["occupant"] == "modifica":
            calendario.rilascia(con, ch["to_slot"], who)
    con.execute("UPDATE cal_changes SET status=?, updated_at=? WHERE id=?", (status, db.now(), ch["id"]))
    _supera_casi(con, ch, who)
    evento(con, ch["id"], who, status.replace("_", " "), text)
    a, p, _, cosa = _descrivi(con, ch)
    if mail:
        mail_ospedale(con, ch, f"Modifica NON confermata - {p['name']} - {a['service']} del {a['at'][:10]}",
                      f"La proposta di {cosa} non è stata confermata ({STATO[status].lower()}).\n"
                      + ("ATTENZIONE: avete chiesto di chiudere il posto di prima, quindi la visita va riorganizzata a mano con il paziente." if ch["blocca"] else
                         "Resta valida la prenotazione di prima; il posto proposto torna libero."))
    if ch["blocca"]:
        _caso_a_mano(con, ch, "Il paziente non ha confermato, ma l'ambulatorio ha chiesto di chiudere il posto di prima: "
                              "la visita va riorganizzata a mano (il posto resta aperto finché non si decide).")


def ritira(case_or_change: str, user: dict, motivo: str = ""):
    with db.tx() as con:
        ch = _ch(con, case_or_change)
        if not ch:
            c = con.execute("SELECT payload FROM cases WHERE id=?", (case_or_change,)).fetchone()
            ch = _ch(con, json.loads(c["payload"] or "{}").get("change") or "") if c else None
        if not ch or ch["status"] not in ("da_approvare", "senza_esito", "errore"):
            raise ValueError("Questa modifica non si può più ritirare.")
        if ch["status"] == "senza_esito":
            _chiudi(con, ch, "nessuna_risposta", user, f"{user['name']} non fa richiamare: resta la data di prima ({motivo or 'nessun motivo'}).")
        else:
            _chiudi(con, ch, "ritirata", user, f"{user['name']} ritira la modifica ({motivo or 'nessun motivo'}).")
        con.execute("UPDATE cases SET status='ok', decided_by=?, decided_at=? WHERE status='pending' AND kind='modifica_errore' "
                    "AND json_extract(payload,'$.change')=?", (user["username"], db.now(), ch["id"]))


# ------------------------------------------------------------------ 3-4. esito della chiamata

def esito_chiamata(cid: str) -> bool:
    with db.tx() as con:
        ch = con.execute("SELECT * FROM cal_changes WHERE call_id=?", (cid,)).fetchone()
        if not ch:
            return False
        if con.execute("UPDATE cal_changes SET status='esito', updated_at=? WHERE id=? AND status='in_chiamata'",
                       (db.now(), ch["id"])).rowcount != 1:
            return True  # già applicato: una seconda fine chiamata non rifà nulla
        ch = {**dict(ch), "status": "esito"}
        call = dict(con.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone())
    scelta = (json.loads(call["outcome"] or "{}").get("modifica") or {}).get("scelta")
    if scelta == "accetta":
        applica(ch, AGENTE)
    elif scelta in ("rifiuta", "vuole_altra_data"):
        with db.tx() as con:
            _chiudi(con, ch, "rifiutata" if scelta == "rifiuta" else "altra_data", AGENTE,
                    f"Il paziente non conferma la modifica (chiamata {cid}): resta la data di prima.")
    else:
        perche = {"identita_non_confermata": "identità non confermata", "allarme": "segnale d'allarme durante la chiamata (già passato al medico)"}.get(
            call["status"], "nessuna risposta registrata")
        with db.tx() as con:
            if ch["calls"] >= MAX_CHIAMATE:
                _chiudi(con, ch, "nessuna_risposta", AGENTE, f"{perche} anche al tentativo {ch['calls']} di {MAX_CHIAMATE}: resta la data di prima.")
            else:
                con.execute("UPDATE cal_changes SET status='senza_esito', updated_at=? WHERE id=?", (db.now(), ch["id"]))
                ch["status"] = "senza_esito"
                evento(con, ch["id"], AGENTE, "senza esito", f"{perche} (chiamata {cid}). La data di prima resta valida"
                       + (" e il posto nuovo resta tenuto" if ch["to_slot"] else "") + ": l'accettazione decide se richiamare.")
                _salva_caso(con, ch)
    return True


def _applica_nel_calendario(con, ch: dict) -> list[str]:
    """Tutto o niente, nella transazione di chi chiama."""
    fatti = []
    if ch["tipo"] == "sposta":
        calendario.rilascia(con, ch["to_slot"], AGENTE)
        fs = calendario.sposta(con, ch["appointment_id"], ch["to_slot"], AGENTE, libera_per_lista=not ch["blocca"])
        if fs:
            fatti.append(f"il posto di prima passa alla lista d'attesa ({fs})")
    else:
        fs = calendario.cancella(con, ch["appointment_id"], AGENTE, ch["motivo"], alla_lista=not ch["blocca"])
        if fs:
            fatti.append(f"il posto passa alla lista d'attesa ({fs})")
    if ch["blocca"]:
        calendario.chiudi_posto(con, ch["from_slot"], AGENTE, ch["motivo"])
        fatti.append("il posto di prima è chiuso come chiesto dall'ambulatorio")
    con.execute("UPDATE cal_changes SET status='confermata', updated_at=? WHERE id=? AND status IN ('esito','errore')", (db.now(), ch["id"]))
    return fatti


def verifica_modifica(con, ch: dict) -> list[str]:
    a = _appt(con, ch["appointment_id"])
    prob = []
    if ch["tipo"] == "sposta":
        nuovo = calendario.posto(con, slot_id=ch["to_slot"])
        if a["status"] != "prenotata" or a["at"] != nuovo["at"] or a["place"] != nuovo["place"]:
            prob.append("la prenotazione non risulta alla data nuova")
        prob += calendario.verifica_prenotazione(con, a["id"], "prenotata")
    else:
        if a["status"] != "annullata":
            prob.append("la prenotazione non risulta annullata")
        prob += calendario.verifica_prenotazione(con, a["id"])
    vecchio = calendario.posto(con, slot_id=ch["from_slot"])
    atteso = "bloccato" if ch["blocca"] else "libero"
    if vecchio["status"] != atteso or vecchio["appointment_id"]:
        prob.append(f"il posto di prima è '{vecchio['status']}', atteso '{atteso}'")
    if con.execute("SELECT status FROM cal_changes WHERE id=?", (ch["id"],)).fetchone()[0] != "confermata":
        prob.append("la modifica non risulta confermata")
    return list(dict.fromkeys(prob))


def applica(ch: dict, who: dict = AGENTE):
    from . import cup_mail
    from .actions import _mail
    try:
        with db.tx() as con:
            fatti = _applica_nel_calendario(con, ch)
            evento(con, ch["id"], who, "applicata", "Il paziente conferma: calendario aggiornato" + "".join(f"; {f}" for f in fatti) + ".")
    except Exception as e:
        _errore(ch, f"errore durante il salvataggio ({type(e).__name__}: {str(e)[:120]})")
        return
    with db.tx() as con:
        prob = verifica_modifica(con, ch)
    if prob:
        _errore(ch, "rilettura del calendario non superata: " + "; ".join(prob))
        return
    with db.tx() as con:
        a, p, _, cosa = _descrivi(con, ch)
        evento(con, ch["id"], AGENTE, "verificata", "Calendario riletto: la modifica è salvata e coerente.")
        mail_ospedale(con, ch, f"Modifica CONFERMATA - {p['name']} - {a['service']}",
                      f"Il paziente ha confermato al telefono: {cosa}.\nIl calendario è aggiornato e riletto."
                      + (" Il posto di prima passa alla lista d'attesa." if not ch["blocca"] else " Il posto di prima è chiuso."))
        try:
            if ch["tipo"] == "sposta":
                prio = con.execute("SELECT priority FROM waitlist WHERE patient_id=? AND service=?", (p["id"], a["service"])).fetchone()
                msg = cup_mail.manda(con, ch["call_id"] or ch["id"], "segreteria", p, a, prio[0] if prio else None)
            else:
                msg = _mail(con, ch["call_id"] or ch["id"], "segreteria", cup_mail.destinatario(p),
                            f"Annullamento prenotazione - {a['service']} del {a['at'][:10]}",
                            f"Gentile {p['name']},\ncome concordato al telefono la {a['service']} del {_quando(a['at'])} è annullata "
                            f"({ch['motivo']}).\nPer una nuova data chiami il CUP al {cup_mail.NUMERO_CUP}.\n\n"
                            "Messaggio di PROVA generato dal prototipo Filo. Dati ed ente inventati.", sender=cup_mail.MITTENTE)
            evento(con, ch["id"], AGENTE, "mail paziente", f"Conferma al paziente: {msg}.")
        except Exception as e:
            evento(con, ch["id"], AGENTE, "errore mail", f"Modifica salvata, ma la mail al paziente NON è partita ({str(e)[:120]}): va mandata a mano.")
        con.execute("INSERT INTO action_log VALUES (?,?,?,?)", (db.now(), AGENTE["username"], ch["call_id"] or ch["id"],
                    f"{p['name']}: {cosa} confermata al telefono; calendario verificato, ambulatorio e paziente avvisati"))
        from . import posti
        posti.avanza(con)  # il posto lasciato può andare subito alla lista d'attesa


def _errore(ch: dict, perche: str):
    with db.tx() as con:
        con.execute("UPDATE cal_changes SET status='errore', updated_at=? WHERE id=?", (db.now(), ch["id"]))
        evento(con, ch["id"], AGENTE, "errore aggiornamento", f"Il paziente ha confermato, ma {perche}. Nessuna mail di conferma: da controllare a mano.")
        _caso_a_mano(con, ch, f"Il paziente ha confermato al telefono, ma {perche}. Nessuna mail di conferma è partita.")


# ------------------------------------------------------------------ a mano (pagina Calendario)

def esito_a_mano(cid: str, scelta: str, user: dict) -> str:
    from .voce import chiamata as chv
    if scelta not in ("accetta", "rifiuta", "nessuna_risposta"):
        raise ValueError("Esito non valido.")
    with db.tx() as con:
        ch = _ch(con, cid)
        if not ch or ch["status"] != "in_chiamata":
            raise ValueError("Questa modifica non aspetta una risposta.")
        c = chv.carica(con, ch["call_id"])
        out = c["outcome"]
        if scelta != "nessuna_risposta":
            out["modifica"] = {"scelta": scelta}
        con.execute("UPDATE calls SET outcome=? WHERE id=?", (json.dumps(out, ensure_ascii=False), c["id"]))
        evento(con, cid, user, "esito a mano", f"{user['name']} registra l'esito: {scelta.replace('_', ' ')}.")
        ended = bool(c["ended_at"])
    if ended:
        esito_chiamata(ch["call_id"])
    else:
        chv.concludi(ch["call_id"])
    return "esito registrato"


def riprova(cid: str, user: dict) -> str:
    with db.tx() as con:
        ch = _ch(con, cid)
        if not ch or ch["status"] != "errore":
            raise ValueError("Niente da riprovare per questa modifica.")
        evento(con, cid, user, "riprova", f"{user['name']} riprova ad applicare la modifica.")
    applica(ch, user)
    with db.tx() as con:
        return STATO[_ch(con, cid)["status"]]


# ------------------------------------------------------------------ posto chiuso dall'ambulatorio

def blocca(con, slot_id: str, motivo: str, who: dict) -> dict | None:
    """Chiude un posto. Libero o di prova: si chiude subito. Con la prenotazione di una persona: si propone lo spostamento
    al primo posto libero (o l'annullamento se non ce ne sono), e il posto si chiude solo quando il paziente conferma."""
    s = calendario.posto(con, slot_id=slot_id)
    if not s:
        raise Errore("posto inesistente")
    if not s["appointment_id"]:
        calendario.chiudi_posto(con, slot_id, who, motivo)
        return None
    a = _appt(con, s["appointment_id"])
    if a["status"] != "prenotata":
        raise Errore("nel posto c'è una proposta in attesa di risposta: riprova dopo")
    tipo = "sposta" if primo_posto(con, a, escludi={slot_id}) else "annulla"
    return richiedi(con, a["id"], tipo, motivo, who, blocca=True)


# ------------------------------------------------------------------ disdetta chiesta dal paziente

def avvisa_richiesta_paziente(con, appointment_id: str, nome: str, who: dict):
    """La modifica l'ha chiesta il paziente (sportello, telefono): si applica subito, e l'ambulatorio viene avvisato."""
    from .actions import _mail
    a = _appt(con, appointment_id)
    p = _patient(con, a["patient_id"])
    cosa = (f"disdetta della {a['service']} del {_quando(a['at'])}" if nome == "calendario_cancella"
            else f"{a['service']} spostata al {_quando(a['at'])}")
    _mail(con, appointment_id, "segreteria", ambulatorio(a), f"Modifica chiesta dal paziente - {p['name']} - {a['service']}",
          f"Su richiesta del paziente: {cosa}. Il calendario è aggiornato e riletto.\n\nCentrale Operativa Territoriale (messaggio generato da Filo).")
    calendario._log(con, who, "avviso ospedale", None, appointment_id, "ok", f"mail a {ambulatorio(a)}: {cosa}")


# ------------------------------------------------------------------ per la pagina

def quadro(con, limite: int = 30) -> list[dict]:
    out = []
    for r in con.execute("SELECT * FROM cal_changes ORDER BY created_at DESC LIMIT ?", (limite,)).fetchall():
        ch = dict(r)
        a, p, nuovo, cosa = _descrivi(con, ch)
        ch.update(patient=p["name"], cosa=cosa, stato=STATO.get(ch["status"], ch["status"]),
                  eventi=[dict(e) for e in con.execute("SELECT * FROM cal_change_events WHERE change_id=? ORDER BY rowid", (ch["id"],))])
        out.append(ch)
    return out
