"""Le chiamate dell'agente vocale: perché si chiama, cosa può sapere il modello, strumenti e controlli.

Regole che decide Filo, non il modello:
- finché l'identità non è confermata (anno di nascita controllato qui) il prompt NON contiene visite né terapie;
- i segnali d'allarme si riconoscono con regole fisse sulla frase e hanno una risposta fissa;
- ogni valore passato a uno strumento viene controllato prima di toccare il database;
- in segreteria si lascia solo un messaggio neutro.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timedelta

from .. import cup_mail, db, trasporti
from ..config import NUMERO_CENTRALE, TODAY

AGENTE = {"username": "agente-voce", "name": "agente vocale"}
MAX_ERRORI_IDENTITA = 2

KINDS = {
    "trasporto": {"label": "Trasporto per la visita", "role": "segreteria"},
    "appuntamento": {"label": "Proposta di appuntamento", "role": "segreteria"},
    "controllo": {"label": "Chiamata di controllo: come sta", "role": "medico"},
    "modifica": {"label": "Conferma di una modifica alla prenotazione", "role": "segreteria"},
}
STATUS = {"pronta": "Pronta", "in_corso": "In corso", "conclusa": "Conclusa", "da_richiamare": "Da richiamare",
          "allarme": "Allarme: serve una persona subito", "identita_non_confermata": "Identità non confermata"}

# Segnali d'allarme: regole fisse, in italiano parlato. Meglio un falso allarme che un allarme perso.
ALLARMI = [
    (r"dolor\w* (forte |molto |tanto )?(al|nel|sul) petto|male (forte )?al petto|oppressione al petto|stretta al petto", "dolore al petto"),
    (r"non (riesco a |ce la faccio a )?respir|manca(va)? il (fiato|respiro)|fiato corto|fatico a respirare|soffoc", "fatica a respirare"),
    (r"(sono|è|son) cadut|caduta|cascat", "caduta"),
    (r"svenut|perso i sensi|perdo i sensi|mi gira (tanto|forte) la testa e", "svenimento"),
    (r"non (riesco a )?muovere (il braccio|la gamba|metà)|bocca storta|parlo male|non riesco a parlare", "possibile ictus"),
    (r"sangue|emorragi|sanguin", "sangue o sanguinamento"),
    (r"confus\w+ (tanto|molto)|non so dove sono", "confusione"),
]
RISPOSTA_ALLARME = ("Mi ascolti, è importante. Se sta male adesso chiami subito il 112. "
                    "Avviso immediatamente l'infermiere della Centrale, che la richiama tra pochi minuti.")
SEGRETERIA = ("Buongiorno, è la Centrale Operativa dell'ospedale con un messaggio per {nome}. "
              "La preghiamo di richiamarci al numero {numero}. Grazie.")


def allarme_in(testo: str) -> str | None:
    t = (testo or "").lower()
    for pat, label in ALLARMI:
        if re.search(pat, t):
            return label
    return None


# ------------------------------------------------------------------ creazione

def crea(con, patient_id: str, kind: str, ref_id: str | None, user: dict) -> str:
    cid = "CH-" + uuid.uuid4().hex[:6]
    con.execute("INSERT INTO calls (id, patient_id, kind, ref_id, role, status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (cid, patient_id, kind, ref_id, KINDS[kind]["role"], "pronta", user["username"], db.now()))
    evento(con, cid, "creata", f"Chiamata preparata da {user['name']}: {KINDS[kind]['label']}.")
    return cid


def evento(con, cid, kind, text, clinical=False):
    con.execute("INSERT INTO call_events VALUES (?,?,?,?,?)", (cid, db.now(), kind, text, int(clinical)))


def carica(con, cid) -> dict | None:
    c = con.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()
    if not c:
        return None
    c = dict(c)
    c["patient"] = dict(con.execute("SELECT * FROM patients WHERE id=?", (c["patient_id"],)).fetchone())
    c["outcome"] = json.loads(c["outcome"] or "{}")
    c["ref"] = None
    if c["kind"] == "trasporto" and c["ref_id"]:
        r, p, a, s = trasporti._load(con, c["ref_id"])
        c["ref"] = {"request": r, "appointment": a, "service": s}
    elif c["kind"] == "appuntamento" and c["ref_id"]:
        a = dict(con.execute("SELECT * FROM appointments WHERE id=?", (c["ref_id"],)).fetchone())
        c["ref"] = {"appointment": a}
    elif c["kind"] == "modifica" and c["ref_id"]:
        from .. import calendario
        ch = dict(con.execute("SELECT * FROM cal_changes WHERE id=?", (c["ref_id"],)).fetchone())
        a = dict(con.execute("SELECT * FROM appointments WHERE id=?", (ch["appointment_id"],)).fetchone())
        c["ref"] = {"change": ch, "appointment": a, "nuovo": calendario.posto(con, slot_id=ch["to_slot"]) if ch["to_slot"] else None}
    elif c["kind"] == "controllo":
        c["ref"] = {"case": dict(con.execute("SELECT * FROM cases WHERE id=?", (c["ref_id"],)).fetchone())} if c["ref_id"] else {}
        nxt = con.execute("SELECT * FROM appointments WHERE patient_id=? AND status='prenotata' ORDER BY at LIMIT 1", (c["patient_id"],)).fetchone()
        c["ref"]["appointment"] = dict(nxt) if nxt else None
    return c


def dati_per_elevenlabs(c: dict) -> dict:
    """Il MINIMO che va a ElevenLabs all'avvio: il nome per salutare e il codice della chiamata. Il resto lo sa solo Filo."""
    nome = c["patient"]["name"]
    titolo = "la signora" if c["patient"].get("sex") == "F" else "il signor"
    return {"dynamic_variables": {"nome": nome, "saluto_nome": f"{titolo} {nome}", "call_id": c["id"]},
            "custom_llm_extra_body": {"call_id": c["id"]}}


