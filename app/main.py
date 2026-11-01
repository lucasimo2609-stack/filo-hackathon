"""Filo: applicazione web della Centrale Operativa Territoriale.

    uvicorn app.main:app --port 8000

Due ruoli separati (accettazione e medico), accesso con password + codice a tempo,
ogni apertura di una scheda finisce nel registro accessi.
"""
from __future__ import annotations

import json
import random
import re
import secrets
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import actions, auth, calendario, db, extract, gateway, importa, kit_visita, modifiche, posti, trasporti, ui, valutazione
from . import posta as posta_vera
from .voce import chiamata as voce_ch, eleven as voce_el
from .config import (SESSION_COOKIE, VOCE_PORT, voce_token, AGENTS, ATTESTATION_MODE, AUTO_RUN_AT, HTTPS_ONLY, IMPORT_DIR, K_MIN, NEW_DOCS_DAYS, OUTBOX,
                     TODAY, session_secret)

HERE = Path(__file__).resolve().parent
IDLE_SECONDS = 15 * 60
ROLE_LABEL = {"segreteria": "Accettazione", "medico": "Medico"}
KIND_LABEL = {"trasporto": "Trasporto", "posto": "Posto liberato", "posto_errore": "Posto liberato: da controllare",
              "modifica": "Modifica prenotazione", "modifica_errore": "Modifica prenotazione: da controllare", "kit": "Visita accessibile",
              "cure": "Cure che si incrociano", "aderenza": "Terapia sospesa", "doppione": "Esame doppio",
              "allarme": "Allarme in chiamata", "esito_chiamata": "Esito chiamata di controllo"}

def _auto_runner():
    """Elaborazione automatica ogni mattina all'ora di FILO_AUTO_RUN_AT (es. 07:00). Senza, si parte dal pulsante."""
    last = None
    while True:
        now = datetime.now()
        if now.strftime("%H:%M") == AUTO_RUN_AT and last != now.date():
            last = now.date()
            try:
                gateway.run("elaborazione automatica")
            except Exception as e:  # noqa: BLE001 - l'errore resta scritto nell'elaborazione
                db.log_access("sistema", "elaborazione automatica fallita", str(e)[:200])
        time.sleep(20)


@asynccontextmanager
async def lifespan(_app):
    db.init()
    if AUTO_RUN_AT:
        threading.Thread(target=_auto_runner, daemon=True, name="filo-auto-run").start()
    from . import posta
    posta.contatti_demo()
    posta.avvia_ascolto()
    from .voce import guardia
    guardia.avvia()
    yield


app = FastAPI(title="Filo", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=session_secret(), session_cookie=SESSION_COOKIE,
                   max_age=8 * 3600, same_site="strict", https_only=HTTPS_ONLY)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
tpl = Jinja2Templates(directory=HERE / "templates")
tpl.env.globals.update(ROLE_LABEL=ROLE_LABEL, KIND_LABEL=KIND_LABEL, TODAY=TODAY)
tpl.env.filters["js"] = lambda o: json.dumps(o, ensure_ascii=False)
tpl.env.filters["fromjson"] = lambda s: json.loads(s) if s else None
tpl.env.filters["fa"] = ui.fa


def _json_leggibile(o):
    """Un campo per riga, stesso contenuto. Un testo che non è JSON resta com'è."""
    try:
        return json.dumps(json.loads(o) if isinstance(o, str) else o, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        return o


tpl.env.filters["leggibile"] = _json_leggibile
tpl.env.globals.update(level=ui.level, level_of=ui.level_of, origine=ui.origine, LEVELS=ui.LEVELS, SCARTO=ui.SCARTO)

class _UsedCodes:
    """Codici a tempo già usati: stanno nel database, quindi valgono una volta sola anche dopo un riavvio."""
    def clear(self):
        with db.tx() as con:
            con.execute("DELETE FROM used_codes")


_used_codes = _UsedCodes()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    # nessuna pagina si può incorniciare, tranne l'anteprima della mail dentro "Posta in uscita"
    anteprima = request.url.path.startswith("/posta/") and request.url.path.endswith("/anteprima")
    resp.headers["X-Frame-Options"] = "SAMEORIGIN" if anteprima else "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    voce = request.url.path.startswith("/chiamate/")  # solo la pagina della chiamata parla con ElevenLabs e usa il microfono
    eleven = "https://api.elevenlabs.io wss://api.elevenlabs.io https://api.eu.residency.elevenlabs.io wss://api.eu.residency.elevenlabs.io"
    if "content-security-policy" in resp.headers:
        return resp
    # niente caratteri da Google: ogni postazione ne rivelava l'indirizzo. Il PDF e l'anteprima della mail si mostrano solo da Filo stesso.
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
        "frame-src 'self'; object-src 'self'; base-uri 'none'"
        + (f"; script-src 'self' blob:; worker-src 'self' blob:; media-src 'self' blob: data:; connect-src 'self' {eleven}" if voce else ""))
    mic = voce or request.url.path.startswith("/kit/visita/")  # e la visita col kit: microfono del medico verso le cuffie
    resp.headers["Permissions-Policy"] = "microphone=(self)" if mic else "microphone=(), camera=(), geolocation=()"
    return resp


# ------------------------------------------------------------------ sessione e permessi

class NeedLogin(Exception):
    pass


@app.exception_handler(NeedLogin)
async def _need_login(request: Request, exc: NeedLogin):
    return RedirectResponse("/login", status_code=303)


def csrf(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return request.session["csrf"]


def check_csrf(request: Request, token: str):
    if not token or not secrets.compare_digest(token, request.session.get("csrf", "")):
        raise HTTPException(400, "Modulo scaduto: ricarica la pagina.")


def _end_session(s):
    if s.get("sid"):
        with db.tx() as con:
            con.execute("DELETE FROM sessions WHERE sid=?", (s["sid"],))
    s.clear()


def current_user(request: Request) -> dict:
    """La sessione vale solo se è ancora registrata lato server: dopo l'uscita una copia del cookie non serve più."""
    s = request.session
    if not s.get("user") or not s.get("sid"):
        # senza accesso si tolgono solo le chiavi dell'accesso: il codice del modulo della pagina di accesso resta,
        # così una scheda vecchia che chiede /novita non fa finire il clic su "Avanti" in "Modulo scaduto"
        for k in ("user", "sid", "last", "pending", "pending_at"):
            s.pop(k, None)
        raise NeedLogin()
    with db.tx() as con:
        row = con.execute("SELECT username, last_seen FROM sessions WHERE sid=?", (s["sid"],)).fetchone()
    if not row or row["username"] != s["user"]:
        s.clear()
        raise NeedLogin()
    if time.time() - row["last_seen"] > IDLE_SECONDS:
        db.log_access(s["user"], "uscita automatica per inattività")
        _end_session(s)
        raise NeedLogin()
    with db.tx() as con:
        con.execute("UPDATE sessions SET last_seen=? WHERE sid=?", (time.time(), s["sid"]))
    s["last"] = time.time()
    row = auth.get_user(s["user"])
    if not row:
        s.clear()
        raise NeedLogin()
    return dict(row)


def _versione_statici() -> str:
    """Cambia quando cambia un file statico: negli indirizzi (?v=) obbliga il browser a riprendere css e js dopo un aggiornamento."""
    d = Path(__file__).parent / "static"
    return str(int(max(f.stat().st_mtime for f in d.glob("*.*"))))


tpl.env.globals["V"] = _versione_statici()


def render(request: Request, name: str, user: dict | None = None, **ctx):
    # i messaggi arrivano dalla sessione, non dall'indirizzo: un link non può far comparire un testo scelto da altri
    ctx.setdefault("flash", request.session.pop("flash", None))
    ctx.setdefault("error", request.session.pop("error", None))
    ctx.setdefault("ora", db.now())  # ora del server: da qui la pagina chiede le novità
    ctx.setdefault("segui", request.session.get("segui"))  # il paziente che si sta seguendo (barra in cima a ogni pagina)
    return tpl.TemplateResponse(request, name, {"user": user, "csrf": csrf(request), **ctx})


def _segui(request: Request, pid: str, name: str):
    """Chi apre una pratica, un trasporto, una cartella o una chiamata resta su quella persona finché non sceglie "Tutti i pazienti"."""
    request.session["segui"] = {"id": pid, "name": name}


def _seguito(request: Request) -> str | None:
    return (request.session.get("segui") or {}).get("id")


def go(request: Request, url: str, flash: str | None = None, error: str | None = None):
    if flash:
        request.session["flash"] = flash[:500]
    if error:
        request.session["error"] = error[:500]
    return RedirectResponse(url, status_code=303)


def one_time_code_ok(username: str, code: str) -> tuple[bool, str]:
    now = time.time()
    with db.tx() as con:
        con.execute("DELETE FROM used_codes WHERE ts < ?", (now - 120,))
        if con.execute("SELECT 1 FROM used_codes WHERE username=? AND code=?", (username, code)).fetchone():
            return False, "Codice già usato: aspetta il prossimo."
    ok, msg = auth.check_totp(username, code)
    if ok:
        with db.tx() as con:
            con.execute("INSERT OR REPLACE INTO used_codes VALUES (?,?,?)", (username, code, now))
    return ok, msg


# ------------------------------------------------------------------ accesso

@app.get("/", include_in_schema=False)
def home(request: Request):
    return RedirectResponse("/cruscotto" if request.session.get("user") else "/login", status_code=303)


# Accesso della demo: si sceglie il ruolo e si entra con il suo utente di prova, senza password né codice (decisione di
# Ambra, 23/10: la giuria deve poter entrare con un clic). La firma del medico chiede ancora il codice a tempo.
UTENTI_DEMO = {"segreteria": "m.bianchi", "medico": "e.gatti"}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "login.html", step="password", error=None, role="segreteria", UTENTI_DEMO=UTENTI_DEMO)


