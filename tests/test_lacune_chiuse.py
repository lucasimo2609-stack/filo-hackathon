"""Le lacune del documento "Filo - lacune del codice e del flusso", rovesciate: ogni test PASSA se la lacuna è chiusa.

Dove la lacuna stava in una rotta, il test passa dalla rotta vera (con login), non da una copia della sua logica.
Il numero tra parentesi è quello del documento.
"""
import html
import json
import threading

import pyotp
import pytest
from fastapi.testclient import TestClient

from app import actions, agents, auth, cf, db, extract, gateway, importa, trasporti
from app.agents import call as real_call  # preso prima che conftest lo sostituisca con il finto
from app.auth import hash_password
from app.main import _used_codes, app, one_time_code_ok
from conftest import fake_call, libera_posto
from test_web import PW, login, tok

USER = {"username": "m.bianchi", "name": "Marta Bianchi"}


@pytest.fixture
def client():
    secrets_ = {}
    with db.tx() as con:
        for u in ("m.bianchi", "e.gatti"):
            salt, h = hash_password(PW)
            con.execute("UPDATE users SET pw_salt=?, pw_hash=?, failed=0, locked_until=NULL WHERE username=?", (salt, h, u))
            secrets_[u] = con.execute("SELECT totp_secret FROM users WHERE username=?", (u,)).fetchone()[0]
    _used_codes.clear()
    getattr(auth, "_pw_fails", {}).clear()
    c = TestClient(app)
    c.secrets = secrets_
    return c


def cases(kind=None, pid=None, status=None):
    q, a = "SELECT * FROM cases WHERE 1=1", []
    for col, v in (("kind", kind), ("patient_id", pid), ("status", status)):
        if v:
            q += f" AND {col}=?"; a.append(v)
    with db.tx() as con:
        return [dict(r) for r in con.execute(q, a)]


def answer(monkeypatch, over):
    def f(agent, payload):
        if agent in over:
            o = over[agent]
            if isinstance(o, Exception):
                raise o
            return (o(payload) if callable(o) else o), "finto", 1
        return fake_call(agent, payload)
    monkeypatch.setattr(agents, "call", f)


def summary(run_id):
    with db.tx() as con:
        return json.loads(con.execute("SELECT summary FROM runs WHERE id=?", (run_id,)).fetchone()[0])


# ------------------------------------------------------------------ risposte degli agenti (1-5)

def test_04_gravita_in_parole_non_blocca_e_il_caso_resta_a_mano(monkeypatch):
    answer(monkeypatch, {"cure": {"da_verificare": True, "gravita": "alta", "spiegazione": "x"}})
    s = gateway.run("t")
    assert summary(s["run_id"])["scartate"] >= 1
    assert cases("cure") and all(c["tag"] == "Da controllare a mano" for c in cases("cure"))


def test_04_false_tra_virgolette_e_falso_tranne_la_combinazione_pericolosa(monkeypatch):
    answer(monkeypatch, {"cure": {"da_verificare": "false", "gravita": 1, "spiegazione": "niente"}})
    gateway.run("t")
    cure = cases("cure")
    assert [c["patient_id"] for c in cure] == ["P01"]  # P05 (diuretico + antinfiammatorio) non torna; P01 sì, per la regola fissa
    assert "Regola locale fissa" in cure[0]["action"] and "anticoagulante" in cure[0]["action"]


def test_04_gravita_fuori_scala_scartata(monkeypatch):
    answer(monkeypatch, {"cure": {"da_verificare": True, "gravita": 9, "spiegazione": "x"}})
    gateway.run("t")
    assert all(dict(json.loads(c["parts"]))["Gravità"] <= 24 for c in cases("cure"))


def test_01_agente_in_errore_diventa_caso_da_controllare(monkeypatch):
    answer(monkeypatch, {"cure": RuntimeError("timeout")})
    s = gateway.run("t")
    assert summary(s["run_id"])["a_mano"] >= 2
    assert {c["patient_id"] for c in cases("cure")} == {"P01", "P05"}
    assert all("non ha dato una risposta valida" in c["action"] for c in cases("cure"))


def test_02_il_modello_non_spegne_il_trasporto_urgente(monkeypatch):
    answer(monkeypatch, {"trasporti": {"serve_trasporto": False, "servizio": None, "partenza": None, "motivo": "no"}})
    gateway.run("t")
    c = cases("trasporto", "P05")
    assert c and "Regola locale fissa" in c[0]["action"]
    assert not cases("trasporto", "P10")  # visita tra 5 giorni senza sciopero: lì decide l'agente


