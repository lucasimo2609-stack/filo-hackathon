"""Agente trasporti: l'elenco dei servizi viaggia nel pacchetto, già filtrato in ospedale.

Prima l'elenco completo stava nel prompt della piattaforma (un secondo posto da tenere allineato) e l'agente
riceveva solo i giorni di calendario: non poteva sapere se STACCO, che si prenota lun-ven, era ancora in tempo.
"""
import json

from app import agents, db, gateway


def _trasporti_out():
    with db.tx() as con:
        rows = [json.loads(r["payload"]) for r in con.execute("SELECT payload FROM packets_out")]
    return [p for p in rows if p.get("tipo") == "trasporto"]


def test_ogni_pacchetto_trasporti_porta_i_servizi_possibili(run):
    out = _trasporti_out()
    assert out, "nessun pacchetto trasporti uscito"
    for p in out:
        assert isinstance(p["servizi_possibili"], list)
        for s in p["servizi_possibili"]:
            assert set(s) == {"id", "nome", "regole", "costo"}  # solo dati pubblici del servizio


def test_stacco_fuori_se_mancano_tre_giorni_lavorativi(run):
    """Visita domani (oggi è giovedì 22/10): STACCO non è in tempo e non deve essere offerto all'agente."""
    for p in _trasporti_out():
        ids = [s["id"] for s in p["servizi_possibili"]]
        if p["giorni_alla_visita"] <= 2:
            assert "T1" not in ids, p
        if p["giorni_alla_visita"] >= 7:
            assert "T1" in ids, p


def test_distretto_sud_o_tolto_esclude_il_servizio_solo_nord(run):
    for p in _trasporti_out():
        if p["distretto"] != "Nord":
            assert "T3" not in [s["id"] for s in p["servizi_possibili"]], p


def test_filtro_usa_solo_il_distretto_inviato():
    """Se il cancello toglie il distretto per l'anonimato, la lista non deve rivelarlo: T3 (solo Nord) sparisce."""
    with db.tx() as con:
        nord = [s["id"] for s in gateway.servizi_possibili(con, "80-89", "Nord", True, True, "2026-11-20 10:00")]
        tolto = [s["id"] for s in gateway.servizi_possibili(con, "80-89", None, True, True, "2026-11-20 10:00")]
    assert "T3" in nord and "T3" not in tolto


def test_prompt_senza_elenco_dei_servizi():
    p = agents.PROMPTS["trasporti"]
    assert "{servizi}" not in p and "STACCO" not in p and "servizi_possibili" in p


def test_servizio_null_come_testo_vale_nessuno():
    t, err = gateway.check_output("trasporti", {"serve_trasporto": True, "servizio": "null", "partenza": None, "motivo": "x"}, {})
    assert err is None and t["servizio"] is None


def test_agente_senza_servizio_non_lascia_nota_strana(monkeypatch):
    """L'agente dice 'serve, servizio null': l'ospedale sceglie il primo valido senza scrivere 'proposto (None)'."""
    def call(agent, payload):
        if agent == "trasporti":
            return {"serve_trasporto": True, "servizio": None, "partenza": None, "motivo": "test"}, "finto", 1
        from conftest import fake_call
        return fake_call(agent, payload)
    monkeypatch.setattr(agents, "call", call)
    gateway.run("test")
    with db.tx() as con:
        acts = [r["action"] for r in con.execute("SELECT action FROM cases WHERE kind='trasporto'")]
    assert acts and not any("proposto dal cloud (None)" in a for a in acts)