@app.post("/login")
def login(request: Request, role: str = Form("segreteria"), csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    username = UTENTI_DEMO.get(role)
    u = auth.get_user(username) if username else None
    if not u or u["role"] != role:
        db.log_access(username or "?", "accesso rifiutato", role)
        return render(request, "login.html", step="password", error="Ruolo non disponibile.", role="segreteria", UTENTI_DEMO=UTENTI_DEMO)
    request.session.clear()  # nuova sessione all'accesso: niente riuso del cookie precedente
    sid = secrets.token_urlsafe(24)
    with db.tx() as con:
        con.execute("INSERT INTO sessions VALUES (?,?,?,?)", (sid, username, db.now(), time.time()))
    request.session.update(user=username, sid=sid, last=time.time(), csrf=secrets.token_urlsafe(24))
    db.log_access(username, "accesso", ROLE_LABEL[role])
    return RedirectResponse("/cruscotto", status_code=303)


@app.get("/login/codice", include_in_schema=False)
@app.post("/login/codice", include_in_schema=False)
def otp_vecchio(request: Request):
    """Il secondo passo non c'è più: un segnalibro o una scheda vecchia tornano all'accesso."""
    return RedirectResponse("/login", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    if request.session.get("user"):
        db.log_access(request.session["user"], "uscita")
    _end_session(request.session)
    return RedirectResponse("/login", status_code=303)


@app.post("/ripristina")
def ripristina(request: Request, csrf_token: str = Form("")):
    """Riporta la demo allo stato iniziale: pazienti, pratiche, chiamate, trasporti e posta di partenza.
    Gli utenti restano, con password e app di autenticazione: la sessione continua."""
    import subprocess
    import sys
    user = current_user(request)
    check_csrf(request, csrf_token)
    r = subprocess.run([sys.executable, "-m", "app.seed", "--reset"], cwd=Path(__file__).resolve().parent.parent,
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise HTTPException(500, "Ripristino non riuscito: " + (r.stderr or r.stdout)[-300:])
    gateway._fixed_ids.clear()
    db.log_access(user["username"], "demo riportata alle impostazioni iniziali")
    return go(request, "/cruscotto", flash="Demo riportata alle impostazioni iniziali")


# ------------------------------------------------------------------ cruscotto e schede

def _patients(con):
    return {p["id"]: dict(p) for p in con.execute("SELECT * FROM patients")}


def _last_run(con):
    r = con.execute("SELECT * FROM runs WHERE finished_at IS NOT NULL AND summary NOT LIKE '%\"errore\"%' AND summary NOT LIKE '%\"bloccato\"%' "
                    "ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(r) if r else None


DOC_KIND = {"lettera_dimissione": "Lettera di dimissione", "referto": "Referto", "esami_laboratorio": "Esami", "richiesta": "Richiesta di esame",
            "telefonata": "Telefonata", "nota_reparto": "Nota di reparto", "nota_sociale": "Scheda sociale", "prenotazione": "Prenotazione CUP",
            "verbale_ps": "Pronto soccorso", "trascrizione_visita": "Trascrizione della visita"}
tpl.env.globals.update(DOC_KIND=DOC_KIND)
tpl.env.filters["weekday"] = lambda d: date.fromisoformat(d).weekday()


def need(user, role):
    if user["role"] != role:
        db.log_access(user["username"], "TENTATIVO NEGATO", f"pagina riservata a {ROLE_LABEL[role]}")
        raise HTTPException(403, f"Pagina riservata al ruolo {ROLE_LABEL[role]}.")


def _age(p):
    return int(TODAY[:4]) - p["birth_year"]


@app.get("/cruscotto", response_class=HTMLResponse)
def cruscotto(request: Request):
    user = current_user(request)
    with db.tx() as con:
        if user["role"] == "segreteria":
            trasporti.scadenze(con)
        rows = [dict(r) for r in con.execute("SELECT * FROM cases WHERE role=? AND status != 'superato' ORDER BY status='pending' DESC, score DESC", (user["role"],))]
        pts = _patients(con)
        runs = dict(con.execute("SELECT id, started_at FROM runs").fetchall())
        run = _last_run(con)
        events = [dict(e) for e in con.execute("SELECT * FROM external_events ORDER BY date")]
        trs = [dict(r) for r in con.execute(
            "SELECT t.*, s.name AS service, a.at FROM transport_requests t JOIN transport_services s ON s.id=t.service_id "
            "JOIN appointments a ON a.id=t.appointment_id ORDER BY t.updated_at DESC")] if user["role"] == "segreteria" else []
        kit_visite = [dict(r) for r in con.execute(
            "SELECT b.*, k.label, p.name AS patient, a.service FROM kit_bookings b JOIN kits k ON k.id=b.kit_id JOIN patients p ON p.id=b.patient_id "
            "JOIN appointments a ON a.id=b.appointment_id WHERE b.status='riservato' ORDER BY b.at")] if user["role"] == "medico" else []
        agenda, nuovi = [], []
        if user["role"] == "medico":  # le prossime visite; chi è entrato da un posto liberato in evidenza, con la cartella a un clic
            agenda = [dict(r) for r in con.execute(
                "SELECT a.*, p.name AS patient, f.id AS slot FROM appointments a JOIN patients p ON p.id=a.patient_id "
                "LEFT JOIN freed_slots f ON f.assigned_to=a.patient_id AND f.at=a.at AND f.service=a.service "
                "WHERE a.status='prenotata' AND a.at >= ? ORDER BY a.at LIMIT 12", (TODAY,))]
            for r in con.execute(
                    "SELECT e.ts, o.patient_id, o.appointment_id, w.priority FROM slot_events e JOIN slot_offers o ON o.id=e.offer_id "
                    "LEFT JOIN waitlist w ON w.id=o.waitlist_id WHERE e.step='verificato' ORDER BY e.ts DESC LIMIT 3"):
                a = con.execute("SELECT * FROM appointments WHERE id=? AND status='prenotata'", (r["appointment_id"],)).fetchone()
                if not a or a["at"] < TODAY:
                    continue
                last = con.execute("SELECT id, title, date FROM notes WHERE patient_id=? ORDER BY date DESC LIMIT 1", (r["patient_id"],)).fetchone()
                nuovi.append({"ts": r["ts"], "pid": r["patient_id"], "priority": r["priority"], "a": dict(a),
                              "n_docs": con.execute("SELECT COUNT(*) FROM notes WHERE patient_id=?", (r["patient_id"],)).fetchone()[0],
                              "last": dict(last) if last else None,
                              "open": con.execute("SELECT COUNT(*) FROM cases WHERE patient_id=? AND role='medico' AND status='pending'",
                                                  (r["patient_id"],)).fetchone()[0]})
        since = (date.fromisoformat(TODAY) - timedelta(days=NEW_DOCS_DAYS)).isoformat()
        docs_new = [dict(r) for r in con.execute("SELECT id, patient_id, kind, title, date FROM notes WHERE date >= ? "
                                                 "AND kind NOT IN ('prenotazione') ORDER BY date DESC", (since,))] if user["role"] == "medico" else []
    # widget della dashboard: le persone della giornata (visita oggi o domani, o qualcosa da fare), le visite vicine, la posta
    domani = (date.fromisoformat(TODAY) + timedelta(days=1)).isoformat()
    with db.tx() as con:
        aperti = defaultdict(int)
        for r in con.execute("SELECT patient_id, COUNT(*) FROM cases WHERE role=? AND status='pending' GROUP BY patient_id", (user["role"],)):
            aperti[r[0]] = r[1]
        prossime = {r["patient_id"]: dict(r) for r in con.execute(
            "SELECT * FROM appointments WHERE status='prenotata' AND at >= ? ORDER BY at DESC", (TODAY,))}
        tr_stato = {r["patient_id"]: trasporti.STATUS.get(r["status"], r["status"]) for r in con.execute(
            "SELECT patient_id, status FROM transport_requests ORDER BY updated_at")}
        visite_vicine = [dict(r) for r in con.execute(
            "SELECT a.*, p.name AS patient FROM appointments a JOIN patients p ON p.id=a.patient_id WHERE a.status='prenotata' "
            "AND a.at >= ? AND a.at < ? ORDER BY a.at", (TODAY, (date.fromisoformat(domani) + timedelta(days=1)).isoformat()))]
        posta_w = [dict(r) for r in con.execute("SELECT id, ts, recipient, subject, sent_to FROM outbox ORDER BY id DESC LIMIT 5")]
    persone = []
    for pid, p in pts.items():
        v = prossime.get(pid)
        oggi = bool(v and v["at"][:10] in (TODAY, domani))
        persone.append({"id": pid, "name": p["name"], "age": _age(p), "visita": v, "oggi": oggi, "aperti": aperti.get(pid, 0),
                        "trasporto": tr_stato.get(pid), "kit": bool(p.get("hearing_support"))})
    persone.sort(key=lambda x: (not (x["oggi"] or x["aperti"]), (x["visita"] or {}).get("at") or "9", x["name"]))
    pannello = None
    if _seguito(request):  # il cruscotto guarda tutti; di lato, la persona che si sta seguendo
        from . import persona
        with db.tx() as con:
            pannello = persona.per_pannello(con, _seguito(request), user["role"])
    for c in rows:
        p = pts[c["patient_id"]]
        c["patient"] = {"id": p["id"], "name": p["name"], "age": _age(p), "quartiere": p["quartiere"], "mmg": p["mmg"]}
        c["ev"] = json.loads(c["evidence"])
        c["since"] = runs.get(c["run_id"]) or c.get("decided_at")  # quando è stata segnalata (l'elaborazione che l'ha creata)
    for t in trs:
        t["patient"] = pts[t["patient_id"]]["name"]
        t["label"] = trasporti.STATUS.get(t["status"], t["status"])
    for d in docs_new:
        d["patient"] = pts[d["patient_id"]]["name"]
    for n in nuovi:
        p = pts[n["pid"]]
        n["patient"] = {"id": p["id"], "name": p["name"], "age": _age(p), "mmg": p["mmg"]}
    db.log_access(user["username"], "apre il cruscotto", f"{len(rows)} casi")
    pending = [c for c in rows if c["status"] == "pending"]
    done = [c for c in rows if c["status"] != "pending"]
    run = run and {**run, "s": json.loads(run["summary"] or "{}")}
    name = "medico.html" if user["role"] == "medico" else "cruscotto.html"
    # una persona = una riga: trasporto, kit e posto della stessa persona si decidono insieme, non in schede separate
    groups = {}
    for c in pending:
        groups.setdefault(c["patient_id"], {"patient": c["patient"], "top": c, "cose": []})["cose"].append(c)
    groups = list(groups.values())  # pending è già in ordine di punteggio: la prima segnalazione di ognuno è la più alta
    lv_of = (lambda c: ui.level_of(c)["key"])
    counts = ({k: sum(1 for g in groups if lv_of(g["top"]) == k) for _, k, *_ in ui.LEVELS} if user["role"] == "segreteria"
              else {k: sum(1 for c in pending if lv_of(c) == k) for _, k, *_ in ui.LEVELS})
    events = sorted(events, key=lambda e: e["kind"] != "sciopero")[:2]  # al massimo due avvisi, il più grave prima (NHS, VA)
    return render(request, name, user, pending=pending, groups=groups, done=done, run=run, events=events, trs=trs, docs_new=docs_new, kit_visite=kit_visite,
                  counts=counts, since_docs=since, agenda=agenda, nuovi=nuovi, pannello=pannello, persone=persone,
                  visite_vicine=visite_vicine, posta_w=posta_w, domani=domani)


# ------------------------------------------------------------------ cartella clinica (solo medico)

FIELD_LABEL = {"farmaco": "Farmaco", "condizione": "Condizione", "aderenza": "Terapia sospesa", "aderenza_giorni": "Da quanto",
               "aderenza_motivo": "Motivo", "aderenza_terapia": "Terapia", "richiesta": "Richiesta di esame", "udito": "Udito",
               "vive_solo": "Vive solo", "senza_auto": "Senza auto"}
tpl.env.globals.update(PRIO_TESTO=posti.PRIORITA_TESTO, FIELD_LABEL=FIELD_LABEL, URGENCY=valutazione.URGENCY, VFLAGS=valutazione.FLAGS, MIN_FLAGS=valutazione.MIN_FLAGS)


def _highlight(text: str, snippets: list[tuple[str, str]], focus: str | None):
    """Spezza il testo in pezzi [(testo, etichetta o None, è_focus)] mettendo in evidenza le frasi da cui vengono i dati."""
    spans = []
    for snip, label in snippets:
        i = text.find(snip) if snip else -1
        if i >= 0 and not any(i < e and i + len(snip) > b for b, e, *_ in spans):
            spans.append((i, i + len(snip), label, snip == focus))
    out, pos = [], 0
    for b_, e, label, f in sorted(spans):
        out.append((text[pos:b_], None, False)); out.append((text[b_:e], label, f)); pos = e
    out.append((text[pos:], None, False))
    return out


@app.get("/pazienti", response_class=HTMLResponse)
def pazienti(request: Request, q: str = ""):
    user = current_user(request)
    need(user, "medico")
    q = q.strip()
    with db.tx() as con:
        where, args = ("WHERE name LIKE ? OR cf LIKE ?", (f"%{q}%", f"%{q.upper()}%")) if q else ("", ())
        total = con.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
        pts = [dict(p) for p in con.execute(f"SELECT * FROM patients {where} ORDER BY origin='anagrafe' DESC, name LIMIT 200", args)]
        nd = dict(con.execute("SELECT patient_id, COUNT(*) FROM notes GROUP BY patient_id").fetchall())
        last = dict(con.execute("SELECT patient_id, MAX(date) FROM notes GROUP BY patient_id").fetchall())
        open_ = dict(con.execute("SELECT patient_id, COUNT(*) FROM cases WHERE role='medico' AND status='pending' GROUP BY patient_id").fetchall())
        n_inbox = con.execute("SELECT COUNT(*) FROM inbox WHERE status='da smistare'").fetchone()[0]
    for p in pts:
        p.update(age=_age(p), n_docs=nd.get(p["id"], 0), last=last.get(p["id"]), open=open_.get(p["id"], 0))
    db.log_access(user["username"], "apre elenco pazienti", f"ricerca: {q}" if q else "")
    return render(request, "pazienti.html", user, pts=pts, q=q, total=total, n_inbox=n_inbox)


@app.get("/pazienti/{pid}", response_class=HTMLResponse)
def cartella(request: Request, pid: str, doc: str | None = None, tipo: str | None = None, hl: str | None = None):
    user = current_user(request)
    need(user, "medico")
    with db.tx() as con:
        p = con.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "Paziente non trovato")
        p = dict(p)
        docs = [dict(d) for d in con.execute("SELECT * FROM notes WHERE patient_id=? ORDER BY date DESC", (pid,))]
        appts = [dict(a) for a in con.execute("SELECT * FROM appointments WHERE patient_id=? ORDER BY at", (pid,))]
        from . import kit as kit_mod
        kits = kit_mod.per_appuntamenti(con, [a["id"] for a in appts])
        for a in appts:
            a["kit"] = kits.get(a["id"])
        cases = [dict(c) for c in con.execute("SELECT * FROM cases WHERE patient_id=? AND role='medico' ORDER BY status='pending' DESC, score DESC", (pid,))]
        wl = [dict(w) for w in con.execute("SELECT * FROM waitlist WHERE patient_id=? AND status IN ('in attesa', 'proposto')", (pid,))]
        vals = valutazione.history(con, pid)
        stored = defaultdict(list)  # i dati usati davvero dall'ultima elaborazione (regole e, se acceso, modello locale con la sua frase)
        for s_ in con.execute("SELECT * FROM signals WHERE patient_id=?", (pid,)):
            stored[s_["evidence_id"]].append({"field": s_["field"], "value": s_["value"], "snippet": s_["snippet"]})
    kinds = sorted({d["kind"] for d in docs})
    shown = [d for d in docs if not tipo or d["kind"] == tipo]
    cur = next((d for d in docs if d["id"] == doc), shown[0] if shown else None)
    flagged = {e[0] for c in cases if c["status"] == "pending" for e in json.loads(c["evidence"])}
    # dati letti dai documenti, ognuno con la sua fonte (lettura locale, lista chiusa)
    therapy, conds, by_doc = {}, {}, {}
    for d in docs:
        sigs = stored.get(d["id"]) or extract.rules(d)
        by_doc[d["id"]] = sigs
        for s_ in sigs:
            if s_["field"] == "farmaco":
                cls, spec = s_["value"].split("|")
                therapy.setdefault(cls, []).append((d["id"], spec, s_["snippet"]))
            elif s_["field"] == "condizione":
                conds.setdefault(s_["value"], []).append((d["id"], d["source"], s_["snippet"]))
    parts, cur_sigs, verified = None, [], None
    if cur:
        cur_sigs = by_doc[cur["id"]]
        labels = []
        for s_ in cur_sigs:
            v = s_["value"].split("|")[0].replace("_", " ")
            labels.append((s_["snippet"], f"{FIELD_LABEL.get(s_['field'], s_['field'])}: {v}"))
        parts = _highlight(cur["text"], labels, hl)
        verified = importa.verify(cur["file"], cur["sha256"]) if cur.get("file") else None
        if cur.get("file"):
            cur["pages"] = importa.page_count(cur["file"]) if importa.can_preview() or not cur["file"].endswith(".pdf") else 0
    db.log_access(user["username"], "apre la cartella", f"{p['name']}" + (f" · legge {cur['id']}" if cur else ""))
    _segui(request, pid, p["name"])
    return render(request, "cartella.html", user, p={**p, "age": _age(p)}, docs=shown, all_docs=docs, cur=cur, kinds=kinds, tipo=tipo,
                  appts=appts, cases=cases, flagged=flagged, vals=vals, therapy=therapy, conds=conds, wl=wl, parts=parts, cur_sigs=cur_sigs,
                  verified=verified)


@app.post("/pazienti/{pid}/valutazione")
def valuta(request: Request, pid: str, csrf_token: str = Form(""), urgenza: str = Form(""),
           motivi: list[str] = Form([]), nota: str = Form(""), visita: str = Form("")):
    """Fine visita: il medico segna l'urgenza (i motivi sono facoltativi). Basta essere entrati come medico."""
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "medico")
    try:
        with db.tx() as con:
            v = valutazione.save(con, pid, user["username"], urgenza, motivi, nota, visita or None)
            name = con.execute("SELECT name FROM patients WHERE id=?", (pid,)).fetchone()[0]
    except ValueError as e:
        return go(request, f"/pazienti/{pid}#valutazione", error=str(e))
    db.log_action(user["username"], v["id"], f"{name}: fine visita, urgenza {valutazione.URGENCY[v['urgency']].lower()} "
                                             f"({'; '.join(v['labels'])}) - registrata da {user['name']}")
    return go(request, f"/pazienti/{pid}#valutazione", flash=f"Valutazione registrata: urgenza {valutazione.URGENCY[v['urgency']].lower()}, "
                                                           f"{len(v['flags'])} motivi. I casi in attesa di {name} sono stati riordinati.")


@app.get("/documenti/{nid}/originale")
def originale(request: Request, nid: str, scarica: int = 0):
    user = current_user(request)
    need(user, "medico")
    with db.tx() as con:
        n = con.execute("SELECT n.*, p.name AS pname FROM notes n JOIN patients p ON p.id=n.patient_id WHERE n.id=?", (nid,)).fetchone()
    if not n or not n["file"]:
        raise HTTPException(404, "Originale non disponibile")
    path = (importa.ARCHIVE / n["file"]).resolve()
    if importa.ARCHIVE.resolve() not in path.parents or not path.exists():
        raise HTTPException(404)
    db.log_access(user["username"], "scarica originale" if scarica else "apre originale", f"{nid} ({n['pname']})")
    ext = Path(n["file"]).suffix
    return FileResponse(path, media_type=n["mime"] or importa.guess_mime(n["file"]), filename=f"{nid}{ext}",
                        content_disposition_type="attachment" if scarica else "inline")


def _page_png(rel, n):
    img = importa.preview(rel, n)
    if not img:
        raise HTTPException(404, "Anteprima non disponibile")
    return FileResponse(img, media_type="image/png" if img.suffix == ".png" else "image/jpeg")


@app.get("/documenti/{nid}/pagina/{n}")
def pagina(request: Request, nid: str, n: int):
    user = current_user(request)
    need(user, "medico")
    with db.tx() as con:
        row = con.execute("SELECT file FROM notes WHERE id=?", (nid,)).fetchone()
    if not row or not row["file"] or not 1 <= n <= 50:
        raise HTTPException(404)
    db.log_access(user["username"], "apre anteprima pagina", f"{nid} p.{n}")
    return _page_png(row["file"], n)


@app.get("/smistare/{iid}/pagina/{n}")
def smistare_pagina(request: Request, iid: str, n: int):
    user = current_user(request)
    need(user, "medico")
    with db.tx() as con:
        row = con.execute("SELECT file FROM inbox WHERE id=?", (iid,)).fetchone()
    if not row or not 1 <= n <= 50:
        raise HTTPException(404)
    db.log_access(user["username"], "apre anteprima documento da smistare", f"{iid} p.{n}")
    return _page_png(row["file"], n)


@app.post("/pazienti/{pid}/documenti")
async def carica(request: Request, pid: str, csrf_token: str = Form(""), files: list[UploadFile] = File(...)):
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "medico")
    res = []
    for f in files[:50]:
        data = await f.read(importa.MAX_BYTES + 1)
        with db.tx() as con:
            res.append(importa.ingest(con, data, f.filename or "senza_nome", "caricato a mano", user["username"], forced_pid=pid))
    db.log_access(user["username"], "carica documenti", f"{pid}: {len(res)} file")
    ok = [r for r in res if r["esito"] == "aggiunto"]
    msg = f"{len(ok)} documenti aggiunti" + "".join(f" · {r['file']}: {r['esito']} ({r.get('motivo', '')})" for r in res if r["esito"] != "aggiunto")
    return go(request, f"/pazienti/{pid}" + (f"?doc={ok[0]['id']}" if ok else ""), flash=msg)


# ------------------------------------------------------------------ importazione massiva e documenti da smistare

@app.get("/importa", response_class=HTMLResponse)
def importa_page(request: Request, job: str | None = None):
    user = current_user(request)
    need(user, "medico")
    with db.tx() as con:
        jobs = [dict(j) for j in con.execute("SELECT * FROM import_jobs ORDER BY started_at DESC LIMIT 10")]
        inbox = [dict(i) for i in con.execute("SELECT * FROM inbox WHERE status='da smistare' ORDER BY imported_at DESC")]
        n_pat = con.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
        n_cons = con.execute("SELECT COUNT(*) FROM patients WHERE consenso_fse=1").fetchone()[0]
        n_docs = con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    for j in jobs:
        j["s"] = json.loads(j["summary"] or "{}")
    cur = next((j for j in jobs if j["id"] == job), None)
    files_in_dir = sum(1 for f in IMPORT_DIR.rglob("*") if f.is_file()) if IMPORT_DIR.exists() else 0
    return render(request, "importa.html", user, jobs=jobs, inbox=inbox, cur=cur, import_dir=IMPORT_DIR, files_in_dir=files_in_dir,
                  n_pat=n_pat, n_cons=n_cons, n_docs=n_docs, ocr=importa._ocr_available())


@app.post("/importa/cartella")
def importa_cartella(request: Request, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "medico")
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    r = importa.import_folder(IMPORT_DIR, user["username"])
    db.log_access(user["username"], "importazione dalla cartella condivisa", f"{r['id']}: {r['file']} file")
    return RedirectResponse(f"/importa?job={r['id']}", status_code=303)


@app.post("/importa/pacchetto")
async def importa_pacchetto(request: Request, csrf_token: str = Form(""), files: list[UploadFile] = File(...)):
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "medico")
    def items():  # un file alla volta: niente pacchetto intero decompresso in memoria
        for f in files[:500]:
            f.file.seek(0)
            if (f.filename or "").lower().endswith(".zip"):
                yield from importa.iter_zip_file(f.file)
            else:
                yield f.filename or "senza_nome", f.file.read(importa.MAX_BYTES + 1)
    r = importa.run_job(items(), "pacchetto caricato", user["username"])
    db.log_access(user["username"], "importazione da pacchetto", f"{r['id']}: {r['file']} file")
    return RedirectResponse(f"/importa?job={r['id']}", status_code=303)


