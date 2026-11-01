"""Posta vera per la prova dal vivo: Filo manda e legge mail su caselle reali.

Per la prova servono, nel .env:
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM   casella da cui scrive Filo (per Gmail: password per le app)
  MAIL_SERVIZIO_PROVA   casella che fa da Croce Verde (riceve le richieste e risponde)
  MAIL_FAMIGLIA_PROVA   casella della famiglia di Bruno Tonin (riceve la conferma)
  MAIL_AMBULATORI_PROVA casella del coordinamento infermieristico degli ambulatori (riceve le richieste di kit)
  MAIL_PROVA            facoltativa: riceve una copia delle altre mail interne del prototipo

Regole:
- partono mail vere SOLO verso questi indirizzi; gli altri destinatari del prototipo restano finti;
- Filo legge SOLO le mail arrivate dalla casella del servizio, con il codice della pratica nell'oggetto;
- la risposta del servizio è testo libero: la legge un controllo locale con regole fisse, non va mai al cloud.
  Se la risposta non è chiara, o l'orario non sta nella finestra della visita, decide l'accettazione.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
import smtplib
import ssl
import threading
import time
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

from . import db

AGENTE_POSTA = {"username": "agente-posta", "name": "agente (posta)"}
RE_PRATICA = re.compile(r"\bTR-[0-9a-f]{5,8}\b")  # 5 caratteri le pratiche vecchie, 8 quelle nuove (trasporti.start)
RE_MAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _env(k):
    return (os.getenv(k) or "").strip()


def mail_servizio():
    return _env("MAIL_SERVIZIO_PROVA").lower()


def mail_famiglia():
    return _env("MAIL_FAMIGLIA_PROVA").lower()


def indirizzi_veri() -> set[str]:
    return {x for x in (_env("MAIL_PROVA").lower(), _env("MAIL_AMBULATORI_PROVA").lower(), mail_servizio(), mail_famiglia()) if x}


def smtp_pronto() -> bool:
    return bool(_env("SMTP_HOST") and _env("SMTP_USER") and _env("SMTP_PASSWORD"))


def indirizzo(to: str) -> str | None:
    """L'indirizzo vero contenuto nel destinatario, se è tra quelli ammessi."""
    m = RE_MAIL.search(to or "")
    if m and m.group(0).lower() in indirizzi_veri():
        return m.group(0).lower()
    return None


def nuovo_msgid() -> str:
    dom = (_env("SMTP_FROM") or _env("SMTP_USER") or "filo@localhost").split("@")[-1].strip("> ")
    return make_msgid(domain=dom)


def invia(dest: str, oggetto: str, testo: str, html: str | None = None, ics: str | None = None,
          msgid: str | None = None, in_reply_to: str | None = None, references: str | None = None) -> str:
    msg = EmailMessage()
    mitt = _env("SMTP_FROM") or _env("SMTP_USER")
    msg["Subject"], msg["To"] = oggetto, dest
    msg["From"] = formataddr(("Centrale Operativa - Filo", parseaddr(mitt)[1] or mitt))
    msg["Message-ID"] = msgid or nuovo_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    msg.set_content(testo)
    if html:
        msg.add_alternative(html, subtype="html")
    if ics:
        msg.add_attachment(ics.encode(), maintype="text", subtype="calendar", filename="appuntamento.ics")
    with smtplib.SMTP(_env("SMTP_HOST"), int(_env("SMTP_PORT") or 587), timeout=20) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
        s.send_message(msg)
    return dest


# ------------------------------------------------------------------ contatti della prova

def contatti_demo():
    """Mette le caselle della prova al posto dei contatti finti: Croce Verde e famiglie di TUTTI i pazienti."""
    from . import kit
    with db.tx() as con:
        kit.assicura(con)
        if mail_servizio():
            con.execute("UPDATE transport_services SET email=? WHERE id='T2'", (mail_servizio(),))
        if mail_famiglia():
            con.execute("UPDATE patients SET email=?", (mail_famiglia(),))
            con.execute("UPDATE patients SET contatto=NULL WHERE id='P05'")  # Bruno risponde di persona


