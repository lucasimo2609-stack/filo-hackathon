"""Il cancello: tutto ciò che separa l'ospedale dal cloud.

Passi di un'elaborazione (run):
 1. estrazione locale dei segnali (lista bianca)
 2. costruzione dei pacchetti PER COMPITO: ogni agente riceve solo i campi che gli servono
 3. controllo di riconoscibilità (k persone uguali e l condizioni diverse nella popolazione) e generalizzazione;
    se non basta, il pacchetto NON parte e decide il controllo locale
 4. codici usa e getta + tabella delle chiavi (resta qui)
 5. verifica della cassaforte (attestazione) prima dell'invio: se non passa, non parte niente
 6. invio a gruppi mescolati, FUORI da ogni transazione del database; un pacchetto già mandato oggi con lo stesso
    contenuto non si rimanda (si riusa la risposta)
 7. ricomposizione: codici -> persone, controllo fisso di ogni risposta, regole locali di riserva
 8. casi per ruolo (accettazione / medico) con punteggio calcolato in locale; un caso già deciso non rinasce,
    un caso in attesa si aggiorna senza cambiare identificativo

Nessun caso sparisce in silenzio: se l'agente va in errore, risponde fuori formato o non c'è un servizio in regola,
nasce un caso "da controllare a mano" per il ruolo giusto.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import secrets
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from . import agents, calendario, db, extract, posti, trasporti, valutazione
from .config import (AGENTS, ATTESTATION_MODE, ATTESTATION_REPORT, BATCH_SIZE, EU_MODELS, FIXED_IDS_FOR_TEST, FW_BASE_URL, K_MIN, L_MIN,
                     SIM_BASE_URL, TODAY)

CONDITION_CATEGORY = {
    "fibrillazione atriale": "malattia cardiovascolare", "scompenso cardiaco": "malattia cardiovascolare",
    "amiloidosi cardiaca": "malattia cardiovascolare", "ipertensione": "malattia cardiovascolare",
    "diabete": "malattia metabolica", "ipotiroidismo": "malattia metabolica",
    "BPCO": "malattia respiratoria", "artrosi": "malattia osteoarticolare",
}
# Combinazioni che vanno SEMPRE al medico, qualunque cosa risponda l'agente (regola locale fissa).
DANGEROUS = [({"anticoagulante", "antinfiammatorio"}, "anticoagulante insieme ad antinfiammatorio: rischio di sanguinamento"),
             ({"antinfiammatorio", "cortisonico"}, "antinfiammatorio insieme a cortisonico: rischio di lesioni allo stomaco")]
# Terapie la cui sospensione va sempre segnalata al medico dopo pochi giorni.
CRITICAL_THERAPY = {"anticoagulante", "antidiabetico", "antipertensivo"}
DRUG_WINDOW_DAYS = 180  # un farmaco citato solo in documenti più vecchi non conta più
PRIORITY_ORDER = {"U": 0, "B": 1, "D": 2, "P": 3}

_run_lock = threading.Lock()


def _today() -> date:
    return date.fromisoformat(TODAY)


def _days(dt: str) -> int:
    return (datetime.fromisoformat(dt).date() - _today()).days


def age_band(age: int, width: int = 10) -> str:
    if width >= 20:
        return "80+" if age >= 80 else "60-79" if age >= 60 else "under 60"
    lo = (age // 10) * 10
    return "90+" if lo >= 90 else f"{lo}-{lo + 9}"


def servizi_possibili(con, band: str, distretto: str | None, alone: bool, nocar: bool, visit_at: str) -> list[dict]:
    """Servizi di trasporto in regola per questo pacchetto, filtrati in ospedale (dati pubblici, nessun dato della persona).

    Il filtro usa SOLO quello che il pacchetto dice già: distretto inviato (se è stato tolto per l'anonimato valgono
    solo i servizi che coprono tutto il territorio), fascia d'età, vive solo, senza auto e giorno della visita (che il
    cloud ricava comunque da giorni_alla_visita). Quindi la lista non aggiunge nulla di riconoscibile, ma porta
    all'agente il preavviso in giorni lavorativi di STACCO e la regola "solo persone fragili", che dai campi da soli
    non poteva calcolare. L'elenco dei servizi non vive più anche nel prompt della piattaforma."""
    lo = int(re.match(r"\d+", band).group()) if re.match(r"\d+", band) else 0
    who = {"distretto": distretto, "age": lo, "alone": alone, "nocar": nocar}
    return [{"id": s["id"], "nome": s["name"], "regole": s["rules"], "costo": s["cost"]}
            for s in trasporti.services_for(con, who, visit_at)]


def _age_match(age: int, band: str) -> bool:
    if band.endswith("+"):
        return age >= int(band[:-1])
    if band == "under 60":
        return age < 60
    lo, hi = band.split("-")
    return int(lo) <= age <= int(hi)


def riassunto_calendario(con, pid: str, service: str) -> dict:
    """Quello che l'agente agende nel cloud sa del calendario, letto adesso: se la persona ha già l'esame prenotato
    (a fasce, come la visita nell'aderenza: un numero esatto di giorni collegherebbe questo pacchetto ad altri) e quanti
    posti liberi ci sono per quell'esame nei prossimi 30 giorni. Niente date, orari, luoghi, nomi o codici delle prenotazioni."""
    gia = calendario.prenotazioni_paziente(con, pid, service)
    prenot = "nessuna"
    if gia:
        d = _days(gia[0]["at"])
        prenot = "già passata" if d < 0 else "entro 7 giorni" if d <= 7 else "entro 30 giorni" if d <= 30 else "oltre 30 giorni"
    n = len(calendario.disponibilita(con, service, al=(_today() + timedelta(days=30)).isoformat() + " 23:59", limite=100))
    return {"prenotazione_esistente": prenot, "posti_liberi_30_giorni": "nessuno" if n == 0 else "pochi" if n <= 5 else "molti"}