# ------------------------------------------------------------------ prompt (costruito in ospedale a ogni turno)

def _quando(at: str) -> str:
    d = datetime.fromisoformat(at)
    giorni = (d.date() - date.fromisoformat(TODAY)).days
    nomi = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
    rel = "domani" if giorni == 1 else "oggi" if giorni == 0 else f"{nomi[d.weekday()]} {d.day}"
    return f"{rel} alle {d.strftime('%H:%M').replace(':00', '')}"


def prompt(c: dict) -> str:
    p = c["patient"]
    base = [
        "Sei l'assistente telefonico automatico della Centrale Operativa Territoriale dell'ospedale. Parli in italiano con persone anziane.",
        "Regole di voce: frasi corte, una domanda per volta, parole semplici, niente elenchi, niente numeri lunghi. "
        "Se la persona non capisce, ripeti con parole diverse. Sii cordiale e calmo. Non dire mai che sei una persona.",
        "Non dai MAI consigli medici, dosi o diagnosi. Se chiede cose mediche, dici che riferisci al medico e che sarà richiamata.",
        "Usa passa_operatore SOLO se la persona chiede di parlare con qualcuno o se non riesci ad aiutarla.",
        f"Se risponde una segreteria telefonica, usa lo strumento di segreteria e lascia SOLO questo messaggio: "
        f"\"{SEGRETERIA.format(nome=p['name'], numero=NUMERO_CENTRALE)}\"",
        "Quando hai finito, saluta e chiudi la chiamata con lo strumento end_call.",
        "Non dire mai 'ho segnato' o 'ho registrato' se non hai appena chiamato lo strumento: appena la persona risponde, chiama lo strumento.",
    ]
    if not c["identity_ok"]:
        base += [
            "",
            f"PRIMA COSA: verifica di parlare con {p['name']}. Chiedi gentilmente l'anno di nascita e chiama lo strumento "
            "verifica_identita con il numero che dice. Non dire tu l'anno, non suggerirlo.",
            "Finché l'identità non è confermata NON parlare di visite, terapie, salute o trasporti: non li conosci ancora.",
            "Se risponde un'altra persona (un familiare), chiedi se può passarti la persona o a che ora richiamare, poi chiudi.",
        ]
        if c["identity_fails"]:
            base.append(f"L'anno detto finora non corrisponde ({c['identity_fails']} tentativo/i). Chiedi di ripeterlo una volta con calma.")
        return "\n".join(base)

    ref, out = c["ref"] or {}, c["outcome"]
    base += ["", f"Identità confermata: stai parlando con {p['name']}."]
    if c["kind"] == "trasporto":
        a, s, r = ref["appointment"], ref["service"], ref["request"]
        base += [
            f"MOTIVO DELLA CHIAMATA: ha una {a['service']} {_quando(a['at'])}, in {a['place']}.",
            f"Il servizio {s['name']} può passare a prenderla sotto casa alle {r['pickup'] or trasporti._pickup(a)} e riportarla a casa dopo.",
            f"Se la persona vuole un altro orario, sono possibili solo partenze tra le {(datetime.fromisoformat(a['at']) - timedelta(hours=3)).strftime('%H:%M')} "
            f"e le {(datetime.fromisoformat(a['at']) - timedelta(minutes=30)).strftime('%H:%M')} (almeno mezz'ora prima della visita). "
            "Non accettare mai un orario a voce: chiama lo strumento conferma_trasporto, è lui che dice se l'orario va bene.",
            "Chiedi se le serve il passaggio. Se sì, conferma l'orario (se preferisce un altro orario, chiedi quale). "
            "Poi chiama lo strumento conferma_trasporto con la risposta.",
            "Ricorda di portare la tessera sanitaria e gli ultimi referti.",
            "Se conferma il passaggio, digli che gli arriva una mail di conferma con l'orario e le indicazioni del servizio.",
        ]
    elif c["kind"] == "appuntamento":
        a = ref["appointment"]
        base += [
            f"MOTIVO DELLA CHIAMATA: si è liberato un posto per {a['service']} {_quando(a['at'])}, in {a['place']}. "
            "La persona era in lista d'attesa.",
            "Chiedi se le va bene quel giorno e quell'ora. Chiama conferma_appuntamento con la risposta.",
        ]
        if not p["has_car"] or p["lives_alone"]:
            base.append("Se accetta, chiedi se ha qualcuno che la accompagna o se le serve un passaggio, e passalo nello strumento. "
                        "Il passaggio lo organizza la Centrale da sola: di' che la richiameranno per l'orario. Non serve l'operatore.")
    elif c["kind"] == "modifica":
        a, ch, nuovo = ref["appointment"], ref["change"], ref["nuovo"]
        if ch["tipo"] == "sposta":
            base += [
                f"MOTIVO DELLA CHIAMATA: la sua {a['service']} prevista {_quando(a['at'])}, in {a['place']}, andrebbe spostata a "
                f"{_quando(nuovo['at'])}, in {nuovo['place']}. Motivo: {ch['motivo']}.",
                "Spiega il cambio con calma e chiedi se le va bene la nuova data. Se non le va bene, la data di prima resta valida"
                + (" (non dirlo se l'ambulatorio ha chiuso quel posto: di' solo che la Centrale la richiama per trovare un'altra data)." if ch["blocca"] else "."),
                "Chiama conferma_modifica con la risposta: accetta, rifiuta, o vuole_altra_data.",
            ]
        else:
            base += [
                f"MOTIVO DELLA CHIAMATA: la sua {a['service']} prevista {_quando(a['at'])}, in {a['place']}, andrebbe annullata. "
                f"Motivo: {ch['motivo']}.",
                "Spiega con calma e chiedi se è d'accordo con l'annullamento. Se vuole un'altra data, segnalo: la Centrale la richiama.",
                "Chiama conferma_modifica con la risposta: accetta, rifiuta, o vuole_altra_data.",
            ]
    elif c["kind"] == "controllo":
        base += [
            "MOTIVO DELLA CHIAMATA: il medico vuole sapere come sta e se riesce a prendere le medicine.",
            "Fai queste domande, una per volta: come si sente in questi giorni (meglio, uguale o peggio); "
            "se sta prendendo le medicine che le ha dato il medico (sì, no, solo in parte); se ha avuto disturbi. "
            "Poi chiedi se vuole essere richiamata dal medico. Alla fine chiama registra_stato con le risposte.",
            "Non commentare le risposte dal punto di vista medico. Ringrazia e di' che il medico leggerà tutto oggi.",
        ]
        if ref.get("appointment"):
            base.append(f"Se chiede della prossima visita: {ref['appointment']['service']} {_quando(ref['appointment']['at'])}.")
    if out:
        base += ["", "GIÀ FATTO IN QUESTA CHIAMATA (non richiedere di nuovo): " + json.dumps(out, ensure_ascii=False)]
    return "\n".join(base)


