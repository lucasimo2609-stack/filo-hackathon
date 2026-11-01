"""Modifiche alle prenotazioni volute dall'ospedale: mail all'ambulatorio, chiamata al paziente (approvata da una persona),
calendario cambiato solo con la conferma, riletto prima delle mail di conferma."""
import json
import sqlite3

from app import actions, calendario, db, gateway, modifiche
from app.voce import chiamata as ch
from conftest import libera_posto
from test_web import client, login, tok  # noqa: F401

U = {"username": "m.bianchi", "name": "Marta Bianchi"}
A06 = "A-06"  # Sergio Vianello, visita diabetologica 2026-11-03 08:40, Poliambulatorio Sud (nata 1951)


def q(sql, *a):
    with db.tx() as con:
        return [dict(r) for r in con.execute(sql, a)]


def proponi(tipo="sposta", **k):
    return calendario.esegui("calendario_sposta" if tipo == "sposta" else "calendario_cancella",
                             {"prenotazione": A06, "motivo": "medico assente", **k}, U)


def caso():
    c = q("SELECT * FROM cases WHERE kind='modifica' AND status='pending'")
    assert len(c) == 1, c
    return c[0]


def chiama(scelta=None):
    c = caso()
    actions.execute(c["id"], U)
    m = q("SELECT * FROM cal_changes WHERE id=?", json.loads(c["payload"])["change"])[0]
    if scelta is not False:
        ch.esegui(m["call_id"], "verifica_identita", {"anno_nascita": 1951})
        if scelta:
            ch.esegui(m["call_id"], "conferma_modifica", {"scelta": scelta})
        ch.concludi(m["call_id"])
    return q("SELECT * FROM cal_changes WHERE id=?", m["id"])[0]


def mail_osp(mid):
    return q("SELECT * FROM outbox WHERE case_id=? AND recipient LIKE 'ambulatorio%' ORDER BY id", mid)


def prima():
    return q("SELECT * FROM appointments WHERE id=?", A06)[0]


def test_la_modifica_non_si_applica_senza_il_paziente():
    r = proponi()
    assert r["ok"] and r["in_attesa"] and r["posto_tenuto"]
    assert prima()["at"] == "2026-11-03 08:40" and prima()["status"] == "prenotata"  # niente cambiato
    s = q("SELECT * FROM cal_slots WHERE id=?", r["posto_tenuto"])[0]
    assert s["status"] == "tenuto" and s["occupant"] == "modifica" and s["service"] == "visita diabetologica"
    m = mail_osp(r["richiesta"])
    assert len(m) == 1 and m[0]["recipient"] == "ambulatorio diabetologia (Poliambulatorio Sud)" and "NON è ancora valida" in m[0]["body"]
    assert not q("SELECT * FROM calls")  # nessuna chiamata senza approvazione
    assert "Sergio Vianello" in caso()["summary"]
    with db.tx() as con:
        assert calendario.coerenza(con) == []


def test_conferma_del_paziente_cambia_rilegge_e_avvisa_tutti():
    r = proponi()
    nuovo = q("SELECT at FROM cal_slots WHERE id=?", r["posto_tenuto"])[0]["at"]
    m = chiama("accetta")
    assert m["status"] == "confermata"
    assert prima()["at"] == nuovo and prima()["status"] == "prenotata"  # stessa prenotazione, data nuova
    assert q("SELECT appointment_id FROM cal_slots WHERE id=?", r["posto_tenuto"])[0]["appointment_id"] == A06
    vecchio = q("SELECT status FROM cal_slots WHERE service='visita diabetologica' AND at='2026-11-03 08:40'")[0]
    assert vecchio["status"] == "libero" and q("SELECT * FROM freed_slots WHERE at='2026-11-03 08:40'")  # va alla lista d'attesa
    osp = mail_osp(m["id"])
    assert [x["subject"].split(" - ")[0] for x in osp] == ["Modifica proposta", "Modifica CONFERMATA"]
    paz = q("SELECT * FROM outbox WHERE case_id=? AND recipient LIKE '%@example.com%'", m["call_id"])
    assert len(paz) == 1 and paz[0]["subject"].startswith("Conferma prenotazione")
    steps = [e["step"] for e in q("SELECT step FROM cal_change_events WHERE change_id=? ORDER BY rowid", m["id"])]
    assert steps.index("applicata") < steps.index("verificata") < len(steps) - 1
    assert steps[steps.index("verificata") + 1] == "mail ospedale" and "mail paziente" in steps  # mail solo dopo la rilettura
    with db.tx() as con:
        assert calendario.coerenza(con) == []