def visit_bucket(days: int | None) -> str:
    if days is None:
        return "nessuna"
    return "entro 2 giorni" if days <= 2 else "entro 7 giorni" if days <= 7 else "entro 30 giorni" if days <= 30 else "oltre 30 giorni"


# ------------------------------------------------------------------ 1. estrazione

def extract_all(con, run_id: str) -> dict:
    con.execute("DELETE FROM signals")
    use_llm = extract._llm_available()  # una prova sola per elaborazione, non una per nota
    used = set()
    for note in con.execute("SELECT * FROM notes ORDER BY date"):
        sigs, engine = extract.extract(note, use_llm=use_llm)
        used.add(engine)
        for s in sigs:
            con.execute("INSERT INTO signals VALUES (?,?,?,?,?,?)", (note["patient_id"], s["field"], s["value"], note["id"], engine, s.get("snippet")))
    return {"engines": sorted(used) or ["regole"]}


def profile(con, pid: str) -> dict:
    p = dict(con.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone())
    dates = {n["id"]: n["date"] or "" for n in con.execute("SELECT id, date FROM notes WHERE patient_id=?", (pid,))}
    sig = defaultdict(list)
    for s in con.execute("SELECT * FROM signals WHERE patient_id=?", (pid,)):
        sig[s["field"]].append((s["value"], s["evidence_id"]))
    # farmaci: solo quelli citati negli ultimi mesi e non sospesi dopo da un medico
    lim = (_today() - timedelta(days=DRUG_WINDOW_DAYS)).isoformat()
    stop = {}
    for v, e in sig.get("sospeso_dal_medico", []):
        stop[v] = max(stop.get(v, ""), dates.get(e, ""))
    sig["farmaco"] = [(v, e) for v, e in sig.get("farmaco", [])
                      if dates.get(e, "") >= lim and not (v.split("|")[0] in stop and stop[v.split("|")[0]] >= dates.get(e, ""))]
    attive = {(v, e) for v, e in sig["farmaco"]}
    vive = {(v.split("|")[0], e) for v, e in attive}
    sig["principio_attivo"] = [(v, e) for v, e in sig.get("principio_attivo", []) if (v.split("|")[0], e) in vive]  # stessi limiti dei farmaci
    p["age"] = _today().year - p["birth_year"]
    p["sig"] = sig
    p["alone"] = bool(p["lives_alone"]) or bool(sig.get("vive_solo"))
    p["nocar"] = p["has_car"] == 0 or bool(sig.get("senza_auto"))
    p["car_unknown"] = p["has_car"] is None and not sig.get("senza_auto")
    p["appointments"] = [dict(a) for a in con.execute(
        "SELECT * FROM appointments WHERE patient_id=? AND status='prenotata' ORDER BY at", (pid,))]
    p["valutazione"] = valutazione.latest(con, pid)  # urgenza decisa dal medico a fine visita (resta in ospedale)
    return p


# ------------------------------------------------------------------ 3. riconoscibilità

def group_stats(con, band: str, distretto: str | None, alone: bool | None, nocar: bool | None) -> tuple[int, int]:
    """k = persone del distretto con la stessa combinazione; l = condizioni diverse tra loro (l-diversità)."""
    n, conds = 0, set()
    for r in con.execute("SELECT age, distretto, condition, lives_alone, has_car FROM residents"):
        if not _age_match(r["age"], band):
            continue
        if distretto and r["distretto"] != distretto:
            continue
        if alone is not None and bool(r["lives_alone"]) != alone:
            continue
        if nocar is not None and (not r["has_car"]) != nocar:
            continue
        n += 1
        conds.add(r["condition"])
    return n, len(conds)


def count_residents(con, band, distretto, alone, nocar) -> int:
    return group_stats(con, band, distretto, alone, nocar)[0]


def condition_count(con, condition: str) -> int:
    return con.execute("SELECT COUNT(*) FROM residents WHERE condition=?", (condition,)).fetchone()[0]


# ------------------------------------------------------------------ 2. pacchetti per compito

