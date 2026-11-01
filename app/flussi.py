"""Flussi sulla piattaforma FastwebAI Agents: la regia di tre momenti sta in workflow pubblicati, Filo resta l'esecutore.

  risposta del servizio  il servizio di trasporto risponde alla mail: l'agente legge la risposta, Filo controlla e fa il passo
                         (chiamata alla persona, servizio successivo, oppure appunto all'accettazione)
  esito della chiamata   la chiamata finisce: il workflow sceglie il ramo (conferma, annullo, nuovo tentativo...), Filo esegue
                         (mail al servizio, conferma CUP alla famiglia, prenotazione, avvisi)
  posto liberato         una persona disdice: il workflow chiede i candidati anonimi a Filo, li passa all'agente Agende,
                         Filo controlla la scelta con le regole fisse e prepara la proposta (la chiamata resta da approvare)

Come gira: Filo lancia il workflow in un thread (non blocca mai la pagina né il database) e gli passa l'indirizzo pubblico del
tunnel e un permesso firmato che vale solo per quel flusso e scade in 10 minuti. Il workflow richiama Filo su /filo/...
(servizio voce, l'unico esposto su internet). Ogni passo si "prenota" (in_corso -> applicato): se il workflow non risponde,
sbaglia o non applica il passo, il thread lo fa in locale come prima. Un passo non si fa mai due volte.

Acceso con FILO_FLUSSI=on. Spento (test, installazioni senza piattaforma): tutto resta in locale, identico a prima.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta

from . import db
from .config import voce_token

WORKFLOW = {  # workflow pubblicati nel workspace "Hackathon - TEAM10" (non sono segreti: si cambiano con SIM_WF_FLUSSO_*)
    "risposta_servizio": os.getenv("SIM_WF_FLUSSO_RISPOSTA", "e22d93aa-7d17-45c4-a86e-0b275ae348f6"),
    "esito_chiamata": os.getenv("SIM_WF_FLUSSO_CHIAMATA", "a045e2d1-d8c5-4119-9f9a-a6df05d46315"),
    "posto_liberato": os.getenv("SIM_WF_FLUSSO_POSTO", "32046ff7-25a8-438c-8b79-0f1d73203942"),
}
DURATA = 600  # secondi di validità del permesso
ATTESA_WORKFLOW = 90
FLUSSO = {"username": "flusso-piattaforma", "name": "flusso sulla piattaforma FastwebAI Agents"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS flussi (id TEXT PRIMARY KEY, tipo TEXT, ref TEXT, stato TEXT, creato TEXT, aggiornato TEXT, dati TEXT, nota TEXT);
"""
_base = {"url": None, "letto": 0.0}


def acceso() -> bool:
    return os.getenv("FILO_FLUSSI", "off").lower() == "on"


def attivo(tipo: str) -> bool:
    return acceso() and bool(WORKFLOW.get(tipo)) and bool(base_pubblica())


def base_pubblica() -> str | None:
    """Indirizzo pubblico del servizio voce: FILO_URL_PUBBLICO, oppure quello del tunnel letto dalle metriche di cloudflared."""
    fisso = os.getenv("FILO_URL_PUBBLICO", "").strip().rstrip("/")
    if fisso:
        return fisso
    if _base["url"] and time.time() - _base["letto"] < 60:
        return _base["url"]
    metriche = os.getenv("FILO_TUNNEL_METRICS", "127.0.0.1:20242")
    try:
        with urllib.request.urlopen(f"http://{metriche}/quicktunnel", timeout=2) as r:
            host = json.load(r).get("hostname")
        _base.update(url=f"https://{host}" if host else None, letto=time.time())
    except Exception:  # noqa: BLE001
        _base.update(url=None, letto=time.time())
    return _base["url"]


# ------------------------------------------------------------------ permessi

def _firma(fid: str, scade: int) -> str:
    return hmac.new(voce_token().encode(), f"flusso|{fid}|{scade}".encode(), hashlib.sha256).hexdigest()[:40]


def permesso(fid: str) -> str:
    scade = int(time.time()) + DURATA
    return f"{fid}.{scade}.{_firma(fid, scade)}"