def test_il_paziente_rifiuta_resta_la_data_di_prima():
    r = proponi()
    m = chiama("rifiuta")
    assert m["status"] == "rifiutata" and prima()["at"] == "2026-11-03 08:40"
    assert q("SELECT status FROM cal_slots WHERE id=?", r["posto_tenuto"])[0]["status"] == "libero"
    assert mail_osp(m["id"])[-1]["subject"].startswith("Modifica NON confermata")
    assert not q("SELECT * FROM outbox WHERE recipient LIKE '%@example.com%'")  # al paziente niente "conferma"


def test_annullamento_confermato():
    m = None
    r = proponi("annulla")
    assert r["ok"] and r["posto_tenuto"] is None
    m = chiama("accetta")
    assert m["status"] == "confermata" and prima()["status"] == "annullata"
    assert q("SELECT * FROM freed_slots WHERE at='2026-11-03 08:40'")
    paz = q("SELECT * FROM outbox WHERE recipient LIKE '%@example.com%'")
    assert len(paz) == 1 and paz[0]["subject"].startswith("Annullamento prenotazione")


def test_nessuna_risposta_si_richiama_solo_con_nuova_approvazione():
    proponi()
    m = chiama()  # chiamata chiusa senza risposta
    assert m["status"] == "senza_esito" and caso()["tag"].endswith("nuovo tentativo")
    assert prima()["at"] == "2026-11-03 08:40"
    m = chiama()
    assert m["status"] == "nessuna_risposta" and prima()["at"] == "2026-11-03 08:40"
    assert q("SELECT status FROM cal_slots WHERE id=?", m["to_slot"])[0]["status"] == "libero"
    assert mail_osp(m["id"])[-1]["subject"].startswith("Modifica NON confermata")


def test_errore_di_aggiornamento_niente_mail_di_conferma(monkeypatch):
    real = modifiche._applica_nel_calendario

    def rotto(con, chg):
        calendario.rilascia(con, chg["to_slot"], modifiche.AGENTE)  # mezzo lavoro...
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(modifiche, "_applica_nel_calendario", rotto)
    r = proponi()
    m = chiama("accetta")
    assert m["status"] == "errore" and prima()["at"] == "2026-11-03 08:40"
    assert q("SELECT status FROM cal_slots WHERE id=?", r["posto_tenuto"])[0]["status"] == "tenuto"  # niente salvato a metà
    assert [x["subject"].split(" - ")[0] for x in mail_osp(m["id"])] == ["Modifica proposta"]  # nessuna "CONFERMATA"
    assert not q("SELECT * FROM outbox WHERE recipient LIKE '%@example.com%'")
    assert q("SELECT * FROM cases WHERE kind='modifica_errore' AND status='pending'")
    monkeypatch.setattr(modifiche, "_applica_nel_calendario", real)
    with db.tx() as con:
        pass
    assert modifiche.riprova(m["id"], U) == "Confermata dal paziente"
    assert mail_osp(m["id"])[-1]["subject"].startswith("Modifica CONFERMATA")


def test_rilettura_non_superata_niente_mail(monkeypatch):
    monkeypatch.setattr(modifiche, "verifica_modifica", lambda con, chg: ["la prenotazione non risulta alla data nuova"])
    proponi()
    m = chiama("accetta")
    assert m["status"] == "errore"
    assert not any(x["subject"].startswith("Modifica CONFERMATA") for x in mail_osp(m["id"]))
    assert not q("SELECT * FROM outbox WHERE recipient LIKE '%@example.com%'")


def test_calendario_cambiato_prima_dell_approvazione():
    r = proponi()
    with db.tx() as con:  # qualcuno libera e riprende il posto tenuto per sbaglio
        con.execute("UPDATE cal_slots SET status='prenotato', occupant='prova' WHERE id=?", (r["posto_tenuto"],))
    c = caso()
    try:
        actions.execute(c["id"], U)
        raise AssertionError("doveva essere rifiutata")
    except ValueError as e:
        assert "calendario" in str(e).lower()
    assert not q("SELECT * FROM calls")


def test_una_sola_modifica_per_prenotazione():
    assert proponi()["ok"]
    r = proponi()
    assert not r["ok"] and "già una modifica" in r["errore"]


def test_posto_di_arrivo_occupato_o_esame_diverso_rifiutati():
    s = q("SELECT id FROM cal_slots WHERE service='visita diabetologica' AND status='prenotato' AND occupant='prova' LIMIT 1")[0]["id"]
    assert "non disponibile" in proponi(posto=s)["errore"]
    t = q("SELECT id FROM cal_slots WHERE service='tac_torace' AND status='libero' LIMIT 1")[0]["id"]
    assert "non per visita diabetologica" in proponi(posto=t)["errore"]


