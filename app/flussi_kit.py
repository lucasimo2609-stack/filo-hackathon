"""Agente kit sulla piattaforma FastwebAI Agents: due flussi, stesso schema di flussi.py (Filo lancia, il workflow richiama
/filo/kit/... con il permesso firmato, Filo controlla con regole fisse ed esegue; se il workflow non applica il passo, Filo lo fa
in locale come prima).

  kit_prepara  l'accettazione dà il via libera al kit: l'agente sceglie tra i kit liberi in quell'orario e scrive un consiglio
               pratico per il medico; Filo controlla (kit davvero libero, consiglio breve) e prenota, con la mail al coordinamento.
  kit_invio    il medico preme "Manda la trascrizione": l'agente rende leggibile la trascrizione (punteggiatura, frasi spezzate)
               senza riassumere; Filo controlla che non abbia tolto, aggiunto o cambiato parole e numeri, poi manda la mail.

Al cloud non vanno nome, indirizzo, telefono, email della persona: solo specialità, orari, kit e il testo della trascrizione
con il nome tolto. L'indirizzo a cui mandare resta nel database dell'ospedale.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

from . import db, flussi, kit, kit_visita

flussi.WORKFLOW.update({  # workflow pubblicati nel workspace "Hackathon - TEAM10"
    "kit_prepara": os.getenv("SIM_WF_FLUSSO_KIT_PREPARA", "1c67419e-f535-440b-a598-cb1300c5cc5a"),
    "kit_invio": os.getenv("SIM_WF_FLUSSO_KIT_INVIO", "dc809c20-ea66-4f13-8439-aa0d78d5b839"),
})
AGENTE = {"username": "agente kit", "name": "agente kit (piattaforma FastwebAI)"}
MAX_CONSIGLIO = 240


# ------------------------------------------------------------------ 1. prepara il kit (via libera dell'accettazione)

def _caso(con, case_id: str):
    c = dict(con.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())
    p = dict(con.execute("SELECT * FROM patients WHERE id=?", (c["patient_id"],)).fetchone())
    a = dict(con.execute("SELECT * FROM appointments WHERE id=?", (json.loads(c["payload"])["appointment"],)).fetchone())
    return c, p, a


def prepara(con, case_id: str, user: dict) -> list[str]:
    """Dentro l'approvazione (transazione aperta): con il flusso acceso parte il workflow, altrimenti si prenota subito."""
    c, p, a = _caso(con, case_id)
    if not flussi.attivo("kit_prepara"):
        return kit.riserva(con, case_id, p, a, user)
    flussi.lancia("kit_prepara", case_id, lambda: prepara_applica(None, case_id, None, None, user), con=con)
    return ["l'agente kit sulla piattaforma sceglie il kit e scrive al coordinamento ambulatori"]


def prepara_dati(case_id: str) -> dict:
    with db.tx() as con:
        kit.assicura(con)
        _, _, a = _caso(con, case_id)
        lib = kit.liberi(con, a["at"])
    quando = datetime.fromisoformat(a["at"])
    return {"richiesta": "supporto per l'udito chiesto al CUP", "specialita": a["specialty"], "visita": a["service"],
            "giorno_settimana": ["lun", "mar", "mer", "gio", "ven", "sab", "dom"][quando.weekday()], "ora": a["at"][11:16],
            "kit_liberi": [{"kit": k["id"], "contenuto": k["contenuto"]} for k in lib]}


def controlla_scelta(dati: dict, scelta: str | None, consiglio: str | None) -> tuple[str | None, str | None, str | None]:
    """Kit valido solo se è tra i liberi; il consiglio entra solo se breve e senza indirizzi o numeri di telefono."""
    ids = [k["kit"] for k in dati["kit_liberi"]]
    perche = None
    if not ids:
        perche, scelta = "nessun kit libero in quell'orario: serve l'accettazione", None
    elif scelta not in ids:
        perche = f"kit {scelta!r} non libero o inesistente: preso il primo libero"
        scelta = ids[0]
    consiglio = " ".join((consiglio or "").split())
    if consiglio and (len(consiglio) > MAX_CONSIGLIO or re.search(r"@|https?://|\d{6,}", consiglio)):
        perche = (perche + "; " if perche else "") + "consiglio scartato (troppo lungo o con dati da non scrivere)"
        consiglio = None
    return scelta, consiglio or None, perche


def prepara_applica(fid: str | None, case_id: str, scelta: str | None, consiglio: str | None, user: dict = AGENTE) -> dict:
    if fid and not flussi.prendi(fid):
        return {"fatto": "niente: il passo era già stato fatto"}
    dati = prepara_dati(case_id)
    kit_id, cons, perche = controlla_scelta(dati, scelta, consiglio) if fid else (None, None, None)
    with db.tx() as con:
        _, p, a = _caso(con, case_id)
        righe = kit.riserva(con, case_id, p, a, user, kit_id=kit_id, consiglio=cons)
        if fid:
            con.execute("INSERT INTO action_log VALUES (?,?,?,?)", (db.now(), AGENTE["username"], case_id,
                        f"{p['name']}: flusso 'kit' sulla piattaforma, l'agente sceglie {scelta or 'nessun kit'}"
                        + (f". Controllo fisso: {perche}" if perche else ". Le regole fisse sono d'accordo.")))
    return {"fatto": "; ".join(righe), "perche": perche}


# ------------------------------------------------------------------ 2. trascrizione alla persona