# ------------------------------------------------------------------ strumenti

def _fn(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": props, "required": req}}}


T_IDENTITA = _fn("verifica_identita", "Controlla l'anno di nascita detto dalla persona.",
                 {"anno_nascita": {"type": "integer", "description": "Anno di nascita detto dalla persona, es. 1942"}}, ["anno_nascita"])
T_OPERATORE = _fn("passa_operatore", "La persona vuole parlare con una persona della Centrale, o la chiamata va passata a un operatore.",
                  {"motivo": {"type": "string", "description": "In poche parole"}}, ["motivo"])
T_TRASPORTO = _fn("conferma_trasporto", "Registra se alla persona serve il passaggio per la visita e a che ora.",
                  {"serve": {"type": "boolean"}, "orario_partenza": {"type": "string", "description": "HH:MM, solo se la persona chiede un orario diverso"},
                   "accompagnatore": {"type": "boolean", "description": "Vero se chiede che qualcuno la accompagni dentro"}}, ["serve"])
T_APPUNTAMENTO = _fn("conferma_appuntamento", "Registra la risposta alla proposta di appuntamento.",
                     {"scelta": {"type": "string", "enum": ["accetta", "rifiuta", "vuole_altra_data"]},
                      "serve_passaggio": {"type": "boolean", "description": "Vero se le serve un passaggio per arrivare"}}, ["scelta"])
T_MODIFICA = _fn("conferma_modifica", "Registra se il paziente conferma la modifica della sua prenotazione.",
                 {"scelta": {"type": "string", "enum": ["accetta", "rifiuta", "vuole_altra_data"]}}, ["scelta"])
T_STATO = _fn("registra_stato", "Registra come sta la persona e se prende le medicine.",
              {"come_sta": {"type": "string", "enum": ["meglio", "uguale", "peggio", "non_sa"]},
               "medicine": {"type": "string", "enum": ["le_prende", "non_le_prende", "in_parte", "non_sa"]},
               "disturbi": {"type": "string", "description": "Disturbi raccontati, in poche parole, oppure 'nessuno'"},
               "vuole_richiamata_medico": {"type": "boolean"}}, ["come_sta", "medicine", "vuole_richiamata_medico"])


def strumenti(c: dict) -> list[dict]:
    if not c["identity_ok"]:
        return [T_IDENTITA, T_OPERATORE]
    return [T_OPERATORE] + {"trasporto": [T_TRASPORTO], "appuntamento": [T_APPUNTAMENTO], "controllo": [T_STATO],
                            "modifica": [T_MODIFICA]}[c["kind"]]


OBIETTIVO = {"trasporto": ("conferma_trasporto", "trasporto"), "appuntamento": ("conferma_appuntamento", "appuntamento"),
             "controllo": ("registra_stato", "stato"), "modifica": ("conferma_modifica", "modifica")}


def manca_esito(c: dict) -> str | None:
    """Nome dello strumento dell'esito se l'identità è confermata ma l'esito non è ancora registrato."""
    if not c["identity_ok"] or c["alarm"] or c["status"] in ("identita_non_confermata", "da_richiamare"):
        return None
    tool, key = OBIETTIVO[c["kind"]]
    return None if key in c["outcome"] else tool


LOCALI = {"verifica_identita", "passa_operatore", "conferma_trasporto", "conferma_appuntamento", "registra_stato", "conferma_modifica"}


def _hhmm(v):
    return v.strip() if isinstance(v, str) and re.fullmatch(r"([01]?\d|2[0-3])[:.][0-5]\d", v.strip()) else None


def esegui(cid: str, name: str, args: dict) -> dict:
    """Esegue uno strumento con controlli fissi. Ritorna il risultato da mostrare al modello."""
    with db.tx() as con:
        c = carica(con, cid)
        if not c:
            return {"errore": "chiamata sconosciuta"}
        if name not in {t["function"]["name"] for t in strumenti(c)}:
            evento(con, cid, "rifiutato", f"Strumento {name} non permesso in questa fase.")
            return {"errore": "azione non permessa adesso"}
        out = c["outcome"]

        if name == "verifica_identita":
            try:
                anno = int(str(args.get("anno_nascita")).strip())
            except (TypeError, ValueError):
                return {"esito": "non capito", "istruzione": "chiedi di ripetere l'anno"}
            if anno == c["patient"]["birth_year"]:
                con.execute("UPDATE calls SET identity_ok=1 WHERE id=?", (cid,))
                evento(con, cid, "identita", "Identità confermata con l'anno di nascita.")
                return {"esito": "confermata", "istruzione": "ora spiega il motivo della chiamata"}
            fails = c["identity_fails"] + 1
            con.execute("UPDATE calls SET identity_fails=? WHERE id=?", (fails, cid))
            evento(con, cid, "identita", f"Anno di nascita non corrispondente (tentativo {fails}).")
            if fails >= MAX_ERRORI_IDENTITA:
                con.execute("UPDATE calls SET status='identita_non_confermata' WHERE id=?", (cid,))
                return {"esito": "non confermata", "istruzione": "scusati, di' che la Centrale richiamerà, saluta e chiudi la chiamata. Non dire nulla della visita."}
            return {"esito": "non corrisponde", "istruzione": "chiedi gentilmente di ripetere l'anno di nascita"}

        if name == "passa_operatore":
            motivo = str(args.get("motivo") or "")[:200]
            out["operatore"] = motivo or "richiesto"
            con.execute("UPDATE calls SET outcome=?, status='da_richiamare' WHERE id=?", (json.dumps(out, ensure_ascii=False), cid))
            evento(con, cid, "operatore", f"Chiede di parlare con una persona: {motivo}", clinical=c["kind"] == "controllo")
            return {"esito": "registrato", "istruzione": "di' che una persona della Centrale la richiama entro un'ora, saluta e chiudi"}

        if name == "conferma_trasporto":
            if not isinstance(args.get("serve"), bool):
                return {"errore": "serve vero o falso"}
            r = c["ref"]["request"]; a = c["ref"]["appointment"]
            if args["serve"]:
                new = _hhmm(args.get("orario_partenza"))
                if new:
                    new = datetime.strptime(new.replace(".", ":"), "%H:%M").strftime("%H:%M")  # sempre HH:MM
                    visit = datetime.fromisoformat(a["at"])
                    dt = datetime.combine(visit.date(), datetime.strptime(new, "%H:%M").time())
                    if not (visit - timedelta(hours=3) <= dt <= visit - timedelta(minutes=30)):
                        return {"esito": "orario non possibile",
                                "istruzione": "spiega che il passaggio deve partire tra tre ore e mezz'ora prima della visita e chiedi un altro orario"}
                    trasporti._set(con, r["id"], pickup=new)
                    trasporti._event(con, r["id"], "agente", "orario", f"La persona chiede la partenza alle {new}.")
            out["trasporto"] = {"serve": args["serve"], "orario": _hhmm(args.get("orario_partenza")), "accompagnatore": bool(args.get("accompagnatore"))}
            con.execute("UPDATE calls SET outcome=? WHERE id=?", (json.dumps(out, ensure_ascii=False), cid))
            evento(con, cid, "trasporto", "Il trasporto serve." if args["serve"] else "Il trasporto non serve.")
        elif name == "conferma_appuntamento":
            scelta = args.get("scelta")
            if scelta not in ("accetta", "rifiuta", "vuole_altra_data"):
                return {"errore": "scelta non valida"}
            out["appuntamento"] = {"scelta": scelta, "serve_passaggio": bool(args.get("serve_passaggio"))}
            con.execute("UPDATE calls SET outcome=? WHERE id=?", (json.dumps(out, ensure_ascii=False), cid))
            evento(con, cid, "appuntamento", {"accetta": "Accetta l'appuntamento.", "rifiuta": "Rifiuta l'appuntamento.",
                                              "vuole_altra_data": "Vorrebbe un'altra data."}[scelta]
                   + (" Le serve un passaggio." if args.get("serve_passaggio") else ""))
        elif name == "conferma_modifica":
            scelta = args.get("scelta")
            if scelta not in ("accetta", "rifiuta", "vuole_altra_data"):
                return {"errore": "scelta non valida"}
            out["modifica"] = {"scelta": scelta}
            con.execute("UPDATE calls SET outcome=? WHERE id=?", (json.dumps(out, ensure_ascii=False), cid))
            evento(con, cid, "modifica", {"accetta": "Conferma la modifica.", "rifiuta": "Non conferma la modifica.",
                                          "vuole_altra_data": "Vorrebbe un'altra data."}[scelta])
        elif name == "registra_stato":
            ok = {"come_sta": ("meglio", "uguale", "peggio", "non_sa"), "medicine": ("le_prende", "non_le_prende", "in_parte", "non_sa")}
            if any(args.get(k) not in v for k, v in ok.items()) or not isinstance(args.get("vuole_richiamata_medico"), bool):
                return {"errore": "valori non validi"}
            out["stato"] = {k: args.get(k) for k in ("come_sta", "medicine", "vuole_richiamata_medico")}
            out["stato"]["disturbi"] = str(args.get("disturbi") or "")[:200]
            con.execute("UPDATE calls SET outcome=? WHERE id=?", (json.dumps(out, ensure_ascii=False), cid))
            evento(con, cid, "stato", f"Come sta: {args['come_sta']}; medicine: {args['medicine'].replace('_', ' ')}; "
                                      f"disturbi: {out['stato']['disturbi'] or 'nessuno'}; richiamata dal medico: {'sì' if args['vuole_richiamata_medico'] else 'no'}.",
                   clinical=True)
        return {"esito": "registrato", "istruzione": "ringrazia, riepiloga in una frase, saluta e chiudi la chiamata"}


# ------------------------------------------------------------------ allarme e fine chiamata

def segna_allarme(cid: str, label: str, frase: str):
    with db.tx() as con:
        c = carica(con, cid)
        if not c:
            return
        out = c["outcome"]; out["allarme"] = label
        con.execute("UPDATE calls SET alarm=1, status='allarme', outcome=? WHERE id=?", (json.dumps(out, ensure_ascii=False), cid))
        evento(con, cid, "allarme", f"SEGNALE D'ALLARME ({label}). Detto dalla persona: \"{frase[:200]}\"", clinical=True)
        # caso in cima alla lista del medico
        con.execute("INSERT INTO cases (id,run_id,patient_id,role,kind,score,parts,confidence,tag,summary,action,evidence,role_note,payload) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{cid}-AL", "voce", c["patient_id"], "medico", "allarme", 100, json.dumps([["Allarme in chiamata", 100]]), "alta",
                     "Allarme durante la chiamata", f"Durante la chiamata ha detto qualcosa che fa pensare a: {label}. "
                     "L'agente ha consigliato il 112 e promesso una richiamata dell'infermiere.",
                     "Richiamare subito la persona (infermiere o medico).", json.dumps([[cid, "Chiamata dell'agente vocale", frase[:300]]], ensure_ascii=False),
                     None, json.dumps({"call": cid}, ensure_ascii=False)))


