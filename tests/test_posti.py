"""Posti liberati: ordine fisso, una chiamata per ogni approvazione, accettazione, rifiuto, mancata risposta, errori.

Dati di partenza (seed): un ecocardiogramma liberato per domani (FS-1) e tre persone in lista:
  W-01 Teresa Furlan   priorità B, in lista dal 12/09  (attende di più)
  W-03 Franco De Marchi priorità B, in lista dal 30/09
  W-02 Giovanna Boscolo priorità D
Ordine atteso: De Marchi, Furlan (stessa priorità: vale il cognome, non l'attesa), Boscolo.
"""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import actions, calendario, cup_mail, db, gateway, posti
from app.main import app
from app.voce import chiamata as ch
from conftest import libera_posto
from test_web import client, login, tok  # noqa: F401  (client è una fixture)

U = {"username": "m.bianchi", "name": "Marta Bianchi"}


def q(sql, *a):
    with db.tx() as con:
        return [dict(r) for r in con.execute(sql, a)]


def proposta():
    """La proposta in attesa di approvazione (deve essere una sola)."""
    rows = q("SELECT * FROM cases WHERE kind='posto' AND status='pending'")
    assert len(rows) <= 1, rows
    return rows[0] if rows else None


def offerta(oid):
    return q("SELECT * FROM slot_offers WHERE id=?", oid)[0]


def approva():
    c = proposta()
    actions.execute(c["id"], U)
    o = offerta(json.loads(c["payload"])["offer"])
    return o, o["call_id"]


def rispondi(cid, scelta=None, passaggio=False):
    anno = q("SELECT p.birth_year FROM calls c JOIN patients p ON p.id=c.patient_id WHERE c.id=?", cid)[0]["birth_year"]
    ch.esegui(cid, "verifica_identita", {"anno_nascita": anno})
    if scelta:
        ch.esegui(cid, "conferma_appuntamento", {"scelta": scelta, "serve_passaggio": passaggio})
    ch.concludi(cid)


def eventi(step=None):
    return [e for e in q("SELECT * FROM slot_events WHERE slot_id='FS-1' ORDER BY rowid") if step is None or e["step"] == step]


# ------------------------------------------------------------------ ordine

def test_ordine_priorita_poi_cognome_e_nome():
    with db.tx() as con:
        lista = [w["patient_id"] for w in posti.ordine(con, posti._slot(con, "FS-1"))]
    assert lista == ["P08", "P03", "P07"]  # De Marchi (B), Furlan (B), Boscolo (D)


def test_a_parita_di_cognome_vale_il_nome():
    with db.tx() as con:
        con.execute("UPDATE patients SET name='Bruno Rossi', cf=NULL WHERE id='P03'")
        con.execute("UPDATE patients SET name='Anna Rossi', cf=NULL WHERE id='P08'")
        con.execute("UPDATE waitlist SET priority='B' WHERE id='W-02'")
        con.execute("UPDATE patients SET name='Carla Rossi', cf=NULL WHERE id='P07'")
        lista = [w["name"] for w in posti.ordine(con, posti._slot(con, "FS-1"))]
    assert lista == ["Anna Rossi", "Bruno Rossi", "Carla Rossi"]


def test_cognome_riconosciuto_dal_codice_fiscale():
    from app.cf import compute
    cf = compute("De Marchi", "Franco", "1949-04-21", "M")
    assert posti.cognome_nome("Franco De Marchi", cf) == ("De Marchi", "Franco")
    assert posti.cognome_nome("De Marchi Franco", cf) == ("De Marchi", "Franco")  # documento importato in ordine cognome nome
    assert posti.cognome_nome("Franco De Marchi") == ("De Marchi", "Franco")


def test_priorita_prima_dell_alfabeto_e_flag_mai_modificati():
    with db.tx() as con:
        con.execute("UPDATE waitlist SET priority='U' WHERE id='W-02'")  # Boscolo diventa urgente: passa prima di tutti
    before = {w["id"]: w["priority"] for w in q("SELECT * FROM waitlist")}
    gateway.run("t")
    assert proposta()["patient_id"] == "P07"
    o, cid = approva()
    rispondi(cid, "rifiuta")
    assert {w["id"]: w["priority"] for w in q("SELECT * FROM waitlist")} == before


def test_il_posto_non_va_piu_al_cloud(run):
    assert not q("SELECT * FROM packets_out WHERE payload LIKE '%posto_liberato%'")
    assert not q("SELECT * FROM vault WHERE agent='agende_candidato'")


# ------------------------------------------------------------------ approvazione per ogni chiamata