def verifica(perm: str | None, tipo: str, ref: str) -> dict | None:
    """Il flusso del permesso, se il permesso è valido, non scaduto, e riguarda proprio quel tipo e quella pratica/posto/chiamata."""
    try:
        fid, scade, firma = (perm or "").split(".")
        scade_i = int(scade)
    except ValueError:
        return None
    if scade_i < time.time() or not hmac.compare_digest(firma, _firma(fid, scade_i)):
        return None
    with db.tx() as con:
        f = con.execute("SELECT * FROM flussi WHERE id=?", (fid,)).fetchone()
    if not f or f["tipo"] != tipo or f["ref"] != ref:
        return None
    return dict(f)


# ------------------------------------------------------------------ stato dei flussi

def prendi(fid: str, stato: str = "applicato", nota: str | None = None) -> bool:
    """Prenota il passo: vale solo per il primo che arriva (workflow o riserva locale)."""
    with db.tx() as con:
        return con.execute("UPDATE flussi SET stato=?, aggiornato=?, nota=COALESCE(?, nota) WHERE id=? AND stato='in_corso'",
                           (stato, db.now(), nota, fid)).rowcount == 1


def annota(fid: str, nota: str, dati: dict | None = None):
    with db.tx() as con:
        con.execute("UPDATE flussi SET nota=?, aggiornato=?" + (", dati=?" if dati is not None else "") + " WHERE id=?",
                    (nota, db.now(), *( [json.dumps(dati, ensure_ascii=False)] if dati is not None else []), fid))


def in_corso(con, tipo: str, ref: str) -> bool:
    lim = (datetime.now() - timedelta(seconds=DURATA)).strftime("%Y-%m-%d %H:%M:%S")
    return bool(con.execute("SELECT 1 FROM flussi WHERE tipo=? AND ref=? AND stato='in_corso' AND creato>=?", (tipo, ref, lim)).fetchone())


def ultimi(con, n: int = 20) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM flussi ORDER BY creato DESC LIMIT ?", (n,))]


def _esegui_workflow(tipo: str, fid: str, ref: str) -> dict:
    from .agents import _sim_post
    return _sim_post(WORKFLOW[tipo], {"input": {"base": base_pubblica(), "permesso": permesso(fid), "ref": ref}})


def lancia(tipo: str, ref: str, locale, con=None) -> str:
    """Registra il flusso e lo lancia sulla piattaforma in un thread. `locale` è il passo fatto da Filo da solo: parte se il
    workflow non applica il passo. Con `con` (dentro una transazione aperta) il thread parte dopo un attimo, a transazione chiusa."""
    fid = "FL-" + uuid.uuid4().hex[:8]
    riga = (fid, tipo, ref, "in_corso", db.now(), db.now(), None, None)
    if con is not None:
        con.execute("INSERT INTO flussi VALUES (?,?,?,?,?,?,?,?)", riga)
    else:
        with db.tx() as c2:
            c2.execute("INSERT INTO flussi VALUES (?,?,?,?,?,?,?,?)", riga)

    def gira():
        esito = None
        try:
            esito = _esegui_workflow(tipo, fid, ref)
        except Exception as e:  # noqa: BLE001
            esito = {"errore": str(e)[:200]}
        if prendi(fid, "locale", f"il workflow non ha applicato il passo ({str(esito)[:160]}): fatto da Filo in locale"):
            try:
                locale()
            except Exception as e:  # noqa: BLE001
                annota(fid, f"anche il passo locale non è riuscito: {str(e)[:200]}")

    t = threading.Timer(0.5 if con is not None else 0, gira)
    t.daemon = True
    t.start()
    _thread.append(t)
    del _thread[:-20]
    return fid


_thread: list = []  # gli ultimi thread lanciati (i test li aspettano)


# ------------------------------------------------------------------ 1. risposta del servizio

def _senza_nomi(testo: str, p: dict) -> str:
    """Al cloud va il testo della risposta, ma senza nome e cognome della persona né numeri di telefono."""
    for parola in (p.get("name") or "").split():
        if len(parola) > 2:
            testo = re.sub(re.escape(parola), "[persona]", testo, flags=re.I)
    return re.sub(r"\+?\d[\d .]{6,}\d", "[telefono]", testo)


