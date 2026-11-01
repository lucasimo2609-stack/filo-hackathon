"""La scheda di una persona: le stesse sezioni del menu (trasporti, calendario, posti liberati, chiamate, posta), ma solo sue.

Il cruscotto e le pagine del menu guardano tutti; dentro la scheda si guarda una persona sola, così chi lavora su Bruno
non si ritrova davanti la prenotazione di qualcun altro. L'accettazione vede la parte logistica, il medico anche la cartella.
"""
from __future__ import annotations

import json

from . import calendario, trasporti
from .config import TODAY

SEZIONI = [("panoramica", "Panoramica"), ("trasporti", "Trasporti"), ("calendario", "Calendario"), ("posti", "Posti liberati"),
           ("chiamate", "Chiamate"), ("posta", "Posta in uscita")]


def _riferimenti(con, pid: str) -> set[str]:
    """Tutti gli identificativi che riguardano la persona: pratiche, trasporti, visite col kit, modifiche, posti."""
    refs = {r[0] for r in con.execute("SELECT id FROM cases WHERE patient_id=?", (pid,))}
    refs |= {r[0] for r in con.execute("SELECT id FROM transport_requests WHERE patient_id=?", (pid,))}
    refs |= {r[0] for r in con.execute("SELECT id FROM kit_visite WHERE patient_id=?", (pid,))}
    refs |= {r[0] for r in con.execute("SELECT id FROM cal_changes WHERE patient_id=?", (pid,))}
    refs |= {r[0] for r in con.execute("SELECT case_id FROM slot_offers WHERE patient_id=? AND case_id IS NOT NULL", (pid,))}
    return refs


def kit_info(con, pid: str) -> dict:
    """Se la persona ha bisogno del kit per l'udito, da dove lo sappiamo e a che punto è: sempre in vista nella sua scheda."""
    p = con.execute("SELECT hearing_support FROM patients WHERE id=?", (pid,)).fetchone()
    fonte = con.execute("SELECT s.snippet, n.title, n.date FROM signals s LEFT JOIN notes n ON n.id=s.evidence_id "
                        "WHERE s.patient_id=? AND s.field='udito' AND s.value='supporto_richiesto' LIMIT 1", (pid,)).fetchone()
    serve = bool((p and p["hearing_support"]) or fonte)
    prenotazioni = [dict(r) for r in con.execute(
        "SELECT b.*, k.label, a.service FROM kit_bookings b LEFT JOIN kits k ON k.id=b.kit_id LEFT JOIN appointments a ON a.id=b.appointment_id "
        "WHERE b.patient_id=? ORDER BY b.at DESC", (pid,))]
    visite = [dict(r) for r in con.execute("SELECT * FROM kit_visite WHERE patient_id=? ORDER BY started_at DESC", (pid,))]
    caso = con.execute("SELECT id FROM cases WHERE patient_id=? AND kind='kit' AND status='pending' LIMIT 1", (pid,)).fetchone()
    riservato = next((b for b in prenotazioni if b["status"] == "riservato"), None)
    if visite and visite[0]["status"] == "chiusa":
        stato = "trascrizione mandata a " + visite[0]["sent_to"] if visite[0]["sent_to"] else "visita fatta, trascrizione nella cartella"
    elif visite:
        stato = "visita col kit in corso"
    elif riservato:
        stato = f"{riservato['label']} riservato per la visita del {riservato['at'][8:10]}/{riservato['at'][5:7]} alle {riservato['at'][11:16]}"
    elif caso:
        stato = "da riservare: serve il via libera"
    else:
        from datetime import date, timedelta
        entro = (date.fromisoformat(TODAY) + timedelta(days=30)).isoformat()
        v = con.execute("SELECT service, at FROM appointments WHERE patient_id=? AND status='prenotata' AND at>=? AND at<=? ORDER BY at LIMIT 1",
                        (pid, TODAY, entro + " 23:59")).fetchone()
        stato = (f"non ancora riservato per la {v['service']} del {v['at'][8:10]}/{v['at'][5:7]}: la pratica arriva con Elabora le novità"
                 if v else "nessuna visita nei prossimi 30 giorni") if serve else ""
    return {"serve": serve, "fonte": dict(fonte) if fonte else None, "da_anagrafica": bool(p and p["hearing_support"]),
            "prenotazioni": prenotazioni, "visite": visite, "caso": caso[0] if caso else None, "riservato": riservato, "stato": stato,
            "per_visita": {b["appointment_id"]: b for b in prenotazioni}}


