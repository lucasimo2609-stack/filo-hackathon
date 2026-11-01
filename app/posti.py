"""Posti liberati: a chi proporli, in che ordine, e cosa succede dopo ogni chiamata. Tutto in ospedale.

Regole (scheda degli agenti, agente agende, compito B, versione del 23/10):
- l'ordine lo decide Filo, non il modello: prima la classe di priorità della ricetta (U, B, D, P), cioè il flag di
  gravità messo dal medico prescrittore e già scritto nella lista d'attesa (Filo non lo cambia mai); a parità di classe,
  cognome e poi nome in ordine alfabetico. Il pacchetto "posto_liberato" non va più al cloud: con un ordine fisso
  l'agente non avrebbe niente da decidere, e i cognomi non escono dall'ospedale;
- OGNI chiamata ha l'approvazione di una persona: Filo prepara la proposta (un caso per l'accettazione) e solo
  con il via libera prepara la chiamata dell'agente vocale. Dopo un rifiuto Filo prepara da solo la proposta per il
  successivo, che va approvata di nuovo;
- un posto ha al massimo UNA proposta aperta, e una persona ha al massimo una proposta aperta: lo garantiscono due
  indici unici del database, non solo il codice;
- con l'accettazione: prenotazione nuova confermata, vecchia data annullata (e il suo posto torna libero), richiesta
  tolta dalla lista d'attesa, poi Filo RILEGGE il database e solo se tutto torna manda la mail di conferma;
- ogni passo (proposta, approvazione, chiamata, risposta, aggiornamento, mail, errore) finisce in slot_events.
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
from datetime import datetime

from . import calendario
from . import cf as cfmod
from . import db

PRIORITA = {"U": 0, "B": 1, "D": 2, "P": 3}
PRIORITA_TESTO = {"U": "U (urgente)", "B": "B (breve)", "D": "D (differibile)", "P": "P (programmata)"}
MAX_CHIAMATE = 2  # chiamate senza esito alla stessa persona per lo stesso posto: dopo la seconda si passa al successivo
ATTIVE = ("da_approvare", "in_chiamata", "esito", "senza_esito", "errore")  # finché un'offerta è in uno di questi stati, il posto è tenuto
STATO = {"da_approvare": "Chiamata da approvare", "in_chiamata": "Chiamata approvata: si aspetta la risposta",
         "esito": "Risposta in registrazione",
         "senza_esito": "Chiamata senza risposta: da decidere", "accettato": "Ha accettato", "rifiutato": "Ha rifiutato",
         "altra_data": "Vuole un'altra data", "nessuna_risposta": "Non ha risposto", "scartato": "Saltato dall'accettazione",
         "errore": "Errore: da controllare a mano", "scaduto": "Posto passato senza risposta"}
AGENTE = {"username": "filo-posti", "name": "procedura dei posti liberati"}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS slot_offers (
  id TEXT PRIMARY KEY, slot_id TEXT, waitlist_id TEXT, patient_id TEXT, rank INTEGER, status TEXT,
  case_id TEXT, call_id TEXT, appointment_id TEXT, calls INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS slot_offers_una_per_posto ON slot_offers(slot_id) WHERE status IN {ATTIVE};
CREATE UNIQUE INDEX IF NOT EXISTS slot_offers_una_per_persona ON slot_offers(patient_id) WHERE status IN {ATTIVE};
CREATE TABLE IF NOT EXISTS slot_events (slot_id TEXT, offer_id TEXT, ts TEXT, who TEXT, step TEXT, text TEXT);
"""


class Errore(Exception):
    """Un aggiornamento che non torna: la transazione si annulla, nessuna mail parte."""


# ------------------------------------------------------------------ ordine

def cognome_nome(name: str, cf: str | None = None) -> tuple[str, str]:
    """Il nome è salvato in un campo solo, di solito "Nome Cognome" (documenti importati: anche "Cognome Nome").
    Se c'è il codice fiscale, le sue prime sei lettere dicono dove finisce il cognome; senza, vale "Nome Cognome"."""
    parts = (name or "").split()
    if len(parts) < 2:
        return (name or "").strip(), ""
    code = (cf or "").upper()[:6]
    if len(code) == 6:
        for i in range(1, len(parts)):
            a, b = " ".join(parts[:i]), " ".join(parts[i:])
            if cfmod.name_code(b, a) == code:
                return b, a
            if cfmod.name_code(a, b) == code:
                return a, b
    return " ".join(parts[1:]), parts[0]


def _alfa(s: str) -> str:
    """Per l'ordine alfabetico: senza accenti, maiuscole, spazi e apostrofi ("De Marchi" come "Demarchi")."""
    return "".join(c for c in unicodedata.normalize("NFKD", s or "").casefold() if c.isalpha())


def chiave(w: dict) -> tuple:
    """Priorità della ricetta, poi cognome, poi nome. In fondo, solo per avere sempre lo stesso ordine: da quando è in lista."""
    cognome, nome = cognome_nome(w["name"], w.get("cf"))
    return PRIORITA.get((w.get("priority") or "").strip().upper(), 9), _alfa(cognome), _alfa(nome), w.get("since") or "", w["id"]


def _lista(con, service: str) -> list[dict]:
    rows = con.execute("SELECT w.*, p.name, p.cf FROM waitlist w JOIN patients p ON p.id=w.patient_id WHERE w.service=?", (service,))
    return sorted((dict(r) for r in rows), key=chiave)


def _lista_posto(con, slot: dict) -> tuple[list[dict], dict]:
    """Chi conta per questo posto: chi è in lista (in attesa o con una proposta aperta) e chi l'ha già avuto proposto."""
    offers = {o["waitlist_id"]: dict(o) for o in con.execute("SELECT * FROM slot_offers WHERE slot_id=? ORDER BY created_at", (slot["id"],))}
    return [w for w in _lista(con, slot["service"]) if w["status"] in ("in attesa", "proposto") or w["id"] in offers], offers


def ordine(con, slot: dict) -> list[dict]:
    """La lista d'attesa di quell'esame in ordine, con lo stato di ognuno per questo posto (per la pagina Posti liberati)."""
    lista, offers = _lista_posto(con, slot)
    out = []
    for i, w in enumerate(lista, 1):
        o = offers.get(w["id"])
        st = STATO.get(o["status"], o["status"]) if o else ("Ha una proposta per un altro posto" if w["status"] == "proposto" else "In attesa del suo turno")
        out.append({**w, "pos": i, "offer": o, "stato": st})
    return out