def test_nessuna_chiamata_senza_approvazione(run, client):
    assert not q("SELECT * FROM calls")
    c = proposta()
    assert c["patient_id"] == "P08" and "1° in ordine" in c["summary"] and "alfabetico" in c["summary"]
    login(client, "m.bianchi", "segreteria")
    r = client.post("/chiamate/nuova", data={"csrf_token": tok(client.get("/chiamate").text), "kind": "appuntamento",
                                             "ref_id": "x", "patient_id": "P08"})
    assert r.status_code == 400 and not q("SELECT * FROM calls")
    r = client.post(f"/caso/{c['id']}/approva", data={"csrf_token": tok(client.get(f"/caso/{c['id']}").text)}, follow_redirects=False)
    call = q("SELECT * FROM calls")
    assert len(call) == 1 and r.headers["location"] == f"/chiamate/{call[0]['id']}"
    assert call[0]["status"] == "pronta"  # preparata, non ancora fatta: la avvia la persona dalla pagina
    assert "Posti liberati" in client.get("/posti").text and "De Marchi" in client.get("/posti").text


def test_dopo_un_rifiuto_la_proposta_successiva_aspetta_una_nuova_approvazione(run):
    o, cid = approva()
    rispondi(cid, "rifiuta")
    assert offerta(o["id"])["status"] == "rifiutato"
    assert q("SELECT status FROM waitlist WHERE id='W-03'")[0]["status"] == "in attesa"  # resta in lista
    assert q("SELECT status FROM appointments WHERE id=?", o["appointment_id"])[0]["status"] == "annullata"
    nxt = proposta()
    assert nxt["patient_id"] == "P03" and "2° in ordine" in nxt["summary"]
    assert len(q("SELECT * FROM calls")) == 1  # la seconda chiamata non c'è finché nessuno la approva
    assert q("SELECT assigned_to FROM freed_slots WHERE id='FS-1'")[0]["assigned_to"] is None


# ------------------------------------------------------------------ accettazione

def test_accettazione_aggiorna_tutto_poi_manda_la_mail(run):
    o, cid = approva()
    rispondi(cid, "accetta")
    o = offerta(o["id"])
    assert o["status"] == "accettato"
    assert q("SELECT status, at FROM appointments WHERE id=?", o["appointment_id"])[0] == {"status": "prenotata", "at": "2026-10-23 09:40"}
    assert q("SELECT status FROM waitlist WHERE id='W-03'")[0]["status"] == "prenotato"
    assert q("SELECT assigned_to FROM freed_slots WHERE id='FS-1'")[0]["assigned_to"] == "P08"
    mails = q("SELECT * FROM outbox WHERE case_id=? AND recipient LIKE '%@example.com%'", cid)
    assert len(mails) == 1 and mails[0]["subject"].startswith("Conferma prenotazione") and "franco.demarchi@example.com" in mails[0]["recipient"]
    osp = q("SELECT * FROM outbox WHERE case_id=? AND recipient LIKE 'ambulatorio%'", cid)  # e l'ospedale è avvisato
    assert len(osp) == 1 and "cardiologia" in osp[0]["recipient"] and "Franco De Marchi" in osp[0]["body"]
    steps = [e["step"] for e in eventi()]
    assert steps.index("accettato") < steps.index("verificato") < steps.index("mail")  # la mail solo dopo la verifica
    assert not proposta()  # il posto non viene più proposto a nessuno
    gateway.run("t")
    assert not proposta()


def test_doppia_fine_chiamata_non_rifa_nulla(run):
    o, cid = approva()
    rispondi(cid, "accetta")
    posti.esito_chiamata(cid)
    ch.concludi(cid)
    assert len(q("SELECT * FROM outbox WHERE case_id=? AND recipient LIKE '%@example.com%'", cid)) == 1
    assert len(q("SELECT * FROM outbox WHERE case_id=? AND recipient LIKE 'ambulatorio%'", cid)) == 1


def test_niente_doppia_prenotazione_per_la_stessa_richiesta(run):
    sid = libera_posto("ecocardiogramma", "2026-12-04 11:00", fs_id="FS-X")  # De Marchi aveva già una data più lontana
    with db.tx() as con:
        con.execute("DELETE FROM freed_slots WHERE id='FS-X'")
        old = calendario.prenota(con, "P08", sid)
    o, cid = approva()
    rispondi(cid, "accetta")
    pren = q("SELECT id FROM appointments WHERE patient_id='P08' AND service='ecocardiogramma' AND status='prenotata'")
    assert pren == [{"id": o["appointment_id"]}]
    assert q("SELECT status FROM appointments WHERE id=?", old)[0]["status"] == "annullata"
    assert q("SELECT * FROM freed_slots WHERE at='2026-12-04 11:00' AND assigned_to IS NULL")  # la vecchia data torna libera
    assert q("SELECT status FROM cal_slots WHERE id=?", sid)[0]["status"] == "libero"
    with db.tx() as con:
        assert not calendario.coerenza(con)