# ------------------------------------------------------------------ fili delle conversazioni

def segna_filo(con, rid: str, msgid: str, verso: str):
    con.execute("INSERT INTO mail_threads VALUES (?,?,?,?)", (rid, msgid, db.now(), verso))


def filo_di(con, rid: str) -> tuple[str | None, str | None]:
    """(ultimo Message-ID, References) per rispondere nella stessa conversazione col servizio."""
    ids = [r[0] for r in con.execute("SELECT msgid FROM mail_threads WHERE rid=? ORDER BY rowid", (rid,))]
    return (ids[-1], " ".join(ids)) if ids else (None, None)


def indicazioni(con, rid: str) -> str | None:
    r = con.execute("SELECT text FROM mail_in WHERE rid=? AND esito='si' ORDER BY ts DESC LIMIT 1", (rid,)).fetchone()
    return r[0] if r else None


# ------------------------------------------------------------------ lettura della risposta (regole fisse, in locale)

_TAGLIO = re.compile(r"^(>|-{2,}\s*original|_{5,}|da:|from:|inviato:|sent:|il giorno .+ ha scritto|on .+ wrote|il .+ ha scritto:)", re.I)
_NO = re.compile(r"\b(no|non (siamo|abbiamo|possiamo|riusciamo|ci sono|c'è posto|è possibile|si può|si riesce)(?:\s+\w+)?|nessun[ao]? (disponibilità|mezzo|posto)|"
                 r"non disponibil\w*|impossibile|purtroppo|siamo pieni|al completo)\b", re.I)
_SI = re.compile(r"\b(ok|okay|sì|si|va bene|disponibil\w*|confermo|confermiamo|possiamo|riusciamo|passiamo|certo|perfetto)\b", re.I)
_NEUTRI = re.compile(r"\b(nessun problema|non c'è problema|non ci sono problemi|non si preoccupi)\b", re.I)
_ORA = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b|\balle\s+(?:ore\s+)?([01]?\d|2[0-3])\b(?![:.]\d)", re.I)


def pulisci(testo: str) -> str:
    righe = []
    for r in (testo or "").replace("\r", "").split("\n"):
        if _TAGLIO.match(r.strip()):
            break
        righe.append(r.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(righe)).strip()[:800]


def interpreta(testo: str) -> tuple[str | None, str | None]:
    """('si' | 'no' | None, orario HH:MM o None). None = non chiaro, decide l'accettazione."""
    t = _NEUTRI.sub(" ", testo.lower())
    no, si = bool(_NO.search(t)), bool(_SI.search(_NO.sub(" ", t)))  # "non disponibili" non conta come sì
    m = _ORA.search(t)
    ora = None
    if m:
        h, mi = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), "00")
        ora = f"{int(h):02d}:{mi}"
    if no and not si:
        return "no", None
    if si and not no:
        return "si", ora
    return None, ora


def _testo_mail(m: email.message.Message) -> str:
    if m.is_multipart():
        for part in m.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        for part in m.walk():
            if part.get_content_type() == "text/html":
                h = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
                h = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", h)
                return re.sub(r"<[^>]+>", " ", h)
        return ""
    return m.get_payload(decode=True).decode(m.get_content_charset() or "utf-8", "replace")