def _prossimo(con, slot: dict) -> tuple[dict | None, int, str]:
    """Il primo in ordine che può ricevere la proposta. Ritorna (riga della lista, posizione, motivo se nessuno)."""
    lista, tried = _lista_posto(con, slot)
    busy = {r[0] for r in con.execute(f"SELECT patient_id FROM slot_offers WHERE status IN {ATTIVE}")}
    waiting = [w for w in lista if w["status"] == "in attesa" and w["id"] not in tried]
    liberi = [w for w in waiting if w["patient_id"] not in busy]
    if liberi:
        w = scegli(con, slot, liberi)
        return w, lista.index(w) + 1, ""
    if waiting:
        return None, 0, "candidati impegnati con la proposta di un altro posto: si riprova appena si liberano"
    return None, 0, "lista d'attesa esaurita"


# ------------------------------------------------------------------ chi prende il posto: l'agente Agende sceglie, Filo controlla

URG_MEDICO = {"alta": 0, "media": 1, "bassa": 2}
MAX_CANDIDATI = 8


def _fascia_lista(since: str | None) -> str:
    from . import gateway
    d = -gateway._days(since + " 00:00") if since else 0
    return "meno di 30 giorni" if d < 30 else ("30-90 giorni" if d <= 90 else "oltre 90 giorni")


def _candidato(con, slot: dict, w: dict) -> dict:
    """Quello che l'agente sa di un candidato: nessun nome, nessun codice fiscale, solo classi e fasce."""
    from . import gateway, trasporti, valutazione
    p = gateway.profile(con, w["patient_id"])
    v = valutazione.latest(con, w["patient_id"])
    puo = True
    if p.get("nocar"):
        puo = bool(trasporti.services_for(con, trasporti._profile(p), slot["at"]))
    gia = [a for a in calendario.prenotazioni_paziente(con, w["patient_id"], slot["service"]) if a["status"] == "prenotata"]
    if gia:
        g = gateway._days(gia[0]["at"])
        data = "entro 7 giorni" if g <= 7 else ("entro 30 giorni" if g <= 30 else "oltre 30 giorni")
    else:
        data = "nessuna"
    return {"priorita_ricetta": (w.get("priority") or "P").strip().upper()[:1], "urgenza_medico": v["urgency"] if v else "nessuna",
            "in_lista_da": _fascia_lista(w.get("since")), "puo_arrivare": puo, "data_gia_fissata": data}


def _ammessi(cands: dict) -> set[str]:
    """Regole fisse sopra la scelta dell'agente: chi può arrivare; la priorità della ricetta migliore presente, oppure
    un'urgenza alta del medico se nessuno con quella priorità migliore ce l'ha."""
    ok = {k: c for k, c in cands.items() if c["puo_arrivare"]} or dict(cands)
    best = min(PRIORITA.get(c["priorita_ricetta"], 9) for c in ok.values())
    alta_migliore = {PRIORITA.get(c["priorita_ricetta"], 9) for c in ok.values() if c["urgenza_medico"] == "alta"}
    out = set()
    for k, c in ok.items():
        cl = PRIORITA.get(c["priorita_ricetta"], 9)
        if cl == best or (c["urgenza_medico"] == "alta" and not any(x < cl for x in alta_migliore)):
            out.add(k)
    return out


def scegli(con, slot: dict, liberi: list[dict]) -> dict:
    """Con un solo candidato non c'è niente da scegliere. Con più candidati il pacchetto anonimo va all'agente Agende, che
    sceglie per urgenza; Filo controlla la scelta (_ammessi) e, se l'agente non risponde o sceglie fuori regola, usa l'ordine
    fisso (priorità della ricetta, poi cognome e nome). La chiamata parte comunque solo con il via libera dell'accettazione."""
    if len(liberi) == 1:
        return liberi[0]
    from . import agents, gateway
    scelti = liberi[:MAX_CANDIDATI]
    codici = {}
    for w in scelti:
        codici["C-" + uuid.uuid4().hex[:6]] = w
    cands = {k: _candidato(con, slot, w) for k, w in codici.items()}
    pacchetto = {"tipo": "posto_liberato", "esame": slot["service"], "giorni_al_posto": gateway._days(slot["at"]),
                 "candidati": [{"codice": k, **c} for k, c in cands.items()]}
    try:
        out, backend, ms = agents.call("agende", pacchetto)
        pulito, scarto = gateway.check_output("agende", out, pacchetto)
    except Exception as e:  # noqa: BLE001
        pulito, scarto, backend = None, f"agente non raggiungibile ({str(e)[:80]})", "nessuno"
    if pulito is None and backend.startswith("piattaforma"):  # flusso pubblicato senza le istruzioni del posto liberato: riserva
        try:
            pulito, scarto = gateway.check_output("agende", agents._call_api("agende", pacchetto), pacchetto)
            backend = "API diretta AI Factory, riserva"
        except Exception as e:  # noqa: BLE001
            scarto = f"{scarto}; riserva non riuscita ({str(e)[:60]})"
    k, testo = _valida(cands, list(codici), pulito, scarto, backend)
    _registra_scelta(con, slot, codici[k], cands[k], testo, len(cands))
    return codici[k]


def _valida(cands: dict, ordine_fisso: list[str], pulito: dict | None, scarto: str | None, backend: str) -> tuple[str, str]:
    """Regole fisse sopra la scelta dell'agente. ordine_fisso: i codici nell'ordine della ricetta (poi cognome e nome)."""
    ammessi = _ammessi(cands)
    fisso = next((k for k in ordine_fisso if k in ammessi), ordine_fisso[0])
    if pulito and pulito["codice_scelto"] in ammessi:
        return pulito["codice_scelto"], f"Agente Agende ({backend}): {pulito['motivo']}"
    if pulito:
        return fisso, (f"Regola locale fissa: l'agente proponeva un candidato che scavalca una priorità più alta senza urgenza "
                       f"alta del medico, o che non può arrivare ({pulito['motivo']}); vale l'ordine della ricetta.")
    return fisso, f"Ordine fisso (priorità della ricetta, poi cognome e nome): {scarto}."