def test_con_il_passaggio_parte_anche_la_pratica_di_trasporto():
    with db.tx() as con:  # Furlan vive sola e non ha l'auto: con priorità U è la prima
        con.execute("UPDATE waitlist SET priority='U' WHERE id='W-01'")
    gateway.run("t")
    o, cid = approva()
    assert o["patient_id"] == "P03"
    rispondi(cid, "accetta", passaggio=True)
    assert offerta(o["id"])["status"] == "accettato"
    assert q("SELECT * FROM outbox WHERE case_id=?", cid)  # conferma della prenotazione subito
    assert q("SELECT * FROM transport_requests WHERE patient_id='P03'")


# ------------------------------------------------------------------ fino a lista esaurita

def test_tutti_rifiutano_il_posto_resta_libero(run):
    for pid in ("P08", "P03", "P07"):
        assert proposta()["patient_id"] == pid
        _, cid = approva()
        rispondi(cid, "rifiuta")
    assert not proposta()
    assert q("SELECT assigned_to FROM freed_slots WHERE id='FS-1'")[0]["assigned_to"] is None
    assert len(eventi("lista esaurita")) == 1
    gateway.run("t")
    gateway.run("t")
    assert not proposta() and len(eventi("lista esaurita")) == 1  # niente proposte nuove, niente righe ripetute
    assert all(w["status"] == "in attesa" for w in q("SELECT status FROM waitlist"))


def test_lista_vuota():
    with db.tx() as con:
        con.execute("DELETE FROM waitlist")
    gateway.run("t")
    assert not proposta() and eventi("lista esaurita")


# ------------------------------------------------------------------ mancata risposta

def test_mancata_risposta_si_richiama_solo_con_una_nuova_approvazione(run):
    o, cid = approva()
    ch.concludi(cid)  # chiusa senza nessuna risposta
    assert offerta(o["id"])["status"] == "senza_esito"
    c = proposta()
    assert c["patient_id"] == "P08" and c["tag"].endswith("nuovo tentativo")
    assert q("SELECT status FROM waitlist WHERE id='W-03'")[0]["status"] == "proposto"  # posto ancora tenuto: nessun altro lo riceve
    _, cid2 = approva()
    assert cid2 != cid
    ch.concludi(cid2)  # seconda volta senza risposta: si passa al successivo
    assert offerta(o["id"])["status"] == "nessuna_risposta"
    assert proposta()["patient_id"] == "P03"
    assert q("SELECT status FROM waitlist WHERE id='W-03'")[0]["status"] == "in attesa"


def test_identita_non_confermata_conta_come_senza_risposta(run):
    o, cid = approva()
    ch.esegui(cid, "verifica_identita", {"anno_nascita": 1900})
    ch.esegui(cid, "verifica_identita", {"anno_nascita": 1901})
    ch.concludi(cid)
    assert offerta(o["id"])["status"] == "senza_esito" and proposta()["patient_id"] == "P08"


def test_scartare_la_proposta_passa_al_successivo(run, client):
    login(client, "m.bianchi", "segreteria")
    c = proposta()
    client.post(f"/caso/{c['id']}/scarta", data={"csrf_token": tok(client.get(f"/caso/{c['id']}").text), "motivo": "ricoverato"})
    assert offerta(json.loads(c["payload"])["offer"])["status"] == "scartato"
    assert proposta()["patient_id"] == "P03"
    assert q("SELECT status FROM waitlist WHERE id='W-03'")[0]["status"] == "in attesa"


def test_esito_registrato_a_mano(run):
    o, cid = approva()
    posti.esito_a_mano(o["id"], "accetta", U)
    assert offerta(o["id"])["status"] == "accettato" and q("SELECT * FROM outbox WHERE case_id=?", cid)


# ------------------------------------------------------------------ errori

def test_errore_di_aggiornamento_niente_mail_e_posto_tenuto(run, monkeypatch):
    real = posti._registra_prenotazione

    def rotto(con, o):
        con.execute("UPDATE appointments SET status='prenotata' WHERE id=?", (o["appointment_id"],))  # mezzo lavoro...
        raise sqlite3.OperationalError("disk I/O error")  # ...e poi il database si rompe
    monkeypatch.setattr(posti, "_registra_prenotazione", rotto)
    o, cid = approva()
    rispondi(cid, "accetta")
    o = offerta(o["id"])
    assert o["status"] == "errore"
    assert q("SELECT status FROM appointments WHERE id=?", o["appointment_id"])[0]["status"] == "da confermare"  # niente salvato a metà
    assert not q("SELECT * FROM outbox WHERE case_id=?", cid)
    assert q("SELECT * FROM cases WHERE kind='posto_errore' AND status='pending'")
    assert not proposta()  # il posto non va a un altro: la persona ha detto sì
    gateway.run("t")
    assert not proposta() and q("SELECT * FROM cases WHERE kind='posto_errore' AND status='pending'")  # l'elaborazione non lo cancella
    monkeypatch.setattr(posti, "_registra_prenotazione", real)
    posti.riprova(o["id"], U)
    assert offerta(o["id"])["status"] == "accettato" and q("SELECT * FROM outbox WHERE case_id=?", cid)


