"""Strumento Calendario: popolamento sintetico, operazioni, conflitti, errori, e uso da parte dell'agente Agende."""
import json
import sqlite3

from app import actions, calendario, db, gateway, posti
from app.voce import chiamata as ch
from conftest import libera_posto
from test_web import client, login, tok  # noqa: F401

U = {"username": "m.bianchi", "name": "Marta Bianchi"}
FS1 = ("ecocardiogramma", "2026-10-23 09:40", "Ospedale Laguna, pad. C")


def q(sql, *a):
    with db.tx() as con:
        return [dict(r) for r in con.execute(sql, a)]


def tool(nome, **args):
    return calendario.esegui(nome, args, U)


def slot(service, at, place=None):
    with db.tx() as con:
        return calendario.posto(con, service=service, at=at, place=place)


# ------------------------------------------------------------------ popolamento

def test_calendario_popolato_dal_dataset_e_marcato_come_prova():
    tot = q("SELECT COUNT(*) AS n, SUM(test) AS t, SUM(fonte LIKE 'sintetico%') AS f FROM cal_slots")[0]
    assert tot["n"] > 1000 and tot["t"] == tot["n"] == tot["f"]
    legati = {r["appointment_id"]: r for r in q("SELECT * FROM cal_slots WHERE appointment_id IS NOT NULL")}
    for a in q("SELECT * FROM appointments WHERE status='prenotata'"):  # ogni visita del dataset è nel suo posto
        s = legati[a["id"]]
        assert (s["service"], s["at"], s["place"], s["status"]) == (a["service"], a["at"], a["place"], "prenotato")
    assert slot(*FS1)["status"] == "libero"  # il posto disdetto del dataset è libero
    assert not q("SELECT * FROM cal_slots WHERE occupant='prova' AND appointment_id IS NOT NULL")  # le prove non hanno persone
    with db.tx() as con:
        assert calendario.coerenza(con) == []


def test_ripopolare_non_crea_doppioni():
    before = q("SELECT id, status, occupant FROM cal_slots ORDER BY id")
    with db.tx() as con:
        r = calendario.popola(con)
    assert r["nuovi"] == 0 and r["prove"] == 0 and r["incoerenze"] == []
    assert q("SELECT id, status, occupant FROM cal_slots ORDER BY id") == before


def test_un_posto_liberato_non_torna_occupato_da_una_prova():
    r = tool("calendario_cancella", richiesta_dal_paziente=True, prenotazione="A-10", motivo="test")
    assert r["ok"]
    with db.tx() as con:
        calendario.popola(con)
    assert q("SELECT status, occupant FROM cal_slots WHERE service='visita oculistica' AND at='2026-10-27 15:00'")[0] == \
        {"status": "libero", "occupant": None}


# ------------------------------------------------------------------ lettura

def test_consulta_e_disponibilita_leggono_lo_stato_attuale():
    r = tool("calendario_disponibilita", esame="ecocardiogramma")
    assert r["ok"] and [x["at"] for x in r["dati"]] == ["2026-10-23 09:40"]  # pieno, tranne il posto disdetto
    c = tool("calendario_consulta", esame="visita cardiologica di controllo", dal="2026-10-23", al="2026-10-23 23:59")
    mine = [x for x in c["dati"] if x["appointment_id"] == "A-02"]
    assert mine and mine[0]["patient"] == "Anna Zanella" and "version" in mine[0]
    assert tool("calendario_posto_libero", posto=slot(*FS1)["id"])["dati"]["libero"] is True
    assert tool("calendario_posto_libero", posto=mine[0]["id"])["dati"]["libero"] is False


def test_strumenti_dichiarati_sono_tutti_eseguibili():
    nomi = [t["function"]["name"] for t in calendario.STRUMENTI]
    for n in nomi:
        r = calendario.esegui(n, {}, U)
        assert "strumento sconosciuto" not in (r.get("errore") or "")


# ------------------------------------------------------------------ scrittura e verifica