@app.get("/smistare/{iid}", response_class=HTMLResponse)
def smistare(request: Request, iid: str, q: str = ""):
    user = current_user(request)
    need(user, "medico")
    with db.tx() as con:
        it = con.execute("SELECT * FROM inbox WHERE id=?", (iid,)).fetchone()
        if not it:
            raise HTTPException(404)
        it = dict(it)
        cands = [dict(p) for p in con.execute("SELECT id, name, birth_date, cf FROM patients WHERE name LIKE ? OR cf LIKE ? ORDER BY name LIMIT 30",
                                               (f"%{q}%", f"%{q.upper()}%"))] if q else []
    it["pages"] = importa.page_count(it["file"]) if importa.can_preview() or not it["file"].endswith(".pdf") else 0
    db.log_access(user["username"], "apre documento da smistare", iid)
    return render(request, "smistare.html", user, it=it, q=q, cands=cands)


@app.get("/smistare/{iid}/file")
def smistare_file(request: Request, iid: str):
    user = current_user(request)
    need(user, "medico")
    with db.tx() as con:
        it = con.execute("SELECT * FROM inbox WHERE id=?", (iid,)).fetchone()
    if not it:
        raise HTTPException(404)
    path = (importa.ARCHIVE / it["file"]).resolve()
    if importa.ARCHIVE.resolve() not in path.parents or not path.exists():
        raise HTTPException(404)
    db.log_access(user["username"], "apre originale da smistare", iid)
    return FileResponse(path, media_type=it["mime"], content_disposition_type="inline")