def test_verifica_non_superata_niente_mail(run, monkeypatch):
    vera_verifica = posti.verifica
    monkeypatch.setattr(posti, "verifica", lambda con, o: ["la persona risulta prenotata 2 volte per lo stesso esame"])
    o, cid = approva()
    rispondi(cid, "accetta")
    assert offerta(o["id"])["status"] == "errore" and not q("SELECT * FROM outbox WHERE case_id=?", cid)
    assert any("2 volte" in e["text"] for e in eventi("errore aggiornamento"))
    monkeypatch.setattr(posti, "verifica", vera_verifica)
    posti.libera(o["id"], U)  # l'accettazione decide di liberare: tutto annullato, si passa al successivo
    assert q("SELECT assigned_to FROM freed_slots WHERE id='FS-1'")[0]["assigned_to"] is None
    assert q("SELECT status FROM appointments WHERE id=?", o["appointment_id"])[0]["status"] == "annullata"
    assert proposta()["patient_id"] == "P03"


def test_errore_della_mail_registrato(run, monkeypatch):
    def guasta(*a, **k):
        raise RuntimeError("SMTP giù")
    monkeypatch.setattr(cup_mail, "manda", guasta)
    o, cid = approva()
    rispondi(cid, "accetta")
    assert offerta(o["id"])["status"] == "accettato"  # la prenotazione è salvata e verificata
    e = eventi("errore mail")
    assert e and "NON è partita" in e[0]["text"] and not eventi("mail")


def test_lista_d_attesa_non_leggibile(monkeypatch):
    def guasta(con, service):
        raise sqlite3.OperationalError("database is locked")
    vera = posti._lista
    monkeypatch.setattr(posti, "_lista", guasta)
    gateway.run("t")
    gateway.run("t")
    assert not proposta() and len(eventi("errore lista")) == 1
    monkeypatch.setattr(posti, "_lista", vera)  # non monkeypatch.undo(): toglierebbe anche gli agenti finti e chiamerebbe quelli veri
    gateway.run("t")
    assert proposta()["patient_id"] == "P08"  # appena la lista si rilegge, la procedura riparte


def test_errore_del_telefono_registrato(run, client, monkeypatch):
    from app.voce import eleven
    monkeypatch.setattr(eleven, "telefono_configurato", lambda: True)
    monkeypatch.setattr(eleven, "chiama_telefono", lambda d: (_ for _ in ()).throw(RuntimeError("numero non raggiungibile")))
    o, cid = approva()
    login(client, "m.bianchi", "segreteria")
    client.post(f"/chiamate/{cid}/telefono", data={"csrf_token": tok(client.get(f"/chiamate/{cid}").text)})
    assert eventi("errore chiamata") and offerta(o["id"])["status"] == "in_chiamata"


# ------------------------------------------------------------------ un posto, una proposta

def test_un_posto_una_proposta_aperta_e_una_persona_una_proposta(run):
    with db.tx() as con:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO slot_offers (id, slot_id, waitlist_id, patient_id, status) VALUES ('X','FS-1','W-01','P03','da_approvare')")
    with db.tx() as con:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO slot_offers (id, slot_id, waitlist_id, patient_id, status) VALUES ('Y','FS-9','W-03','P08','da_approvare')")


def test_due_posti_due_persone_diverse():
    libera_posto("ecocardiogramma", "2026-10-30 10:00")
    gateway.run("t")
    rows = q("SELECT patient_id, json_extract(payload,'$.slot') AS slot FROM cases WHERE kind='posto' AND status='pending' ORDER BY slot")
    assert rows == [{"patient_id": "P08", "slot": "FS-1"}, {"patient_id": "P03", "slot": "FS-2"}]


def test_doppia_approvazione_una_sola_chiamata(run):
    c = proposta()
    actions.execute(c["id"], U)
    assert actions.execute(c["id"], U) is None
    assert len(q("SELECT * FROM calls")) == 1


def test_posto_passato_chiude_la_proposta(run):
    o, cid = approva()
    with db.tx() as con:
        con.execute("UPDATE freed_slots SET at='2026-10-21 09:40'")
    gateway.run("t")
    assert offerta(o["id"])["status"] == "scaduto"
    assert q("SELECT status FROM waitlist WHERE id='W-03'")[0]["status"] == "in attesa"
    assert not proposta()


def test_pagina_posti_solo_accettazione(client):
    login(client, "e.gatti", "medico")
    assert client.get("/posti").status_code == 403