def build_bundles(con, run_id: str) -> list[dict]:
    """Ogni bundle: {agent, pid, fields (solo lista bianca), evidence (resta in ospedale), local (non parte)}."""
    bundles = []
    for row in con.execute("SELECT id FROM patients ORDER BY id"):
        p = profile(con, row["id"])
        sig = p["sig"]

        # cure incrociate: farmaci da almeno due reparti, oppure una combinazione pericolosa anche dallo stesso reparto
        drugs = list(dict.fromkeys(sig.get("farmaco", [])))
        classes = {v.split("|")[0] for v, _ in drugs}
        danger = [why for combo, why in DANGEROUS if combo <= classes]
        if (len(classes) >= 2 and len({v.split("|")[1] for v, _ in drugs}) >= 2) or danger:
            by_class = defaultdict(list)
            for v, _ in drugs:
                by_class[v.split("|")[0]].append(v.split("|")[1])
            # il workflow Cure cerca le coppie per principio attivo in DDInter e DailyMed: senza il nome non può cercare nulla
            nomi = defaultdict(set)
            for v, _ in sig.get("principio_attivo", []):
                nomi[v.split("|")[0]].add(v.split("|")[1])
            farmaci = [{"classe": c, "principio_attivo": n, "prescritto_da": sorted(set(by_class[c]))}
                       for c in by_class for n in sorted(nomi.get(c, ()))]
            fields = {"tipo": "cure", "farmaci": farmaci}
            senza_nome = len({f["principio_attivo"] for f in farmaci}) < 2
            conds = list(dict.fromkeys(v for v, _ in sig.get("condizione", [])))
            if conds:  # tutte le condizioni, ognuna generalizzata se rara
                sent, ks, gens = [], [], []
                for c in conds:
                    k = condition_count(con, c)
                    ks.append(k)
                    if k >= K_MIN:
                        sent.append(c)
                    else:
                        sent.append(CONDITION_CATEGORY.get(c, "malattia cronica"))
                        gens.append(f"condizione '{c}' condivisa da {k} persone: inviata solo la categoria")
                fields["condizioni"] = list(dict.fromkeys(sent))
                con.execute("INSERT INTO gate_checks VALUES (?,?,?,?,?,?)", (run_id, p["id"], "cure", min(ks), None, "; ".join(gens) or None))
            if senza_nome:  # meno di due principi attivi noti: niente da cercare nelle fonti, decide il controllo locale
                con.execute("INSERT INTO gate_checks VALUES (?,?,?,?,?,?)", (run_id, p["id"], "cure", None, None,
                            "NON INVIATO: meno di due principi attivi riconosciuti nei documenti, decide il controllo locale in ospedale"))
            bundles.append({"agent": "cure", "pid": p["id"], "fields": fields, "danger": danger, "classes": sorted(classes), "local": senza_nome,
                            "evidence": list(dict.fromkeys([e for _, e in drugs] + [e for _, e in sig.get("condizione", [])]))})

        # aderenza (la visita va per fasce: lo stesso numero di giorni non deve comparire anche nel pacchetto trasporti)
        if sig.get("aderenza"):
            nxt = p["appointments"][0] if p["appointments"] else None
            fields = {"tipo": "aderenza",
                      "terapia": (sig.get("aderenza_terapia") or [("non indicata", None)])[0][0],
                      "giorni_sospensione": int((sig.get("aderenza_giorni") or [("0", None)])[0][0]),
                      "motivo": (sig.get("aderenza_motivo") or [("altro", None)])[0][0],
                      "visita": visit_bucket(_days(nxt["at"]) if nxt else None)}
            bundles.append({"agent": "aderenza", "pid": p["id"], "fields": fields,
                            "evidence": sorted({e for _, e in sig["aderenza"]})})

        # trasporti: OGNI visita entro 30 giorni di chi è senza auto o vive solo
        for a in p["appointments"]:
            d = _days(a["at"])
            needs = p["nocar"] or p["alone"] or (p["car_unknown"] and p["age"] >= 80)
            if not (0 <= d <= 30 and needs):
                continue
            strike, heat = trasporti.strike_at(con, a["at"]), trasporti.heat_at(con, a["at"])
            band = age_band(p["age"])
            distretto = p["distretto"] if p["distretto"] in trasporti.KNOWN_DISTRICTS else None
            alone, nocar = p["alone"], p["nocar"]
            k, l = group_stats(con, band, distretto, alone, nocar)
            steps = []
            if k < K_MIN or l < L_MIN:
                band = age_band(p["age"], 20); steps.append("fascia d'età allargata")
                k, l = group_stats(con, band, distretto, alone, nocar)
            if (k < K_MIN or l < L_MIN) and distretto:
                distretto = None; steps.append("distretto tolto")
                k, l = group_stats(con, band, None, alone, nocar)
            local = k < K_MIN or l < L_MIN
            if local:
                steps.append("NON INVIATO: troppo riconoscibile, decide il controllo locale in ospedale")
            con.execute("INSERT INTO gate_checks VALUES (?,?,?,?,?,?)", (run_id, p["id"], "trasporti", k, l, "; ".join(steps) or None))
            fields = {"tipo": "trasporto", "fascia_eta": band, "distretto": distretto or "non indicato",
                      "vive_solo": alone, "senza_auto": nocar, "giorni_alla_visita": d,
                      "sciopero_quel_giorno": bool(strike), "caldo_quel_giorno": bool(heat),
                      "servizi_possibili": servizi_possibili(con, band, distretto, alone, nocar, a["at"])}
            bundles.append({"agent": "trasporti", "pid": p["id"], "fields": fields, "appointment": a["id"], "local": local,
                            "evidence": [a["id"]] + [e for _, e in sig.get("vive_solo", [])] + [e for _, e in sig.get("senza_auto", [])]})

        # richieste doppie
        by_service = defaultdict(list)
        for v, e in sig.get("richiesta", []):
            by_service[v.split("|")[0]].append((v.split("|")[1], e))
        for service, items in by_service.items():
            if len({s for s, _ in items}) >= 2:
                dates = sorted(r[0] for r in con.execute(f"SELECT date FROM notes WHERE id IN ({','.join('?' * len(items))})",
                                                         [e for _, e in items]).fetchall() if r[0])
                interval = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days if len(dates) >= 2 else None
                bundles.append({"agent": "agende", "pid": p["id"], "service": service,
                                "fields": {"tipo": "richieste_doppie", "esame": service, "richiesto_da": [s for s, _ in items], "intervallo_giorni": interval,
                                           "calendario": riassunto_calendario(con, p["id"], service)},
                                "evidence": [e for _, e in items]})

    # posti liberati: non partono più pacchetti. L'ordine (priorità della ricetta, poi cognome e nome) si decide in
    # ospedale (app/posti.py): con un ordine fisso l'agente non avrebbe niente da scegliere, e i cognomi non escono.
    return bundles


# ------------------------------------------------------------------ 4. codici usa e getta

_fixed_ids: dict[str, str] = {}


def new_code(pid: str | None, agent: str = "") -> str:
    if FIXED_IDS_FOR_TEST and pid:  # codice "da anagrafica": sempre uguale per la stessa persona
        return _fixed_ids.setdefault(pid, f"P-{len(_fixed_ids) + 1:02d}") + f"/{agent}"
    return "F-" + secrets.token_hex(3)