@app.post("/smistare/{iid}/assegna")
def smistare_assegna(request: Request, iid: str, csrf_token: str = Form(""), patient_id: str = Form(...)):
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "medico")
    with db.tx() as con:
        it = dict(con.execute("SELECT * FROM inbox WHERE id=? AND status='da smistare'", (iid,)).fetchone() or {})
        pat = con.execute("SELECT id, name FROM patients WHERE id=?", (patient_id,)).fetchone()
        if not it or not pat:
            raise HTTPException(400, "Documento o paziente non valido")
        h = importa.parse(it["text"] or "")
        nid = "D-" + it["sha256"][:12]
        con.execute("INSERT INTO notes (id,patient_id,kind,source,date,text,title,author,file,sha256,mime,origin,imported_at,imported_by,read_with) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (nid, pat["id"], h["kind"], h["source"], h["date"] or db.now()[:10], h["body"] or it["text"] or "(nessun testo leggibile)",
                     h["title"] or "Documento", h["author"] or "non indicato", it["file"], it["sha256"], it["mime"],
                     it["origin"] + f" · assegnato a mano da {user['username']}", db.now(), user["username"], it["read_with"] or "non indicato"))
        con.execute("UPDATE inbox SET status='assegnato' WHERE id=?", (iid,))
    db.log_action(user["username"], iid, f"Documento da smistare {iid} assegnato a {pat['name']} da {user['name']}")
    return go(request, f"/pazienti/{pat['id']}?doc={nid}", flash=f"Documento assegnato a {pat['name']}")


@app.post("/smistare/{iid}/scarta")
def smistare_scarta(request: Request, iid: str, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "medico")
    with db.tx() as con:
        con.execute("UPDATE inbox SET status='scartato' WHERE id=?", (iid,))
    db.log_action(user["username"], iid, f"Documento da smistare {iid} scartato da {user['name']}")
    return RedirectResponse("/importa", status_code=303)


# ------------------------------------------------------------------ trasporti (solo accettazione)

def _transport(con, rid):
    r = con.execute("SELECT * FROM transport_requests WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404, "Pratica non trovata")
    r, p, a, s = trasporti._load(con, rid)
    ev = [dict(e) for e in con.execute("SELECT * FROM transport_events WHERE request_id=? ORDER BY rowid", (rid,))]
    mails = [dict(m) for m in con.execute("SELECT * FROM outbox WHERE case_id=? ORDER BY id", (rid,))]
    return r, p, a, s, ev, mails


@app.get("/trasporti", response_class=HTMLResponse)
def trasporti_list(request: Request):
    user = current_user(request)
    need(user, "segreteria")
    with db.tx() as con:
        trasporti.scadenze(con)
        rows = [dict(r) for r in con.execute(
            "SELECT t.*, s.name AS service, a.at, a.service AS visit, p.name AS patient FROM transport_requests t "
            "JOIN transport_services s ON s.id=t.service_id JOIN appointments a ON a.id=t.appointment_id "
            "JOIN patients p ON p.id=t.patient_id ORDER BY a.at")]
        services = [dict(s) for s in con.execute("SELECT * FROM transport_services")]
    for r in rows:
        r["label"] = trasporti.STATUS.get(r["status"], r["status"])
    return render(request, "trasporti.html", user, rows=rows, services=services)


# passi che chi lavora deve vedere subito, senza ricaricare: chi ha risposto, chi ha preso un posto
NOVITA = {"mail ricevuta": "Mail del servizio arrivata", "lettura": "L'agente ha letto la risposta",
          "appunto": "Appunto dell'agente", "chiamata": "L'agente chiama la persona",
          "risposta servizio": "Risposta ricevuta", "esito chiamata": "Esito della chiamata", "nessuna risposta": "La persona non risponde",
          "scadenza": "Il servizio non ha risposto", "cambio servizio": "Cambio di servizio", "fermo": "Serve l'accettazione"}
NOVITA_POSTI = {"proposta": "Posto liberato: serve il via libera", "accettato": "Posto assegnato", "rifiutato": "Ha rifiutato il posto",
                "senza esito": "Chiamata senza risposta", "nessuna risposta": "Non ha risposto", "altra data": "Vuole un'altra data", "errore aggiornamento": "Posto: da controllare a mano"}


_AVVIO = db.now()


@app.get("/trasporti/novita")
def novita_pagina_vecchia(request: Request):
    """Indirizzo di prima di /novita: lo chiedono solo le schede aperte prima dell'aggiornamento, con il JavaScript vecchio.
    Nessun dato: un solo avviso (ora fissa, quindi il vecchio script lo mostra una volta) che chiede di ricaricare."""
    current_user(request)
    return {"ora": db.now(), "novita": [{"rid": "", "ts": _AVVIO, "step": "pagina vecchia", "titolo": "Pagina da ricaricare",
                                          "patient": "Filo", "text": "Questa scheda è stata aperta prima dell'aggiornamento di Filo: premi F5."}]}


@app.get("/novita")
def novita(request: Request, dopo: str = ""):
    """Passi registrati da `dopo` in poi (ora del server), per il ruolo di chi chiede. La pagina li chiede ogni pochi secondi.
    `ricarica`: le pagine che, se aperte, si aggiornano da sole dopo l'avviso."""
    user = current_user(request)
    dopo = dopo[:19] or db.now()
    out = []
    with db.tx() as con:
        if user["role"] == "segreteria":
            for r in con.execute(
                    f"SELECT e.request_id AS rid, e.ts, e.step, e.text, p.name AS patient FROM transport_events e "
                    f"JOIN transport_requests t ON t.id=e.request_id JOIN patients p ON p.id=t.patient_id "
                    f"WHERE e.ts >= ? AND e.step IN ({','.join('?' * len(NOVITA))}) ORDER BY e.ts", (dopo, *NOVITA)):
                out.append({"ts": r["ts"], "step": r["step"], "titolo": NOVITA[r["step"]], "patient": r["patient"], "text": r["text"],
                            "link": f"/trasporti/{r['rid']}", "ricarica": [f"/trasporti/{r['rid']}", "/trasporti", "/cruscotto"]})
            for r in con.execute(
                    f"SELECT e.ts, e.step, e.text, e.slot_id, COALESCE(o.case_id, (SELECT c.id FROM cases c WHERE c.kind='posto' AND c.payload LIKE '%' || o.id || '%' "
                    f"ORDER BY c.rowid DESC LIMIT 1)) AS case_id, p.name AS patient FROM slot_events e JOIN slot_offers o ON o.id=e.offer_id "
                    f"JOIN patients p ON p.id=o.patient_id WHERE e.ts >= ? AND e.step IN ({','.join('?' * len(NOVITA_POSTI))}) ORDER BY e.ts",
                    (dopo, *NOVITA_POSTI)):
                out.append({"ts": r["ts"], "step": r["step"], "titolo": NOVITA_POSTI[r["step"]], "patient": r["patient"], "text": r["text"],
                            "link": (f"/caso/{r['case_id']}" if r["step"] == "proposta" and r["case_id"] else
                                     "/cruscotto" if r["step"] in ("proposta", "senza esito") else "/posti"),
                            "ricarica": ["/posti", "/cruscotto", "/chiamate"]})
            for r in con.execute("SELECT l.ts, l.text, a.patient_id, a.service, a.at, p.name AS patient FROM cal_log l "
                                 "JOIN appointments a ON a.id=l.appointment_id JOIN patients p ON p.id=a.patient_id "
                                 "WHERE l.ts >= ? AND l.op='cancella' ORDER BY l.ts", (dopo,)):
                fs = re.search(r"\((FS-[0-9a-f]+)\)", r["text"] or "")
                n = sum(w["status"] == "in attesa" for w in posti._lista_posto(con, posti._slot(con, fs.group(1)))[0]) if fs else 0
                out.append({"ts": r["ts"], "step": "cancellata", "titolo": "Prestazione cancellata", "patient": r["patient"],
                            "text": f"{r['service']} del {r['at'][8:10]}/{r['at'][5:7]} alle {r['at'][11:16]}: " + (
                                f"{n} {'persona' if n == 1 else 'persone'} in lista d'attesa {'può' if n == 1 else 'possono'} prendere il posto, "
                                "tra pochi secondi la proposta da approvare." if n else
                                "nessuno in lista d'attesa per questa prestazione, il posto torna libero nel calendario."),
                            "link": "/posti" if n else f"/persona/{r['patient_id']}/calendario", "ricarica": ["/posti", "/cruscotto"]})
            for r in con.execute("SELECT e.ts, e.text, f.service, f.at FROM slot_events e JOIN freed_slots f ON f.id=e.slot_id "
                                 "WHERE e.ts >= ? AND e.step='scelta in corso' ORDER BY e.ts", (dopo,)):
                out.append({"ts": r["ts"], "step": "scelta in corso", "titolo": "Prestazione cancellata: posto libero",
                            "patient": f"{r['service']} {r['at'][8:10]}/{r['at'][5:7]} {r['at'][11:16]}",
                            "text": "L'agente Agende sta scegliendo dalla lista d'attesa chi può prenderlo: la proposta arriva tra pochi secondi.",
                            "link": "/posti", "ricarica": []})
        else:  # il medico: chi è appena entrato nella sua agenda da un posto liberato, con la cartella a un clic
            for r in con.execute(
                    "SELECT e.ts, e.step, o.patient_id, p.name AS patient, s.service, s.at FROM slot_events e JOIN slot_offers o ON o.id=e.offer_id "
                    "JOIN patients p ON p.id=o.patient_id JOIN freed_slots s ON s.id=e.slot_id WHERE e.ts >= ? AND e.step='verificato' ORDER BY e.ts",
                    (dopo,)):
                out.append({"ts": r["ts"], "step": "nuovo in agenda", "titolo": "Nuovo in agenda", "patient": r["patient"],
                            "text": f"Ha preso il posto liberato: {r['service']} del {r['at'][8:10]}/{r['at'][5:7]} alle {r['at'][11:16]}. Apri la cartella.",
                            "link": f"/pazienti/{r['patient_id']}", "ricarica": ["/cruscotto"]})
    out.sort(key=lambda x: x["ts"])
    return {"ora": db.now(), "novita": out}


@app.get("/trasporti/{rid}", response_class=HTMLResponse)
def trasporto(request: Request, rid: str):
    user = current_user(request)
    need(user, "segreteria")
    with db.tx() as con:
        trasporti.scadenze(con)
        r, p, a, s, ev, mails = _transport(con, rid)
        strike = trasporti.strike_at(con, a["at"])
    db.log_access(user["username"], "apre pratica trasporto", f"{rid} ({p['name']})")
    script = trasporti.call_script(p, a, s, r["pickup"] or trasporti._pickup(a), strike)
    _segui(request, r["patient_id"], p["name"])
    with db.tx() as con:
        call = con.execute("SELECT id FROM calls WHERE ref_id=? AND kind='trasporto' AND ended_at IS NULL ORDER BY created_at DESC", (rid,)).fetchone()
    return render(request, "trasporto.html", user, r=r, p=p, a=a, s=s, ev=ev, mails=mails, script=script,
                  label=trasporti.STATUS.get(r["status"], r["status"]), pickup=trasporti._pickup(a), posta=posta_vera.stato(), call=dict(call) if call else None)


@app.post("/trasporti/{rid}/risposta")
def trasporto_risposta(request: Request, rid: str, csrf_token: str = Form(""), esito: str = Form(...), partenza: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "segreteria")
    norm = trasporti.norm_time(partenza)
    if esito == "disponibile" and partenza.strip() and not norm:
        return go(request, f"/trasporti/{rid}", error=f"Orario non valido: {partenza.strip()[:10]}. Scrivi ore e minuti, per esempio 09:30.")
    trasporti.risposta_servizio(rid, esito == "disponibile", norm, user)
    if esito == "disponibile":
        trasporti.chiama_se_possibile(rid)
    return RedirectResponse(f"/trasporti/{rid}", status_code=303)


@app.post("/trasporti/{rid}/chiamata")
def trasporto_chiamata(request: Request, rid: str, csrf_token: str = Form(""), esito: str = Form(...)):
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "segreteria")
    if esito not in ("serve", "non_serve", "non_risponde"):
        raise HTTPException(400, "Esito non valido")
    trasporti.esito_chiamata(rid, esito, user)
    return RedirectResponse(f"/trasporti/{rid}", status_code=303)