def test_ambulatorio_chiude_un_posto_con_una_prenotazione():
    sid = q("SELECT id FROM cal_slots WHERE appointment_id=?", A06)[0]["id"]
    r = calendario.esegui("calendario_blocca", {"posto": sid, "motivo": "ambulatorio chiuso per lavori"}, U)
    assert r["ok"] and r["in_attesa"]
    assert q("SELECT status FROM cal_slots WHERE id=?", sid)[0]["status"] == "prenotato"  # si chiude solo dopo la conferma
    m = chiama("accetta")
    assert m["status"] == "confermata"
    assert q("SELECT status, appointment_id FROM cal_slots WHERE id=?", sid)[0] == {"status": "bloccato", "appointment_id": None}
    assert not q("SELECT * FROM freed_slots WHERE at='2026-11-03 08:40'")  # un posto chiuso non va alla lista d'attesa


def test_ambulatorio_chiude_il_posto_ma_il_paziente_rifiuta_va_a_mano():
    sid = q("SELECT id FROM cal_slots WHERE appointment_id=?", A06)[0]["id"]
    calendario.esegui("calendario_blocca", {"posto": sid, "motivo": "ambulatorio chiuso"}, U)
    chiama("rifiuta")
    assert q("SELECT * FROM cases WHERE kind='modifica_errore' AND status='pending'")
    assert "riorganizzata a mano" in mail_osp(q("SELECT id FROM cal_changes")[0]["id"])[-1]["body"]


def test_chiudere_un_posto_libero_o_di_prova():
    s = q("SELECT id FROM cal_slots WHERE status='prenotato' AND occupant='prova' LIMIT 1")[0]["id"]
    r = calendario.esegui("calendario_blocca", {"posto": s, "motivo": "manutenzione"}, U)
    assert r["ok"] and r.get("fatto") == "posto chiuso" and q("SELECT status FROM cal_slots WHERE id=?", s)[0]["status"] == "bloccato"


def test_disdetta_chiesta_dal_paziente_si_applica_e_avvisa_l_ambulatorio():
    r = calendario.esegui("calendario_cancella", {"prenotazione": A06, "motivo": "disdetta al telefono", "richiesta_dal_paziente": True}, U)
    assert r["ok"] and prima()["status"] == "annullata" and not q("SELECT * FROM cal_changes")
    assert q("SELECT * FROM outbox WHERE recipient LIKE 'ambulatorio diabetologia%' AND subject LIKE 'Modifica chiesta dal paziente%'")


def test_prompt_della_chiamata_dopo_l_identita():
    proponi()
    c = caso()
    actions.execute(c["id"], U)
    m = q("SELECT * FROM cal_changes")[0]
    with db.tx() as con:
        assert "visita diabetologica" not in ch.prompt(ch.carica(con, m["call_id"])).lower()  # prima dell'identità non sa nulla
    ch.esegui(m["call_id"], "verifica_identita", {"anno_nascita": 1951})
    with db.tx() as con:
        c2 = ch.carica(con, m["call_id"])
    p = ch.prompt(c2)
    assert "andrebbe spostata" in p and "medico assente" in p and "conferma_modifica" in p
    assert [t["function"]["name"] for t in ch.strumenti(c2)] == ["passa_operatore", "conferma_modifica"]


def test_pagina_calendario_con_modifica(client):
    proponi()
    login(client, "m.bianchi", "segreteria")
    r = client.get("/calendario?esame=visita%20diabetologica")
    assert r.status_code == 200 and "Modifiche alle prenotazioni" in r.text and "Sergio Vianello" in r.text
    assert "Proponi la modifica" in client.get("/persona/P06/calendario").text  # la modifica si propone dal calendario della persona
    c = caso()
    r = client.post(f"/caso/{c['id']}/approva", data={"csrf_token": tok(client.get(f"/caso/{c['id']}").text)}, follow_redirects=False)
    assert r.headers["location"].startswith("/chiamate/CH-")
    assert client.get("/chiamate").text.count("modifica di visita diabetologica") == 1


def test_proposta_dalla_pagina_e_ritiro(client):
    login(client, "m.bianchi", "segreteria")
    t = tok(client.get("/calendario").text)
    r = client.post("/calendario/modifica", data={"csrf_token": t, "prenotazione": A06, "azione": "sposta", "posto": "", "motivo": "ferie"},
                    follow_redirects=False)
    assert r.headers["location"] == "/cruscotto"
    c = caso()
    client.post(f"/caso/{c['id']}/scarta", data={"csrf_token": tok(client.get(f"/caso/{c['id']}").text), "motivo": "ci ripensiamo"})
    m = q("SELECT * FROM cal_changes")[0]
    assert m["status"] == "ritirata" and q("SELECT status FROM cal_slots WHERE id=?", m["to_slot"])[0]["status"] == "libero"
    assert mail_osp(m["id"])[-1]["subject"].startswith("Modifica NON confermata")