def test_prenota_sposta_cancella_aggiornano_davvero_il_calendario():
    s = slot(*FS1)
    r = tool("calendario_prenota", paziente="P09", posto=s["id"], versione=s["version"])
    assert r == {"ok": True, "verificato": True, "prenotazione": r["prenotazione"]}
    a = r["prenotazione"]
    assert slot(*FS1)["status"] == "prenotato" and q("SELECT status FROM appointments WHERE id=?", a)[0]["status"] == "prenotata"
    dest = libera_posto("ecocardiogramma", "2026-10-30 10:00", fs_id="FS-T")
    with db.tx() as con:
        con.execute("DELETE FROM freed_slots WHERE id='FS-T'")
    v = slot("ecocardiogramma", "2026-10-30 10:00")["version"]
    r = tool("calendario_sposta", richiesta_dal_paziente=True, prenotazione=a, posto=dest, versione=v)
    assert r["ok"] and r["posto_liberato"]
    assert q("SELECT at FROM appointments WHERE id=?", a)[0]["at"] == "2026-10-30 10:00"
    assert slot(*FS1)["status"] == "libero" and slot("ecocardiogramma", "2026-10-30 10:00")["appointment_id"] == a
    r = tool("calendario_cancella", richiesta_dal_paziente=True, prenotazione=a, motivo="non serve più")
    assert r["ok"] and q("SELECT status FROM appointments WHERE id=?", a)[0]["status"] == "annullata"
    assert slot("ecocardiogramma", "2026-10-30 10:00")["status"] == "libero"
    assert q("SELECT * FROM freed_slots WHERE id=?", r["posto_liberato"])  # il posto va alla lista d'attesa
    ops = [l["esito"] for l in q("SELECT esito FROM cal_log WHERE appointment_id=? ORDER BY rowid", a)]
    assert ops.count("verificata") == 3  # ogni modifica è stata riletta
    with db.tx() as con:
        assert calendario.coerenza(con) == []


def test_posto_gia_occupato_rifiutato():
    s = slot(*FS1)
    assert tool("calendario_prenota", paziente="P09", posto=s["id"], versione=s["version"])["ok"]
    r = tool("calendario_prenota", paziente="P10", posto=s["id"], versione=s["version"] + 1)
    assert not r["ok"] and "non disponibile" in r["errore"]


def test_dato_vecchio_rifiutato():
    s = slot(*FS1)  # letto, poi cambiato da un'altra operazione, poi riletto con la versione vecchia
    a = tool("calendario_prenota", paziente="P09", posto=s["id"], versione=s["version"])["prenotazione"]
    tool("calendario_cancella", richiesta_dal_paziente=True, prenotazione=a)
    r = tool("calendario_prenota", paziente="P10", posto=s["id"], versione=s["version"])
    assert not r["ok"] and "non aggiornato" in r["errore"] and slot(*FS1)["status"] == "libero"


def test_doppione_rifiutato():
    libera_posto("visita cardiologica di controllo", "2026-10-26 08:30", "Ospedale Laguna, pad. B", "FS-D")
    s = slot("visita cardiologica di controllo", "2026-10-26 08:30")
    r = tool("calendario_prenota", paziente="P02", posto=s["id"], versione=s["version"])  # Zanella ha già il controllo il 23/10
    assert not r["ok"] and r["errore"].startswith("doppione")


def test_sovrapposizione_rifiutata():
    libera_posto("visita cardiologica", "2026-10-23 09:30", "Ospedale Laguna, pad. B", "FS-S")
    s = slot("visita cardiologica", "2026-10-23 09:30")
    r = tool("calendario_prenota", paziente="P02", posto=s["id"], versione=s["version"])  # alla stessa ora ha il controllo
    assert not r["ok"] and r["errore"].startswith("sovrapposizione")