def _case_for(user, case_id):
    with db.tx() as con:
        c = con.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not c:
            raise HTTPException(404, "Caso non trovato")
        c = dict(c)
        if c["role"] != user["role"]:
            db.log_access(user["username"], "TENTATIVO NEGATO su caso di altro ruolo", case_id)
            raise HTTPException(403, "Questo caso non è di tua competenza.")
        p = dict(con.execute("SELECT * FROM patients WHERE id=?", (c["patient_id"],)).fetchone())
        log = [dict(r) for r in con.execute("SELECT * FROM action_log WHERE case_id=? ORDER BY ts", (case_id,))]
        code = con.execute("SELECT v.code FROM vault v WHERE v.run_id=? AND v.patient_id=? AND v.agent NOT LIKE '%candidato' LIMIT 1",
                           (c["run_id"], c["patient_id"])).fetchone()
    return c, p, log, code and code[0]


@app.get("/caso/{case_id}", response_class=HTMLResponse)
def caso(request: Request, case_id: str):
    user = current_user(request)
    c, p, log, code = _case_for(user, case_id)
    db.log_access(user["username"], "apre la scheda", f"{case_id} ({p['name']})")
    if user["role"] == "segreteria":  # minimo necessario: niente medico di famiglia, niente note cliniche
        p = {k: p[k] for k in ("name", "birth_year", "address", "phone", "quartiere", "distretto", "lives_alone", "has_car")}
        val = None
    else:
        with db.tx() as con:
            val = valutazione.latest(con, c["patient_id"])
    with db.tx() as con:
        r0 = con.execute("SELECT started_at FROM runs WHERE id=?", (c["run_id"],)).fetchone()
    c["since"] = r0[0] if r0 else None
    with db.tx() as con:  # tutto quello che c'è da decidere per la stessa persona sta su questa schermata
        altri = [dict(r) for r in con.execute("SELECT * FROM cases WHERE patient_id=? AND role=? AND status='pending' AND id != ? ORDER BY score DESC",
                                              (c["patient_id"], user["role"], case_id))]
    _segui(request, c["patient_id"], p["name"])
    return render(request, "caso.html", user, c=c, p=p, log=log, val=val, altri=altri,
                  ev=json.loads(c["evidence"]), parts=(parts := json.loads(c["parts"] or "[]")), tot=sum(v for _, v in parts))