def turno(cid: str, speaker: str, text: str):
    if not text:
        return
    with db.tx() as con:
        con.execute("INSERT INTO call_turns VALUES (?,?,?,?)", (cid, db.now(), speaker, text[:2000]))
        con.execute("UPDATE calls SET status=CASE WHEN status='pronta' THEN 'in_corso' ELSE status END, "
                    "started_at=COALESCE(started_at, ?) WHERE id=?", (db.now(), cid))


def concludi(cid: str, conversation_id: str | None = None) -> list[str]:
    """Applica gli esiti a Filo. Idempotente: una seconda chiamata non rifà nulla."""
    done = []
    with db.tx() as con:
        c = carica(con, cid)
        if not c or c["ended_at"]:
            return done
        out = c["outcome"]
        status = c["status"]
        if c["kind"] == "trasporto" and "trasporto" in out:
            done.append("pratica di trasporto aggiornata")
        elif c["kind"] == "appuntamento" and "appuntamento" in out:
            done.append("appuntamento aggiornato")
        elif c["kind"] == "controllo" and "stato" in out:
            done.append("risposte registrate per il medico")
        elif c["kind"] == "modifica" and "modifica" in out:
            done.append("risposta sulla modifica registrata")
        if status in ("pronta", "in_corso"):
            status = "conclusa" if done else "da_richiamare"
        con.execute("UPDATE calls SET status=?, ended_at=?, conversation_id=COALESCE(?, conversation_id) WHERE id=?",
                    (status, db.now(), conversation_id, cid))
        evento(con, cid, "fine", "Chiamata conclusa." + (" " + "; ".join(done) + "." if done else " Nessun esito registrato: da richiamare."))
    # effetti sulle pratiche, fuori dalla transazione della chiamata: dal flusso "esito della chiamata" sulla piattaforma
    # se è acceso (app/flussi.py), altrimenti qui. Con il flusso, se il workflow non applica il passo lo fa Filo in locale.
    from .. import flussi
    if flussi.attivo("esito_chiamata") and c["kind"] in ("trasporto", "appuntamento", "modifica"):
        flussi.lancia("esito_chiamata", cid, lambda: applica_effetti(cid))
    else:
        _effetti(c, out)
    return done