def gestisci_risposta(rid: str, mittente: str, msgid: str, testo: str) -> str:
    """Applica la risposta del servizio alla pratica. Ritorna l'esito registrato."""
    from . import trasporti
    from datetime import datetime, timedelta
    pulito = pulisci(testo)
    esito, ora = interpreta(pulito)
    with db.tx() as con:
        if con.execute("SELECT 1 FROM mail_in WHERE msgid=?", (msgid,)).fetchone():
            return "già letta"
        r = con.execute("SELECT * FROM transport_requests WHERE id=?", (rid,)).fetchone()
        nota = None
        if r and esito == "si" and ora:
            a = con.execute("SELECT at FROM appointments WHERE id=?", (r["appointment_id"],)).fetchone()
            at = datetime.fromisoformat(a["at"])
            p = datetime.fromisoformat(a["at"][:10] + " " + ora)
            if not (at - timedelta(hours=3) <= p <= at - timedelta(minutes=30)):
                nota = f"il servizio propone le {ora}, fuori dalla finestra utile per la visita delle {a['at'][11:]}"
                esito = None
        con.execute("INSERT INTO mail_in VALUES (?,?,?,?,?,?)", (msgid, rid, db.now(), mittente, pulito, esito or "da_leggere"))
        segna_filo(con, rid, msgid, "in")
        if not r:
            return "pratica sconosciuta"
        if r["status"] != "attesa_servizio":
            doppione = con.execute("SELECT 1 FROM mail_in WHERE rid=? AND text=? AND msgid<>?", (rid, pulito, msgid)).fetchone()
            if doppione:  # stessa risposta arrivata due volte: si annota nella storia, l'accettazione non deve fare niente
                trasporti._event(con, rid, "agente", "mail doppia", "Arrivata di nuovo la stessa risposta del servizio: ignorata.")
                return "doppione"
            trasporti._event(con, rid, "agente", "appunto", f"Nuova mail dal servizio dopo la risposta, da leggere: \"{pulito[:200]}\"")
            return "pratica già avanti"
        trasporti._event(con, rid, "agente", "mail ricevuta", f"Risposta del servizio per mail: \"{pulito[:400]}\"")

    def locale():
        if esito is None:
            with db.tx() as con:
                trasporti._event(con, rid, "agente", "appunto",
                                 "Risposta non chiara per il controllo locale" + (f" ({nota})" if nota else "") +
                                 ". Serve l'accettazione: leggi la mail e usa i pulsanti qui sotto.")
            return "da leggere"
        trasporti.risposta_servizio(rid, esito == "si", ora, AGENTE_POSTA)
        if esito == "si":
            trasporti.chiama_se_possibile(rid)
        return esito

    from . import flussi  # con il flusso acceso legge la risposta l'agente sulla piattaforma; Filo controlla (app/flussi.py)
    if flussi.attivo("risposta_servizio"):
        flussi.lancia("risposta_servizio", rid, locale)
        return "alla piattaforma"
    return locale()


def leggi_casella() -> int:
    """Una passata sulla casella: solo mail non lette dal servizio di prova, con il codice pratica nell'oggetto."""
    serv = mail_servizio()
    if not (smtp_pronto() and serv):
        return 0
    host = _env("IMAP_HOST") or _env("SMTP_HOST").replace("smtp.", "imap.", 1)
    n = 0
    with imaplib.IMAP4_SSL(host, 993, ssl_context=ssl.create_default_context()) as im:
        im.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
        im.select("INBOX")
        _, data = im.search(None, "UNSEEN", "FROM", f'"{serv}"')
        for num in (data[0] or b"").split():
            _, raw = im.fetch(num, "(BODY.PEEK[])")
            m = email.message_from_bytes(raw[0][1])
            oggetto = str(make_header(decode_header(m.get("Subject", ""))))
            pr = RE_PRATICA.search(oggetto)
            if not pr or parseaddr(m.get("From", ""))[1].lower() != serv:
                continue  # non è una risposta a una nostra pratica: la lasciamo stare, non letta
            gestisci_risposta(pr.group(0), serv, (m.get("Message-ID") or f"<{num.decode()}@imap>").strip(), _testo_mail(m))
            im.store(num, "+FLAGS", "\\Seen")
            n += 1
    return n


_stato = {"ultimo": None, "errore": None}


def stato() -> dict:
    return dict(_stato, attivo=bool(smtp_pronto() and mail_servizio()), servizio=mail_servizio(), famiglia=mail_famiglia())


def avvia_ascolto(ogni: int = 15):
    if not (smtp_pronto() and mail_servizio()):
        return

    def giro():
        while True:
            try:
                leggi_casella()
                _stato.update(ultimo=db.now(), errore=None)
            except Exception as e:  # noqa: BLE001 la casella non deve mai fermare Filo
                _stato["errore"] = str(e)[:160]
            time.sleep(ogni)
    threading.Thread(target=giro, daemon=True, name="filo-posta").start()