@app.post("/caso/{case_id}/approva")
def approva(request: Request, case_id: str, csrf_token: str = Form(""), code: str = Form(""), torna: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    c, *_ = _case_for(user, case_id)
    if c["status"] != "pending":
        return go(request, f"/caso/{case_id}", error="Questo caso è già stato deciso.")
    if user["role"] == "medico":  # firma: serve di nuovo il codice a tempo
        ok, msg = one_time_code_ok(user["username"], code.strip())
        db.log_access(user["username"], "firma con codice" if ok else "firma rifiutata", case_id)
        if not ok:
            return go(request, f"/caso/{case_id}", error=msg)
    try:
        done = actions.execute(case_id, user)  # presa del caso atomica: un doppio clic non esegue due volte
    except ValueError as e:
        return go(request, f"/caso/{case_id}", error=str(e))
    if done is None:
        return go(request, f"/caso/{case_id}", error="Questo caso è già stato deciso (in un'altra finestra o da un collega).")
    with db.tx() as con:
        resta = [dict(r) for r in con.execute("SELECT id, tag FROM cases WHERE patient_id=? AND role=? AND status='pending' AND id != ? "
                                              "ORDER BY score DESC", (c["patient_id"], c["role"], case_id))]
        tr = con.execute("SELECT id FROM transport_requests WHERE case_id=?", (case_id,)).fetchone() if c["kind"] == "trasporto" else None
    if resta:  # la stessa persona ha altro da decidere: si resta sulla sua schermata
        dove = "/cruscotto" if torna == "cruscotto" else f"/caso/{torna if torna in {r['id'] for r in resta} else case_id}"
        extra = f" Pratica del trasporto aperta: {tr[0]}." if tr else ""
        return go(request, dove, flash=f"Fatto: {'; '.join(done)}.{extra} Per questa persona resta: {', '.join(r['tag'] for r in resta)}.")
    if c["kind"] == "trasporto":
        with db.tx() as con:
            row = con.execute("SELECT id FROM transport_requests WHERE case_id=?", (case_id,)).fetchone()
        if row:
            return RedirectResponse(f"/trasporti/{row[0]}", status_code=303)
    if c["kind"] in ("posto", "modifica"):  # la chiamata appena approvata: si avvia da lì
        with db.tx() as con:
            call = json.loads(con.execute("SELECT payload FROM cases WHERE id=?", (case_id,)).fetchone()[0]).get("call")
        if call:
            return go(request, f"/chiamate/{call}", flash=f"Fatto: {'; '.join(done)}")
    return go(request, "/cruscotto", flash=f"Fatto: {'; '.join(done)}")


@app.post("/caso/{case_id}/scarta")
def scarta(request: Request, case_id: str, csrf_token: str = Form(""), motivo: str = Form(""), code: str = Form(""), tipo: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    c, p, *_ = _case_for(user, case_id)
    if user["role"] == "medico":  # scartare una segnalazione clinica pesa quanto firmarla
        ok, msg = one_time_code_ok(user["username"], code.strip())
        db.log_access(user["username"], "scarto con codice" if ok else "scarto rifiutato", case_id)
        if not ok:
            return go(request, f"/caso/{case_id}", error=msg)
    tipo = tipo if tipo in ui.SCARTO else ""
    motivo = ": ".join(x for x in (tipo, motivo.strip()[:300]) if x) or "nessun motivo indicato"
    with db.tx() as con:
        n = con.execute("UPDATE cases SET status='scartato', decided_by=?, decided_at=? WHERE id=? AND status='pending'",
                        (user["username"], db.now(), case_id)).rowcount
    if not n:
        return go(request, f"/caso/{case_id}", error="Questo caso è già stato deciso: non si può più scartare.")
    db.log_action(user["username"], case_id, f"{p['name']}: segnalazione scartata da {user['name']}. Motivo: {motivo}")
    if c["kind"] == "modifica":  # modifica ritirata: resta la data di prima, l'ambulatorio viene avvisato
        try:
            modifiche.ritira(case_id, user, motivo)
        except ValueError:
            pass
        return go(request, "/calendario", flash="Modifica ritirata: resta la data di prima, l'ambulatorio è stato avvisato")
    if c["kind"] == "posto":  # proposta saltata: Filo prepara quella per il successivo in ordine (da approvare)
        posti.scartato(case_id, user, motivo)
        return go(request, "/posti", flash="Proposta saltata e annotata: se c'è, la proposta per il successivo è nel cruscotto")
    return go(request, "/cruscotto", flash="Segnalazione scartata e annotata nel registro")


@app.post("/caso/{case_id}/nota")
def nota(request: Request, case_id: str, csrf_token: str = Form(""), testo: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    c, p, *_ = _case_for(user, case_id)
    if testo.strip():
        db.log_action(user["username"], case_id, f"{p['name']}: nota di {user['name']}: {testo.strip()[:500]}")
    return RedirectResponse(f"/caso/{case_id}", status_code=303)


# ------------------------------------------------------------------ elaborazione

@app.post("/elabora")
def elabora(request: Request, csrf_token: str = Form("")):
    """Aperta a entrambi i ruoli: un'elaborazione non cancella più i casi in attesa (li aggiorna con lo stesso identificativo)
    e non fa rinascere quelli già decisi."""
    check_csrf(request, csrf_token)
    user = current_user(request)
    db.log_access(user["username"], "avvia elaborazione")
    try:
        s = gateway.run(user["username"])
    except Exception as e:
        return go(request, "/cruscotto", error=f"Elaborazione non riuscita: {type(e).__name__}: {str(e)[:200]}")
    if s.get("blocked"):
        return go(request, "/cruscotto", error=s["attestazione"]["detail"])
    nuovi = s.get("salvataggio", {}).get("nuovi", 0)
    return go(request, "/cruscotto", flash=f"Novità elaborate: {nuovi} segnalazioni nuove"
              + (f", {s['a_mano']} da controllare a mano" if s.get("a_mano") else "") + ".")


@app.get("/elaborazione")
def elaborazione_vecchia(request: Request, run: str | None = None):
    return RedirectResponse("/impostazioni/invii" + (f"?run={run}" if run else ""), status_code=303)


@app.get("/impostazioni/invii", response_class=HTMLResponse)
def elaborazione(request: Request, run: str | None = None):
    user = current_user(request)
    medico = user["role"] == "medico"
    with db.tx() as con:
        r = (con.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone() if run else None) or _last_run(con)
        r = dict(r) if r else None
        packets, results, gates, recomp, hidden = [], {}, [], [], 0
        if r:
            rid = r["id"]
            packets = [dict(x) for x in con.execute("SELECT * FROM packets_out WHERE run_id=? ORDER BY batch, seq", (rid,))]
            results = {x["code"]: {**dict(x), "o": json.loads(x["output"])} for x in con.execute("SELECT * FROM results_in WHERE run_id=?", (rid,))}
            gates = [dict(x) for x in con.execute("SELECT g.*, p.name FROM gate_checks g JOIN patients p ON p.id=g.patient_id WHERE run_id=?", (rid,))]
            if medico:  # la ricomposizione codice -> persona la vede solo chi ha titolo clinico
                recomp = [dict(x) for x in con.execute("SELECT v.code, v.agent, p.name FROM vault v JOIN patients p ON p.id=v.patient_id WHERE run_id=? ORDER BY v.agent", (rid,))]
            else:
                # all'accettazione solo la parte logistica: niente pacchetti clinici, niente righe del controllo sulle malattie
                keep = [pk for pk in packets if pk["agent"] == "trasporti" or '"tipo": "posto_liberato"' in pk["payload"]]
                hidden = len(packets) - len(keep)
                packets = keep
                gates = [g for g in gates if g["agent"] == "trasporti"]
        n_notes = con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    random.SystemRandom().shuffle(gates)  # l'ordine delle righe non deve dire nulla su chi è vicino a chi
    db.log_access(user["username"], "apre elaborazione", r["id"] if r else "")
    return render(request, "elaborazione.html", user, tab="invii", r=r and {**r, "s": json.loads(r["summary"] or "{}")}, packets=packets,
                  results=results, gates=gates, recomp=recomp, agents=AGENTS, k_min=K_MIN, hidden=hidden,
                  attestation_mode=ATTESTATION_MODE, n_notes=n_notes)


# ------------------------------------------------------------------ registro e posta

def _appunti(con) -> list[dict]:
    """Appunti dell'agente (pratiche di trasporto e kit): stanno nel registro e in ogni pratica, non nel cruscotto,
    che deve mostrare solo quello che c'è da fare."""
    appunti = [dict(r, link=f"/trasporti/{r['request_id']}", ref=f"pratica {r['request_id']}") for r in con.execute(
        "SELECT e.request_id, e.ts, e.text, p.name AS patient FROM transport_events e JOIN transport_requests t ON t.id=e.request_id "
        "JOIN patients p ON p.id=t.patient_id WHERE e.step='appunto' ORDER BY e.rowid DESC LIMIT 8")]
    appunti += [dict(ts=r["ts"], patient=r["patient"], link=None, ref="kit per la visita",
                     text=(f"{r['label']} riservato per la visita del {r['at']} e richiesto al coordinamento ambulatori. "
                           "Il medico trova la nota sull'appuntamento." if r["status"] == "riservato" else r["nota"]))
                for r in con.execute("SELECT b.*, k.label, p.name AS patient FROM kit_bookings b LEFT JOIN kits k ON k.id=b.kit_id "
                                     "JOIN patients p ON p.id=b.patient_id ORDER BY b.ts DESC LIMIT 8")]
    appunti.sort(key=lambda r: r["ts"], reverse=True)
    return appunti


@app.get("/registro")
def registro_vecchio(request: Request):
    return RedirectResponse("/impostazioni/log", status_code=303)


@app.get("/impostazioni/log", response_class=HTMLResponse)
def registro(request: Request):
    user = current_user(request)
    with db.tx() as con:
        # ogni ruolo vede gli accessi del proprio ruolo: sapere che il medico ha aperto una scheda è già un'informazione clinica
        acc = [dict(x) for x in con.execute(
            "SELECT a.* FROM access_log a JOIN users u ON u.username=a.username WHERE u.role=? ORDER BY ts DESC LIMIT 200", (user["role"],))]
        # le azioni si vedono per ruolo di chi le ha fatte: lo smistamento di un documento clinico non finisce all'accettazione.
        # Le azioni dell'agente vocale seguono il ruolo della chiamata.
        act = [dict(x) for x in con.execute(
            "SELECT a.* FROM action_log a LEFT JOIN users u ON u.username=a.username LEFT JOIN calls k ON k.id=a.case_id "
            "LEFT JOIN cases c ON c.id=a.case_id WHERE COALESCE(u.role, k.role, c.role)=? ORDER BY ts DESC LIMIT 200",
            (user["role"],))]
        appunti = _appunti(con) if user["role"] == "segreteria" else []
    return render(request, "registro.html", user, tab="log", acc=acc, act=act, appunti=appunti)


# ------------------------------------------------------------------ impostazioni: profilo e aspetto

TITLES = ["", "Dott.", "Dott.ssa", "Inf.", "Sig.", "Sig.ra"]
THEMES = {"auto": "Come il sistema", "light": "Chiaro", "dark": "Scuro"}
tpl.env.globals.update(TITLES=TITLES, THEMES=THEMES)


def _profile_defaults(user: dict) -> dict:
    """Prima volta: titolo, nome e cognome ricavati dal nome mostrato (es. "Dott.ssa Elena Gatti")."""
    words = (user.get("name") or "").split()
    title = words[0] if words and words[0] in TITLES else ""
    rest = words[1:] if title else words
    return {"title": user.get("title") if user.get("first_name") else title,
            "first_name": user.get("first_name") or (rest[0] if rest else ""),
            "last_name": user.get("last_name") or " ".join(rest[1:]),
            "qualifica": user.get("qualifica") or "", "struttura": user.get("struttura") or "", "reparto": user.get("reparto") or ""}


@app.get("/impostazioni", response_class=HTMLResponse)
def impostazioni(request: Request):
    user = current_user(request)
    return render(request, "impostazioni.html", user, tab="profilo", prof=_profile_defaults(user))


@app.post("/impostazioni/profilo")
def impostazioni_profilo(request: Request, csrf_token: str = Form(""), title: str = Form(""), first_name: str = Form(""),
                         last_name: str = Form(""), qualifica: str = Form(""), struttura: str = Form(""), reparto: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    clean = lambda v: re.sub(r"\s+", " ", v).strip()[:80]
    first_name, last_name = clean(first_name), clean(last_name)
    if not first_name or not last_name:
        return go(request, "/impostazioni", error="Nome e cognome sono obbligatori.")
    if title not in TITLES:
        return go(request, "/impostazioni", error="Titolo non previsto.")
    name = " ".join(x for x in (title, first_name, last_name) if x)
    with db.tx() as con:
        con.execute("UPDATE users SET name=?, title=?, first_name=?, last_name=?, qualifica=?, struttura=?, reparto=? WHERE username=?",
                    (name, title, first_name, last_name, clean(qualifica), clean(struttura), clean(reparto), user["username"]))
    db.log_access(user["username"], "aggiorna il proprio profilo")
    return go(request, "/impostazioni", flash="Profilo salvato.")


@app.post("/impostazioni/tema")
def impostazioni_tema(request: Request, csrf_token: str = Form(""), theme: str = Form("auto")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    if theme not in THEMES:
        raise HTTPException(400, "Tema non previsto")
    with db.tx() as con:
        con.execute("UPDATE users SET theme=? WHERE username=?", (theme, user["username"]))
    return go(request, "/impostazioni#aspetto", flash=f"Aspetto: {THEMES[theme].lower()}.")


@app.get("/posta", response_class=HTMLResponse)
def posta(request: Request):
    user = current_user(request)
    with db.tx() as con:
        mails = [dict(x) for x in con.execute("SELECT * FROM outbox WHERE role=? ORDER BY id DESC", (user["role"],))]
    db.log_access(user["username"], "apre posta in uscita")
    return render(request, "posta.html", user, mails=mails)


@app.get("/posta/{mail_id}/anteprima", response_class=HTMLResponse)
def posta_anteprima(request: Request, mail_id: int):
    user = current_user(request)
    with db.tx() as con:
        m = con.execute("SELECT html FROM outbox WHERE id=? AND role=?", (mail_id, user["role"])).fetchone()
    if not m or not m["html"]:
        raise HTTPException(404)
    resp = HTMLResponse(m["html"])
    resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'"
    return resp


@app.get("/posta/{mail_id}/calendario.ics")
def ics(request: Request, mail_id: int):
    user = current_user(request)
    with db.tx() as con:
        m = con.execute("SELECT * FROM outbox WHERE id=? AND role=?", (mail_id, user["role"])).fetchone()
    if not m or not m["ics"]:
        raise HTTPException(404)
    path = (OUTBOX / m["ics"]).resolve()
    if path.parent != OUTBOX.resolve() or not path.exists():
        raise HTTPException(404)
    db.log_access(user["username"], "scarica evento calendario", m["ics"])
    return FileResponse(path, media_type="text/calendar", filename="visita.ics")


# ------------------------------------------------------------------ calendario (solo accettazione)

@app.get("/calendario", response_class=HTMLResponse)
def calendario_pagina(request: Request, esame: str = "ecocardiogramma", dal: str = ""):
    user = current_user(request)
    need(user, "segreteria")
    if esame not in calendario.AGENDE:
        esame = "ecocardiogramma"
    try:
        start = date.fromisoformat(dal) if dal else date.fromisoformat(TODAY)
    except ValueError:
        start = date.fromisoformat(TODAY)
    giorni = [start + timedelta(days=i) for i in range(7)]
    with db.tx() as con:  # letto adesso dal database: è la stessa fonte che usa l'agente Agende
        slots = calendario.consulta(con, esame, giorni[0].isoformat(), giorni[-1].isoformat() + " 23:59", limite=2000)
        log = [dict(r) for r in con.execute("SELECT * FROM cal_log ORDER BY rowid DESC LIMIT 30")]
        liberi = calendario.disponibilita(con, esame, limite=5)
        liberi_scelta = calendario.disponibilita(con, esame, dal=(date.fromisoformat(TODAY) + timedelta(days=1)).isoformat(), limite=40)
        n = con.execute("SELECT COUNT(*), SUM(test) FROM cal_slots").fetchone()
        prenotate = [dict(r) for r in con.execute(
            "SELECT a.*, p.name AS patient FROM appointments a JOIN patients p ON p.id=a.patient_id WHERE a.status='prenotata' AND a.at>=? "
            "ORDER BY a.at", (TODAY,))]
        mods = modifiche.quadro(con)
    orari = sorted({x["at"][11:] for x in slots})
    griglia = {(x["at"][:10], x["at"][11:]): x for x in slots}
    colonne = []  # una colonna per giorno: i posti occupati di fila diventano una riga sola, liberi e persone di Filo restano in vista
    for g in giorni:
        voci, pieno = [], None
        for x in [y for y in slots if y["at"][:10] == g.isoformat()]:
            if x["status"] == "prenotato" and not x["patient_id"]:
                if pieno:
                    pieno.update(al=x["at"][11:16], n=pieno["n"] + 1)
                else:
                    pieno = {"tipo": "pieno", "dal": x["at"][11:16], "al": x["at"][11:16], "n": 1}
                    voci.append(pieno)
                continue
            pieno = None
            voci.append({"tipo": x["status"], "ora": x["at"][11:16], "x": x})
        colonne.append({"g": g, "voci": voci, "liberi": sum(v["tipo"] == "libero" for v in voci),
                        "persone": sum(1 for v in voci if v.get("x") and v["x"]["patient_id"])})
    db.log_access(user["username"], "apre calendario", esame)
    return render(request, "calendario.html", user, esame=esame, esami=list(calendario.AGENDE), giorni=giorni, orari=orari, griglia=griglia,
                  colonne=colonne,
                  log=log, liberi=liberi, prima=(start - timedelta(days=7)).isoformat(), dopo=(start + timedelta(days=7)).isoformat(),
                  totale=n[0], prova=n[1] or 0, STATO=calendario.STATO_TESTO, prenotate=prenotate, liberi_scelta=liberi_scelta,
                  mods=mods)


@app.post("/calendario/modifica")
def calendario_modifica(request: Request, csrf_token: str = Form(""), prenotazione: str = Form(...), azione: str = Form(...),
                        posto: str = Form(""), motivo: str = Form(""), torna: str = Form("")):
    """L'ospedale deve cambiare una prenotazione: si apre la procedura (posto tenuto, ambulatorio avvisato, chiamata da approvare)."""
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "segreteria")
    if not motivo.strip():
        return go(request, "/calendario", error="Scrivi il motivo della modifica: lo sentono l'ambulatorio e il paziente.")
    r = calendario.esegui("calendario_sposta" if azione == "sposta" else "calendario_cancella",
                          {"prenotazione": prenotazione, "posto": posto or None, "motivo": motivo.strip()[:300]}, user)
    db.log_access(user["username"], "propone modifica prenotazione", prenotazione)
    if not r["ok"]:
        return go(request, "/calendario", error="Modifica non aperta: " + (r.get("errore") or "; ".join(r.get("verifica", []))))
    return go(request, torna if torna.startswith("/persona/") else "/cruscotto",
              flash="Modifica proposta: posto tenuto, ambulatorio avvisato. Approva la chiamata al paziente: è tra le cose da fare.")


@app.post("/calendario/disdetta")
def calendario_disdetta(request: Request, csrf_token: str = Form(""), prenotazione: str = Form(...), motivo: str = Form(""), torna: str = Form("")):
    """La persona avvisa che non può venire: la prenotazione si annulla nel calendario, il posto passa subito alla lista d'attesa
    e l'agente Agende sceglie per urgenza a chi proporlo. La chiamata parte solo con il via libera nel cruscotto."""
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "segreteria")
    try:
        with db.tx() as con:
            a = con.execute("SELECT a.*, p.name FROM appointments a JOIN patients p ON p.id=a.patient_id WHERE a.id=?", (prenotazione,)).fetchone()
            if not a or a["status"] != "prenotata":
                raise ValueError("Prenotazione non trovata o non più attiva.")
            fs = calendario.cancella(con, prenotazione, user, f"la persona ha avvisato che non può venire{': ' + motivo.strip()[:200] if motivo.strip() else ''}")
            con.execute("UPDATE kit_bookings SET status='annullato' WHERE appointment_id=? AND status='riservato'", (prenotazione,))
            nuove = posti.avanza(con) if fs else []
            chi = next((c for c in nuove if c["payload"].get("slot") == fs), None)
            in_scelta = bool(fs) and posti._scelta_in_corso(con, fs)
    except (ValueError, calendario.Errore) as e:
        return go(request, "/calendario", error=f"Disdetta non registrata: {e}")
    db.log_action(user["username"], None, f"disdetta di {a['name']}: {a['service']} {a['at']}")
    dove = torna if torna.startswith("/persona/") else None
    if not fs:
        return go(request, dove or "/calendario", flash=f"Disdetta registrata per {a['name']}. Il posto è già passato o non va alla lista.")
    return go(request, dove or "/cruscotto", flash=f"Disdetta registrata: {a['service']} del {a['at'][8:10]}/{a['at'][5:7]} alle {a['at'][11:16]} torna libero. "
              + (f"L'agente Agende propone {chi['patient']['name']}: serve il via libera." if chi else
                 ("L'agente Agende sta scegliendo sulla piattaforma: la proposta arriva nel cruscotto tra pochi secondi." if in_scelta
                  else "Nessuno in lista a cui proporlo adesso.")))


@app.post("/calendario/blocca")
def calendario_blocca(request: Request, csrf_token: str = Form(""), posto: str = Form(...), motivo: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "segreteria")
    r = calendario.esegui("calendario_blocca", {"posto": posto, "motivo": motivo.strip()[:300] or "ambulatorio non disponibile"}, user)
    db.log_access(user["username"], "blocca posto", posto)
    if not r["ok"]:
        return go(request, "/calendario", error="Posto non chiuso: " + (r.get("errore") or "; ".join(r.get("verifica", []))))
    if r.get("in_attesa"):
        return go(request, "/cruscotto", flash="Nel posto c'era una prenotazione: proposto lo spostamento, ambulatorio avvisato, chiamata da approvare.")
    return go(request, "/calendario", flash="Posto chiuso.")


@app.post("/calendario/modifiche/{mid}/{azione}")
def calendario_modifica_azione(request: Request, mid: str, azione: str, csrf_token: str = Form(""), scelta: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "segreteria")
    try:
        if azione == "esito":
            msg = modifiche.esito_a_mano(mid, scelta, user)
        elif azione == "riprova":
            msg = modifiche.riprova(mid, user)
        elif azione == "ritira":
            modifiche.ritira(mid, user, "ritirata dalla pagina Calendario"); msg = "modifica ritirata"
        else:
            raise HTTPException(404)
    except ValueError as e:
        return go(request, "/calendario", error=str(e))
    db.log_access(user["username"], f"modifica prenotazione: {azione}", mid)
    return go(request, "/calendario", flash=f"Fatto: {msg}")


# ------------------------------------------------------------------ posti liberati (solo accettazione)

@app.post("/posti/disdetta-prova")
def posti_disdetta_prova(request: Request, csrf_token: str = Form("")):
    """Demo: arriva una disdetta dal CUP. La proposta nasce subito; la chiamata parte solo con il via libera."""
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "segreteria")
    try:
        with db.tx() as con:
            msg = posti.disdetta_prova(con, user)
    except ValueError as e:
        return go(request, "/posti", error=str(e))
    db.log_action(user["username"], None, f"disdetta di prova: {msg}")
    return go(request, "/cruscotto", flash=f"Disdetta di prova: {msg}.")


@app.get("/posti", response_class=HTMLResponse)
def posti_liberati(request: Request):
    user = current_user(request)
    need(user, "segreteria")
    with db.tx() as con:
        rows = posti.quadro(con)
    db.log_access(user["username"], "apre posti liberati")
    return render(request, "posti.html", user, rows=rows, STATO=posti.STATO, max_chiamate=posti.MAX_CHIAMATE)


@app.post("/posti/{oid}/{azione}")
def posti_azione(request: Request, oid: str, azione: str, csrf_token: str = Form(""), scelta: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "segreteria")
    try:
        if azione == "esito":
            msg = posti.esito_a_mano(oid, scelta, user)
        elif azione == "riprova":
            msg = posti.riprova(oid, user)
        elif azione == "libera":
            msg = posti.libera(oid, user)
        else:
            raise HTTPException(404)
    except ValueError as e:
        return go(request, "/posti", error=str(e))
    db.log_access(user["username"], f"posti liberati: {azione}", oid)
    return go(request, "/posti", flash=f"Fatto: {msg}")


# ------------------------------------------------------------------ chiamate dell'agente vocale

def _call_for(user, cid):
    with db.tx() as con:
        c = voce_ch.carica(con, cid)
    if not c:
        raise HTTPException(404, "Chiamata non trovata")
    if c["role"] != user["role"]:
        db.log_access(user["username"], "TENTATIVO NEGATO su chiamata di altro ruolo", cid)
        raise HTTPException(403, "Questa chiamata non è di tua competenza.")
    return c


@app.post("/chiamate/nuova")
def chiamata_nuova(request: Request, csrf_token: str = Form(""), kind: str = Form(...), ref_id: str = Form(""), patient_id: str = Form(...)):
    check_csrf(request, csrf_token)
    user = current_user(request)
    if kind not in voce_ch.KINDS:
        raise HTTPException(400, "Tipo di chiamata non valido")
    need(user, voce_ch.KINDS[kind]["role"])
    if kind in ("appuntamento", "modifica"):  # ogni chiamata per un posto o una modifica nasce dall'approvazione del suo caso
        raise HTTPException(400, "Queste chiamate si preparano approvando la proposta nel cruscotto.")
    with db.tx() as con:  # il riferimento deve essere di quel paziente
        ok = {"trasporto": "SELECT 1 FROM transport_requests WHERE id=? AND patient_id=? AND status IN ('attesa_paziente','richiamare')",
              "appuntamento": "SELECT 1 FROM appointments WHERE id=? AND patient_id=? AND status='da confermare'",
              "controllo": "SELECT 1 FROM cases WHERE id=? AND patient_id=? AND role='medico'"}[kind]
        if ref_id and not con.execute(ok, (ref_id, patient_id)).fetchone():
            raise HTTPException(400, "Riferimento non valido per questo paziente o già chiuso")
        if kind != "controllo" and not ref_id:
            raise HTTPException(400, "Manca il riferimento")
        if not con.execute("SELECT 1 FROM patients WHERE id=?", (patient_id,)).fetchone():
            raise HTTPException(400, "Paziente non valido")
        cid = voce_ch.crea(con, patient_id, kind, ref_id or None, user)
    db.log_access(user["username"], "prepara chiamata vocale", f"{cid} ({kind})")
    return RedirectResponse(f"/chiamate/{cid}", status_code=303)


def voce_allineata() -> str | None:
    """None se il cervello della voce (porta VOCE_PORT, dietro il tunnel) legge lo stesso database di Filo; altrimenti il problema."""
    import hashlib
    import httpx
    from .config import DB_PATH, VOCE_PORT
    try:
        d = httpx.get(f"http://127.0.0.1:{VOCE_PORT}/salute", timeout=1.5).json()
    except Exception:  # noqa: BLE001
        return f"Il cervello della voce non risponde sulla porta {VOCE_PORT}: la chiamata si chiuderà dopo la prima frase."
    if d.get("db") and d["db"] != hashlib.sha256(str(DB_PATH.resolve()).encode()).hexdigest()[:12]:
        return (f"Il cervello della voce sulla porta {VOCE_PORT} legge un altro database (un'altra copia di Filo): "
                "non trova questa chiamata e la chiude alla prima risposta. Riavvialo da questa cartella.")
    return None


@app.get("/chiamate/in-arrivo")
def chiamate_in_arrivo(request: Request):
    """Chiamate che l'agente ha preparato da solo e che nessuno ha ancora avviato: squillano nell'accettazione."""
    user = current_user(request)
    if user["role"] != "segreteria":
        return {"chiamate": []}
    with db.tx() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT c.id, p.name AS patient FROM calls c JOIN patients p ON p.id=c.patient_id "
            "WHERE c.role='segreteria' AND c.status='pronta' AND c.channel IS NULL AND c.ended_at IS NULL "
            "AND c.created_by LIKE 'agente%' ORDER BY c.created_at")]
    return {"chiamate": rows}