def risposta_dati(rid: str) -> dict:
    from . import posta, trasporti
    with db.tx() as con:
        r, p, a, s = trasporti._load(con, rid)
        m = con.execute("SELECT * FROM mail_in WHERE rid=? ORDER BY ts DESC, rowid DESC LIMIT 1", (rid,)).fetchone()
    esito, ora = posta.interpreta(m["text"]) if m else (None, None)
    at = datetime.fromisoformat(a["at"])
    return {"pratica": rid, "stato": r["status"], "servizio": s["name"], "testo": _senza_nomi(m["text"], p) if m else "",
            "regole": {"esito": esito, "orario": ora},
            "finestra": {"dalle": (at - timedelta(hours=3)).strftime("%H:%M"), "alle": (at - timedelta(minutes=30)).strftime("%H:%M")}}


def risposta_applica(fid: str, rid: str, azione: str, orario: str | None, motivo: str) -> dict:
    """Controlli fissi sopra la lettura dell'agente, poi il passo. Se l'agente e le regole non sono d'accordo decide l'accettazione."""
    from . import trasporti
    d = risposta_dati(rid)
    if d["stato"] != "attesa_servizio":
        prendi(fid, "applicato", "pratica già avanti")
        return {"fatto": "niente: la pratica è già andata avanti"}
    regole = d["regole"]["esito"]
    ora = trasporti.norm_time(orario) or d["regole"]["orario"]
    perche = None
    if azione == "disponibile":
        if regole == "no":
            perche = "l'agente legge una disponibilità, le regole fisse un rifiuto"
        elif not ora or not (d["finestra"]["dalle"] <= ora <= d["finestra"]["alle"]):
            perche = f"orario {ora or 'mancante'} fuori dalla finestra {d['finestra']['dalle']}-{d['finestra']['alle']}"
    elif azione == "non_disponibile":
        if regole == "si":
            perche = "l'agente legge un rifiuto, le regole fisse una disponibilità"
    elif azione != "non_chiaro":
        perche = f"azione sconosciuta: {azione!r}"
    if not prendi(fid):
        return {"fatto": "niente: il passo era già stato fatto"}
    nota = f"Flusso 'risposta del servizio' sulla piattaforma: l'agente legge \"{azione.replace('_', ' ')}\"" + (f" ({motivo[:160]})" if motivo else "")
    if azione == "non_chiaro" or perche:
        with db.tx() as con:
            trasporti._event(con, rid, FLUSSO["username"], "appunto", nota + (f". Controllo fisso: {perche}" if perche else "")
                             + ". Serve l'accettazione: leggi la mail e usa i pulsanti della pratica.")
        return {"fatto": "appunto all'accettazione", "perche": perche}
    with db.tx() as con:
        trasporti._event(con, rid, FLUSSO["username"], "lettura", nota + ". Le regole fisse sono d'accordo.")
    trasporti.risposta_servizio(rid, azione == "disponibile", ora, FLUSSO)
    if azione == "disponibile":
        trasporti.chiama_se_possibile(rid)
        return {"fatto": f"disponibile alle {ora}: l'agente vocale chiama la persona"}
    return {"fatto": "non disponibile: richiesta al servizio successivo (o accettazione se non ce ne sono)"}


# ------------------------------------------------------------------ 2. esito della chiamata

def chiamata_esito(cid: str) -> dict:
    from .voce import chiamata as ch
    with db.tx() as con:
        c = ch.carica(con, cid)
    out = c["outcome"] or {}
    if c["kind"] == "trasporto":
        e = ("serve" if out["trasporto"]["serve"] else "non_serve") if "trasporto" in out else "nessuna_risposta"
    elif c["kind"] == "appuntamento":
        e = (out.get("appuntamento") or {}).get("scelta") or "nessuna_risposta"
    elif c["kind"] == "modifica":
        e = (out.get("modifica") or {}).get("scelta") or "nessuna_risposta"
    else:
        e = "risposte" if "stato" in out else "nessuna_risposta"
    return {"chiamata": cid, "tipo": c["kind"], "esito": e, "conclusa": bool(c["ended_at"])}


def chiamata_applica(fid: str, cid: str, azione: str) -> dict:
    from .voce import chiamata as ch
    d = chiamata_esito(cid)
    if azione != d["esito"]:
        return {"fatto": "niente", "errore": f"il workflow chiede {azione!r}, l'esito registrato è {d['esito']!r}"}
    if not prendi(fid):
        return {"fatto": "niente: il passo era già stato fatto"}
    ch.applica_effetti(cid)
    return {"fatto": f"{d['tipo']}: {azione.replace('_', ' ')} applicato"}


# ------------------------------------------------------------------ 3. posto liberato

