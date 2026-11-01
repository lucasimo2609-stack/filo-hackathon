"""Aiuti di presentazione per le liste di lavoro: livello di priorità, provenienza della segnalazione, da quanto aspetta.

Scala a cinque livelli con l'ordine e i colori del triage italiano (Linee di indirizzo nazionali, Accordo Stato-Regioni 2019),
ma con nomi propri: Filo NON è un triage di pronto soccorso e non promette tempi di intervento. Le soglie sul punteggio
0-100 sono una scelta di design del prototipo, non vengono da una fonte.

Il colore non è mai l'unica informazione (WCAG 2.2, 1.4.1): ogni livello ha parola, colore e simbolo.
Come nel NEWS2 ("red score"), un singolo fattore critico porta il caso almeno a Urgente anche se il totale è basso:
in Filo è la regola locale fissa (combinazione pericolosa, terapia critica sospesa, trasporto indispensabile).
"""
from __future__ import annotations

from datetime import datetime

# soglia minima, chiave, etichetta, simbolo, cosa vuol dire per chi lavora la lista
LEVELS = [
    (85, "immediata", "Immediata", "!", "da fare subito"),
    (65, "urgente", "Urgente", "▲", "da fare per prima, oggi"),
    (40, "differibile", "Differibile", "●", "in giornata"),
    (20, "minore", "Minore", "▼", "quando possibile"),
    (0, "nonurgente", "Non urgente", "–", "nessuna fretta"),
]
_ORDER = [k for _, k, *_ in LEVELS]


def _pack(t) -> dict:
    floor, key, label, symbol, hint = t
    return {"key": key, "label": label, "symbol": symbol, "hint": hint}


def level(score: int | None) -> dict:
    s = score or 0
    return next(_pack(t) for t in LEVELS if s >= t[0])


def level_of(c: dict) -> dict:
    """Livello di un caso: dal punteggio, ma una regola fissa critica lo porta almeno a Urgente."""
    lv = level(c.get("score"))
    if "Regola locale fissa" in (c.get("action") or "") and _ORDER.index(lv["key"]) > _ORDER.index("urgente"):
        lv = {**_pack(LEVELS[1]), "raised": True}
    return lv


def origine(c: dict) -> dict:
    """Chi ha prodotto la segnalazione ("perché lo vedo"): è la fonte obbligatoria delle card CDS Hooks."""
    action, tag = c.get("action") or "", c.get("tag") or ""
    if c.get("run_id") == "voce":
        return {"key": "voce", "label": "Dalla chiamata", "hint": "Nato durante una chiamata dell'agente vocale"}
    if tag == "Da controllare a mano" or "non ha dato una risposta valida" in action:
        return {"key": "mano", "label": "Da controllare", "hint": "L'agente non ha dato una risposta valida: decide una persona"}
    if "Regola locale fissa" in action:
        return {"key": "regola", "label": "Regola fissa", "hint": "Tenuto da una regola dell'ospedale, anche se l'agente diceva di no"}
    if "Pacchetto non inviato" in action:
        return {"key": "regola", "label": "Deciso in ospedale", "hint": "Troppo riconoscibile per il cloud: ha deciso il controllo locale"}
    if c.get("kind") == "kit":
        return {"key": "regola", "label": "Richiesta al CUP", "hint": "La persona ha chiesto il supporto per l'udito"}
    return {"key": "agente", "label": "Agente", "hint": "Proposto da un agente sui dati anonimi, controllato in ospedale"}


def fa(ts: str | None, now: datetime | None = None) -> str:
    """'3 min fa', '2 h fa', 'ieri 23:10', '12/10'."""
    if not ts:
        return ""
    try:
        t = datetime.fromisoformat(ts[:19])
    except ValueError:
        return ""
    now = now or datetime.now()
    sec = (now - t).total_seconds()
    if sec < 60:
        return "adesso"
    if sec < 3600:
        return f"{int(sec // 60)} min fa"
    if sec < 86400 and t.date() == now.date():
        return f"{int(sec // 3600)} h fa"
    if (now.date() - t.date()).days == 1:
        return "ieri " + t.strftime("%H:%M")
    return t.strftime("%d/%m")


# Motivi di scarto da elenco (come gli overrideReasons delle card CDS Hooks): leggibili, confrontabili, contabili.
SCARTO = ["Già gestito in altro modo", "Non pertinente per questo paziente", "Dati non aggiornati", "Falso allarme dell'agente", "Altro"]