def _registra_scelta(con, slot: dict, w: dict, c: dict, testo: str, n: int):
    evento(con, slot["id"], None, "agente agende" if testo.startswith("Agente") else AGENTE["username"], "scelta",
           f"{w['name']}: {testo} Candidato: priorità {c['priorita_ricetta']}, urgenza del medico {c['urgenza_medico']}, "
           f"in lista da {c['in_lista_da']}{'' if c['puo_arrivare'] else ', non può arrivare'}. Candidati valutati: {n}.")


# ------------------------------------------------------------------ registro

def evento(con, slot_id: str, offer_id: str | None, who: str, step: str, text: str):
    con.execute("INSERT INTO slot_events VALUES (?,?,?,?,?,?)", (slot_id, offer_id, db.now(), who, step, text))


def _evento_una_volta(con, slot_id: str, step: str, text: str):
    """Per gli stati che si ripetono a ogni elaborazione (lista esaurita, lista non leggibile): una riga sola finché non cambia."""
    last = con.execute("SELECT step, text FROM slot_events WHERE slot_id=? ORDER BY rowid DESC LIMIT 1", (slot_id,)).fetchone()
    if not last or (last["step"], last["text"]) != (step, text):
        evento(con, slot_id, None, AGENTE["username"], step, text)


def nota_chiamata(cid: str, step: str, text: str):
    """Avvio della chiamata, errori del telefono: se la chiamata è di un posto liberato, finisce anche nel suo registro."""
    with db.tx() as con:
        o = con.execute("SELECT id, slot_id FROM slot_offers WHERE call_id=?", (cid,)).fetchone()
        if o:
            evento(con, o["slot_id"], o["id"], "chiamata", step, text)


# ------------------------------------------------------------------ casi per l'accettazione

def _slot(con, sid) -> dict:
    return dict(con.execute("SELECT * FROM freed_slots WHERE id=?", (sid,)).fetchone())


def _offer(con, oid) -> dict | None:
    r = con.execute("SELECT * FROM slot_offers WHERE id=?", (oid,)).fetchone()
    return dict(r) if r else None


def _prima(con, slot_id: str, oid: str) -> str:
    rows = con.execute("SELECT o.status, p.name FROM slot_offers o JOIN patients p ON p.id=o.patient_id "
                       "WHERE o.slot_id=? AND o.id != ? ORDER BY o.created_at", (slot_id, oid)).fetchall()
    return "; ".join(f"{r['name']}: {STATO.get(r['status'], r['status']).lower()}" for r in rows)


def _caso(con, slot: dict, o: dict) -> dict:
    from . import gateway  # import qui: gateway usa questo modulo
    w = dict(con.execute("SELECT * FROM waitlist WHERE id=?", (o["waitlist_id"],)).fetchone())
    p = gateway.profile(con, o["patient_id"])
    d = gateway._days(slot["at"])
    s, parts = gateway.score(p, gateway._urg(d), 12 if w["priority"] in ("U", "B") else 6, 18)
    pari = sum(1 for x in _lista(con, slot["service"]) if x["priority"] == w["priority"] and x["status"] == "in attesa") > 1
    prima = _prima(con, slot["id"], o["id"])
    retry = o["status"] == "senza_esito"
    sc = con.execute("SELECT text FROM slot_events WHERE slot_id=? AND step='scelta' ORDER BY rowid DESC LIMIT 1", (slot["id"],)).fetchone()
    scelta = sc[0].split(": ", 1)[1] if sc and ": " in sc[0] else None
    summary = (f"Si è liberato un {slot['service'].replace('_', ' ')} il {slot['at'][:10]} alle {slot['at'][11:]} ({slot['place']}). "
               f"{p['name']} è {o['rank']}° in ordine: priorità {PRIORITA_TESTO.get(w['priority'], w['priority'] or 'non indicata')}"
               + (", e a parità di priorità vale l'ordine alfabetico (cognome, poi nome)." if pari else ".")
               + (f" Prima: {prima}." if prima else "")
               + (f" Perché lei/lui: {scelta}" if scelta else "")
               + (f" La chiamata precedente non ha avuto risposta ({o['calls']} di {MAX_CHIAMATE})." if retry else ""))
    action = ("Approvando autorizzi UNA chiamata dell'agente vocale a questa persona" + (" (un nuovo tentativo)" if retry else "")
              + ": finché non risponde il posto resta tenuto per lei e non viene proposto ad altri. Se accetta, Filo conferma la "
              "prenotazione, la toglie dalla lista d'attesa e manda la mail di conferma; se rifiuta, prepara la proposta per il "
              "successivo in ordine, da approvare di nuovo. "
              + ("Scartando, si registra che non ha risposto e si passa al successivo." if retry else
                 "Scartando, questa persona viene saltata per questo posto (resta in lista) e si passa al successivo.")
              + (" Senza auto: se accetta, l'agente chiede se le serve un passaggio." if p["nocar"] else ""))
    return dict(role="segreteria", kind="posto", patient=p, score=s, parts=parts, conf="alta",
                tag="Posto liberato: nuovo tentativo" if retry else "Posto liberato", summary=summary, action=action, role_note=None,
                evidence=[[slot["id"], "Agenda CUP · disdetta", f"{slot['service']} {slot['at']} disdetto alle {(slot['freed_at'] or '')[11:16]}"],
                          [w["id"], "Lista d'attesa", f"{w['service']}, priorità {w['priority']}, in attesa dal {w['since']}"]],
                ckey=f"posto|{slot['id']}|{w['id']}|{o['id']}|{o['calls']}",
                payload={"slot": slot["id"], "waitlist": w["id"], "offer": o["id"], "tentativo": o["calls"] + 1})


