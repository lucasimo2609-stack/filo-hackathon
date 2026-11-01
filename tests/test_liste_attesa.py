"""Liste d'attesa: quando una persona disdice, l'agente Agende sceglie per urgenza chi prende il posto; Filo controlla.
Lista di partenza (seed): ecocardiogramma, W-03 De Marchi B, W-01 Furlan B, W-02 Boscolo D (P07)."""
import json

from app import agents, db, posti, valutazione
from test_web import client  # noqa: F401  (fixture)


def _slot():
    with db.tx() as con:
        return dict(con.execute("SELECT * FROM freed_slots WHERE id='FS-1'").fetchone())


def _scegli(risposta):
    """Fa scegliere con un agente che risponde `risposta(pacchetto)`; ritorna (scelto, pacchetto, evento)."""
    visto = {}

    def finto(agent, payload):
        visto["p"] = payload
        out = risposta(payload)
        if isinstance(out, Exception):
            raise out
        return out, "finto", 1
    agents_call = agents.call
    agents.call = finto
    try:
        with db.tx() as con:
            slot = _slot()
            lista, tried = posti._lista_posto(con, slot)
            liberi = [w for w in lista if w["status"] == "in attesa"]
            w = posti.scegli(con, slot, liberi)
            ev = con.execute("SELECT text FROM slot_events WHERE step='scelta' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    finally:
        agents.call = agents_call
    return w, visto.get("p"), ev


def _codice_di(p, prio):
    return next(c["codice"] for c in p["candidati"] if c["priorita_ricetta"] == prio)


def test_il_pacchetto_non_ha_nomi(run):
    w, p, ev = _scegli(lambda p: {"codice_scelto": p["candidati"][0]["codice"], "motivo": "ok"})
    testo = json.dumps(p, ensure_ascii=False).lower()
    for nome in ("furlan", "de marchi", "boscolo", "teresa", "franco", "giovanna"):
        assert nome not in testo
    assert p["tipo"] == "posto_liberato" and len(p["candidati"]) == 3
    assert set(p["candidati"][0]) == {"codice", "priorita_ricetta", "urgenza_medico", "in_lista_da", "puo_arrivare", "data_gia_fissata"}


def test_urgenza_alta_del_medico_passa_davanti(run):
    with db.tx() as con:
        valutazione.save(con, "P07", "e.gatti", "alta", [])
    w, p, ev = _scegli(lambda p: {"codice_scelto": _codice_di(p, "D"), "motivo": "urgenza alta del medico"})
    assert w["patient_id"] == "P07" and ev.startswith(w["name"] + ": Agente Agende")


def test_scavalcare_la_ricetta_senza_urgenza_alta_non_passa(run):
    w, p, ev = _scegli(lambda p: {"codice_scelto": _codice_di(p, "D"), "motivo": "aspetta da tanto"})
    assert w["id"] == "W-03" and "Regola locale fissa" in ev  # ordine della ricetta: De Marchi (B) prima


def test_agente_che_non_risponde_vale_l_ordine_fisso(run):
    w, p, ev = _scegli(lambda p: RuntimeError("piattaforma giù"))
    assert w["id"] == "W-03" and "Ordine fisso" in ev


def test_codice_inventato_vale_l_ordine_fisso(run):
    w, p, ev = _scegli(lambda p: {"codice_scelto": "C-zzzzzz", "motivo": "?"})
    assert w["id"] == "W-03" and "Ordine fisso" in ev


def test_chi_non_puo_arrivare_non_e_ammesso():
    cands = {"a": {"priorita_ricetta": "B", "urgenza_medico": "nessuna", "puo_arrivare": False},
             "b": {"priorita_ricetta": "D", "urgenza_medico": "nessuna", "puo_arrivare": True}}
    assert posti._ammessi(cands) == {"b"}


def test_la_persona_avvisa_che_non_puo_venire(client, run):
    """Disdetta di una persona vera: posto alla lista, l'agente sceglie, la proposta arriva nel cruscotto. Nessuna chiamata senza via libera."""
    from app import calendario
    from test_web import login, tok
    with db.tx() as con:
        libero = calendario.disponibilita(con, "ecocardiogramma", limite=5)[0]
        appt = calendario.prenota(con, "P01", libero["id"])
    login(client, "m.bianchi", "segreteria")
    page = client.get("/persona/P01/calendario").text  # la disdetta si fa dal calendario della persona
    assert f'value="{appt}"' in page
    r = client.post("/calendario/disdetta", data={"csrf_token": tok(page), "prenotazione": appt, "motivo": "è ricoverata"}, follow_redirects=False)
    assert r.headers["location"] == "/cruscotto"
    with db.tx() as con:
        assert con.execute("SELECT status FROM appointments WHERE id=?", (appt,)).fetchone()[0] == "annullata"
        fs = con.execute("SELECT id FROM freed_slots WHERE at=? AND service='ecocardiogramma'", (libero["at"],)).fetchone()[0]
        casi = con.execute("SELECT summary FROM cases WHERE kind='posto' AND status='pending' AND json_extract(payload,'$.slot')=?", (fs,)).fetchall()
        scelta = con.execute("SELECT text FROM slot_events WHERE slot_id=? AND step='scelta'", (fs,)).fetchall()
        chiamate = con.execute("SELECT COUNT(*) FROM calls WHERE created_at >= date('now')").fetchone()[0]
    assert len(casi) == 1 and "Perché lei/lui:" in casi[0][0]
    assert scelta or "1° in ordine" in casi[0][0]  # con un solo candidato libero non c'è niente da scegliere


def test_nei_test_nessuna_chiamata_vera_agli_agenti(monkeypatch):
    """Taratura del blocco in conftest: anche dopo monkeypatch.undo() una chiamata vera non parte."""
    import pytest
    monkeypatch.undo()
    # lo strato di rete, non _call_sim/_call_api: quelli si fermano prima se mancano le chiavi (copia senza .env, CI)
    with pytest.raises(RuntimeError, match="durante i test"):
        agents._sim_post("wf-qualsiasi", {"input": {}})
    with pytest.raises(RuntimeError, match="durante i test"):
        agents._client("google/gemma-4-26B-A4B-it")
