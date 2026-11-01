"""Azioni eseguite dopo il via libera umano. Mail e calendario finiscono in data/posta_in_uscita:
nessun invio reale, ma file .ics veri che si aprono con Google, Outlook e iPhone."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from . import cup_mail, db
from .config import OUTBOX


VTIMEZONE = ("BEGIN:VTIMEZONE\r\nTZID:Europe/Rome\r\n"
             "BEGIN:DAYLIGHT\r\nTZOFFSETFROM:+0100\r\nTZOFFSETTO:+0200\r\nTZNAME:CEST\r\nDTSTART:19700329T020000\r\n"
             "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU\r\nEND:DAYLIGHT\r\n"
             "BEGIN:STANDARD\r\nTZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100\r\nTZNAME:CET\r\nDTSTART:19701025T030000\r\n"
             "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n")
SPECIALTY_OF = {"ecocardiogramma": "cardiologia", "visita_cardiologica": "cardiologia", "tac_torace": "radiologia", "rmn_ginocchio": "radiologia"}


def _ics(summary: str, start: str, minutes: int, location: str, description: str) -> str:
    dt = datetime.fromisoformat(start)
    fmt = "%Y%m%dT%H%M%S"
    end = dt + timedelta(minutes=minutes)
    alarm = "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Promemoria\r\nTRIGGER:-P1D\r\nEND:VALARM\r\n"
    esc = lambda s: s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Filo//Hackathon//IT\r\nCALSCALE:GREGORIAN\r\n" + VTIMEZONE + "BEGIN:VEVENT\r\n"
            f"UID:{uuid.uuid4()}@filo\r\nDTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}Z\r\n"
            f"DTSTART;TZID=Europe/Rome:{dt.strftime(fmt)}\r\nDTEND;TZID=Europe/Rome:{end.strftime(fmt)}\r\n"
            f"SUMMARY:{esc(summary)}\r\nLOCATION:{esc(location)}\r\nDESCRIPTION:{esc(description)}\r\n"
            f"{alarm}END:VEVENT\r\nEND:VCALENDAR\r\n")


def _mail(con, case_id, role, to, subject, body, ics=None, html_body=None, sender=None, msgid=None, in_reply_to=None, references=None):
    ics_name = None
    if ics:
        OUTBOX.mkdir(parents=True, exist_ok=True)
        ics_name = f"{case_id}-{uuid.uuid4().hex[:6]}.ics"
        (OUTBOX / ics_name).write_text(ics, encoding="utf-8")
    cur = con.execute("INSERT INTO outbox (ts, case_id, role, recipient, subject, body, ics, html, sender) VALUES (?,?,?,?,?,?,?,?,?)",
                      (db.now(), case_id, role, to, subject, body, ics_name, html_body, sender))
    from . import posta
    vero = posta.indirizzo(to)
    if vero and posta.smtp_pronto():
        _invio_vero(cur.lastrowid, vero, subject, body, html_body, ics, msgid, in_reply_to, references)
    else:
        _copia_vera(cur.lastrowid, to, subject, body, html_body, ics)
    return f"mail a {to}" + (" con evento in calendario" if ics_name else "")


def _invio_vero(mail_id, dest, subject, body, html_body, ics, msgid, in_reply_to, references):
    """Destinatario vero della prova (servizio o famiglia): la mail parte a lui, nella stessa conversazione se è una risposta."""
    import threading

    def run():
        from .posta import invia
        try:
            esito = invia(dest, subject, body, html_body, ics, msgid, in_reply_to, references)
        except Exception as e:  # noqa: BLE001
            esito = f"invio non riuscito: {str(e)[:80]}"
        with db.tx() as c2:
            c2.execute("UPDATE outbox SET sent_to=? WHERE id=?", (esito, mail_id))
    threading.Thread(target=run, daemon=True).start()


def _copia_vera(mail_id, to, subject, body, html_body, ics):
    """Se configurato, manda una copia VERA a MAIL_PROVA (in un thread: non rallenta Filo). I destinatari del prototipo sono finti."""
    import os
    import threading
    if not (os.getenv("SMTP_HOST") and os.getenv("MAIL_PROVA")):
        return

    def run():
        from .cup_mail import invia_vera
        try:
            dest = invia_vera(f"[per {to}] {subject}", f"Mail interna della Centrale Operativa. Destinatario: {to}\n\n{body}",
                              html_body or f"<pre style='font-family:Arial,sans-serif;white-space:pre-wrap'>Mail interna della Centrale Operativa. Destinatario: {to}\n\n{body}</pre>",
                              ics)
            esito = dest or None
        except Exception as e:  # noqa: BLE001
            esito = f"invio non riuscito: {str(e)[:80]}"
        if esito:
            with db.tx() as c2:
                c2.execute("UPDATE outbox SET sent_to=? WHERE id=?", (esito, mail_id))
    threading.Thread(target=run, daemon=True).start()


def _mmg(p) -> str:
    return f"{p['mmg']} (medico di famiglia)" if p.get("mmg") and p["mmg"] != "non indicato" else "medico di famiglia (non registrato: da completare)"


def execute(case_id: str, user) -> list[str] | None:
    """Esegue il caso approvato. Ritorna None se il caso non era più in attesa (doppio clic, due persone insieme):
    la presa del caso e il cambio di stato stanno nella stessa transazione, quindi parte una volta sola."""
    done = []
    with db.tx() as con:
        if con.execute("UPDATE cases SET status='in_corso' WHERE id=? AND status='pending'", (case_id,)).rowcount == 0:
            return None
        c = dict(con.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())
        p = dict(con.execute("SELECT * FROM patients WHERE id=?", (c["patient_id"],)).fetchone())
        pl = json.loads(c["payload"])
        ev = json.loads(c["evidence"])
        role = c["role"]

        if c["kind"] == "trasporto" and not pl.get("service"):
            done.append("presa in carico a mano: nessun servizio rispetta distretto, preavviso e regole per questa visita")
        elif c["kind"] == "trasporto":
            from . import trasporti
            s = dict(con.execute("SELECT * FROM transport_services WHERE id=?", (pl["service"],)).fetchone())
            rid = trasporti.start(con, case_id, p["id"], pl["appointment"], pl["service"], user, pl.get("pickup"))
            done.append(f"richiesta di disponibilità mandata a {s['name']} (pratica {rid}); poi l'agente chiama la persona")
        elif c["kind"] == "posto":  # il via libera vale per UNA chiamata a questa persona (app/posti.py)
            from . import posti
            done += posti.approva(con, c, p, user)
        elif c["kind"] == "modifica":  # il via libera vale per UNA chiamata al paziente (app/modifiche.py)
            from . import modifiche
            done += modifiche.approva(con, c, p, user)
        elif c["kind"] == "kit":
            from . import flussi_kit  # con il flusso acceso sceglie l'agente kit sulla piattaforma, Filo controlla e prenota
            done += flussi_kit.prepara(con, case_id, user)
        elif c["kind"] in ("cure", "aderenza"):
            proofs = "\n".join(f"- {e[0]} ({e[1]}): {e[2]}" for e in ev)
            subj = "Da verificare: cure che si incrociano" if c["kind"] == "cure" else "Terapia sospesa"
            done.append(_mail(con, case_id, role, _mmg(p), f"{subj} - {p['name']}",
                              f"Segnalazione della Centrale Operativa Territoriale, firmata da {user['name']}.\n\n"
                              f"{c['summary']}\n\nProve:\n{proofs}\n\nNon è una diagnosi: si chiede una verifica."))
            if c["kind"] == "aderenza":
                done.append(_mail(con, case_id, role, "infermiere di famiglia del distretto " + (p.get("distretto") or "non indicato"),
                                  f"Terapia sospesa - {p['name']}", f"{c['summary']} Contattare la persona prima della prossima visita."))
        elif c["kind"] == "doppione":
            done.append(_mail(con, case_id, role, "reparto che ha fatto la seconda richiesta", f"Esame già richiesto - {p['name']}",
                              f"{c['summary']}\nRichieste: " + ", ".join(e[0] for e in ev) + f"\nFirmato: {user['name']}"))
        else:
            done.append("presa visione")

        con.execute("UPDATE cases SET status='ok', decided_by=?, decided_at=? WHERE id=?", (user["username"], db.now(), case_id))
        verb = "firmato" if role == "medico" else "approvato"
        for d in done:
            con.execute("INSERT INTO action_log VALUES (?,?,?,?)", (db.now(), user["username"], case_id, f"{p['name']}: {d} ({verb} da {user['name']})"))
    return done