@app.get("/chiamate", response_class=HTMLResponse)
def chiamate(request: Request):
    user = current_user(request)
    with db.tx() as con:
        rows = [dict(r) for r in con.execute("SELECT c.*, p.name AS patient FROM calls c JOIN patients p ON p.id=c.patient_id "
                                             "WHERE c.role=? ORDER BY c.created_at DESC", (user["role"],))]
        da_proporre = [dict(r) for r in con.execute(  # posti con una chiamata approvata che aspetta di essere fatta o di avere una risposta
            "SELECT o.*, p.name AS patient, s.service, s.at FROM slot_offers o JOIN patients p ON p.id=o.patient_id "
            "JOIN freed_slots s ON s.id=o.slot_id WHERE o.status='in_chiamata' ORDER BY s.at")] if user["role"] == "segreteria" else []
        da_modificare = [dict(r) for r in con.execute(
            "SELECT m.*, p.name AS patient, a.service, a.at FROM cal_changes m JOIN patients p ON p.id=m.patient_id "
            "JOIN appointments a ON a.id=m.appointment_id WHERE m.status='in_chiamata' ORDER BY a.at")] if user["role"] == "segreteria" else []
        da_chiamare = [dict(r) for r in con.execute("SELECT t.*, p.name AS patient, a.at FROM transport_requests t JOIN patients p ON p.id=t.patient_id "
                                                    "JOIN appointments a ON a.id=t.appointment_id WHERE t.status IN ('attesa_paziente','richiamare')")] if user["role"] == "segreteria" else []
    for r in rows:
        r["label"] = voce_ch.KINDS[r["kind"]]["label"]
        r["st"] = voce_ch.STATUS.get(r["status"], r["status"])
    return render(request, "chiamate.html", user, rows=rows, da_proporre=da_proporre, da_chiamare=da_chiamare, da_modificare=da_modificare,
                  eleven=voce_el.configurato(), tel=voce_el.telefono_configurato())


@app.get("/chiamate/{cid}", response_class=HTMLResponse)
def chiamata(request: Request, cid: str):
    user = current_user(request)
    c = _call_for(user, cid)
    db.log_access(user["username"], "apre chiamata vocale", f"{cid} ({c['patient']['name']})")
    _segui(request, c["patient_id"], c["patient"]["name"])
    return render(request, "chiamata.html", user, c=c, label=voce_ch.KINDS[c["kind"]]["label"],
                  eleven=voce_el.configurato(), tel=voce_el.telefono_configurato(),
                  voce_ko=voce_allineata() if voce_el.configurato() and not c["ended_at"] else None)


