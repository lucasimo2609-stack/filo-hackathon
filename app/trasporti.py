"""Flusso dei trasporti dopo il via libera dell'accettazione.

I servizi di Venezia e Mestre (STACCO, Croce Verde) non pubblicano calendari né prenotazioni online:
si prenota per telefono o per mail e rispondono loro (STACCO entro 48 ore). Quindi l'agente:
1. manda da solo la richiesta di disponibilità al servizio scelto, con una scadenza per la risposta;
2. registra la risposta; se il servizio non può, o non risponde entro la scadenza, passa da solo al successivo
   che rispetta distretto, preavviso e regole del servizio;
3. chiama la persona per sapere se il trasporto serve davvero (agente vocale, oppure esito registrato a mano);
   se non risponde fissa il tentativo successivo; dopo 3 tentativi avvisa il servizio e torna all'accettazione;
4. se serve, conferma al servizio (andata e ritorno) e alla persona arriva la conferma del CUP con il trasporto;
   se non serve, annulla;
5. se la visita viene spostata o annullata, avvisa il servizio e riporta la pratica all'accettazione.

Tutto questo gira in ospedale: nome, indirizzo e telefono vanno al servizio di trasporto, mai al cloud.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timedelta

from . import cup_mail, db, posta
from .actions import _mail
from .config import TODAY

STATUS = {
    "attesa_servizio": "Richiesta inviata, si aspetta il servizio",
    "attesa_paziente": "Servizio disponibile, l'agente sta chiamando la persona",
    "richiamare": "La persona non ha risposto: nuovo tentativo programmato",
    "confermato": "Confermato",
    "annullato": "Non serve, annullato",
    "nessun_servizio": "Nessun servizio disponibile: serve l'accettazione",
    "persona_non_raggiunta": "La persona non risponde: serve l'accettazione (il servizio è stato avvisato)",
    "visita_cambiata": "La visita è stata spostata o annullata: serve l'accettazione",
}
ACTIVE = ("attesa_servizio", "attesa_paziente", "richiamare", "confermato")
MAX_TENTATIVI = 3
RICHIAMA_DOPO = timedelta(hours=1)
KNOWN_DISTRICTS = {"Nord", "Sud"}


# ------------------------------------------------------------------ regole dei servizi (usate anche dal cancello)

def _today() -> date:
    return date.fromisoformat(TODAY)


def norm_time(v) -> str | None:
    """'9:30' -> '09:30'. Orari impossibili (es. 99:99) -> None."""
    m = re.fullmatch(r"\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*", str(v or ""))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def notice_available(s: dict, visit_day: date, today: date | None = None) -> int:
    """Giorni di preavviso che si riescono a dare. Chi prenota solo lun-ven conta i giorni lavorativi."""
    today = today or _today()
    if "lun-ven" in (s.get("booking") or ""):
        return sum(1 for i in range((visit_day - today).days) if (today + timedelta(days=i)).weekday() < 5)
    return (visit_day - today).days


def is_fragile(p: dict) -> bool:
    return p.get("age", 0) >= 75 or bool(p.get("alone")) or bool(p.get("nocar"))


def eligible(s: dict, p: dict, visit_at: str, today: date | None = None) -> tuple[bool, str]:
    """Il servizio va bene per questa persona e questa visita? Ritorna (sì/no, perché no)."""
    covered = set((s.get("distretti") or "").split(","))
    distr = p.get("distretto")
    if distr in KNOWN_DISTRICTS:
        if distr not in covered:
            return False, f"non copre il distretto {distr}"
    elif not KNOWN_DISTRICTS <= covered:  # distretto non noto (es. paziente importato): solo chi copre tutto il territorio
        return False, "distretto della persona non noto e il servizio non copre tutto il territorio"
    visit_day = datetime.fromisoformat(visit_at).date()
    if notice_available(s, visit_day, today) < s["notice_days"]:
        return False, f"chiede {s['notice_days']} giorni di preavviso" + (" lavorativi" if "lun-ven" in (s.get("booking") or "") else "")
    if "fragili" in (s.get("rules") or "").lower() and not is_fragile(p):
        return False, "riservato a persone fragili"
    return True, ""


def services_for(con, p: dict, visit_at: str, exclude=()) -> list[dict]:
    """Servizi in regola, prima quelli che chiedono più preavviso (gratuiti, sociali), poi i più rapidi."""
    out = []
    for s in con.execute("SELECT * FROM transport_services ORDER BY notice_days DESC, id"):
        s = dict(s)
        if s["id"] not in exclude and eligible(s, p, visit_at)[0]:
            out.append(s)
    return out


def strike_at(con, at: str) -> str | None:
    """Testo dello sciopero se tocca la fascia del viaggio (da 90 minuti prima a 2 ore dopo la visita)."""
    t = datetime.fromisoformat(at)
    for e in con.execute("SELECT * FROM external_events WHERE kind='sciopero' AND date=?", (at[:10],)):
        m = re.search(r"dalle (\d{1,2})[:.](\d{2}) alle (\d{1,2})[:.](\d{2})", e["text"] or "")
        if not m:
            return e["text"]  # nessun orario: vale tutto il giorno
        a = t.replace(hour=int(m.group(1)), minute=int(m.group(2)))
        b = t.replace(hour=int(m.group(3)), minute=int(m.group(4)))
        if t - timedelta(minutes=90) < b and t + timedelta(hours=2) > a:
            return e["text"]
    return None


def heat_at(con, at: str) -> str | None:
    """Ondata di calore: l'evento vale dal giorno indicato per tre giorni."""
    d = date.fromisoformat(at[:10])
    for e in con.execute("SELECT * FROM external_events WHERE kind='caldo'"):
        start = date.fromisoformat(e["date"])
        if start <= d <= start + timedelta(days=2):
            return e["text"]
    return None