def _content_hash(agent: str, fields: dict) -> str:
    return hashlib.sha256((agent + json.dumps(fields, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()


def seal(con, run_id: str, bundles: list[dict]) -> list[dict]:
    """Assegna i codici, scrive la tabella delle chiavi, restituisce i pacchetti (quelli "local" non escono)."""
    out = []
    for b in bundles:
        fields = json.loads(json.dumps(b["fields"]))
        h = None if fields.get("tipo") == "posto_liberato" else _content_hash(b["agent"], fields)
        if fields.get("tipo") == "posto_liberato":  # anche ogni candidato riceve un codice suo
            for c in fields["candidati"]:
                cc = new_code(c["pid"], "cand")
                con.execute("INSERT INTO vault VALUES (?,?,?,?,?,?,?)",
                            (cc, run_id, c["pid"], "agende_candidato", json.dumps({"waitlist": c["waitlist"]}), json.dumps([c["waitlist"]]), db.now()))
                for key in ("pid", "waitlist"):
                    c.pop(key)
                c["codice"] = cc
        code = new_code(b["pid"], b["agent"])
        extra = {k: b[k] for k in ("slot", "appointment") if k in b}
        con.execute("INSERT INTO vault VALUES (?,?,?,?,?,?,?)",
                    (code, run_id, b["pid"], b["agent"], json.dumps(extra), json.dumps(b["evidence"]), db.now()))
        out.append({"code": code, "agent": b["agent"], "payload": {"codice": code, **fields}, "bundle": b, "hash": h})
    return out


# ------------------------------------------------------------------ 5. cassaforte

def _host_ok(url: str) -> bool:
    host = urlparse(url or "").hostname or ""
    return host.endswith("ai-factory.fastweb.it") or host.endswith("fastwebai-agents.it")


def attest() -> dict:
    """Prima di ogni invio. Blocca se un agente usa un modello fuori dall'elenco UE, se gli indirizzi non sono Fastweb,
    o (modalità "report") se manca il report o la sua impronta non torna. La verifica della firma del report della
    AI Factory è ancora da collegare: oggi si confronta solo l'impronta con quella attesa."""
    if ATTESTATION_MODE == "off":
        return {"ok": True, "mode": "disattivata", "detail": "verifica disattivata da configurazione"}
    problems = []
    for name, a in AGENTS.items():
        if a["model"] not in EU_MODELS:
            problems.append(f"l'agente {name} usa {a['model']}, che non è tra i modelli in area UE")
        if a["backend"] == "sim" and not _host_ok(SIM_BASE_URL):
            problems.append(f"la piattaforma configurata ({SIM_BASE_URL}) non è quella Fastweb")
    if not _host_ok(FW_BASE_URL):
        problems.append(f"l'indirizzo dei modelli ({FW_BASE_URL}) non è la AI Factory Fastweb")
    models = sorted({a["model"] for a in AGENTS.values()})
    measurement = hashlib.sha256(("|".join(models) + "|filo-agents-v1").encode()).hexdigest()[:16]
    mode = "simulata"
    if ATTESTATION_MODE == "report":
        mode = "report"
        try:
            with open(ATTESTATION_REPORT or "", encoding="utf-8") as f:
                rep = json.load(f)
        except (OSError, ValueError):
            rep = None
        if not rep:
            problems.append("report di attestazione mancante o illeggibile")
        elif rep.get("measurement") != measurement:
            problems.append("l'impronta del report non corrisponde ai modelli configurati")
    if problems:
        return {"ok": False, "mode": mode, "detail": "Invio bloccato: " + "; ".join(problems)}
    detail = (f"Prova di attestazione SIMULATA: modelli {', '.join(models)} in area UE, indirizzi Fastweb, impronta {measurement}. "
              "In produzione: verifica del report firmato fornito dalla AI Factory.") if mode == "simulata" else \
        f"Report di attestazione presente, impronta {measurement} corrispondente."
    return {"ok": True, "mode": mode, "detail": detail}


# ------------------------------------------------------------------ 6. invio a gruppi mescolati

def _safe_call(pk):
    try:
        return agents.call(pk["agent"], pk["payload"])
    except Exception as e:
        return {"errore": str(e)[:200]}, "errore", 0


def dispatch(run_id: str, packets: list[dict]) -> list[dict]:
    """Nessuna transazione aperta durante le chiamate: il database resta libero per login, registro e approvazioni."""
    rnd = random.SystemRandom()
    rnd.shuffle(packets)
    results = []
    for bi in range(0, len(packets), BATCH_SIZE):
        batch = packets[bi:bi + BATCH_SIZE]
        with db.tx() as con:
            for seq, pk in enumerate(batch):
                con.execute("INSERT INTO packets_out VALUES (?,?,?,?,?,?,?,?)",
                            (run_id, bi // BATCH_SIZE, seq, pk["code"], pk["agent"], json.dumps(pk["payload"], ensure_ascii=False), db.now(),
                             AGENTS[pk["agent"]]["backend"]))
        with ThreadPoolExecutor(max_workers=len(batch)) as ex:
            outs = list(ex.map(_safe_call, batch))
        with db.tx() as con:
            for pk, (out, backend, ms) in zip(batch, outs):
                con.execute("UPDATE packets_out SET backend=? WHERE run_id=? AND code=?", (backend, run_id, pk["code"]))  # il canale usato davvero
                con.execute("INSERT INTO results_in VALUES (?,?,?,?,?,?)",
                            (run_id, pk["code"], pk["agent"], json.dumps({"risultato": out, "backend": backend}, ensure_ascii=False), db.now(), ms))
                results.append({**pk, "out": out, "backend": backend})
    return results


# ------------------------------------------------------------------ 7-8. ricomposizione e casi

def score(p: dict, urg: int, grav: int, feas: int, medico: bool = False) -> tuple[int, list]:
    """medico=True: caso clinico, che prende anche la valutazione del medico a fine visita (l'accettazione non la vede)."""
    frag = 0
    frag += 12 if p["age"] >= 90 else 9 if p["age"] >= 80 else 6 if p["age"] >= 70 else 2
    frag += 8 if p["alone"] else 0
    frag += 6 if p["nocar"] else 0
    frag += 4 if p["hearing_support"] else 0
    frag = min(frag, 30)
    parts = [["Urgenza", urg], ["Fragilità", frag], ["Gravità", grav], ["Fattibilità", feas]]
    if medico:
        parts = valutazione.apply_to_parts(parts, p.get("valutazione"))
    return min(100, sum(x[1] for x in parts)), parts


def _urg(days: int | None) -> int:
    if days is None:
        return 10
    return 30 if days <= 1 else 22 if days <= 7 else 14 if days <= 30 else 8


def _bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "sì", "si", "vero"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("false", "no", "falso"):
        return False
    raise ValueError(f"atteso vero/falso, arrivato {v!r}")


def _level(v):
    if isinstance(v, bool):
        raise ValueError(f"livello non numerico: {v!r}")
    n = int(str(v).strip())
    if n not in (1, 2, 3):
        raise ValueError(f"livello fuori scala: {v!r}")
    return n


def _text(v, n=300):
    v = re.sub(r"\s+", " ", str(v or "")).strip()
    return v[:n]


FONTI_CURE = {"cure_interazioni_validate": "DDInter", "cure_dailymed_2026": "DailyMed"}


def check_output(agent: str, out, sent: dict) -> tuple[dict | None, str | None]:
    """Controllo deterministico delle risposte: forma, valori ammessi, codici. Ritorna (risposta pulita, motivo dello scarto)."""
    if not isinstance(out, dict) or "errore" in out:
        return None, "nessuna risposta valida" + (f": {out['errore']}" if isinstance(out, dict) and out.get("errore") else "")
    try:
        if agent == "cure":
            fonte = []
            for x in (out.get("fonte") or [])[:10] if isinstance(out.get("fonte"), list) else []:
                if isinstance(x, dict) and x.get("knowledge_base") in FONTI_CURE and isinstance(x.get("testo"), str):
                    fonte.append({"knowledge_base": x["knowledge_base"], "testo": x["testo"][:300],
                                  "livello": _text(x.get("livello"))[:20] if x.get("livello") else None,
                                  "effetto": _text(x.get("effetto"))[:120] if x.get("effetto") else None})
            return {"da_verificare": _bool(out["da_verificare"]), "gravita": _level(out.get("gravita", 2)),
                    "spiegazione": _text(out.get("spiegazione")), "fonte": fonte}, None
        if agent == "aderenza":
            return {"rilevante": _bool(out["rilevante"]), "urgenza": _level(out.get("urgenza", 2)),
                    "spiegazione": _text(out.get("spiegazione"))}, None
        if agent == "trasporti":
            sid = out.get("servizio")
            sid = sid.strip() if isinstance(sid, str) and sid.strip().lower() not in ("", "null", "none", "nessuno") else None
            return {"serve_trasporto": _bool(out["serve_trasporto"]), "servizio": sid,
                    "partenza": trasporti.norm_time(out.get("partenza")), "motivo": _text(out.get("motivo"))}, None
        if agent == "agende" and sent.get("tipo") == "richieste_doppie":
            return {"doppione": _bool(out["doppione"]), "spiegazione": _text(out.get("spiegazione"))}, None
        if agent == "agende" and sent.get("tipo") == "posto_liberato":
            code = out.get("codice_scelto")
            if code not in {c["codice"] for c in sent.get("candidati", [])}:
                return None, f"codice scelto non tra i candidati di questo posto: {code!r}"
            return {"codice_scelto": code, "motivo": _text(out.get("motivo"))}, None
    except (KeyError, ValueError, TypeError) as e:
        return None, f"risposta fuori formato ({e})"
    return None, "tipo di pacchetto sconosciuto"


def _pickup_ok(pickup: str | None, at: str) -> str | None:
    """La partenza proposta dall'agente si usa solo se è tra 3 ore e 20 minuti prima della visita."""
    if not pickup:
        return None
    visit = datetime.fromisoformat(at)
    t = visit.replace(hour=int(pickup[:2]), minute=int(pickup[3:]))
    return pickup if visit - timedelta(hours=3) <= t <= visit - timedelta(minutes=20) else None


def _local_transport_need(p, d, strike) -> bool:
    """Regola locale fissa: chi è senza auto e vive solo (o ha almeno 80 anni) con una visita vicina o durante uno sciopero ha
    bisogno del trasporto, qualunque cosa dica l'agente."""
    return (p["nocar"] or p["car_unknown"]) and (p["alone"] or p["age"] >= 80) and (d <= 2 or bool(strike))


def recompose(con, run_id: str, results: list[dict]) -> tuple[list[dict], dict]:
    notes = {n["id"]: dict(n) for n in con.execute("SELECT * FROM notes")}
    appts = {a["id"]: dict(a) for a in con.execute("SELECT * FROM appointments")}
    vault = {r["code"]: dict(r) for r in con.execute("SELECT * FROM vault WHERE run_id=?", (run_id,))}
    cases, stats = [], {"scartate": 0, "a_mano": 0, "regola_locale": 0}

    def ev(ids):
        outl = []
        for i in ids:
            if i in notes:
                n = notes[i]; outl.append([i, f"{n['kind'].replace('_', ' ')} · {n['source']} · {n['date']}", n["text"]])
            elif i in appts:
                a = appts[i]; outl.append([i, "Agenda CUP", f"{a['service']} il {a['at']}, {a['place']}"])
        return outl

    for r in results:
        b = r["bundle"]
        out, why = check_output(r["agent"], r["out"], r["payload"])
        if not r.get("local") and not r.get("cached"):
            con.execute("UPDATE results_in SET output=json_set(output, '$.controllo', ?) WHERE run_id=? AND code=?",
                        ("superato" if out else f"scartata: {why}", run_id, r["code"]))
        if out is None and not r.get("local"):
            stats["scartate"] += 1
        p = profile(con, b["pid"]) if b["pid"] else None
        evid = b["evidence"]
        manual = f"L'agente non ha dato una risposta valida ({why}): il caso resta qui da controllare a mano." if out is None else None

        if r["agent"] == "cure":
            override = None
            if out is not None and not out["da_verificare"] and b["danger"]:
                override = f"Regola locale fissa: {'; '.join(b['danger'])}. L'agente aveva risposto che non serviva una verifica."
            if out is None or out["da_verificare"] or override:
                g = 3 if (override or (out is None and b["danger"])) else (out["gravita"] if out else 2)
                stats["a_mano"] += out is None
                stats["regola_locale"] += bool(override)
                s, parts = score(p, _urg(None) + 10, 8 * g, 14, medico=True)
                summary = out["spiegazione"] if out and out["da_verificare"] else "; ".join(b["danger"]) or "Farmaci prescritti da reparti diversi."
                fonti = [[FONTI_CURE[x["knowledge_base"]], f"Fonte citata dall'agente · {FONTI_CURE[x['knowledge_base']]}"
                          + (f" · livello {x['livello']}" if x.get("livello") else ""), x["testo"]] for x in (out or {}).get("fonte", [])]
                locale = "Pacchetto non inviato (meno di due principi attivi riconosciuti): ha deciso il controllo locale in ospedale." if r.get("local") else ""
                cases.append(dict(role="medico", kind="cure", patient=p, score=s, parts=parts, conf="bassa" if out is None else "media",
                                  tag=("Cure che si incrociano" if r.get("local") and b["danger"] else "Da controllare a mano") if out is None else "Cure che si incrociano",
                                  summary=summary,
                                  action=f"Pagina per il medico di famiglia ({p['mmg']}) con i referti {', '.join(evid)} affiancati, da verificare."
                                         + (f" {locale}" if locale else (f" {manual}" if manual else "")) + (f" {override}" if override else ""),
                                  evidence=ev(evid) + fonti, ckey=f"cure|{p['id']}|{'+'.join(b['classes'])}",
                                  payload={"code": r["code"], "agent_out": out, "gravita": g}))

        elif r["agent"] == "aderenza":
            f = b["fields"]
            critical = (f["terapia"] in CRITICAL_THERAPY and f["giorni_sospensione"] >= 3) or f["giorni_sospensione"] >= 14
            override = None
            if out is not None and not out["rilevante"] and critical:
                override = (f"Regola locale fissa: {f['terapia'].replace('_', ' ')} sospeso da {f['giorni_sospensione']} giorni. "
                            "L'agente aveva risposto che non era rilevante.")
            if out is None or out["rilevante"] or override:
                g = out["urgenza"] if out and out["rilevante"] else 2
                stats["a_mano"] += out is None
                stats["regola_locale"] += bool(override)
                nxt = p["appointments"][0] if p["appointments"] else None
                s, parts = score(p, _urg(_days(nxt["at"]) if nxt else None), 8 * g, 14, medico=True)
                cases.append(dict(role="medico", kind="aderenza", patient=p, score=s, parts=parts, conf="bassa" if out is None else "alta",
                                  tag="Da controllare a mano" if out is None else "Terapia sospesa",
                                  summary=out["spiegazione"] if out and out["rilevante"] else
                                  f"{f['terapia'].replace('_', ' ').capitalize()} sospeso da {f['giorni_sospensione']} giorni ({f['motivo'].replace('_', ' ')}).",
                                  action=f"Rivedere la terapia e avvisare il medico curante ({p['mmg']}) e l'infermiere di famiglia."
                                         + (f" {manual}" if manual else "") + (f" {override}" if override else ""),
                                  evidence=ev(evid + ([nxt["id"]] if nxt else [])), ckey=f"aderenza|{p['id']}|{f['terapia']}|{'+'.join(evid)}",
                                  payload={"code": r["code"], "agent_out": out}))

        elif r["agent"] == "trasporti":
            a = appts[b["appointment"]]
            d = _days(a["at"])
            strike = trasporti.strike_at(con, a["at"])
            notes_ = []
            if r.get("local"):
                notes_.append("Pacchetto non inviato (troppo riconoscibile): ha deciso il controllo locale in ospedale.")
                needed = _local_transport_need(p, d, strike) or p["nocar"]
                out = None
            elif out is None:
                notes_.append(manual)
                needed = _local_transport_need(p, d, strike) or p["nocar"]
                stats["a_mano"] += 1
            elif not out["serve_trasporto"] and _local_transport_need(p, d, strike):
                notes_.append("Regola locale fissa: senza auto, vive solo o ha almeno 80 anni, e la visita è vicina o durante lo sciopero. "
                              "L'agente aveva risposto che il trasporto non serviva.")
                needed = True
                stats["regola_locale"] += 1
            else:
                needed = out["serve_trasporto"]
            if not needed:
                continue
            sid = out.get("servizio") if out else None
            valid = trasporti.services_for(con, p, a["at"])
            svc = next((x for x in valid if x["id"] == sid), None)
            if not svc:
                svc = valid[0] if valid else None
                if svc and out and sid:
                    notes_.append(f"Il servizio proposto dal cloud ({sid}) non rispettava distretto, preavviso o regole: scelto {svc['name']}.")
            pickup = _pickup_ok(out.get("partenza") if out else None, a["at"])
            s, parts = score(p, _urg(d), 14 if strike else 8, 15 if svc else 6)
            when = "domani" if d == 1 else "oggi" if d == 0 else f"il {a['at'][:10]}"
            summary = f"{a['service'].capitalize()} {when} alle {a['at'][11:]}" + (" con sciopero dei mezzi in quell'orario." if strike else ".")
            summary += " " + ("Vive solo" if p["alone"] else "Non vive solo") + (", senza auto." if p["nocar"] else ", auto non registrata." if p["car_unknown"] else ".")
            if svc:
                action = (f"Con il via libera Filo chiede da solo la disponibilità a {svc['name']} ({svc['response']}), "
                          "poi l'agente chiama la persona per sapere se il trasporto serve davvero e lo conferma."
                          + (f" Partenza suggerita dall'agente: {pickup}." if pickup else ""))
                tag = "Visita a rischio" if strike or d <= 1 else "Trasporto da organizzare"
            else:
                action = ("Nessun servizio rispetta distretto, preavviso e regole per questa visita: va organizzato a mano "
                          "(famiglia, volontari, taxi sociale) o va spostata la visita.")
                tag = "Trasporto da organizzare a mano"
            cases.append(dict(role="segreteria", kind="trasporto", patient=p, score=s, parts=parts,
                              conf="bassa" if not out or not svc else "media" if notes_ else "alta", tag=tag, summary=summary,
                              action=action + "".join(f" Nota del controllo locale: {n}" for n in notes_ if n),
                              # all'accettazione niente testo delle note: solo agenda e una riga sociale senza contenuto clinico
                              evidence=ev([a["id"]]) + [["Scheda sociale", "Situazione registrata",
                                                         ("Vive solo" if p["alone"] else "Non vive solo") + (" · senza auto" if p["nocar"] else " · con auto")]],
                              ckey=f"trasporto|{p['id']}|{a['id']}",
                              payload={"code": r["code"], "service": svc["id"] if svc else None, "appointment": a["id"], "pickup": pickup,
                                       "agent_out": out}))

        elif r["agent"] == "agende" and b["fields"]["tipo"] == "richieste_doppie":
            # il calendario si legge adesso, in ospedale: se l'esame è già prenotato il doppione diventa più che probabile
            gia = calendario.prenotazioni_paziente(con, p["id"], b["service"])
            if out is not None and not out["doppione"] and gia:
                out = {**out, "doppione": True, "spiegazione": out["spiegazione"]}
                notes_cal = (f"Regola locale fissa: nel calendario l'esame è già prenotato il {gia[0]['at']} ({gia[0]['status']}). "
                             "L'agente aveva risposto che non era un doppione.")
                stats["regola_locale"] += 1
            else:
                notes_cal = f"Nel calendario l'esame è già prenotato il {gia[0]['at']} ({gia[0]['status']})." if gia else \
                    "Nel calendario non c'è ancora nessuna prenotazione per questo esame."
            if out is None or out["doppione"]:
                stats["a_mano"] += out is None
                s, parts = score(p, 10, 16, 22, medico=True)
                cases.append(dict(role="medico", kind="doppione", patient=p, score=s, parts=parts, conf="bassa" if out is None else "alta",
                                  tag="Da controllare a mano" if out is None else "Esame doppio",
                                  summary=out["spiegazione"] if out else f"Stesso esame ({b['service'].replace('_', ' ')}) chiesto da reparti diversi.",
                                  action="Segnalare al secondo reparto che l'esame è già stato richiesto, prima della prenotazione. " + notes_cal
                                         + (f" {manual}" if manual else ""),
                                  evidence=ev(evid) + [[g["id"], "Calendario", f"{g['service']} {g['at']}, {g['place']} ({g['status']})"] for g in gia], ckey=f"doppione|{p['id']}|{b['service']}", payload={"code": r["code"], "agent_out": out}))


    # posti liberati: proposta al primo in ordine, una per posto e una per persona (decisione solo locale)
    cases += posti.avanza(con, salva=False, run_id=run_id)

    # visita accessibile: decisione solo locale, non serve il cloud. Un kit per ogni visita, non uno per documento.
    for row in con.execute("SELECT patient_id, MIN(evidence_id) AS evidence_id FROM signals "
                           "WHERE field='udito' AND value='supporto_richiesto' GROUP BY patient_id").fetchall():
        p = profile(con, row["patient_id"])
        for a in p["appointments"]:
            if not 0 <= _days(a["at"]) <= 30:
                continue
            s, parts = score(p, _urg(_days(a["at"])), 6, 16)
            cases.append(dict(role="segreteria", kind="kit", patient=p, score=s, parts=parts, conf="alta",
                              tag="Visita accessibile",
                              summary=f"Ha chiesto il supporto per l'udito al CUP. {a['service'].capitalize()} il {a['at'][:10]} alle {a['at'][11:]}.",
                              action="Con il via libera Filo riserva uno dei kit liberi per l'orario della visita (tablet con sottotitoli, cuffie, microfono a clip), "
                                     "lo chiede al coordinamento infermieristico degli ambulatori e lascia la nota al medico sull'appuntamento.",
                              evidence=ev([row["evidence_id"], a["id"]]), ckey=f"kit|{p['id']}|{a['id']}",
                              payload={"appointment": a["id"], "local_only": True}))

    # nota per il medico: la parte logistica la segue l'accettazione. All'accettazione non si dice chi ha un segnale clinico.
    roles_by_patient = defaultdict(set)
    for c in cases:
        roles_by_patient[c["patient"]["id"]].add(c["role"])
    for c in cases:
        both = len(roles_by_patient[c["patient"]["id"]]) > 1
        c["role_note"] = "La parte logistica (trasporto, agenda) la sta gestendo l'accettazione." if both and c["role"] == "medico" else None
    return cases, stats


def save_cases(con, run_id: str, cases: list[dict]) -> dict:
    """Un caso è identificato dalla sua chiave (tipo, persona, visita o farmaci...).
    - già deciso (fatto, scartato): non rinasce;
    - in attesa: si aggiorna, con lo stesso identificativo (chi lo sta leggendo non lo perde);
    - in attesa ma non più prodotto: "superato", non cancellato.
    I casi nati dalle chiamate dell'agente vocale (run_id 'voce') e quelli aperti dalla procedura dei posti liberati tra
    un'elaborazione e l'altra (run_id 'posti': errori da controllare a mano) non si toccano."""
    existing = {r["ckey"]: dict(r) for r in con.execute("SELECT * FROM cases WHERE ckey IS NOT NULL ORDER BY decided_at")}
    stats = {"nuovi": 0, "aggiornati": 0, "gia_decisi": 0, "superati": 0}
    keys = set()
    for i, c in enumerate(sorted(cases, key=lambda c: -c["score"])):
        if c["ckey"] in keys:
            continue
        keys.add(c["ckey"])
        vals = (run_id, c["score"], json.dumps(c["parts"]), c["conf"], c["tag"], c["summary"], c["action"],
                json.dumps(c["evidence"], ensure_ascii=False), c["role_note"], json.dumps(c["payload"], ensure_ascii=False))
        old = existing.get(c["ckey"])
        if old and old["status"] != "pending":
            stats["gia_decisi"] += 1
        elif old:
            con.execute("UPDATE cases SET run_id=?, score=?, parts=?, confidence=?, tag=?, summary=?, action=?, evidence=?, role_note=?, payload=? "
                        "WHERE id=? AND status='pending'", (*vals, old["id"]))
            stats["aggiornati"] += 1
        else:
            con.execute("INSERT INTO cases (id,run_id,patient_id,role,kind,score,parts,confidence,tag,summary,action,evidence,role_note,payload,ckey) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"{run_id}-{i:02d}", run_id, c["patient"]["id"], c["role"], c["kind"], *vals[1:], c["ckey"]))
            stats["nuovi"] += 1
    for r in con.execute("SELECT id, ckey FROM cases WHERE status='pending' AND run_id NOT IN ('voce', 'posti', 'calendario')").fetchall():
        if r["ckey"] not in keys:
            con.execute("UPDATE cases SET status='superato', decided_by=?, decided_at=? WHERE id=? AND status='pending'",
                        (f"elaborazione {run_id}", db.now(), r["id"]))
            stats["superati"] += 1
    return stats


def run(username: str) -> dict:
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError("C'è già un'elaborazione in corso: riprova tra poco.")
    run_id = "R" + datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    try:
        return _run(run_id, username)
    except Exception as e:
        with db.tx() as con:  # mai un'elaborazione lasciata a metà senza fine
            con.execute("UPDATE runs SET finished_at=?, summary=? WHERE id=?",
                        (db.now(), json.dumps({"errore": f"{type(e).__name__}: {e}"[:300]}, ensure_ascii=False), run_id))
        raise
    finally:
        _run_lock.release()


def _run(run_id: str, username: str) -> dict:
    with db.tx() as con:  # la riga dell'elaborazione si salva subito: anche se dopo qualcosa si rompe, resta traccia e ha una fine
        con.execute("INSERT INTO runs (id, started_at, started_by) VALUES (?,?,?)", (run_id, db.now(), username))
    with db.tx() as con:
        trasporti.scadenze(con)  # visite spostate, servizi che non hanno risposto, tentativi da rifare
        info = extract_all(con, run_id)
        bundles = build_bundles(con, run_id)
        packets = seal(con, run_id, bundles)
        local = [{**pk, "out": None, "backend": "controllo locale (non inviato)", "local": True} for pk in packets if pk["bundle"].get("local")]
        packets = [pk for pk in packets if not pk["bundle"].get("local")]
        cached, to_send = [], []
        for pk in packets:  # stesso contenuto già mandato oggi: non si rimanda, si riusa la risposta
            row = con.execute("SELECT output, backend FROM answers WHERE hash=? AND day=?", (pk["hash"], TODAY)).fetchone() \
                if pk["hash"] and not FIXED_IDS_FOR_TEST else None
            if row:
                cached.append({**pk, "out": json.loads(row["output"]), "backend": "risposta già avuta oggi, pacchetto non rimandato", "cached": True})
            else:
                to_send.append(pk)
    att = attest()
    if not att["ok"]:
        with db.tx() as con:
            con.execute("UPDATE runs SET finished_at=?, summary=? WHERE id=?",
                        (db.now(), json.dumps({"bloccato": att["detail"], "attestazione": att}, ensure_ascii=False), run_id))
        return {"run_id": run_id, "blocked": True, "attestazione": att}
    results = dispatch(run_id, to_send)
    with db.tx() as con:
        for r in results:
            if r["hash"] and check_output(r["agent"], r["out"], r["payload"])[0] is not None:
                con.execute("INSERT OR REPLACE INTO answers VALUES (?,?,?,?,?)",
                            (r["hash"], r["agent"], TODAY, json.dumps(r["out"], ensure_ascii=False), r["backend"]))
        cases, st = recompose(con, run_id, results + cached + local)
        saved = save_cases(con, run_id, cases)
        summary = {"estrazione": info["engines"], "pacchetti": len(results), "risultati": len(results),
                   "errori": sum(1 for r in results if r["backend"] == "errore"), "scartate": st["scartate"],
                   "a_mano": st["a_mano"], "regola_locale": st["regola_locale"], "non_rimandati": len(cached), "decisi_in_ospedale": len(local),
                   "casi": {"segreteria": sum(c["role"] == "segreteria" for c in cases), "medico": sum(c["role"] == "medico" for c in cases)},
                   "salvataggio": saved, "attestazione": att}
        con.execute("UPDATE runs SET finished_at=?, summary=? WHERE id=?", (db.now(), json.dumps(summary, ensure_ascii=False), run_id))
    return {"run_id": run_id, **summary}