def test_03_nessun_servizio_diventa_trasporto_a_mano(monkeypatch):
    with db.tx() as con:
        con.execute("UPDATE transport_services SET notice_days=3")
    gateway.run("t")
    c = cases("trasporto", "P05")[0]
    assert c["tag"] == "Trasporto da organizzare a mano" and json.loads(c["payload"])["service"] is None
    assert actions.execute(c["id"], USER) and cases("trasporto", "P05")[0]["status"] == "ok"


def test_05_errore_della_piattaforma_passa_alla_riserva(monkeypatch):
    monkeypatch.setitem(agents.AGENTS["cure"], "backend", "sim")
    monkeypatch.setitem(agents.AGENTS["cure"], "sim_workflow", "wf")
    monkeypatch.setattr(agents, "SIM_API_KEY", "k")
    sent = []
    monkeypatch.setattr(agents, "_sim_post", lambda wf, body: sent.append(body) or {"success": False, "error": "workflow non pubblicato"})
    monkeypatch.setattr(agents, "_call_api", lambda agent, payload: {"da_verificare": True, "gravita": 2, "spiegazione": "ok"})
    out, backend, _ = real_call("cure", {"tipo": "cure"})
    assert "riserva" in backend and "workflow non pubblicato" in backend and out["gravita"] == 2
    assert len(sent) == 1  # niente secondo invio dello stesso pacchetto (56)


# ------------------------------------------------------------------ privacy (6-9)

def test_06_44_pagina_cloud_dell_accettazione_senza_parte_clinica(client, run):
    login(client, "m.bianchi", "segreteria")
    page = html.unescape(client.get("/elaborazione").text)  # le virgolette nell'HTML arrivano come &#34;: si confronta il testo vero
    assert '"tipo": "trasporto"' in page  # taratura: la ricerca vede i pacchetti quando ci sono
    assert "amiloidosi" not in page and '"tipo": "cure"' not in page and '"tipo": "aderenza"' not in page
    assert "pacchetti clinici di questa elaborazione non sono mostrati" in page
    client.cookies.clear(); _used_codes.clear()
    login(client, "e.gatti", "medico")
    page = html.unescape(client.get("/elaborazione").text)
    assert "amiloidosi" in page and '"tipo": "cure"' in page  # il medico continua a vederli


def test_07_pacchetto_trasporti_sotto_soglia_non_parte():
    with db.tx() as con:
        con.execute("DELETE FROM residents WHERE rowid > 20")
    gateway.run("t")
    with db.tx() as con:
        sent = con.execute("SELECT COUNT(*) FROM packets_out WHERE agent='trasporti'").fetchone()[0]
        low = con.execute("SELECT COUNT(*) FROM gate_checks WHERE agent='trasporti' AND (k < 10 OR l < 3)").fetchone()[0]
    assert low and sent == 0
    c = cases("trasporto", "P05")
    assert c and "non inviato" in c[0]["action"].lower()  # la persona più a rischio non sparisce: decide l'ospedale


def test_08_11_codici_nuovi_e_contenuto_non_rimandato(run):
    gateway.run("t")  # seconda elaborazione dello stesso giorno, stessi dati
    with db.tx() as con:
        first = con.execute("SELECT COUNT(*) FROM packets_out WHERE run_id=(SELECT run_id FROM packets_out ORDER BY rowid LIMIT 1)").fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM packets_out").fetchone()[0]
        aderenza = [json.loads(r[0]) for r in con.execute("SELECT payload FROM packets_out WHERE agent='aderenza'")]
    # la seconda manda solo ciò che è cambiato: con gli stessi dati, niente (i posti liberati non vanno più al cloud)
    assert first > 0 and total == first
    assert aderenza and all("giorni_alla_visita" not in p and p["visita"].startswith(("entro", "oltre", "nessuna")) for p in aderenza)


def test_09_l_diversita_registrata(run):
    with db.tx() as con:
        rows = con.execute("SELECT k, l FROM gate_checks WHERE agent='trasporti'").fetchall()
    assert rows and all(r["l"] is not None and r["l"] >= 3 for r in rows)


# ------------------------------------------------------------------ ciclo dei casi (10-13)