def posto_candidati(fid: str, sid: str) -> dict:
    from . import gateway, posti
    with db.tx() as con:
        slot = posti._slot(con, sid)
        liberi = posti.liberi(con, slot)[:posti.MAX_CANDIDATI]
        codici = {"C-" + uuid.uuid4().hex[:6]: w["id"] for w in liberi}
        by_id = {w["id"]: w for w in liberi}
        cands = [{"codice": k, **posti._candidato(con, slot, by_id[w])} for k, w in codici.items()]
    pacchetto = {"tipo": "posto_liberato", "esame": slot["service"], "giorni_al_posto": gateway._days(slot["at"]), "candidati": cands}
    annota(fid, f"{len(cands)} candidati anonimi passati al workflow", {"codici": codici, "candidati": cands})
    return {"pacchetto": json.dumps(pacchetto, ensure_ascii=False), "candidati": len(cands)}


def _trova(x, chiave: str, n: int = 0):
    """La risposta dell'agente dentro qualunque involucro della piattaforma (content, output, data, result, testo JSON)."""
    if n > 5 or x is None:
        return None
    if isinstance(x, str):
        from . import agents
        return _trova(agents.extract_json(x), chiave, n + 1)
    if isinstance(x, dict):
        if chiave in x:
            return x
        for v in x.values():
            if (t := _trova(v, chiave, n + 1)) is not None:
                return t
    return None


def posto_proposta(fid: str, sid: str, risposta) -> dict:
    from . import agents, gateway, posti
    with db.tx() as con:
        f = con.execute("SELECT dati FROM flussi WHERE id=?", (fid,)).fetchone()
    dati = json.loads(f["dati"] or "{}") if f else {}
    out = _trova(risposta, "codice_scelto")
    pacchetto = {"tipo": "posto_liberato", "candidati": dati.get("candidati", [])}
    pulito, scarto = gateway.check_output("agende", out, pacchetto) if out else (None, "risposta non leggibile")
    if not prendi(fid):
        return {"fatto": "niente: il passo era già stato fatto"}
    return posti.proponi_da_flusso(sid, dati.get("codici", {}), {c["codice"]: c for c in dati.get("candidati", [])}, pulito, scarto)


# ------------------------------------------------------------------ le richieste che i workflow fanno a Filo (servizio voce)

def router():
    """/filo/...: montato sul servizio voce (app/voce/server.py), l'unico dietro il tunnel. Ogni richiesta porta il permesso."""
    from fastapi import APIRouter, Body, Header, HTTPException

    r = APIRouter(prefix="/filo")

    def ok(perm, tipo, ref):
        f = verifica(perm, tipo, ref)
        if not f:
            raise HTTPException(403, "permesso non valido, scaduto o di un altro flusso")
        return f

    @r.get("/trasporti/{rid}/risposta")
    def _r1(rid: str, x_filo_permesso: str = Header("")):
        ok(x_filo_permesso, "risposta_servizio", rid)
        return risposta_dati(rid)

    @r.post("/trasporti/{rid}/esito")
    def _r2(rid: str, corpo: dict = Body(...), x_filo_permesso: str = Header("")):
        f = ok(x_filo_permesso, "risposta_servizio", rid)
        return risposta_applica(f["id"], rid, str(corpo.get("azione", "")), corpo.get("orario"), str(corpo.get("motivo") or ""))

    @r.get("/chiamate/{cid}/esito")
    def _c1(cid: str, x_filo_permesso: str = Header("")):
        ok(x_filo_permesso, "esito_chiamata", cid)
        return chiamata_esito(cid)

    @r.post("/chiamate/{cid}/applica")
    def _c2(cid: str, corpo: dict = Body(...), x_filo_permesso: str = Header("")):
        f = ok(x_filo_permesso, "esito_chiamata", cid)
        return chiamata_applica(f["id"], cid, str(corpo.get("azione", "")))

    @r.get("/posti/{sid}/candidati")
    def _p1(sid: str, x_filo_permesso: str = Header("")):
        f = ok(x_filo_permesso, "posto_liberato", sid)
        return posto_candidati(f["id"], sid)

    @r.post("/posti/{sid}/proposta")
    def _p2(sid: str, corpo: dict = Body(...), x_filo_permesso: str = Header("")):
        f = ok(x_filo_permesso, "posto_liberato", sid)
        return posto_proposta(f["id"], sid, corpo.get("risposta"))

    return r