def applica_effetti(cid: str):
    """Gli effetti di una chiamata conclusa (idempotenti: ogni procedura controlla lo stato prima di agire)."""
    with db.tx() as con:
        c = carica(con, cid)
    _effetti(c, c["outcome"] or {})


def _effetti(c, out):
    cid = c["id"]
    if c["kind"] == "trasporto" and "trasporto" in out and c["ref_id"]:
        trasporti.esito_chiamata(c["ref_id"], "serve" if out["trasporto"]["serve"] else "non_serve", AGENTE)
    elif c["kind"] == "appuntamento":
        from .. import posti  # posti liberati: accettazione, rifiuto o nessuna risposta, con i controlli della procedura
        if not posti.esito_chiamata(cid) and "appuntamento" in out:
            _applica_appuntamento(c, out["appuntamento"])  # chiamata di una versione precedente, senza proposta registrata
    elif c["kind"] == "controllo" and "stato" in out:
        _applica_controllo(c, out["stato"])
    elif c["kind"] == "modifica":
        from .. import modifiche  # conferma, rifiuto o nessuna risposta: calendario, mail all'ambulatorio e al paziente
        modifiche.esito_chiamata(cid)


def _applica_appuntamento(c, esito):
    a = c["ref"]["appointment"]; p = c["patient"]
    with db.tx() as con:
        from .. import calendario  # anche le chiamate di una versione precedente passano dal calendario
        if esito["scelta"] == "accetta":
            calendario.conferma(con, a["id"], AGENTE)
            con.execute("UPDATE waitlist SET status='prenotato' WHERE patient_id=? AND service=? AND status='proposto'", (p["id"], a["service"]))
            db_note = f"{p['name']}: accetta {a['service']} del {a['at']} (chiamata {c['id']})"
        else:
            calendario.cancella(con, a["id"], AGENTE, "posto non accettato al telefono", alla_lista=False)
            con.execute("UPDATE freed_slots SET assigned_to=NULL WHERE assigned_to=? AND at=?", (p["id"], a["at"]))
            con.execute("UPDATE waitlist SET status='in attesa' WHERE patient_id=? AND service=? AND status='proposto'", (p["id"], a["service"]))
            db_note = f"{p['name']}: non accetta il posto del {a['at']}; torna in lista, il posto è di nuovo libero (chiamata {c['id']})"
        con.execute("INSERT INTO action_log VALUES (?,?,?,?)", (db.now(), AGENTE["username"], c["id"], db_note))
        if esito["scelta"] == "accetta" and not esito.get("serve_passaggio"):  # col passaggio la conferma parte quando il trasporto è confermato
            prio = con.execute("SELECT priority FROM waitlist WHERE patient_id=? AND service=?", (p["id"], a["service"])).fetchone()
            cup_mail.manda(con, c["id"], "segreteria", p, {**a, "status": "prenotata"}, prio[0] if prio else None)
        if esito["scelta"] == "accetta" and esito.get("serve_passaggio"):
            valid = trasporti.services_for(con, trasporti._profile(p), a["at"])  # stesse regole del cancello: distretto, preavviso, fragilità
            svc = valid[0] if valid else None
            if svc:
                rid = trasporti.start(con, None, p["id"], a["id"], svc["id"], AGENTE)
                con.execute("INSERT INTO action_log VALUES (?,?,?,?)", (db.now(), AGENTE["username"], c["id"],
                            f"{p['name']}: chiesto il passaggio, aperta la pratica {rid} con {svc['name']}"))


