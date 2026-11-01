"""Flussi sulla piattaforma: il workflow (qui finto, con le stesse richieste del vero) legge da Filo e chiede di fare il passo;
Filo controlla. Se il workflow è giù o non applica il passo, Filo lo fa in locale. Un passo non si fa mai due volte."""
import json

import pytest
from fastapi.testclient import TestClient

from app import db, flussi, posta
from app.voce import server
from test_web import _approva_trasporto, client, login, tok  # noqa: F401

voce = TestClient(server.app)


@pytest.fixture
def acceso(monkeypatch):
    monkeypatch.setenv("FILO_FLUSSI", "on")
    monkeypatch.setenv("FILO_URL_PUBBLICO", "https://prova.example")
    for k in flussi.WORKFLOW:
        monkeypatch.setitem(flussi.WORKFLOW, k, "wf-" + k)
    del flussi._thread[:]


def piattaforma(monkeypatch, passi):
    """passi(tipo, ref, get, post): quello che fa il workflow. get/post chiamano Filo con il permesso, come i blocchi API."""
    def finto(tipo, fid, ref):
        perm = flussi.permesso(fid)
        get = lambda path: voce.get(path, headers={"X-Filo-Permesso": perm}).json()
        post = lambda path, corpo: voce.post(path, json=corpo, headers={"X-Filo-Permesso": perm}).json()
        return passi(tipo, ref, get, post)
    monkeypatch.setattr(flussi, "_esegui_workflow", finto)


def aspetta():
    for t in list(flussi._thread):
        t.join(10)


def stato(rid):
    with db.tx() as con:
        return con.execute("SELECT status FROM transport_requests WHERE id=?", (rid,)).fetchone()[0]


def flusso(ref):
    with db.tx() as con:
        return dict(con.execute("SELECT * FROM flussi WHERE ref=? ORDER BY creato DESC LIMIT 1", (ref,)).fetchone())


def _risposta(client, testo):
    url = _approva_trasporto(client, "P10")
    rid = url.rsplit("/", 1)[1]
    esito = posta.gestisci_risposta(rid, "servizio@example.com", "<m1@x>", testo)
    return rid, esito


def test_risposta_letta_dalla_piattaforma(client, run, acceso, monkeypatch):
    visto = {}
    def passi(tipo, ref, get, post):
        visto["dati"] = get(f"/filo/trasporti/{ref}/risposta")
        return post(f"/filo/trasporti/{ref}/esito", {"azione": "disponibile", "orario": "13:50", "motivo": "ok con orario"})
    piattaforma(monkeypatch, passi)
    rid, esito = _risposta(client, "Ok, passiamo alle 13:50, il volontario suona al citofono")
    aspetta()
    assert esito == "alla piattaforma" and stato(rid) == "attesa_paziente" and flusso(rid)["stato"] == "applicato"
    assert visto["dati"]["regole"] == {"esito": "si", "orario": "13:50"} and "13:50" in visto["dati"]["testo"]


def test_al_cloud_il_testo_senza_nome_ne_telefono():
    assert flussi._senza_nomi("Ok per la sig.ra Teresa Furlan, chiamate il 041 555 0105", {"name": "Teresa Furlan"}) == \
        "Ok per la sig.ra [persona] [persona], chiamate il [telefono]"


def test_agente_e_regole_in_disaccordo_decide_l_accettazione(client, run, acceso, monkeypatch):
    piattaforma(monkeypatch, lambda tipo, ref, get, post: post(f"/filo/trasporti/{ref}/esito", {"azione": "non_disponibile", "motivo": "?"}))
    rid, _ = _risposta(client, "Ok, passiamo alle 13:50")
    aspetta()
    assert stato(rid) == "attesa_servizio"  # nessun passo: appunto all'accettazione
    with db.tx() as con:
        assert con.execute("SELECT 1 FROM transport_events WHERE request_id=? AND step='appunto' AND text LIKE '%Controllo fisso%'", (rid,)).fetchone()


def test_workflow_giu_fa_filo_in_locale(client, run, acceso, monkeypatch):
    def giu(tipo, ref, get, post):
        raise RuntimeError("piattaforma non raggiungibile")
    piattaforma(monkeypatch, giu)
    rid, _ = _risposta(client, "Ok, passiamo alle 13:50")
    aspetta()
    assert stato(rid) == "attesa_paziente" and flusso(rid)["stato"] == "locale"


def test_il_passo_non_si_fa_due_volte(client, run, acceso, monkeypatch):
    def due(tipo, ref, get, post):
        post(f"/filo/trasporti/{ref}/esito", {"azione": "disponibile", "orario": "13:50"})
        return post(f"/filo/trasporti/{ref}/esito", {"azione": "disponibile", "orario": "13:50"})
    piattaforma(monkeypatch, due)
    rid, _ = _risposta(client, "Ok, passiamo alle 13:50")
    aspetta()
    with db.tx() as con:
        n = con.execute("SELECT COUNT(*) FROM transport_events WHERE request_id=? AND step='risposta servizio'", (rid,)).fetchone()[0]
    assert n == 1 and flusso(rid)["stato"] == "applicato"