def quadro(con, pid: str, ruolo: str, sezione: str) -> dict | None:
    p = con.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    if not p:
        return None
    p = dict(p)
    q: dict = {"p": p, "sezione": sezione}
    visite = [dict(r) for r in con.execute("SELECT * FROM appointments WHERE patient_id=? ORDER BY at", (pid,))]
    q["prossima"] = next((v for v in visite if v["status"] == "prenotata" and v["at"] >= TODAY), None)
    q["aperti"] = [dict(r) for r in con.execute(
        "SELECT * FROM cases WHERE patient_id=? AND role=? AND status='pending' ORDER BY score DESC", (pid, ruolo))]
    trs = [dict(r) for r in con.execute(
        "SELECT t.*, s.name AS service, a.at, a.service AS visit FROM transport_requests t JOIN transport_services s ON s.id=t.service_id "
        "JOIN appointments a ON a.id=t.appointment_id WHERE t.patient_id=? ORDER BY t.updated_at DESC", (pid,))]
    for t in trs:
        t["label"] = trasporti.STATUS.get(t["status"], t["status"])
    q["trasporti"] = trs
    q["conteggi"] = {"trasporti": len(trs), "calendario": sum(v["status"] == "prenotata" for v in visite)}
    q["kit_info"] = kit_info(con, pid)

    if sezione == "panoramica":
        q["decisi"] = [dict(r) for r in con.execute(
            "SELECT * FROM cases WHERE patient_id=? AND role=? AND status NOT IN ('pending','superato') ORDER BY decided_at DESC LIMIT 5", (pid, ruolo))]
        q["kit"] = [dict(r) for r in con.execute(
            "SELECT b.*, k.label FROM kit_bookings b LEFT JOIN kits k ON k.id=b.kit_id WHERE b.patient_id=? ORDER BY b.at DESC", (pid,))]
        refs = _riferimenti(con, pid)
        storia = [dict(r) for r in con.execute("SELECT ts, username AS who, text FROM action_log WHERE case_id IN (%s) ORDER BY ts DESC LIMIT 12"
                                               % ",".join("?" * len(refs)), tuple(refs))] if refs else []
        storia += [dict(ts=r["ts"], who=r["who"], text=f"Trasporto: {r['text']}") for r in con.execute(
            "SELECT e.* FROM transport_events e JOIN transport_requests t ON t.id=e.request_id WHERE t.patient_id=? ORDER BY e.ts DESC LIMIT 12", (pid,))]
        q["storia"] = sorted(storia, key=lambda x: x["ts"], reverse=True)[:12]
    elif sezione == "trasporti":
        for t in trs:
            t["eventi"] = [dict(r) for r in con.execute("SELECT * FROM transport_events WHERE request_id=? ORDER BY ts", (t["id"],))]
    elif sezione == "calendario":
        q["visite"] = visite
        q["modifiche"] = [dict(r) for r in con.execute("SELECT * FROM cal_changes WHERE patient_id=? ORDER BY created_at DESC", (pid,))]
        q["esami"] = list(calendario.AGENDE)
    elif sezione == "posti":
        q["attesa"] = [dict(r) for r in con.execute("SELECT * FROM waitlist WHERE patient_id=? ORDER BY since", (pid,))]
        q["proposte"] = [dict(r) for r in con.execute(
            "SELECT o.*, f.service, f.at, f.place FROM slot_offers o JOIN freed_slots f ON f.id=o.slot_id WHERE o.patient_id=? "
            "ORDER BY o.created_at DESC", (pid,))]
    elif sezione == "chiamate":
        q["chiamate"] = [dict(r) for r in con.execute("SELECT * FROM calls WHERE patient_id=? ORDER BY created_at DESC", (pid,))]
    elif sezione == "posta":
        refs = _riferimenti(con, pid)
        conds, args = [], []
        if refs:
            conds.append("case_id IN (%s)" % ",".join("?" * len(refs)))
            args += list(refs)
        if p.get("email"):
            conds.append("recipient LIKE ?")
            args.append(f"%{p['email']}%")
        q["posta"] = [dict(r) for r in con.execute(
            f"SELECT id, ts, recipient, subject, sent_to FROM outbox WHERE {' OR '.join(conds)} ORDER BY id DESC", args)] if conds else []
    return q


def per_pannello(con, pid: str, ruolo: str) -> dict | None:
    """Il riassunto che sta di lato nel cruscotto: la persona che si sta seguendo, a colpo d'occhio."""
    q = quadro(con, pid, ruolo, "pannello")
    if not q:
        return None
    ult = con.execute("SELECT e.ts, e.text FROM transport_events e JOIN transport_requests t ON t.id=e.request_id WHERE t.patient_id=? "
                      "ORDER BY e.ts DESC LIMIT 1", (pid,)).fetchone()
    q["ultimo"] = dict(ult) if ult else None
    q["payloads"] = {c["id"]: json.loads(c["payload"] or "{}") for c in q["aperti"]}
    return q
