"""Cure: Filo manda il principio attivo (il workflow cerca le coppie in DDInter e DailyMed per nome) e tiene le fonti citate."""
import json

from app import db, gateway


def _bundles():
    with db.tx() as con:
        return [b for b in gateway.build_bundles(con, "t") if b["agent"] == "cure"]


def test_ogni_farmaco_inviato_ha_il_principio_attivo(run):
    inviati = [b for b in _bundles() if not b.get("local")]
    assert inviati, "almeno un pacchetto cure deve partire con i dati sintetici"
    for b in inviati:
        assert all(f.get("principio_attivo") for f in b["fields"]["farmaci"])
        assert len({f["principio_attivo"] for f in b["fields"]["farmaci"]}) >= 2
        assert "name" not in json.dumps(b["fields"]) and "cf" not in b["fields"]


def test_senza_due_nomi_il_pacchetto_non_parte(run):
    b = next(b for b in _bundles() if not b.get("local"))
    tieni = b["fields"]["farmaci"][0]["principio_attivo"]
    with db.tx() as con:
        con.execute("DELETE FROM signals WHERE patient_id=? AND field='principio_attivo' AND value NOT LIKE ?", (b["pid"], f"%|{tieni}"))
    dopo = next(x for x in _bundles() if x["pid"] == b["pid"])
    assert dopo["local"] is True  # meno di due nomi: non parte, decide il controllo locale (la regola fissa resta)


def test_check_output_tiene_solo_fonti_ben_formate():
    out = {"da_verificare": True, "gravita": 3, "spiegazione": "Major",
           "fonte": [{"knowledge_base": "cure_interazioni_validate", "testo": "DDInter1951, Warfarin, DDInter900, Ibuprofen, Major", "livello": "Major"},
                     {"knowledge_base": "wikipedia", "testo": "inventata"},
                     {"knowledge_base": "cure_dailymed_2026", "testo": 42}]}
    ok, why = gateway.check_output("cure", out, {})
    assert why is None and [f["knowledge_base"] for f in ok["fonte"]] == ["cure_interazioni_validate"]
    assert ok["fonte"][0]["livello"] == "Major"


def test_le_fonti_finiscono_nelle_prove_del_caso(monkeypatch):
    from app import agents
    def con_fonti(agent, payload):
        if agent == "cure":
            return {"da_verificare": True, "gravita": 3, "spiegazione": "Warfarin + Ibuprofene: livello Major in DDInter.",
                    "fonte": [{"knowledge_base": "cure_interazioni_validate", "testo": "DDInter1951, Warfarin, DDInter900, Ibuprofen, Major", "livello": "Major"},
                              {"knowledge_base": "cure_dailymed_2026", "testo": "Ibuprofen, Warfarin, increase bleeding risk", "effetto": "increase bleeding risk"}]}, "finto", 1
        from conftest import fake_call
        return fake_call(agent, payload)
    monkeypatch.setattr(agents, "call", con_fonti)
    gateway.run("t")
    with db.tx() as con:
        ev = [json.loads(r[0]) for r in con.execute("SELECT evidence FROM cases WHERE kind='cure' AND status='pending'")]
    righe = [e for lista in ev for e in lista if e[0] in ("DDInter", "DailyMed")]
    assert any("Major" in e[1] and "Warfarin" in e[2] for e in righe) and any(e[0] == "DailyMed" for e in righe)