def _salva_caso(con, c: dict, run_id: str = "posti") -> str:
    """Come gateway.save_cases, per un caso solo: in attesa si aggiorna, già deciso non rinasce."""
    old = con.execute("SELECT id, status FROM cases WHERE ckey=?", (c["ckey"],)).fetchone()
    vals = (c["score"], json.dumps(c["parts"]), c["conf"], c["tag"], c["summary"], c["action"],
            json.dumps(c["evidence"], ensure_ascii=False), c["role_note"], json.dumps(c["payload"], ensure_ascii=False))
    if old and old["status"] == "pending":
        con.execute("UPDATE cases SET score=?, parts=?, confidence=?, tag=?, summary=?, action=?, evidence=?, role_note=?, payload=? "
                    "WHERE id=? AND status='pending'", (*vals, old["id"]))
        return old["id"]
    if old:
        return old["id"]
    cid = "PS-" + uuid.uuid4().hex[:8]
    con.execute("INSERT INTO cases (id,run_id,patient_id,role,kind,score,parts,confidence,tag,summary,action,evidence,role_note,payload,ckey) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (cid, run_id, c["patient"]["id"], c["role"], c["kind"], *vals, c["ckey"]))
    return cid


def _caso_a_mano(con, o: dict, perche: str):
    """Errore di aggiornamento o verifica non superata: un caso per l'accettazione, il posto resta tenuto."""
    from . import gateway
    p = gateway.profile(con, o["patient_id"])
    slot = _slot(con, o["slot_id"])
    s, parts = gateway.score(p, 30, 14, 10)
    _salva_caso(con, dict(role="segreteria", kind="posto_errore", patient=p, score=s, parts=parts, conf="bassa",
                          tag="Da controllare a mano",
                          summary=f"{p['name']} ha accettato al telefono il {slot['service'].replace('_', ' ')} del {slot['at']}, "
                                  f"ma la prenotazione non risulta salvata correttamente: {perche}",
                          action="Il posto resta tenuto per questa persona e la mail di conferma NON è partita. Dalla pagina Posti liberati: "
                                 "riprova a salvare la prenotazione, oppure libera il posto e passa al successivo.",
                          evidence=[[slot["id"], "Agenda CUP · disdetta", f"{slot['service']} {slot['at']}"]], role_note=None,
                          ckey=f"posto_errore|{o['id']}|{uuid.uuid4().hex[:4]}", payload={"slot": slot["id"], "offer": o["id"]}))


# ------------------------------------------------------------------ avanzamento

def _crea_proposta(con, slot: dict, w: dict, rank: int) -> dict | None:
    oid = "OF-" + uuid.uuid4().hex[:8]
    try:
        con.execute("INSERT INTO slot_offers (id, slot_id, waitlist_id, patient_id, rank, status, calls, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (oid, slot["id"], w["id"], w["patient_id"], rank, "da_approvare", 0, db.now(), db.now()))
    except sqlite3.IntegrityError:  # un'altra proposta è nata nello stesso momento: gli indici unici l'hanno fermata
        return None
    evento(con, slot["id"], oid, AGENTE["username"], "proposta",
           f"Proposta preparata per {w['name']} ({rank}° in ordine, priorità {w['priority']}). Serve l'approvazione dell'accettazione "
           "prima della chiamata.")
    return _caso(con, slot, _offer(con, oid))


def liberi(con, slot: dict) -> list[dict]:
    """Candidati che possono ricevere la proposta adesso, nell'ordine fisso."""
    lista, tried = _lista_posto(con, slot)
    busy = {r[0] for r in con.execute(f"SELECT patient_id FROM slot_offers WHERE status IN {ATTIVE}")}
    return [w for w in lista if w["status"] == "in attesa" and w["id"] not in tried and w["patient_id"] not in busy]


def _al_flusso(con, slot: dict) -> bool:
    """Con più candidati e il flusso acceso, la scelta la fa il workflow "posto liberato" sulla piattaforma (app/flussi.py)."""
    from . import flussi
    if not flussi.attivo("posto_liberato"):
        return False
    if flussi.in_corso(con, "posto_liberato", slot["id"]):
        return True
    if len(liberi(con, slot)) < 2:
        return False
    flussi.lancia("posto_liberato", slot["id"], lambda sid=slot["id"]: proponi_locale(sid), con=con)
    evento(con, slot["id"], None, AGENTE["username"], "scelta in corso",
           "Posto passato al flusso \"posto liberato\" sulla piattaforma: l'agente Agende sceglie per urgenza, Filo controlla.")
    return True


def proponi_locale(sid: str) -> dict | None:
    """Riserva del flusso: la stessa proposta che prepara avanza, con la scelta fatta da Filo (agente via API o ordine fisso)."""
    with db.tx() as con:
        slot = _slot(con, sid)
        if slot["assigned_to"] or con.execute(f"SELECT 1 FROM slot_offers WHERE slot_id=? AND status IN {ATTIVE}", (sid,)).fetchone():
            return None
        w, rank, why = _prossimo(con, slot)
        c = _crea_proposta(con, slot, w, rank) if w else None
        if c:
            _salva_caso(con, c)
        return c


def proponi_da_flusso(sid: str, codici: dict, cands: dict, pulito: dict | None, scarto: str | None) -> dict:
    """La scelta arrivata dal workflow: stessi controlli di scegli(), poi la proposta (da approvare)."""
    with db.tx() as con:
        slot = _slot(con, sid)
        if slot["assigned_to"] or con.execute(f"SELECT 1 FROM slot_offers WHERE slot_id=? AND status IN {ATTIVE}", (sid,)).fetchone():
            return {"fatto": "niente: il posto ha già una proposta o è assegnato"}
        ancora = {w["id"]: w for w in liberi(con, slot)}
        codici = {k: wid for k, wid in codici.items() if wid in ancora}
        if not codici:
            return {"fatto": "niente: nessun candidato è ancora libero"}
        k, testo = _valida({k: cands[k] for k in codici}, list(codici), pulito, scarto, "piattaforma FastwebAI Agents, flusso posto liberato")
        w = ancora[codici[k]]
        _registra_scelta(con, slot, w, cands[k], testo, len(codici))
        lista, _ = _lista_posto(con, slot)
        c = _crea_proposta(con, slot, w, lista.index(next(x for x in lista if x["id"] == w["id"])) + 1)
        if c:
            _salva_caso(con, c)
    return {"fatto": f"proposta per {w['name']}: serve il via libera dell'accettazione", "scelta": testo}


def avanza(con, salva: bool = True, run_id: str = "posti") -> list[dict]:
    """Per ogni posto libero e non passato: se non ha una proposta aperta, la prepara per il primo in ordine.
    Ritorna i casi in attesa di approvazione (l'elaborazione li salva insieme agli altri; con salva=True li salva qui)."""
    cases = []
    from . import gateway
    for slot in [dict(r) for r in con.execute("SELECT * FROM freed_slots WHERE assigned_to IS NULL ORDER BY at, id")]:
        active = con.execute(f"SELECT * FROM slot_offers WHERE slot_id=? AND status IN {ATTIVE}", (slot["id"],)).fetchone()
        if gateway._days(slot["at"]) < 0:
            if active:
                _chiudi(con, dict(active), "scaduto", AGENTE, "Il posto è passato senza una risposta: proposta chiusa.")
            continue
        if active:
            if active["status"] in ("da_approvare", "senza_esito"):
                cases.append(_caso(con, slot, dict(active)))
            continue
        cal = calendario.e_libero(con, service=slot["service"], at=slot["at"], place=slot["place"])  # stato attuale, letto ora
        if not cal["libero"]:
            _evento_una_volta(con, slot["id"], "non libero nel calendario",
                              f"Il calendario dice che il posto non è disponibile ({cal['motivo']}): nessuna proposta.")
            continue
        if _al_flusso(con, slot):
            continue
        try:
            w, rank, why = _prossimo(con, slot)
        except sqlite3.Error as e:
            _evento_una_volta(con, slot["id"], "errore lista", f"Lista d'attesa non leggibile ({str(e)[:120]}): il posto resta libero, "
                                                               "si riprova alla prossima elaborazione.")
            continue
        if not w:
            _evento_una_volta(con, slot["id"], "lista esaurita" if why.startswith("lista") else "in pausa",
                              f"Nessuno a cui proporre il posto ({why}). Il posto resta libero: niente viene assegnato d'ufficio.")
            continue
        c = _crea_proposta(con, slot, w, rank)
        if c:
            cases.append(c)
    if salva:
        for c in cases:
            _salva_caso(con, c, run_id)
    return cases


def _rilascia(con, o: dict):
    """Il posto torna libero nel calendario: annullata la prenotazione 'da confermare', la persona torna in lista nella sua posizione."""
    if o["appointment_id"]:
        a = con.execute("SELECT status FROM appointments WHERE id=?", (o["appointment_id"],)).fetchone()
        if a and a["status"] == "da confermare":
            calendario.cancella(con, o["appointment_id"], AGENTE, "proposta non accettata", alla_lista=False)
    con.execute("UPDATE waitlist SET status='in attesa' WHERE id=? AND status='proposto'", (o["waitlist_id"],))


def _chiudi(con, o: dict, status: str, who: dict, text: str, rilascia: bool = True):
    if rilascia:
        _rilascia(con, o)
    con.execute("UPDATE slot_offers SET status=?, updated_at=? WHERE id=?", (status, db.now(), o["id"]))
    con.execute("UPDATE cases SET status='superato', decided_by=?, decided_at=? WHERE status='pending' AND kind='posto' "
                "AND json_extract(payload, '$.offer')=?", (who["username"], db.now(), o["id"]))
    evento(con, o["slot_id"], o["id"], who["username"], status.replace("_", " "), text)


# ------------------------------------------------------------------ approvazione = via libera a UNA chiamata

def approva(con, case: dict, p: dict, user: dict) -> list[str]:
    """Dentro la transazione di actions.execute: se qualcosa non torna, ValueError e il caso resta in attesa."""
    from .actions import SPECIALTY_OF
    from .voce import chiamata as ch
    from . import gateway
    pl = json.loads(case["payload"])
    o = _offer(con, pl.get("offer") or "")
    if not o or o["status"] not in ("da_approvare", "senza_esito"):
        raise ValueError("Questa proposta non è più aperta (risposta già arrivata, posto passato o assegnato).")
    slot = _slot(con, o["slot_id"])
    if slot["assigned_to"]:
        raise ValueError("Il posto è già stato assegnato a un'altra persona.")
    if gateway._days(slot["at"]) < 0:
        raise ValueError("Il posto è già passato.")
    if o["status"] == "da_approvare":
        if con.execute("UPDATE waitlist SET status='proposto' WHERE id=? AND status='in attesa'", (o["waitlist_id"],)).rowcount != 1:
            raise ValueError("La persona non è più in lista d'attesa per questo esame.")
        # il calendario si rilegge adesso: posto libero, niente doppioni né sovrapposizioni per questa persona
        cal = calendario.e_libero(con, service=slot["service"], at=slot["at"], place=slot["place"])
        if not cal["libero"]:
            raise ValueError(f"Nel calendario il posto non è più disponibile ({cal['motivo']}).")
        prima = calendario.prenotazioni_paziente(con, p["id"], slot["service"])
        try:  # la data che ha già per lo stesso esame verrà lasciata solo se accetta: non è un doppione
            appt = calendario.prenota(con, p["id"], cal["posto"]["id"], user, stato="da confermare",
                                      sostituisce=prima[0]["id"] if prima else None, versione=cal["posto"]["version"],
                                      specialty=SPECIALTY_OF.get(slot["service"]))
        except calendario.Errore as e:
            raise ValueError(f"Il calendario rifiuta la proposta: {e}.")
        evento(con, slot["id"], o["id"], "calendario", "posto tenuto", f"Nel calendario il posto è tenuto per {p['name']} (prenotazione {appt} da confermare).")
    else:
        appt = o["appointment_id"]
        prob = calendario.verifica_prenotazione(con, appt, "da confermare")
        if prob:
            raise ValueError("Il calendario non torna con la proposta: " + "; ".join(prob))
    cid = ch.crea(con, p["id"], "appuntamento", appt, user)
    con.execute("UPDATE slot_offers SET status='in_chiamata', case_id=?, call_id=?, appointment_id=?, calls=calls+1, updated_at=? WHERE id=?",
                (case["id"], cid, appt, db.now(), o["id"]))
    con.execute("UPDATE cases SET payload=? WHERE id=?", (json.dumps({**pl, "new_appointment": appt, "call": cid}, ensure_ascii=False), case["id"]))
    evento(con, slot["id"], o["id"], user["username"], "approvata",
           f"{user['name']} approva la chiamata {cid} a {p['name']} (tentativo {o['calls'] + 1}). Posto tenuto per lei/lui fino alla risposta.")
    return [f"chiamata {cid} a {p['name']} approvata: il posto {slot['at']} resta tenuto fino alla risposta (si avvia dalla pagina della chiamata)"]


def scartato(case_id: str, user: dict, motivo: str):
    """L'accettazione scarta la proposta: la persona è saltata per questo posto (o, dopo una chiamata senza risposta,
    si registra che non ha risposto) e si passa al successivo."""
    with db.tx() as con:
        c = con.execute("SELECT payload FROM cases WHERE id=?", (case_id,)).fetchone()
        o = _offer(con, json.loads(c["payload"] or "{}").get("offer") or "") if c else None
        if not o or o["status"] not in ("da_approvare", "senza_esito"):
            return
        if o["status"] == "senza_esito":
            _chiudi(con, o, "nessuna_risposta", user, f"{user['name']} non fa richiamare: registrato che non ha risposto ({motivo}).")
        else:
            _chiudi(con, o, "scartato", user, f"{user['name']} salta questa persona per questo posto ({motivo}). Resta in lista d'attesa.")
        avanza(con)


# ------------------------------------------------------------------ esito della chiamata

def esito_chiamata(cid: str) -> bool:
    """Chiamato a fine chiamata (chiamata.concludi). Ritorna False se la chiamata non è di un posto liberato."""
    with db.tx() as con:
        o = con.execute("SELECT * FROM slot_offers WHERE call_id=?", (cid,)).fetchone()
        if not o:
            return False
        # presa dell'esito atomica: una seconda fine chiamata (browser e webhook insieme) non rifà nulla
        if con.execute("UPDATE slot_offers SET status='esito', updated_at=? WHERE id=? AND status='in_chiamata'",
                       (db.now(), o["id"])).rowcount != 1:
            return True
        o = {**dict(o), "status": "esito"}
        call = dict(con.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone())
    out = json.loads(call["outcome"] or "{}")
    esito = out.get("appuntamento") or {}
    if esito.get("scelta") == "accetta":
        _accetta(o, bool(esito.get("serve_passaggio")))
    elif esito.get("scelta") in ("rifiuta", "vuole_altra_data"):
        status = "rifiutato" if esito["scelta"] == "rifiuta" else "altra_data"
        with db.tx() as con:
            name = con.execute("SELECT name FROM patients WHERE id=?", (o["patient_id"],)).fetchone()[0]
            _chiudi(con, o, status, AGENTE, f"{name} " + ("non accetta il posto" if status == "rifiutato" else "vorrebbe un'altra data")
                    + f" (chiamata {cid}). Torna in lista nella sua posizione; il posto passa al successivo in ordine.")
            avanza(con)
    else:
        _senza_esito(o, call)
    return True


def _senza_esito(o: dict, call: dict):
    perche = {"identita_non_confermata": "identità non confermata", "da_richiamare": "chiamata senza risposta o da richiamare",
              "allarme": "durante la chiamata c'è stato un segnale d'allarme (già passato al medico)"}.get(call["status"], "nessuna risposta registrata")
    with db.tx() as con:
        name = con.execute("SELECT name FROM patients WHERE id=?", (o["patient_id"],)).fetchone()[0]
        if o["calls"] >= MAX_CHIAMATE:
            _chiudi(con, o, "nessuna_risposta", AGENTE, f"{name}: {perche} anche al tentativo {o['calls']} di {MAX_CHIAMATE}. "
                                                         "Torna in lista; il posto passa al successivo in ordine.")
        else:
            con.execute("UPDATE slot_offers SET status='senza_esito', updated_at=? WHERE id=?", (db.now(), o["id"]))
            evento(con, o["slot_id"], o["id"], AGENTE["username"], "senza esito",
                   f"{name}: {perche} (chiamata {call['id']}, tentativo {o['calls']} di {MAX_CHIAMATE}). Il posto resta tenuto: "
                   "l'accettazione decide se richiamare (nuova approvazione) o passare al successivo.")
        avanza(con)


def _registra_prenotazione(con, o: dict) -> list[str]:
    """Tutto o niente: ogni passo controlla di aver cambiato esattamente quello che doveva."""
    slot = _slot(con, o["slot_id"])
    fatti = []
    a = con.execute("SELECT patient_id FROM appointments WHERE id=?", (o["appointment_id"],)).fetchone()
    if not a or a["patient_id"] != o["patient_id"]:
        raise Errore("la prenotazione da confermare non c'è più")
    w = dict(con.execute("SELECT * FROM waitlist WHERE id=?", (o["waitlist_id"],)).fetchone())
    # la data precedente per la stessa richiesta: si annulla nel calendario, e quel posto passa alla lista d'attesa
    for old in calendario.prenotazioni_paziente(con, o["patient_id"], w["service"]):
        if old["id"] != o["appointment_id"] and old["status"] == "prenotata":
            fs = calendario.cancella(con, old["id"], AGENTE, "sostituita dal posto liberato accettato")
            fatti.append(f"annullata la data precedente ({old['at']})" + (": quel posto torna libero per la lista" if fs else ""))
    calendario.conferma(con, o["appointment_id"], AGENTE)  # tenuto -> prenotato, da confermare -> prenotata
    if con.execute("UPDATE waitlist SET status='prenotato' WHERE id=? AND status='proposto'", (o["waitlist_id"],)).rowcount != 1:
        raise Errore("la richiesta non risultava più in lista d'attesa")
    doppie = con.execute("UPDATE waitlist SET status='prenotato' WHERE patient_id=? AND service=? AND status IN ('in attesa','proposto')",
                         (o["patient_id"], w["service"])).rowcount
    if doppie:
        fatti.append(f"chiuse anche {doppie} altre richieste in lista per lo stesso esame")
    if con.execute("UPDATE freed_slots SET assigned_to=? WHERE id=? AND assigned_to IS NULL", (o["patient_id"], slot["id"])).rowcount != 1:
        raise Errore("il posto risultava già assegnato")
    con.execute("UPDATE slot_offers SET status='accettato', updated_at=? WHERE id=? AND status IN ('esito','errore')", (db.now(), o["id"]))
    return fatti


def verifica(con, o: dict) -> list[str]:
    """Rilegge il database dopo il salvataggio. Lista vuota = tutto torna."""
    prob = []
    slot = _slot(con, o["slot_id"])
    a = con.execute("SELECT * FROM appointments WHERE id=?", (o["appointment_id"],)).fetchone()
    w = con.execute("SELECT * FROM waitlist WHERE id=?", (o["waitlist_id"],)).fetchone()
    if not a or a["status"] != "prenotata" or a["at"] != slot["at"] or a["patient_id"] != o["patient_id"]:
        prob.append("la nuova prenotazione non risulta confermata alla data del posto")
    if w is None or w["status"] != "prenotato":
        prob.append("la richiesta risulta ancora in lista d'attesa")
    n = con.execute("SELECT COUNT(*) FROM appointments WHERE patient_id=? AND service=? AND status='prenotata'",
                    (o["patient_id"], slot["service"])).fetchone()[0]
    if n != 1:
        prob.append(f"la persona risulta prenotata {n} volte per lo stesso esame")
    if slot["assigned_to"] != o["patient_id"]:
        prob.append("il posto non risulta assegnato a questa persona")
    if con.execute(f"SELECT COUNT(*) FROM slot_offers WHERE slot_id=? AND status IN {ATTIVE}", (slot["id"],)).fetchone()[0]:
        prob.append("c'è ancora una proposta aperta per questo posto")
    prob += [f"calendario: {x}" for x in calendario.verifica_prenotazione(con, o["appointment_id"], "prenotata")]
    return list(dict.fromkeys(prob))


def _accetta(o: dict, serve_passaggio: bool, who: dict = AGENTE):
    from . import cup_mail, trasporti
    try:
        with db.tx() as con:
            fatti = _registra_prenotazione(con, o)
            evento(con, o["slot_id"], o["id"], who["username"], "accettato",
                   "Ha accettato. Prenotazione confermata e richiesta tolta dalla lista d'attesa" + "".join(f"; {f}" for f in fatti) + ".")
    except Exception as e:  # niente è stato salvato (la transazione non è stata confermata)
        _errore(o, f"errore durante il salvataggio ({type(e).__name__}: {str(e)[:120]})")
        return
    with db.tx() as con:
        prob = verifica(con, o)
    if prob:
        _errore(o, "verifica dopo il salvataggio non superata: " + "; ".join(prob))
        return
    with db.tx() as con:
        p = dict(con.execute("SELECT * FROM patients WHERE id=?", (o["patient_id"],)).fetchone())
        a = dict(con.execute("SELECT * FROM appointments WHERE id=?", (o["appointment_id"],)).fetchone())
        prio = con.execute("SELECT priority FROM waitlist WHERE id=?", (o["waitlist_id"],)).fetchone()[0]
        evento(con, o["slot_id"], o["id"], AGENTE["username"], "verificato", "Riletto il database: prenotazione unica, lista d'attesa e posto coerenti.")
        from .actions import _mail
        from .modifiche import ambulatorio
        annullate = [e["text"] for e in con.execute("SELECT text FROM slot_events WHERE offer_id=? AND step='accettato'", (o["id"],))]
        _mail(con, o["call_id"] or o["id"], "segreteria", ambulatorio(a), f"Posto liberato riassegnato - {p['name']} - {a['service']} {a['at'][:10]}",
              f"Il posto di {a['service']} del {a['at']} ({a['place']}) è stato assegnato a {p['name']}, che l'ha accettato al telefono. "
              "Il calendario è aggiornato e riletto." + (f"\n{annullate[0]}" if annullate else "")
              + "\n\nCentrale Operativa Territoriale (messaggio generato da Filo).")
        evento(con, o["slot_id"], o["id"], AGENTE["username"], "mail ospedale", f"Avvisato {ambulatorio(a)} della nuova prenotazione.")
        try:
            msg = cup_mail.manda(con, o["call_id"] or o["id"], "segreteria", p, a, prio)
            sent = con.execute("SELECT sent_to FROM outbox WHERE id=(SELECT MAX(id) FROM outbox)").fetchone()[0] or ""
            evento(con, o["slot_id"], o["id"], AGENTE["username"], "mail" if not sent.startswith("invio non riuscito") else "errore mail",
                   f"Conferma CUP messa in Posta in uscita: {msg}." + (f" Copia vera: {sent}." if sent else ""))
        except Exception as e:
            evento(con, o["slot_id"], o["id"], AGENTE["username"], "errore mail",
                   f"Prenotazione salvata ma la mail di conferma NON è partita ({str(e)[:120]}): va mandata a mano.")
        con.execute("INSERT INTO action_log VALUES (?,?,?,?)", (db.now(), AGENTE["username"], o["call_id"] or o["id"],
                    f"{p['name']}: accetta {a['service']} del {a['at']}; prenotazione verificata, conferma mandata"))
        if serve_passaggio:  # stesse regole del cancello: distretto, preavviso, fragilità
            valid = trasporti.services_for(con, trasporti._profile(p), a["at"])
            if valid:
                rid = trasporti.start(con, None, p["id"], a["id"], valid[0]["id"], AGENTE)
                evento(con, o["slot_id"], o["id"], AGENTE["username"], "trasporto", f"Chiesto il passaggio: aperta la pratica {rid} con {valid[0]['name']}.")
            else:
                evento(con, o["slot_id"], o["id"], AGENTE["username"], "trasporto",
                       "Chiesto il passaggio, ma nessun servizio rispetta distretto, preavviso e regole: da organizzare a mano.")
        avanza(con)  # altri posti in pausa che aspettavano questa persona


def _errore(o: dict, perche: str):
    with db.tx() as con:
        con.execute("UPDATE slot_offers SET status='errore', updated_at=? WHERE id=?", (db.now(), o["id"]))
        evento(con, o["slot_id"], o["id"], AGENTE["username"], "errore aggiornamento",
               f"Ha accettato, ma {perche}. Nessuna mail di conferma. Il posto resta tenuto per questa persona: da controllare a mano.")
        _caso_a_mano(con, o, perche)


# ------------------------------------------------------------------ azioni a mano (pagina Posti liberati)

def esito_a_mano(oid: str, scelta: str, user: dict) -> str:
    """La chiamata si è chiusa senza che Filo lo sapesse (pagina chiusa, webhook perso), oppure una persona ha parlato
    lei con il paziente: l'esito lo registra una persona, e passa dagli stessi controlli."""
    from .voce import chiamata as ch
    if scelta not in ("accetta", "rifiuta", "nessuna_risposta"):
        raise ValueError("Esito non valido.")
    with db.tx() as con:
        o = _offer(con, oid)
        if not o or o["status"] != "in_chiamata":
            raise ValueError("Questa proposta non aspetta una risposta.")
        c = ch.carica(con, o["call_id"])
        out = c["outcome"]
        if scelta != "nessuna_risposta":
            out["appuntamento"] = {"scelta": scelta, "serve_passaggio": False}
        con.execute("UPDATE calls SET outcome=? WHERE id=?", (json.dumps(out, ensure_ascii=False), c["id"]))
        evento(con, o["slot_id"], oid, user["username"], "esito a mano", f"{user['name']} registra l'esito: {scelta.replace('_', ' ')}.")
        ended = bool(c["ended_at"])
    if ended:
        esito_chiamata(o["call_id"])
    else:
        ch.concludi(o["call_id"])
    return "esito registrato"


def riprova(oid: str, user: dict) -> str:
    with db.tx() as con:
        o = _offer(con, oid)
        if not o or o["status"] != "errore":
            raise ValueError("Niente da riprovare per questa proposta.")
        evento(con, o["slot_id"], oid, user["username"], "riprova", f"{user['name']} riprova a salvare la prenotazione.")
    _accetta(o, False, user)
    with db.tx() as con:
        return STATO[_offer(con, oid)["status"]]


def libera(oid: str, user: dict) -> str:
    """Il posto torna libero e si passa al successivo. Se la prenotazione era stata salvata a metà (verifica non superata),
    la si annulla tutta: niente resta assegnato a una persona che l'accettazione ha deciso di non confermare."""
    with db.tx() as con:
        o = _offer(con, oid)
        if not o or o["status"] not in ("errore", "senza_esito", "in_chiamata", "esito"):
            raise ValueError("Questa proposta non tiene il posto.")
        if _slot(con, o["slot_id"])["assigned_to"] == o["patient_id"]:  # salvata ma la verifica non è tornata: si annulla tutto
            con.execute("UPDATE freed_slots SET assigned_to=NULL WHERE id=?", (o["slot_id"],))
            a = con.execute("SELECT status FROM appointments WHERE id=?", (o["appointment_id"],)).fetchone()
            if a and a["status"] in calendario.ATTIVE:
                calendario.cancella(con, o["appointment_id"], user, "liberato dall'accettazione dopo un errore", alla_lista=False)
            con.execute("UPDATE waitlist SET status='in attesa' WHERE id=?", (o["waitlist_id"],))
        _chiudi(con, o, "scartato" if o["status"] == "errore" else "nessuna_risposta", user,
                f"{user['name']} libera il posto e passa al successivo.")
        con.execute("UPDATE cases SET status='ok', decided_by=?, decided_at=? WHERE status='pending' AND kind='posto_errore' "
                    "AND json_extract(payload, '$.offer')=?", (user["username"], db.now(), oid))
        avanza(con)
    return "posto liberato"


# ------------------------------------------------------------------ per le pagine

def quadro(con) -> list[dict]:
    from . import gateway
    out = []
    for s in [dict(r) for r in con.execute("SELECT * FROM freed_slots ORDER BY at DESC, id")]:
        active = con.execute(f"SELECT status FROM slot_offers WHERE slot_id=? AND status IN {ATTIVE}", (s["id"],)).fetchone()
        if s["assigned_to"]:
            st = "Assegnato a " + con.execute("SELECT name FROM patients WHERE id=?", (s["assigned_to"],)).fetchone()[0]
        elif active:
            st = STATO[active[0]]
        elif gateway._days(s["at"]) < 0:
            st = "Passato"
        else:
            last = con.execute("SELECT step FROM slot_events WHERE slot_id=? ORDER BY rowid DESC LIMIT 1", (s["id"],)).fetchone()
            st = {"lista esaurita": "Libero: lista d'attesa esaurita", "errore lista": "Libero: lista d'attesa non leggibile",
                  "in pausa": "Libero: candidati impegnati con un altro posto"}.get(last[0] if last else "", "Libero: proposta alla prossima elaborazione")
        s["stato"] = st
        s["cal"] = calendario.posto(con, service=s["service"], at=s["at"], place=s["place"])
        s["ordine"] = ordine(con, s)
        s["eventi"] = [dict(e) for e in con.execute("SELECT * FROM slot_events WHERE slot_id=? ORDER BY rowid", (s["id"],))]
        out.append(s)
    return out


# ------------------------------------------------------------------ demo: una disdetta che arriva dal CUP

def _scelta_in_corso(con, sid: str) -> bool:
    from . import flussi
    return flussi.in_corso(con, "posto_liberato", sid)


def disdetta_prova(con, user: dict) -> str:
    """Per far vedere il flusso senza aspettare il CUP: nel calendario si annulla una prenotazione di prova (persona che non è
    in Filo) per l'esame con più persone in lista d'attesa; il posto passa alla lista e la proposta nasce subito, come
    all'elaborazione. Il resto è identico al caso vero: serve il via libera dell'accettazione prima della chiamata."""
    from . import gateway
    srv = [r[0] for r in con.execute("SELECT service FROM waitlist WHERE status='in attesa' GROUP BY service ORDER BY COUNT(*) DESC")]
    if not srv:
        raise ValueError("Nessuno in lista d'attesa: una disdetta non avrebbe a chi andare.")
    domani = (gateway._today()).isoformat() + " 23:59"
    s = None
    for service in srv:
        s = con.execute("SELECT * FROM cal_slots WHERE service=? AND status='prenotato' AND occupant='prova' AND appointment_id IS NULL "
                        "AND at > ? ORDER BY at LIMIT 1", (service, domani)).fetchone()
        if s:
            break
    if not s:
        raise ValueError("Nel calendario non c'è una prenotazione di prova da disdire per gli esami con persone in lista.")
    s = dict(s)
    calendario._prendi(con, s, ("prenotato",), "libero", None, None, s["version"])
    sid = "FS-" + uuid.uuid4().hex[:6]
    con.execute("INSERT INTO freed_slots VALUES (?,?,?,?,?,NULL)", (sid, s["service"], s["at"], s["place"], db.now()))
    calendario._log(con, user, "cancella", s["id"], None, "ok", f"disdetta di prova: {s['service']} {s['at']} torna libero per la lista ({sid})")
    evento(con, sid, None, user["username"], "disdetta", f"Disdetta di prova arrivata dal CUP: {s['service']} del {s['at']} ({s['place']}) torna libero.")
    nuove = avanza(con)
    chi = next((c for c in nuove if c["payload"].get("slot") == sid), None)
    at = s["at"]
    return f"si è liberato {s['service']} del {at[8:10]}/{at[5:7]} alle {at[11:16]}" + (
        f": proposta pronta per {chi['patient']['name']}, serve il via libera nel cruscotto" if chi else
        (": l'agente Agende sta scegliendo sulla piattaforma, la proposta arriva nel cruscotto tra pochi secondi"
         if _scelta_in_corso(con, sid) else ": nessuno a cui proporlo adesso (vedi Posti liberati)"))
