"""L'attaccante nel cloud vede tutti i pacchetti di più giorni. Con codici fissi li collega; con codici usa e getta no."""
import json

from app import db, gateway


def _codes_by_run():
    with db.tx() as con:
        rows = con.execute("SELECT run_id, code FROM packets_out").fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["run_id"], set()).add(r["code"])
    return list(out.values())


def test_codici_fissi_si_collegano(monkeypatch):
    monkeypatch.setattr(gateway, "FIXED_IDS_FOR_TEST", True)
    gateway._fixed_ids.clear()
    gateway.run("t"); gateway.run("t")
    a, b = _codes_by_run()
    assert len(a & b) >= 5  # l'attaccante ricostruisce la storia di quasi tutti
    gateway._fixed_ids.clear()


def test_codici_usa_e_getta_non_si_collegano():
    gateway.run("t")
    with db.tx() as con:  # il giorno dopo: le risposte di ieri non valgono, si rimanda tutto
        con.execute("DELETE FROM answers")
    gateway.run("t")
    a, b = _codes_by_run()
    assert not (a & b)


def test_nello_stesso_giro_una_persona_ha_codici_diversi_per_agente(run):
    with db.tx() as con:
        rows = con.execute("SELECT patient_id, code FROM vault WHERE patient_id IS NOT NULL").fetchall()
    per = {}
    for r in rows:
        per.setdefault(r["patient_id"], []).append(r["code"])
    multi = [c for c in per.values() if len(c) > 1]
    assert multi and all(len(set(c)) == len(c) for c in multi)
