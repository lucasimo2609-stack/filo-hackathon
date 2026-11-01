"""Mail di conferma in stile CUP: prenotazione e, se c'è, trasporto organizzato.

Contenuto ricalcato sui promemoria dei CUP (codice prenotazione, assistito, prestazione, priorità, data e luogo,
impegnativa, cosa portare, come disdire e cosa succede se non si disdice). Ente e indirizzi INVENTATI.

Invio: di base la mail resta in "Posta in uscita". Se nel .env ci sono SMTP_* e MAIL_PROVA, ne parte una copia
vera SOLO a MAIL_PROVA (gli indirizzi dei pazienti del prototipo sono finti).
"""
from __future__ import annotations

import hashlib
import html
import os
import smtplib
import ssl
from datetime import date, datetime
from email.message import EmailMessage

ENTE = "ASL Laguna Veneta"
MITTENTE = "CUP ASL Laguna Veneta <noreply-cup@asl-laguna.example>"
NUMERO_CUP = os.getenv("NUMERO_CUP", "800 000 000")
SITO_DISDETTE = "www.asl-laguna.example/disdici"
GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
PREPARAZIONE = {
    "ecocardiogramma": "Nessuna preparazione particolare. Indossare abiti comodi.",
    "visita cardiologica": "Portare gli ECG e gli esami del cuore precedenti e l'elenco dei farmaci che prende.",
    "visita oculistica": "Dopo la visita la vista può restare annebbiata per qualche ora per le gocce: non venire guidando.",
    "visita diabetologica": "Portare il diario delle glicemie e gli ultimi esami del sangue.",
    "visita pneumologica": "Portare gli inalatori che usa e le spirometrie precedenti.",
}
PRIORITA = {"U": "U - urgente (entro 72 ore)", "B": "B - breve (entro 10 giorni)", "D": "D - differibile (entro 30/60 giorni)",
            "P": "P - programmata"}


def codice_prenotazione(appointment_id: str) -> str:
    return "2026" + hashlib.sha256(appointment_id.encode()).hexdigest()[:8].upper()


def nre_sintetico(appointment_id: str) -> str:
    return "0500A" + str(int(hashlib.sha256(("nre" + appointment_id).encode()).hexdigest(), 16))[:10]


def cf_mascherato(cf: str | None) -> str:
    return (cf[:6] + "*****" + cf[-5:]) if cf and len(cf) == 16 else "non disponibile"


def data_estesa(at: str) -> str:
    d = datetime.fromisoformat(at)
    return f"{GIORNI[d.weekday()]} {d.day} {MESI[d.month - 1]} {d.year}, ore {d.strftime('%H:%M')}"