def test_permesso_di_un_altro_flusso_rifiutato(client, run, acceso, monkeypatch):
    piattaforma(monkeypatch, lambda tipo, ref, get, post: get("/filo/trasporti/TR-altra/risposta"))
    rid, _ = _risposta(client, "Ok, passiamo alle 13:50")
    aspetta()
    assert flusso(rid)["stato"] == "locale"  # il workflow non ha potuto leggere: ha fatto Filo
    assert voce.get(f"/filo/trasporti/{rid}/risposta").status_code == 403


def test_posto_liberato_scelto_dal_flusso(client, run, acceso, monkeypatch):
    def passi(tipo, ref, get, post):
        p = json.loads(get(f"/filo/posti/{ref}/candidati")["pacchetto"])
        assert all("name" not in c for c in p["candidati"])
        return post(f"/filo/posti/{ref}/proposta", {"risposta": {"content": json.dumps({"codice_scelto": p["candidati"][0]["codice"], "motivo": "test"})}})
    piattaforma(monkeypatch, passi)
    login(client, "m.bianchi", "segreteria")
    page = client.get("/posti").text
    client.post("/posti/disdetta-prova", data={"csrf_token": tok(page)})
    aspetta()
    with db.tx() as con:
        sid = con.execute("SELECT id FROM freed_slots ORDER BY freed_at DESC, rowid DESC LIMIT 1").fetchone()[0]
        casi = con.execute("SELECT 1 FROM cases WHERE kind='posto' AND status='pending' AND json_extract(payload,'$.slot')=?", (sid,)).fetchall()
        scelta = con.execute("SELECT text FROM slot_events WHERE slot_id=? AND step='scelta'", (sid,)).fetchone()
    assert len(casi) == 1 and "flusso posto liberato" in scelta[0] and flusso(sid)["stato"] == "applicato"


def test_esito_della_chiamata_dal_flusso(client, run, acceso, monkeypatch):
    from app.voce import chiamata as ch
    piattaforma(monkeypatch, lambda t, r, get, post: (get(f"/filo/chiamate/{r}/esito"), post(f"/filo/chiamate/{r}/applica", {"azione": "serve"}))[1])
    url = _approva_trasporto(client, "P10")
    rid = url.rsplit("/", 1)[1]
    monkeypatch.setitem(flussi.WORKFLOW, "risposta_servizio", "")  # la risposta del servizio qui la registra Filo
    from app import trasporti
    trasporti.risposta_servizio(rid, True, "13:50", {"username": "t", "name": "t"})
    with db.tx() as con:
        cid = con.execute("SELECT id FROM calls WHERE ref_id=? AND kind='trasporto'", (rid,)).fetchone()[0]
        anno = con.execute("SELECT p.birth_year FROM calls c JOIN patients p ON p.id=c.patient_id WHERE c.id=?", (cid,)).fetchone()[0]
    ch.esegui(cid, "verifica_identita", {"anno_nascita": anno})
    ch.esegui(cid, "conferma_trasporto", {"serve": True})
    ch.concludi(cid)
    aspetta()
    assert stato(rid) == "confermato" and flusso(cid)["stato"] == "applicato"


def test_esito_chiamata_diverso_da_quello_registrato_non_passa(client, run, acceso, monkeypatch):
    from app.voce import chiamata as ch
    from app import trasporti
    piattaforma(monkeypatch, lambda t, r, get, post: post(f"/filo/chiamate/{r}/applica", {"azione": "non_serve"}))
    monkeypatch.setitem(flussi.WORKFLOW, "risposta_servizio", "")
    rid = _approva_trasporto(client, "P10").rsplit("/", 1)[1]
    trasporti.risposta_servizio(rid, True, "13:50", {"username": "t", "name": "t"})
    with db.tx() as con:
        cid = con.execute("SELECT id FROM calls WHERE ref_id=? AND kind='trasporto'", (rid,)).fetchone()[0]
        anno = con.execute("SELECT p.birth_year FROM calls c JOIN patients p ON p.id=c.patient_id WHERE c.id=?", (cid,)).fetchone()[0]
    ch.esegui(cid, "verifica_identita", {"anno_nascita": anno})
    ch.esegui(cid, "conferma_trasporto", {"serve": True})
    ch.concludi(cid)
    aspetta()
    assert stato(rid) == "confermato" and flusso(cid)["stato"] == "locale"  # il workflow chiedeva l'opposto: ha deciso l'esito registrato