def default_pickup(a) -> str:
    return (datetime.fromisoformat(a["at"]) - timedelta(minutes=60)).strftime("%H:%M")


_pickup = default_pickup  # nome usato dalle pagine e dall'agente vocale


def where(p) -> str:
    q = p.get("quartiere")
    return f"{p['address']} ({q})" if q else (p["address"] or "indirizzo da verificare")


# ------------------------------------------------------------------ pratica

def _event(con, rid, who, step, text):
    con.execute("INSERT INTO transport_events VALUES (?,?,?,?,?)", (rid, db.now(), who, step, text))


def _set(con, rid, **kw):
    kw["updated_at"] = db.now()
    con.execute(f"UPDATE transport_requests SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), rid))


def _load(con, rid):
    r = dict(con.execute("SELECT * FROM transport_requests WHERE id=?", (rid,)).fetchone())
    p = dict(con.execute("SELECT * FROM patients WHERE id=?", (r["patient_id"],)).fetchone())
    a = dict(con.execute("SELECT * FROM appointments WHERE id=?", (r["appointment_id"],)).fetchone())
    s = dict(con.execute("SELECT * FROM transport_services WHERE id=?", (r["service_id"],)).fetchone())
    return r, p, a, s


def _profile(p):
    return {**p, "age": _today().year - p["birth_year"], "alone": bool(p["lives_alone"]), "nocar": p["has_car"] == 0}


def _reply_hours(s) -> int:
    m = re.search(r"entro (\d+) ore", s.get("response") or "")
    return int(m.group(1)) if m else 6 if "giornata" in (s.get("response") or "") else 24


def _reply_by(s, a) -> str:
    """Entro quando aspettare il servizio: i suoi tempi, ma mai oltre mezzogiorno del giorno prima della visita."""
    now = datetime.now()
    by = now + timedelta(hours=_reply_hours(s))
    cap = datetime.fromisoformat(a["at"][:10] + " 12:00") - timedelta(days=1)
    by = min(by, cap)
    return max(by, now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")


def _ask(con, rid, p, a, s, who, pickup):
    """Richiesta al servizio, in un filo di mail con il codice della pratica nell'oggetto: la risposta la legge Filo da solo."""
    msgid = posta.nuovo_msgid()
    posta.segna_filo(con, rid, msgid, "out")
    no_phone = "" if p.get("phone") else "\n- telefono della persona non registrato: vi richiamiamo noi"
    _mail(con, rid, "segreteria", s["email"], f"Richiesta di disponibilità trasporto {a['at'][:10]} [{rid}]",
          f"Buongiorno, chiediamo se avete disponibilità per un trasporto di andata e ritorno:\n"
          f"- persona: {p['name']}, anno di nascita {p['birth_year']}\n- partenza da: {where(p)}\n"
          f"- destinazione: {a['place']}, appuntamento alle {a['at'][11:]} del {a['at'][:10]}\n"
          f"- partenza suggerita: {pickup}; ritorno a casa dopo la visita\n"
          f"- vive solo: {'sì' if p['lives_alone'] else 'no'}; auto in famiglia: {'sì' if p['has_car'] else 'no'}{no_phone}\n\n"
          f"Per favore rispondete a questa mail lasciando il codice {rid} nell'oggetto:\n"
          f"- OK e l'orario in cui passate (es. \"ok, passiamo alle 9:00\"), più eventuali indicazioni per la persona;\n"
          f"- oppure NO se non avete disponibilità.\n\nGrazie.\nCentrale Operativa Territoriale",
          msgid=msgid)
    by = _reply_by(s, a)
    _set(con, rid, reply_by=by)
    _event(con, rid, who, "richiesta", f"Richiesta di disponibilità mandata a {s['name']} ({s['email']}). Loro rispondono: {s['response']}. "
                                       f"Se non rispondono entro il {by[8:10]}/{by[5:7]} alle {by[11:16]} l'agente passa al servizio successivo.")


def start(con, case_id, patient_id, appointment_id, service_id, user, pickup=None) -> str:
    rid = "TR-" + uuid.uuid4().hex[:8]
    a = dict(con.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone())
    pickup = norm_time(pickup) or default_pickup(a)
    con.execute("INSERT INTO transport_requests (id, case_id, patient_id, appointment_id, service_id, status, pickup, tried, created_at, updated_at, visit_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rid, case_id, patient_id, appointment_id, service_id, "attesa_servizio", pickup, json.dumps([service_id]), db.now(), db.now(), a["at"]))
    r, p, a, s = _load(con, rid)
    _event(con, rid, user["username"], "via libera", f"Via libera di {user['name']}.")
    _ask(con, rid, p, a, s, "agente", pickup)
    return rid


def _next_service(con, r, p, a):
    alts = services_for(con, _profile(p), a["at"], exclude=json.loads(r["tried"]))
    return alts[0] if alts else None


def _switch(con, rid, r, p, a, why):
    nxt = _next_service(con, r, p, a)
    if nxt:
        tried = json.loads(r["tried"]) + [nxt["id"]]
        _set(con, rid, service_id=nxt["id"], tried=json.dumps(tried))
        _event(con, rid, "agente", "cambio servizio",
               f"{why} Il controllo locale sceglie il servizio successivo che rispetta distretto, preavviso e regole: {nxt['name']}.")
        _ask(con, rid, p, a, nxt, "agente", r["pickup"] or default_pickup(a))
    else:
        _set(con, rid, status="nessun_servizio")
        _event(con, rid, "agente", "fermo", f"{why} Nessun altro servizio rispetta distretto, preavviso e regole. Torna all'accettazione.")


def call_script(p, a, s, pickup, strike: str | None = None) -> str:
    nome = p["name"].split()[0]
    return (f"Buongiorno, parlo con {p['name']}? Sono l'assistente automatico della Centrale Operativa dell'ospedale. "
            f"La chiamo per la sua {a['service']} del {a['at'][8:10]}/{a['at'][5:7]} alle {a['at'][11:]}. "
            f"{'Quel giorno c’è lo sciopero dei mezzi. ' if strike else ''}"
            f"Le serve un passaggio per arrivare in ospedale e tornare a casa? "
            f"[se sì] {s['name']} può passare a prenderla alle {pickup} sotto casa, in {p['address']}. Le va bene? "
            f"[se no] Va bene {nome}, allora non prenotiamo niente. Se cambia idea chiami la centrale.")


def risposta_servizio(rid, disponibile: bool, pickup: str | None, user):
    with db.tx() as con:
        r, p, a, s = _load(con, rid)
        if r["status"] != "attesa_servizio":
            return
        if disponibile:
            pickup = norm_time(pickup) or r["pickup"] or default_pickup(a)
            _set(con, rid, status="attesa_paziente", pickup=pickup)
            _event(con, rid, user["username"], "risposta servizio", f"{s['name']}: disponibile, partenza alle {pickup}.")
            from .voce import chiamata as voce_ch
            aperta = con.execute("SELECT id FROM calls WHERE ref_id=? AND kind='trasporto' AND ended_at IS NULL", (rid,)).fetchone()
            cid = aperta[0] if aperta else voce_ch.crea(con, p["id"], "trasporto", rid, {"username": "agente", "name": "agente"})
            _event(con, rid, "agente", "chiamata", f"L'agente chiama {p['name']} per sapere se il passaggio serve (chiamata {cid}).")
        else:
            _event(con, rid, user["username"], "risposta servizio", f"{s['name']}: non disponibile.")
            _switch(con, rid, r, p, a, f"{s['name']} non è disponibile.")


def chiama_se_possibile(rid):
    """Se c'è il telefono di prova, l'agente chiama da solo; altrimenti la chiamata squilla nell'accettazione."""
    from .voce import chiamata as voce_ch, eleven
    if not eleven.telefono_configurato():
        return
    with db.tx() as con:
        row = con.execute("SELECT id FROM calls WHERE ref_id=? AND kind='trasporto' AND ended_at IS NULL AND channel IS NULL", (rid,)).fetchone()
        c = voce_ch.carica(con, row[0]) if row else None
    if not c:
        return
    try:
        conv = eleven.chiama_telefono(voce_ch.dati_per_elevenlabs(c))
        with db.tx() as con:
            con.execute("UPDATE calls SET channel='telefono', conversation_id=? WHERE id=?", (conv, c["id"]))
            voce_ch.evento(con, c["id"], "avvio", "Chiamata partita da sola verso il telefono di prova.")
    except Exception as e:  # noqa: BLE001
        with db.tx() as con:
            voce_ch.evento(con, c["id"], "avvio", f"Chiamata al telefono non partita ({str(e)[:100]}): si risponde dal browser.")


def _risposta_servizio(con, rid, s, a, testo):
    """Risponde al servizio nella stessa conversazione della richiesta."""
    from . import posta
    prec, refs = posta.filo_di(con, rid)
    msgid = posta.nuovo_msgid()
    posta.segna_filo(con, rid, msgid, "out")
    _mail(con, rid, "segreteria", s["email"], f"Re: Richiesta di disponibilità trasporto {a['at'][:10]} [{rid}]", testo,
          msgid=msgid, in_reply_to=prec, references=refs)


def esito_chiamata(rid, esito: str, user):
    with db.tx() as con:
        r, p, a, s = _load(con, rid)
        if r["status"] not in ("attesa_paziente", "richiamare"):
            return
        if esito == "serve":
            pickup = norm_time(r["pickup"]) or default_pickup(a)
            _set(con, rid, status="confermato", pickup=pickup, next_call_at=None)
            _event(con, rid, user["username"], "esito chiamata", "La persona conferma che il trasporto serve.")
            _risposta_servizio(con, rid, s, a,
                               f"Buongiorno, la persona ha confermato al telefono: il trasporto SERVE.\n\n"
                               f"Confermiamo il trasporto di andata e ritorno di {p['name']} da {where(p)} il {a['at'][:10]} con partenza alle {pickup}, "
                               f"per l'appuntamento delle {a['at'][11:]} presso {a['place']}. Il ritorno è da concordare con la persona a fine visita. "
                               f"Telefono della persona: {p['phone'] or 'non registrato, chiamare la centrale'}.\n\n"
                               "Grazie.\nCentrale Operativa Territoriale")
            prio = con.execute("SELECT priority FROM waitlist WHERE patient_id=? AND service=?", (p["id"], a["service"])).fetchone()
            ind = posta.indicazioni(con, rid)
            cup_mail.manda(con, rid, "segreteria", p, a, prio[0] if prio else None,
                           {"servizio": s["name"], "partenza": pickup, "indicazioni": ind}, nuova=False)
            _event(con, rid, "agente", "conferma", f"Conferma mandata a {s['name']}. Alla famiglia arriva la mail di conferma del CUP con il trasporto"
                   + (" e le indicazioni del servizio." if ind else "."))
            _event(con, rid, "agente", "appunto",
                   f"Trasporto confermato: {s['name']} passa alle {pickup} il {a['at'][:10]} da {where(p)}. "
                   f"Conferma al servizio e mail alla famiglia ({cup_mail.destinatario(p)}) partite. Niente da fare per l'accettazione.")
        elif esito == "non_serve":
            _set(con, rid, status="annullato", next_call_at=None)
            _event(con, rid, user["username"], "esito chiamata", "La persona dice che il trasporto non serve (ha un passaggio).")
            _risposta_servizio(con, rid, s, a,
                               f"Buongiorno, la persona ci ha detto al telefono che il trasporto NON serve: ha già un passaggio.\n\n"
                               f"La richiesta per {p['name']} del {a['at'][:10]} è annullata, il posto si libera. Grazie lo stesso.\n"
                               "Centrale Operativa Territoriale")
            prio = con.execute("SELECT priority FROM waitlist WHERE patient_id=? AND service=?", (p["id"], a["service"])).fetchone()
            cup_mail.manda(con, rid, "segreteria", p, a, prio[0] if prio else None, nuova=False)
            _event(con, rid, "agente", "annullamento", f"Avvisato {s['name']} che il posto si libera. Alla famiglia arriva il promemoria della visita, senza trasporto.")
            _event(con, rid, "agente", "appunto",
                   f"Trasporto non necessario: {p['name']} arriva con un passaggio suo. {s['name']} avvisato, promemoria mandato alla famiglia.")
        elif esito == "non_risponde":
            n = sum(1 for _ in con.execute("SELECT step FROM transport_events WHERE request_id=? AND step='nessuna risposta'", (rid,))) + 1
            _event(con, rid, user["username"], "nessuna risposta", f"Tentativo {n} di {MAX_TENTATIVI}: la persona non risponde.")
            if n >= MAX_TENTATIVI:
                _set(con, rid, status="persona_non_raggiunta", next_call_at=None)
                _risposta_servizio(con, rid, s, a,
                                   f"Non riusciamo a raggiungere {p['name']}: per ora non contate sul trasporto del {a['at'][:10]}. "
                                   "Vi richiamiamo noi se la persona conferma. Grazie.\nCentrale Operativa Territoriale")
                _event(con, rid, "agente", "fermo", f"Tre tentativi senza risposta: avvisato {s['name']} che per ora il posto non serve. "
                                                    "Serve una telefonata dell'accettazione o un contatto della famiglia.")
            else:
                nxt = (datetime.now() + RICHIAMA_DOPO).strftime("%Y-%m-%d %H:%M:%S")
                _set(con, rid, status="richiamare", next_call_at=nxt)
                _event(con, rid, "agente", "chiamata", f"Nuovo tentativo programmato alle {nxt[11:16]}: a quell'ora la pratica torna tra le chiamate da fare.")


def scadenze(con=None) -> int:
    """Controlli a tempo, fatti da Filo da solo (all'apertura delle pagine e a ogni elaborazione):
    servizio che non risponde entro la scadenza, tentativo di chiamata da rifare, visita spostata o annullata."""
    if con is None:
        with db.tx() as c:
            return scadenze(c)
    now = db.now()
    n = 0
    rows = [dict(x) for x in con.execute(f"SELECT id FROM transport_requests WHERE status IN ({','.join('?' * len(ACTIVE))})", ACTIVE)]
    for row in rows:
        r, p, a, s = _load(con, row["id"])
        moved = bool(r.get("visit_at")) and a["at"] != r["visit_at"]
        if a["status"] != "prenotata" or moved:
            what = "annullata" if a["status"] == "annullata" else f"spostata al {a['at'][:10]} alle {a['at'][11:]}" if moved else f"in stato '{a['status']}'"
            _set(con, r["id"], status="visita_cambiata", next_call_at=None)
            _risposta_servizio(con, r["id"], s, a, f"La visita di {p['name']} è stata {what}: il trasporto richiesto non vale più così. "
                                                   "Vi ricontattiamo noi.\nCentrale Operativa Territoriale")
            _event(con, r["id"], "agente", "visita cambiata", f"La visita è stata {what}. Avvisato {s['name']}; torna all'accettazione.")
            n += 1
        elif r["status"] == "attesa_servizio" and r.get("reply_by") and r["reply_by"] < now:
            _event(con, r["id"], "agente", "scadenza", f"{s['name']} non ha risposto entro la scadenza.")
            _switch(con, r["id"], r, p, a, f"{s['name']} non ha risposto in tempo.")
            n += 1
        elif r["status"] == "richiamare" and r.get("next_call_at") and r["next_call_at"] <= now:
            _set(con, r["id"], status="attesa_paziente", next_call_at=None)
            _event(con, r["id"], "agente", "chiamata", "È l'ora del nuovo tentativo: la persona va richiamata.")
            n += 1
    return n