def _applica_controllo(c, stato):
    p = c["patient"]
    with db.tx() as con:
        grave = stato["come_sta"] == "peggio" or stato["medicine"] == "non_le_prende" or stato["vuole_richiamata_medico"]
        con.execute("INSERT INTO cases (id,run_id,patient_id,role,kind,score,parts,confidence,tag,summary,action,evidence,role_note,payload) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"{c['id']}-ST", "voce", p["id"], "medico", "esito_chiamata", 70 if grave else 30,
                     json.dumps([["Esito della chiamata", 70 if grave else 30]]), "alta", "Esito della chiamata di controllo",
                     f"Come sta: {stato['come_sta'].replace('_', ' ')}. Medicine: {stato['medicine'].replace('_', ' ')}. "
                     f"Disturbi: {stato['disturbi'] or 'nessuno'}. Vuole essere richiamata dal medico: {'sì' if stato['vuole_richiamata_medico'] else 'no'}.",
                     "Leggere la trascrizione e decidere se richiamare." if grave else "Nessuna azione urgente: prendere visione.",
                     json.dumps([[c["id"], "Chiamata dell'agente vocale", "Trascrizione nella pagina della chiamata"]], ensure_ascii=False),
                     None, json.dumps({"call": c["id"]}, ensure_ascii=False)))