def conferma(p: dict, a: dict, priorita: str | None = None, trasporto: dict | None = None, nuova: bool = True) -> tuple[str, str, str]:
    """Ritorna (oggetto, testo semplice, html)."""
    cod = codice_prenotazione(a["id"])
    servizio = a["service"].capitalize()
    prep = next((v for k, v in PREPARAZIONE.items() if k in a["service"].lower()), "Nessuna preparazione particolare.")
    tipo = "Conferma trasporto e promemoria prenotazione" if trasporto else ("Conferma prenotazione" if nuova else "Promemoria prenotazione")
    oggetto = f"{tipo} n. {cod} - {servizio} del {datetime.fromisoformat(a['at']).strftime('%d/%m/%Y')}"
    righe = [
        ("Codice prenotazione", cod),
        ("Assistito", f"{p['name']} - C.F. {cf_mascherato(p.get('cf'))}"),
        ("Prestazione", servizio),
        ("Priorità", PRIORITA.get(priorita or "", "come da impegnativa")),
        ("Quando", data_estesa(a["at"])),
        ("Dove", f"{a['place']} - via della Laguna 1, Mestre (VE)"),
        ("Impegnativa (NRE)", nre_sintetico(a["id"])),
        ("Medico prescrittore", p.get("mmg") or "come da impegnativa"),
        ("Ticket", "secondo la sua esenzione: l'importo è indicato allo sportello o nel Fascicolo Sanitario"),
    ]
    portare = ["Tessera sanitaria e documento d'identità", "Impegnativa del medico (o il numero NRE qui sopra)",
               "Referti ed esami precedenti che riguardano la visita"]
    t_lines = []
    if trasporto:
        t_lines = [f"Servizio: {trasporto['servizio']}",
                   f"Passa a prenderla: {data_estesa(a['at'][:10] + ' ' + trasporto['partenza'])}, sotto casa ({p.get('address') or 'indirizzo registrato'})",
                   "Ritorno a casa dopo la visita."]
        if trasporto.get("indicazioni"):
            t_lines += [f"Indicazioni del servizio: {trasporto['indicazioni']}"]
        t_lines += [f"Se non le serve più, avvisi la Centrale Operativa al {NUMERO_CUP}."]

    disdetta = (f"Se non può presentarsi, disdica ALMENO 2 GIORNI LAVORATIVI PRIMA: chiamando il CUP al {NUMERO_CUP} "
                f"(lunedì-venerdì 8-18) oppure online su {SITO_DISDETTE} con il codice prenotazione. "
                "Se non disdice in tempo e non si presenta, dovrà pagare il ticket anche se esente e potrà ricevere una sanzione.")

    testo = [f"{ENTE} - Centro Unico di Prenotazione", "", f"Gentile {p['name']},",
             "la prestazione è stata prenotata con questi dati:" if nuova else "le ricordiamo la prenotazione:", ""]
    testo += [f"{k}: {v}" for k, v in righe]
    testo += ["", "Arrivi 15 minuti prima, per l'accettazione.", f"Preparazione: {prep}", "", "Cosa portare:"] + [f"- {x}" for x in portare]
    if t_lines:
        testo += ["", "TRASPORTO ORGANIZZATO"] + t_lines
    testo += ["", "Disdetta", disdetta, "",
              "Questo è un messaggio automatico: non risponda a questa mail.",
              "Messaggio di PROVA generato dal prototipo Filo (hackathon FastwebAI). Dati ed ente inventati."]

    e = html.escape
    rows = "".join(f'<tr><td style="padding:6px 10px;color:#5d6a64;width:38%;border-bottom:1px solid #eee">{e(k)}</td>'
                   f'<td style="padding:6px 10px;border-bottom:1px solid #eee"><b>{e(v)}</b></td></tr>' for k, v in righe)
    tbox = ("" if not t_lines else
            '<div style="margin:18px 0;padding:14px 16px;background:#e4efe9;border-left:4px solid #1f5f4a;border-radius:6px">'
            '<div style="font-weight:bold;color:#1f5f4a;margin-bottom:6px">Trasporto organizzato</div>'
            + "".join(f"<div>{e(x)}</div>" for x in t_lines) + "</div>")
    corpo = f"""<!doctype html><html lang="it"><head><meta charset="utf-8"><title>{e(oggetto)}</title></head>
<body style="margin:0;background:#f4f4f4;font-family:Arial,Helvetica,sans-serif;color:#222">
<div style="max-width:620px;margin:0 auto;background:#fff">
<div style="background:#1f5f4a;color:#fff;padding:16px 22px"><div style="font-size:18px;font-weight:bold">{ENTE}</div>
<div style="font-size:13px;opacity:.85">CUP - Centro Unico di Prenotazione</div></div>
<div style="padding:22px">
<p>Gentile <b>{e(p['name'])}</b>,<br>{'la prestazione è stata prenotata con questi dati:' if nuova else 'le ricordiamo la prenotazione:'}</p>
<table style="width:100%;border-collapse:collapse;font-size:14px">{rows}</table>
<p style="margin-top:16px"><b>Arrivi 15 minuti prima</b>, per l'accettazione.<br><b>Preparazione:</b> {e(prep)}</p>
<p style="margin-bottom:4px"><b>Cosa portare</b></p><ul style="margin-top:0">{''.join(f'<li>{e(x)}</li>' for x in portare)}</ul>
{tbox}
<div style="margin-top:18px;padding:12px 14px;background:#fbf0dc;border-radius:6px;font-size:13px"><b>Disdetta.</b> {e(disdetta)}</div>
</div>
<div style="padding:14px 22px;font-size:11px;color:#777;border-top:1px solid #eee">Questo è un messaggio automatico: non risponda a questa mail.<br>
Messaggio di PROVA generato dal prototipo Filo (hackathon FastwebAI). Dati ed ente inventati.</div>
</div></body></html>"""
    return oggetto, "\n".join(testo), corpo


def invia_vera(oggetto: str, testo: str, corpo_html: str, ics: str | None = None) -> str | None:
    """Copia vera SOLO a MAIL_PROVA, se SMTP è configurato. Ritorna l'indirizzo usato o None."""
    host, dest = os.getenv("SMTP_HOST"), (os.getenv("MAIL_PROVA") or "").strip()
    if not (host and dest):
        return None
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = oggetto, os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "filo@localhost")), dest
    msg.set_content(testo)
    msg.add_alternative(corpo_html, subtype="html")
    if ics:
        msg.add_attachment(ics.encode(), maintype="text", subtype="calendar", filename="appuntamento.ics")
    with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=20) as s:
        s.starttls(context=ssl.create_default_context())
        if os.getenv("SMTP_USER"):
            s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD", ""))
        s.send_message(msg)
    return dest


def destinatario(p: dict) -> str:
    return p.get("email") or f"famiglia di {p['name']}"


def manda(con, ref_id: str, role: str, p: dict, a: dict, priorita: str | None = None, trasporto: dict | None = None,
          nuova: bool = True) -> str:
    """Mette la conferma in Posta in uscita (con l'evento di calendario allegato) e, se configurato, ne invia una copia vera."""
    from . import db
    from .actions import _ics, _mail
    oggetto, testo, corpo = conferma(p, a, priorita, trasporto, nuova)
    ics = _ics(a["service"].capitalize(), a["at"], 45, a["place"], f"Codice prenotazione {codice_prenotazione(a['id'])}")
    dest = destinatario(p)
    if p.get("contatto"):
        dest = f"{dest} (per {p['contatto']})"
    msg = _mail(con, ref_id, role, dest, oggetto, testo, ics, html_body=corpo, sender=MITTENTE)
    return msg