def test_10_elaborazione_dell_accettazione_non_azzera_il_medico(client, run):
    medico = {c["id"] for c in cases() if c["role"] == "medico"}
    login(client, "m.bianchi", "segreteria")
    page = client.get("/elaborazione").text
    client.post("/elabora", data={"csrf_token": tok(page)})
    assert medico == {c["id"] for c in cases(status="pending") if c["role"] == "medico"}


def test_11_caso_deciso_non_rinasce(run):
    c = cases("trasporto", "P05")[0]
    actions.execute(c["id"], USER)
    gateway.run("t")
    assert len(cases("trasporto", "P05")) == 1 and cases("trasporto", "P05")[0]["status"] == "ok"
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM transport_requests WHERE patient_id='P05'").fetchone()[0] == 1


def test_12_doppio_clic_contemporaneo_esegue_una_volta(run):
    c = cases("trasporto", "P05")[0]
    go = threading.Barrier(2)
    out = []

    def click():
        go.wait()
        out.append(actions.execute(c["id"], USER))
    ts = [threading.Thread(target=click) for _ in range(2)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert sorted(o is None for o in out) == [False, True]
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM transport_requests WHERE case_id=?", (c["id"],)).fetchone()[0] == 1


def test_13_non_si_scarta_un_caso_gia_eseguito(client, run):
    login(client, "m.bianchi", "segreteria")
    kit = cases("kit")[0]
    page = client.get(f"/caso/{kit['id']}").text
    client.post(f"/caso/{kit['id']}/approva", data={"csrf_token": tok(page)})
    r = client.post(f"/caso/{kit['id']}/scarta", data={"csrf_token": tok(page), "motivo": "prova"})
    assert cases("kit")[0]["status"] == "ok"
    assert "già stato deciso" in r.text


def test_13_scartare_una_segnalazione_clinica_chiede_il_codice(client, run):
    login(client, "e.gatti", "medico")
    cid = cases("cure", "P01")[0]["id"]
    page = client.get(f"/caso/{cid}").text
    client.post(f"/caso/{cid}/scarta", data={"csrf_token": tok(page), "motivo": "x", "code": "000000"})
    assert cases("cure", "P01")[0]["status"] == "pending"
    _used_codes.clear()
    client.post(f"/caso/{cid}/scarta", data={"csrf_token": tok(page), "motivo": "x", "code": pyotp.TOTP(client.secrets["e.gatti"]).now()})
    assert cases("cure", "P01")[0]["status"] == "scartato"


def test_caso_non_piu_segnalato_diventa_superato_non_sparisce(run, monkeypatch):
    with db.tx() as con:
        con.execute("DELETE FROM answers")  # altrimenti la seconda elaborazione dello stesso giorno riusa la risposta di prima
    answer(monkeypatch, {"agende": lambda p: {"doppione": False, "spiegazione": "no"} if p["tipo"] == "richieste_doppie"
                         else fake_call("agende", p)[0]})
    gateway.run("t")
    assert cases("doppione")[0]["status"] == "superato"


# ------------------------------------------------------------------ trasporti (14-23)

def _approva(pid):
    c = cases("trasporto", pid)[0]
    actions.execute(c["id"], USER)
    with db.tx() as con:
        return con.execute("SELECT id FROM transport_requests WHERE case_id=?", (c["id"],)).fetchone()[0]


def test_14_indirizzo_con_il_quartiere_vero(run):
    _approva("P10")
    with db.tx() as con:
        body = con.execute("SELECT body FROM outbox WHERE subject LIKE 'Richiesta di disponibilit%' ORDER BY id DESC").fetchone()[0]
    assert "via Cecchini 9 (Lido)" in body and "Mestre" not in body and "andata e ritorno" in body


def test_15_orario_controllato_dalla_rotta_e_normalizzato(client, run):
    login(client, "m.bianchi", "segreteria")
    url = f"/trasporti/{_approva('P10')}"
    page = client.get(url).text
    r = client.post(url + "/risposta", data={"csrf_token": tok(page), "esito": "disponibile", "partenza": "99:99"})
    assert "Orario non valido" in r.text
    client.post(url + "/risposta", data={"csrf_token": tok(page), "esito": "disponibile", "partenza": "9:30"})
    client.post(url + "/chiamata", data={"csrf_token": tok(page), "esito": "serve"})
    with db.tx() as con:
        r = con.execute("SELECT status, pickup FROM transport_requests").fetchone()
    assert (r["status"], r["pickup"]) == ("confermato", "09:30")


def test_16_servizio_che_non_risponde_si_cambia_da_solo(run):
    rid = _approva("P10")
    with db.tx() as con:
        con.execute("UPDATE transport_requests SET reply_by='2000-01-01 00:00:00' WHERE id=?", (rid,))
    assert trasporti.scadenze() == 1
    with db.tx() as con:
        r = con.execute("SELECT service_id FROM transport_requests WHERE id=?", (rid,)).fetchone()
        ev = [e[0] for e in con.execute("SELECT text FROM transport_events WHERE request_id=?", (rid,))]
    assert r["service_id"] != "T1" and any("non ha risposto" in e for e in ev)


def test_17_18_tentativi_programmati_e_servizio_avvisato(run):
    rid = _approva("P01")
    trasporti.risposta_servizio(rid, True, "10:00", USER)
    trasporti.esito_chiamata(rid, "non_risponde", USER)
    with db.tx() as con:
        r = con.execute("SELECT status, next_call_at FROM transport_requests WHERE id=?", (rid,)).fetchone()
        assert r["status"] == "richiamare" and r["next_call_at"]
        con.execute("UPDATE transport_requests SET next_call_at='2000-01-01 00:00:00' WHERE id=?", (rid,))
    trasporti.scadenze()
    for _ in range(2):
        trasporti.esito_chiamata(rid, "non_risponde", USER)
    with db.tx() as con:
        st = con.execute("SELECT status FROM transport_requests WHERE id=?", (rid,)).fetchone()[0]
        mail = con.execute("SELECT COUNT(*) FROM outbox WHERE case_id=? AND body LIKE 'Non riusciamo a raggiungere%' AND subject LIKE 'Re:%'", (rid,)).fetchone()[0]
    assert st == "persona_non_raggiunta" and mail == 1


def test_19_regole_del_servizio_e_giorni_lavorativi():
    t1 = {"id": "T1", "distretti": "Nord,Sud", "notice_days": 3, "booking": "telefono lun-ven 9-13", "rules": "Gratuito per persone fragili"}
    giovane = {"distretto": "Nord", "age": 60, "alone": False, "nocar": False}
    fragile = {"distretto": "Nord", "age": 85, "alone": True, "nocar": True}
    assert not trasporti.eligible(t1, giovane, "2026-10-30 10:00")[0]
    assert trasporti.eligible(t1, fragile, "2026-10-27 10:00")[0]  # gio 22 -> mar 27: tre giorni lavorativi
    assert not trasporti.eligible(t1, fragile, "2026-10-26 10:00")[0]  # solo gio e ven


def test_20_sciopero_per_fascia_oraria(run):
    with db.tx() as con:
        assert trasporti.strike_at(con, "2026-10-23 10:20")
        assert not trasporti.strike_at(con, "2026-10-23 19:30")


def test_21_tutte_le_visite_vicine(monkeypatch):
    with db.tx() as con:
        con.execute("INSERT INTO appointments VALUES ('A-99','P10','visita cardiologica','cardiologia','2026-11-10 10:00','pad. B','prenotata')")
    gateway.run("t")
    assert {json.loads(c["payload"])["appointment"] for c in cases("trasporto", "P10")} == {"A-10", "A-99"}


def test_22_visita_spostata_avvisa_il_servizio(run):
    rid = _approva("P10")
    with db.tx() as con:
        con.execute("UPDATE appointments SET at='2026-10-29 15:00' WHERE id='A-10'")
    trasporti.scadenze()
    with db.tx() as con:
        st = con.execute("SELECT status FROM transport_requests WHERE id=?", (rid,)).fetchone()[0]
        mail = con.execute("SELECT COUNT(*) FROM outbox WHERE case_id=? AND body LIKE 'La visita di%' AND subject LIKE 'Re:%'", (rid,)).fetchone()[0]
    assert st == "visita_cambiata" and mail == 1


def test_22_partenza_proposta_dall_agente_usata(monkeypatch):
    answer(monkeypatch, {"trasporti": {"serve_trasporto": True, "servizio": "T2", "partenza": "9:10", "motivo": "x"}})
    gateway.run("t")
    rid = _approva("P05")
    with db.tx() as con:
        assert con.execute("SELECT pickup FROM transport_requests WHERE id=?", (rid,)).fetchone()[0] == "09:10"


def test_23_sciopero_letto_dagli_eventi(run):
    with db.tx() as con:
        con.execute("INSERT INTO external_events VALUES ('X-9','sciopero','2026-10-27','Sciopero dalle 14:00 alle 18:00.')")
        a = dict(con.execute("SELECT * FROM appointments WHERE id='A-10'").fetchone())
        p = dict(con.execute("SELECT * FROM patients WHERE id='P10'").fetchone())
        s = dict(con.execute("SELECT * FROM transport_services WHERE id='T2'").fetchone())
        assert "sciopero" in trasporti.call_script(p, a, s, "14:00", trasporti.strike_at(con, a["at"]))


# ------------------------------------------------------------------ posti e kit (24-29)

def test_24_un_posto_per_persona_per_elaborazione(monkeypatch):
    libera_posto("ecocardiogramma", "2026-10-30 10:00")
    seen = []

    def pick(p):
        seen.append(p)
        return {"codice_scelto": seen[0]["candidati"][0]["codice"], "motivo": "x"}  # il secondo posto sceglie un codice del primo
    answer(monkeypatch, {"agende": lambda p: pick(p) if p["tipo"] == "posto_liberato" else fake_call("agende", p)[0]})
    gateway.run("t")
    posti = cases("posto")
    assert len(posti) == 2 and len({c["patient_id"] for c in posti}) == 2


def test_26_specialita_dal_tipo_di_esame():
    with db.tx() as con:  # prima la specialità era sempre "cardiologia": con l'ecocardiogramma non si vedeva, con una TAC sì
        con.execute("DELETE FROM freed_slots")
        con.execute("UPDATE waitlist SET service='tac_torace'")
    libera_posto("tac_torace", "2026-10-23 09:40", "Ospedale Laguna, pad. A", "FS-1")
    gateway.run("t")
    actions.execute(cases("posto")[0]["id"], USER)
    with db.tx() as con:
        assert con.execute("SELECT specialty FROM appointments WHERE status='da confermare'").fetchone()[0] == "radiologia"


def test_27_nessun_posto_nel_passato():
    with db.tx() as con:
        con.execute("UPDATE freed_slots SET at='2026-10-20 09:40'")
    gateway.run("t")
    assert not cases("posto")


def test_28_un_kit_per_visita(monkeypatch):
    with db.tx() as con:
        con.execute("INSERT INTO notes (id,patient_id,kind,source,date,text) VALUES ('X1','P05','prenotazione','CUP','2026-10-21','Chiede supporto per l udito alla visita.')")
    gateway.run("t")
    assert len(cases("kit", "P05")) == 1


# ------------------------------------------------------------------ lettore (30-36)

def test_30_31_negazioni_e_sospensione_del_medico():
    sig, _ = extract.extract({"id": "N", "kind": "referto", "source": "medicina",
                              "text": "Nega diabete. Escluso scompenso. Non assume anticoagulanti."}, use_llm=False)
    assert not sig
    sig, _ = extract.extract({"id": "N", "kind": "referto", "source": "cardiologia",
                              "text": "Ibuprofene sospeso dal cardiologo per rischio emorragico. Prosegue warfarin."}, use_llm=False)
    got = {(s["field"], s["value"]) for s in sig}
    assert ("aderenza", "sospesa") not in got and ("sospeso_dal_medico", "antinfiammatorio") in got


def test_31_farmaco_sospeso_dal_medico_non_conta_piu():
    with db.tx() as con:
        con.execute("INSERT INTO notes (id,patient_id,kind,source,date,text) VALUES ('X2','P01','referto','ortopedia','2026-10-20',"
                    "'Ibuprofene sospeso dal dott. Bortolato per il rischio di sanguinamento.')")
    gateway.run("t")
    with db.tx() as con:
        cure = [json.loads(r[0]) for r in con.execute("SELECT payload FROM packets_out WHERE agent='cure'")]
    assert not any(f["classe"] == "antinfiammatorio" for p in cure if "anticoagulante" in json.dumps(p) for f in p["farmaci"])


def test_33_34_stesso_reparto_e_tutte_le_condizioni():
    with db.tx() as con:
        con.execute("INSERT INTO notes (id,patient_id,kind,source,date,text) VALUES ('X3','P06','referto','medicina','2026-10-20',"
                    "'Assume warfarin e ibuprofene. Scompenso cardiaco noto. Diabete in terapia.')")
    gateway.run("t")
    with db.tx() as con:
        p = [json.loads(r[0]) for r in con.execute("SELECT payload FROM packets_out WHERE agent='cure'") if '"medicina"' in r[0] and "diabete" in r[0]]
    assert p and {"diabete", "scompenso cardiaco"} <= set(p[0]["condizioni"])


def test_35_modello_locale_on_e_una_prova_sola(monkeypatch):
    monkeypatch.setattr(extract, "LOCAL_LLM_ENABLED", "on")
    assert extract._llm_available()
    calls = []
    monkeypatch.setattr(extract, "LOCAL_LLM_ENABLED", "auto")
    monkeypatch.setattr(extract, "_llm_available", lambda: calls.append(1) or False)
    gateway.run("t")
    assert len(calls) == 1


def test_36_segnali_del_modello_solo_con_la_loro_frase(monkeypatch):
    note = {"id": "N", "kind": "referto", "source": "medicina", "text": "Il paziente è in buona salute."}
    monkeypatch.setattr(extract, "llm", lambda n: [{"field": "condizione", "value": "diabete", "frase": "ha il diabete"},
                                                   {"field": "condizione", "value": "artrosi", "frase": "Il paziente è in buona salute."}])
    sig, _ = extract.extract(note, use_llm=True)
    assert [(s["field"], s["value"]) for s in sig] == [("condizione", "artrosi")]


# ------------------------------------------------------------------ importazione (37-43)

def _doc(name, code, extra=""):
    return f"REFERTO\nTipo documento: Referto\nPaziente: {name}\nC.F.: {code}\nData: 01/10/2026\n{extra}".encode()


def test_37_codice_fiscale_omocodico_accettato():
    base = cf.compute("Rossi", "Mario", "1950-01-01", "M")
    omo15 = base[:14] + "LMNPQRSTUV"[int(base[14])]
    assert cf.valid(omo15 + cf.check_char(omo15))


def test_38_documento_senza_cf_con_altro_nome_va_da_smistare():
    with db.tx() as con:
        r = importa.ingest(con, b"REFERTO\nTipo documento: Referto\nPaziente: Altra Persona\nData: 01/10/2026\nTesto.", "x.txt", "a mano", "e.gatti",
                           forced_pid="P01")
    assert r["esito"] == "da smistare"


def test_39_importato_senza_distretto_ha_il_trasporto():
    code = cf.compute("Neri", "Ada", "1938-05-05", "F")
    with db.tx() as con:
        r = importa.ingest(con, _doc("Ada Neri", code, "Vive sola, non guida."), "a.txt", "t", "t")
        con.execute("INSERT INTO appointments VALUES ('A-X',?,'visita','cardiologia','2026-10-23 09:00','pad. B','prenotata')", (r["patient_id"],))
        assert con.execute("SELECT has_car FROM patients WHERE id=?", (r["patient_id"],)).fetchone()[0] is None  # non noto, non "ha l'auto"
    gateway.run("t")
    assert cases("trasporto", r["patient_id"])


def test_40_cf_che_non_torna_con_il_nome_non_crea_pazienti():
    code = cf.compute("Neri", "Ada", "1938-05-05", "F")
    with db.tx() as con:
        r = importa.ingest(con, _doc("Bruno Verdi", code), "b.txt", "t", "t")
    assert r["esito"] == "da smistare" and "non tornano" in r["motivo"]


def test_41_documento_scartato_si_reimporta():
    data = b"REFERTO\nPaziente: Nessuno\nTesto senza codice fiscale."
    with db.tx() as con:
        r = importa.ingest(con, data, "c.txt", "t", "t")
        con.execute("UPDATE inbox SET status='scartato' WHERE id=?", (r["id"],))
    with db.tx() as con:
        r2 = importa.ingest(con, data, "c.txt", "t", "t")
    assert r2["esito"] == "da smistare" and len(r["id"]) == 14


def test_42_xml_con_entita_rifiutato():
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><ClinicalDocument xmlns="urn:hl7-org:v3">&a;</ClinicalDocument>'
    assert importa._cda_text(bomb).startswith("XML rifiutato")


# ------------------------------------------------------------------ sicurezza e permessi (45-50)

def test_45_registro_dell_accettazione_senza_smistamenti_clinici(client, run):
    with db.tx() as con:
        it = importa.ingest(con, b"REFERTO\nPaziente: Nessuno\nTesto senza codice fiscale.", "d.txt", "t", "e.gatti")
    login(client, "e.gatti", "medico")
    page = client.get(f"/smistare/{it['id']}").text
    client.post(f"/smistare/{it['id']}/assegna", data={"csrf_token": tok(page), "patient_id": "P05"})
    assert "assegnato a Bruno Tonin" in client.get("/impostazioni/log").text  # taratura: il medico la vede
    client.cookies.clear(); _used_codes.clear()
    login(client, "m.bianchi", "segreteria")
    assert "assegnato a Bruno Tonin" not in client.get("/impostazioni/log").text


def test_46_anteprime_nel_registro(client, run):
    login(client, "e.gatti", "medico")
    client.get("/documenti/E-061/pagina/1")
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM access_log WHERE action='apre anteprima pagina'").fetchone()[0] == 1


def test_47_chi_conosce_solo_l_utente_non_blocca_il_medico(client):
    for _ in range(6):
        ok, msg = auth.check_password("e.gatti", "sbagliata", "medico", "10.0.0.66")
    assert not ok and "Troppi tentativi" in msg
    assert auth.check_password("e.gatti", PW, "medico", "10.0.0.1") == (True, "")  # da un'altra postazione entra
    for _ in range(6):
        _, msg2 = auth.check_password("nessuno", "x", "medico", "10.0.0.66")
    assert msg2 == msg  # stesso messaggio: non si capisce se l'utente esiste


def test_48_uscita_revoca_la_sessione(client, run):
    login(client, "m.bianchi", "segreteria")
    page = client.get("/cruscotto").text
    from app.config import SESSION_COOKIE
    copia = client.cookies.get(SESSION_COOKIE)  # una copia del cookie presa mentre si è dentro
    replay = lambda: TestClient(app).get("/cruscotto", headers={"cookie": f"{SESSION_COOKIE}={copia}"}, follow_redirects=False)
    assert replay().status_code == 200  # taratura: prima dell'uscita la copia funziona
    client.post("/logout", data={"csrf_token": tok(page)})
    assert replay().headers.get("location") == "/login"


def test_48_codice_usato_registrato_nel_database(client):
    code = pyotp.TOTP(client.secrets["e.gatti"]).now()
    assert one_time_code_ok("e.gatti", code)[0]
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM used_codes WHERE username='e.gatti'").fetchone()[0] == 1
    assert not one_time_code_ok("e.gatti", code)[0]


def test_49_messaggi_non_dall_indirizzo(client, run):
    login(client, "m.bianchi", "segreteria")
    assert "Chiama il 333" not in client.get("/cruscotto?flash=Chiama il 333").text


def test_50_intestazioni(client):
    r = client.get("/login")
    csp = r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in csp
    assert "googleapis" not in csp and "googleapis" not in r.text


# ------------------------------------------------------------------ robustezza (51-58)

def test_52_due_elaborazioni_insieme_non_partono():
    gateway._run_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="già un'elaborazione"):
            gateway.run("t")
    finally:
        gateway._run_lock.release()


def test_53_elaborazione_interrotta_ha_una_fine(monkeypatch):
    monkeypatch.setattr(gateway, "build_bundles", lambda con, rid: (_ for _ in ()).throw(ValueError("rotto")))
    with pytest.raises(ValueError):
        gateway.run("t")
    with db.tx() as con:
        r = con.execute("SELECT finished_at, summary FROM runs").fetchone()
    assert r["finished_at"] and "rotto" in r["summary"]


def test_57_attestazione_blocca_un_modello_fuori_ue(monkeypatch):
    monkeypatch.setitem(gateway.AGENTS["cure"], "model", "anthropic/claude-sonnet")
    s = gateway.run("t")
    assert s.get("blocked")
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM packets_out").fetchone()[0] == 0


def test_58_calendario_standard():
    ics = actions._ics("Visita", "2026-10-23 09:30", 30, "pad. B", "x")
    assert "BEGIN:VTIMEZONE" in ics and "TZID:Europe/Rome" in ics
    assert [l for l in ics.split("\r\n") if l.startswith("DTSTAMP:")][0].endswith("Z")