def _frasi(vid: str) -> tuple[dict, list[str]]:
    with db.tx() as con:
        v = kit_visita.carica(con, vid)
        return v, [s["text"] for s in kit_visita.segmenti(con, vid)]


def _tolti(frasi: list[str], nome: str) -> list[str]:
    """Le parole del nome tolte dalle frasi, nell'ordine in cui compaiono (per rimetterle al posto di [persona])."""
    parti = [re.escape(x) for x in (nome or "").split() if len(x) > 2]
    if not parti:
        return []
    return re.findall("|".join(parti), " ".join(frasi), flags=re.I)


def invio_dati(vid: str) -> dict:
    v, frasi = _frasi(vid)
    p = {"name": v["patient"]}
    return {"visita": v["service"], "data": v["at"][:10], "frasi": [flussi._senza_nomi(f, p) for f in frasi]}


def ricomponi(testo: str | None, tolti: list[str]) -> str | None:
    """Rimette il nome al posto dei segnaposto, nello stesso ordine. Se l'agente ne ha tolti o aggiunti: None."""
    testo = testo or ""
    if testo.count("[persona]") != len(tolti) or "[telefono]" in testo:
        return None
    for parola in tolti:
        testo = testo.replace("[persona]", parola, 1)
    return testo


_PAROLA = re.compile(r"[a-zàèéìòù']{4,}", re.I)
_NUMERO = re.compile(r"\d+(?:[.,]\d+)?")


def controlla_testo(frasi: list[str], testo: str | None) -> str | None:
    """Il testo dell'agente vale solo se è la stessa trascrizione resa leggibile: nessun numero nuovo o cambiato (dosi, orari),
    quasi tutte le parole di prima ancora lì, lunghezza simile. Altrimenti None e si manda la trascrizione così com'è."""
    orig = " ".join(frasi)
    testo = (testo or "").strip()
    if not testo:
        return None
    if sorted(_NUMERO.findall(testo)) != sorted(_NUMERO.findall(orig)):
        return None
    parole = [w.lower() for w in _PAROLA.findall(orig)]
    nuove = {w.lower() for w in _PAROLA.findall(testo)}
    if parole and sum(w in nuove for w in parole) / len(parole) < 0.9:
        return None
    if not 0.8 <= len(testo) / max(len(orig), 1) <= 1.3:
        return None
    return testo


def invio_applica(fid: str | None, vid: str, testo: str | None) -> dict:
    if fid and not flussi.prendi(fid):
        return {"fatto": "niente: il passo era già stato fatto"}
    leggibile = None
    if fid:
        v, frasi = _frasi(vid)
        leggibile = controlla_testo(frasi, ricomponi(testo, _tolti(frasi, v["patient"])))
    with db.tx() as con:
        esito = kit_visita.manda(con, vid, leggibile, AGENTE)
        if fid:
            con.execute("INSERT INTO action_log VALUES (?,?,?,?)", (db.now(), AGENTE["username"], vid,
                        "flusso 'trascrizione' sulla piattaforma: " + ("testo reso leggibile dall'agente, controllo fisso superato"
                                                                         if leggibile else "testo dell'agente scartato dal controllo fisso: "
                                                                         "parte la trascrizione così com'è")))
    return {"fatto": esito, "testo_agente": bool(leggibile)}


def invia(con, vid: str) -> str:
    """Dal tasto del medico (indirizzo già controllato e salvato in kit_visite.invio_a)."""
    if not flussi.attivo("kit_invio"):
        return kit_visita.manda(con, vid, None, AGENTE)
    flussi.lancia("kit_invio", vid, lambda: invio_applica(None, vid, None), con=con)
    return "l'agente kit sulla piattaforma prepara il testo, poi la mail parte da sola"


# ------------------------------------------------------------------ richieste dei workflow (servizio voce, /filo/kit/...)

def router():
    from fastapi import APIRouter, Body, Header, HTTPException

    r = APIRouter(prefix="/filo/kit")

    def ok(perm, tipo, ref):
        f = flussi.verifica(perm, tipo, ref)
        if not f:
            raise HTTPException(403, "permesso non valido, scaduto o di un altro flusso")
        return f

    def con_pacchetto(d):  # anche come testo unico: è quello che il workflow passa all'agente
        return {**d, "pacchetto": json.dumps(d, ensure_ascii=False)}

    @r.get("/casi/{case_id}/visita")
    def _k1(case_id: str, x_filo_permesso: str = Header("")):
        ok(x_filo_permesso, "kit_prepara", case_id)
        return con_pacchetto(prepara_dati(case_id))

    @r.post("/casi/{case_id}/riserva")
    def _k2(case_id: str, corpo: dict = Body(...), x_filo_permesso: str = Header("")):
        f = ok(x_filo_permesso, "kit_prepara", case_id)
        return prepara_applica(f["id"], case_id, corpo.get("kit"), corpo.get("consiglio"))

    @r.get("/visite/{vid}/trascrizione")
    def _t1(vid: str, x_filo_permesso: str = Header("")):
        ok(x_filo_permesso, "kit_invio", vid)
        return con_pacchetto(invio_dati(vid))

    @r.post("/visite/{vid}/invia")
    def _t2(vid: str, corpo: dict = Body(...), x_filo_permesso: str = Header("")):
        f = ok(x_filo_permesso, "kit_invio", vid)
        return invio_applica(f["id"], vid, corpo.get("testo"))

    return r