@app.get("/chiamate/{cid}/eventi")
def chiamata_eventi(request: Request, cid: str):
    user = current_user(request)
    c = _call_for(user, cid)
    with db.tx() as con:
        ev = [dict(e) for e in con.execute("SELECT ts, kind, text, clinical FROM call_events WHERE call_id=? ORDER BY rowid", (cid,))]
        turns = [dict(t) for t in con.execute("SELECT ts, speaker, text FROM call_turns WHERE call_id=? ORDER BY rowid", (cid,))]
    if user["role"] != "medico":  # all'accettazione niente contenuto clinico
        ev = [e if not e["clinical"] else {**e, "text": "Segnalazione clinica passata al medico."} for e in ev]
        if c["alarm"]:
            turns = []
    closing = any(e["kind"] == "chiusura" for e in ev)
    return {"status": voce_ch.STATUS.get(c["status"], c["status"]), "ended": bool(c["ended_at"]), "closing": closing, "events": ev, "turns": turns}


@app.post("/chiamate/{cid}/avvio")
def chiamata_avvio(request: Request, cid: str, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    c = _call_for(user, cid)
    if c["ended_at"]:
        raise HTTPException(400, "Chiamata già conclusa")
    try:
        url = voce_el.signed_url()
    except Exception as e:
        raise HTTPException(502, f"ElevenLabs non raggiungibile: {str(e)[:120]}")
    dati = voce_ch.dati_per_elevenlabs(c)
    with db.tx() as con:
        con.execute("UPDATE calls SET channel='browser' WHERE id=?", (cid,))
        voce_ch.evento(con, cid, "avvio", f"Chiamata avviata dal browser da {user['name']}.")
    posti.nota_chiamata(cid, "chiamata avviata", f"Chiamata {cid} avviata dal browser da {user['name']}.")
    return {"signedUrl": url, "dynamicVariables": dati["dynamic_variables"], "customLlmExtraBody": dati["custom_llm_extra_body"]}


@app.post("/chiamate/{cid}/telefono")
def chiamata_telefono(request: Request, cid: str, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    c = _call_for(user, cid)
    try:
        conv = voce_el.chiama_telefono(voce_ch.dati_per_elevenlabs(c))
    except Exception as e:
        with db.tx() as con:
            voce_ch.evento(con, cid, "errore", f"La chiamata al telefono non è partita: {str(e)[:150]}")
        posti.nota_chiamata(cid, "errore chiamata", f"La chiamata {cid} al telefono non è partita ({str(e)[:120]}): il posto resta tenuto, si può riprovare.")
        return go(request, f"/chiamate/{cid}", error=str(e)[:150])
    with db.tx() as con:
        con.execute("UPDATE calls SET channel='telefono', conversation_id=? WHERE id=?", (conv, cid))
        voce_ch.evento(con, cid, "avvio", f"Chiamata al telefono di prova avviata da {user['name']}.")
    posti.nota_chiamata(cid, "chiamata avviata", f"Chiamata {cid} al telefono di prova avviata da {user['name']}.")
    return RedirectResponse(f"/chiamate/{cid}", status_code=303)


@app.post("/chiamate/{cid}/fine")
def chiamata_fine(request: Request, cid: str, csrf_token: str = Form(""), conversation_id: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    _call_for(user, cid)
    done = voce_ch.concludi(cid, conversation_id[:80] or None)
    db.log_access(user["username"], "chiude chiamata vocale", cid)
    return {"ok": True, "fatto": done}


@app.post("/chiamate/{cid}/testo")
async def chiamata_testo(request: Request, cid: str, csrf_token: str = Form(""), frase: str = Form(...)):
    """Simulatore a testo: stesso cervello della voce, senza ElevenLabs. Per provare e per chi non ha il microfono."""
    import httpx
    check_csrf(request, csrf_token)
    user = current_user(request)
    c = _call_for(user, cid)
    if c["ended_at"]:
        raise HTTPException(400, "Chiamata già conclusa")
    with db.tx() as con:
        storia = [{"role": "user" if t["speaker"] == "persona" else "assistant", "content": t["text"]}
                  for t in con.execute("SELECT speaker, text FROM call_turns WHERE call_id=? ORDER BY rowid", (cid,))]
        if not storia:
            prima = voce_el.PRIMA_FRASE.replace("{{saluto_nome}}", voce_ch.dati_per_elevenlabs(c)["dynamic_variables"]["saluto_nome"])
            voce_ch.turno(cid, "agente", prima)
            storia = [{"role": "assistant", "content": prima}]
            con.execute("UPDATE calls SET channel='testo' WHERE id=?", (cid,))
    storia.append({"role": "user", "content": frase.strip()[:500]})
    end_tool = [{"type": "function", "function": {"name": "end_call", "description": "Chiude la chiamata dopo i saluti.",
                                                  "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}}}}]
    text, chiusa = "", False
    async with httpx.AsyncClient(timeout=60) as cl:
        async with cl.stream("POST", f"http://127.0.0.1:{VOCE_PORT}/v1/chat/completions",
                             headers={"Authorization": f"Bearer {voce_token()}"},
                             json={"model": "filo", "stream": True, "messages": storia, "tools": end_tool,
                                   "elevenlabs_extra_body": {"call_id": cid}}) as r:
            if r.status_code != 200:
                raise HTTPException(502, "Il servizio della voce non risponde: è acceso? (porta %d)" % VOCE_PORT)
            async for line in r.aiter_lines():
                if not line.startswith("data: {"):
                    continue
                d = json.loads(line[6:])["choices"][0]["delta"]
                text += d.get("content") or ""
                chiusa = chiusa or any(t["function"]["name"] == "end_call" for t in d.get("tool_calls") or [])
    if chiusa:
        voce_ch.concludi(cid)
    return {"agente": text.replace("... ", "").strip(), "chiusa": chiusa}


# ------------------------------------------------------------------ visita col kit (in ambulatorio)

def _visita_per(user, vid, con):
    need(user, "medico")
    v = kit_visita.carica(con, vid)
    if not v:
        raise HTTPException(404, "Visita non trovata")
    return v


@app.post("/kit/{booking_id}/visita")
def kit_visita_apri(request: Request, booking_id: str, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    need(user, "medico")
    with db.tx() as con:
        try:
            vid = kit_visita.apri(con, booking_id, user)
        except ValueError as e:
            return go(request, "/cruscotto", error=str(e))
    return go(request, f"/kit/visita/{vid}")


@app.get("/kit/visita/{vid}", response_class=HTMLResponse)
def kit_visita_pagina(request: Request, vid: str):
    user = current_user(request)
    with db.tx() as con:
        v = _visita_per(user, vid, con)
        segs = kit_visita.segmenti(con, vid)
    db.log_access(user["username"], "apre visita col kit", f"{v['patient']} {vid}")
    _segui(request, v["patient_id"], v["patient"])
    schermo = str(request.base_url).rstrip("/") + f"/kit/schermo/{v['token']}" if v["token"] else None
    return render(request, "kit_visita.html", user, v=v, segs=segs, schermo=schermo, backend=kit_visita.backend(),
                  backend_label=kit_visita.BACKEND_LABEL.get(kit_visita.backend() or "", ""))


@app.post("/kit/visita/{vid}/audio")
async def kit_visita_audio(request: Request, vid: str, audio: UploadFile = File(...), seq: int = Form(...), csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    with db.tx() as con:
        v = _visita_per(user, vid, con)
    if v["status"] != "in_corso":
        raise HTTPException(409, "Visita conclusa")
    dati = await audio.read(kit_visita.MAX_AUDIO + 1)
    if len(dati) > kit_visita.MAX_AUDIO:
        raise HTTPException(413, "Pezzo di audio troppo lungo")
    import anyio
    try:
        testo = await anyio.to_thread.run_sync(kit_visita.trascrivi, dati, audio.content_type or "audio/webm")
    except kit_visita.NonConfigurata as e:
        return JSONResponse({"errore": str(e)}, status_code=503)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"errore": f"trascrizione non riuscita: {str(e)[:120]}"}, status_code=502)
    with db.tx() as con:
        kit_visita.aggiungi(con, vid, seq, testo)
    return {"seq": seq, "testo": testo}


def _testo_json(con, vid, dopo):
    return {"segmenti": kit_visita.segmenti(con, vid, dopo)}


@app.get("/kit/visita/{vid}/testo")
def kit_visita_testo(request: Request, vid: str, dopo: int = -1):
    user = current_user(request)
    with db.tx() as con:
        v = _visita_per(user, vid, con)
        return {**_testo_json(con, vid, dopo), "stato": v["status"]}


@app.post("/kit/visita/{vid}/chiudi")
def kit_visita_chiudi(request: Request, vid: str, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    with db.tx() as con:
        _visita_per(user, vid, con)
        nid = kit_visita.chiudi(con, vid, user)
    return go(request, f"/kit/visita/{vid}", flash=f"Visita conclusa: la trascrizione è nella cartella ({nid}).")


@app.post("/kit/visita/{vid}/invia")
def kit_visita_invia(request: Request, vid: str, email: str = Form(""), salva: str = Form(""), csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    user = current_user(request)
    with db.tx() as con:
        _visita_per(user, vid, con)
        try:
            esito = kit_visita.invia(con, vid, email, bool(salva), user)
        except ValueError as e:
            return go(request, f"/kit/visita/{vid}", error=str(e))
    return go(request, f"/kit/visita/{vid}", flash=f"Trascrizione per la persona: {esito}.")


# lo schermo del tablet: niente accesso, entra con il codice della visita e solo mentre la visita è in corso
@app.get("/kit/schermo/{token}", response_class=HTMLResponse)
def kit_schermo(request: Request, token: str):
    with db.tx() as con:
        v = kit_visita.per_token(con, token)
    if not v:
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>Filo</title><p style='font:28px sans-serif;padding:40px'>Visita conclusa.</p>", status_code=404)
    return tpl.TemplateResponse(request, "kit_schermo.html", {"token": token, "V": tpl.env.globals["V"]})


@app.get("/kit/schermo/{token}/testo")
def kit_schermo_testo(token: str, dopo: int = -1):
    with db.tx() as con:
        v = kit_visita.per_token(con, token)
        if not v:
            raise HTTPException(404, "Visita conclusa")
        return _testo_json(con, v["id"], dopo)


@app.post("/segui/tutti")
def segui_tutti(request: Request, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    current_user(request)
    request.session.pop("segui", None)
    return go(request, "/cruscotto")


@app.get("/persona/{pid}", response_class=HTMLResponse)
@app.get("/persona/{pid}/{sezione}", response_class=HTMLResponse)
def persona_pagina(request: Request, pid: str, sezione: str = "panoramica"):
    """La scheda di una persona: le sezioni del menu, ma solo sue."""
    from . import persona
    user = current_user(request)
    ammesse = dict(persona.SEZIONI)
    if sezione not in ammesse:
        raise HTTPException(404, "Sezione non trovata")
    if user["role"] == "medico" and sezione in ("trasporti", "posti"):
        raise HTTPException(403, "Sezione dell'accettazione.")
    with db.tx() as con:
        q = persona.quadro(con, pid, user["role"], sezione)
    if not q:
        raise HTTPException(404, "Paziente non trovato")
    _segui(request, pid, q["p"]["name"])
    db.log_access(user["username"], f"apre scheda persona ({sezione})", q["p"]["name"])
    sezioni = [s_ for s_ in persona.SEZIONI if user["role"] == "segreteria" or s_[0] not in ("trasporti", "posti")]
    return render(request, "persona.html", user, q=q, p=q["p"], sezione=sezione, sezioni=sezioni, age=_age(q["p"]),
                  STATUS_TR=trasporti.STATUS)