def test_errore_di_aggiornamento_niente_salvato(monkeypatch):
    n = q("SELECT COUNT(*) AS n FROM appointments")[0]["n"]

    def rotto(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(calendario, "_prendi", rotto)
    s = slot(*FS1)
    r = tool("calendario_prenota", paziente="P09", posto=s["id"], versione=s["version"])
    assert not r["ok"] and "disk I/O" in r["errore"]
    assert q("SELECT COUNT(*) AS n FROM appointments")[0]["n"] == n and slot(*FS1)["status"] == "libero"
    assert q("SELECT * FROM cal_log WHERE esito='rifiutata'")


def test_verifica_non_superata_non_dichiara_riuscita(monkeypatch):
    monkeypatch.setattr(calendario, "verifica_prenotazione", lambda con, a, st=None: ["il posto nel calendario non corrisponde"])
    s = slot(*FS1)
    r = tool("calendario_prenota", paziente="P09", posto=s["id"], versione=s["version"])
    assert r["ok"] is False and r["verifica"] and q("SELECT * FROM cal_log WHERE esito='verifica non superata'")


# ------------------------------------------------------------------ l'agente Agende usa il calendario

def test_la_proposta_tiene_e_rilascia_il_posto_nel_calendario(run):
    c = q("SELECT * FROM cases WHERE kind='posto' AND status='pending'")[0]
    actions.execute(c["id"], U)
    s = slot(*FS1)
    assert s["status"] == "tenuto" and q("SELECT patient_id FROM appointments WHERE id=?", s["appointment_id"])[0]["patient_id"] == "P08"
    o = q("SELECT * FROM slot_offers WHERE status='in_chiamata'")[0]
    ch.esegui(o["call_id"], "verifica_identita", {"anno_nascita": 1949})
    ch.esegui(o["call_id"], "conferma_appuntamento", {"scelta": "rifiuta"})
    ch.concludi(o["call_id"])
    assert slot(*FS1)["status"] == "libero"  # rifiutato: il posto torna libero nel calendario
    c = q("SELECT * FROM cases WHERE kind='posto' AND status='pending'")[0]
    actions.execute(c["id"], U)
    o = q("SELECT * FROM slot_offers WHERE status='in_chiamata'")[0]
    ch.esegui(o["call_id"], "verifica_identita", {"anno_nascita": 1944})
    ch.esegui(o["call_id"], "conferma_appuntamento", {"scelta": "accetta"})
    ch.concludi(o["call_id"])
    s = slot(*FS1)
    assert s["status"] == "prenotato" and q("SELECT patient_id, status FROM appointments WHERE id=?", s["appointment_id"])[0] == \
        {"patient_id": "P03", "status": "prenotata"}
    assert any(e["step"] == "verificato" for e in q("SELECT step FROM slot_events"))
    with db.tx() as con:
        assert calendario.coerenza(con) == []


def test_l_agente_rilegge_il_calendario_prima_di_agire(run):
    c = q("SELECT * FROM cases WHERE kind='posto' AND status='pending'")[0]
    s = slot(*FS1)  # dopo la proposta, qualcuno prenota quel posto dallo sportello
    assert tool("calendario_prenota", paziente="P09", posto=s["id"], versione=s["version"])["ok"]
    try:
        actions.execute(c["id"], U)
        raise AssertionError("l'approvazione doveva essere rifiutata")
    except ValueError as e:
        assert "calendario" in str(e).lower()
    assert not q("SELECT * FROM calls") and q("SELECT status FROM cases WHERE id=?", c["id"])[0]["status"] == "pending"


def test_posto_non_libero_nel_calendario_niente_proposta():
    s = slot(*FS1)
    assert tool("calendario_prenota", paziente="P09", posto=s["id"], versione=s["version"])["ok"]
    gateway.run("t")
    assert not q("SELECT * FROM cases WHERE kind='posto' AND status='pending'")
    assert any(e["step"] == "non libero nel calendario" for e in q("SELECT step FROM slot_events"))


def test_una_disdetta_dal_calendario_arriva_alla_lista_d_attesa():
    with db.tx() as con:  # a chi è in lista per l'ecocardiogramma si libera un posto il 4/11
        con.execute("DELETE FROM freed_slots")
        con.execute("UPDATE cal_slots SET status='libero', occupant=NULL, version=version+1 WHERE id=?",
                    (calendario.posto(con, service="ecocardiogramma", at="2026-11-04 10:00")["id"],))
        sid = calendario.posto(con, service="ecocardiogramma", at="2026-11-04 10:00")["id"]
        a = calendario.prenota(con, "P09", sid)
    r = tool("calendario_cancella", richiesta_dal_paziente=True, prenotazione=a, motivo="disdetta al telefono")
    assert r["ok"] and r["posto_liberato"]
    gateway.run("t")
    c = q("SELECT * FROM cases WHERE kind='posto' AND status='pending'")
    assert len(c) == 1 and c[0]["patient_id"] == "P08" and json.loads(c[0]["payload"])["slot"] == r["posto_liberato"]


def test_richieste_doppie_leggono_il_calendario(monkeypatch):
    from conftest import fake_call
    from app import agents
    libera_posto("tac_torace", "2026-10-28 09:00", "Ospedale Laguna, pad. A", "FS-TAC")
    with db.tx() as con:
        con.execute("DELETE FROM freed_slots WHERE id='FS-TAC'")
        calendario.prenota(con, "P04", calendario.posto(con, service="tac_torace", at="2026-10-28 09:00")["id"])
    monkeypatch.setattr(agents, "call", lambda a, p: ({"doppione": False, "spiegazione": "no"}, "finto", 1)
                        if p.get("tipo") == "richieste_doppie" else fake_call(a, p))
    gateway.run("t")
    c = q("SELECT * FROM cases WHERE kind='doppione' AND patient_id='P04'")
    assert c and "già prenotato il 2026-10-28 09:00" in c[0]["action"] and "Regola locale fissa" in c[0]["action"]
    assert any(e[1] == "Calendario" for e in json.loads(c[0]["evidence"]))


# ------------------------------------------------------------------ pagina

def test_pagina_calendario(client):
    login(client, "m.bianchi", "segreteria")
    r = client.get("/calendario?esame=visita%20cardiologica%20di%20controllo&dal=2026-10-22")
    assert r.status_code == 200 and "DATI DI PROVA" in r.text and "Anna Zanella" in r.text and "occupati" in r.text
    assert 'href="/persona/P02/calendario"' in r.text  # la prenotazione di una persona porta al suo calendario


def test_pagina_calendario_solo_accettazione(client):
    login(client, "e.gatti", "medico")
    assert client.get("/calendario").status_code == 403


def test_il_pacchetto_agende_porta_il_riassunto_del_calendario():
    libera_posto("tac_torace", "2026-10-28 09:00", "Ospedale Laguna, pad. A", "FS-TAC")
    with db.tx() as con:
        con.execute("DELETE FROM freed_slots WHERE id='FS-TAC'")
        calendario.prenota(con, "P04", calendario.posto(con, service="tac_torace", at="2026-10-28 09:00")["id"])
    gateway.run("t")
    p = [json.loads(r["payload"]) for r in q("SELECT payload FROM packets_out WHERE payload LIKE '%richieste_doppie%'")]
    assert p and p[0]["calendario"]["prenotazione_esistente"] == "entro 7 giorni"
    assert p[0]["calendario"]["posti_liberi_30_giorni"] in ("pochi", "molti")
    testo = json.dumps(p[0], ensure_ascii=False)
    for vietato in ("2026-10-28", "09:00", "pad. A", "Pavan", "A-"):  # niente date, orari, luoghi, nomi o codici di prenotazione
        assert vietato not in testo


def test_senza_prenotazione_il_riassunto_lo_dice(run):
    p = [json.loads(r["payload"]) for r in q("SELECT payload FROM packets_out WHERE payload LIKE '%richieste_doppie%'")]
    assert p and p[0]["calendario"]["prenotazione_esistente"] == "nessuna"
