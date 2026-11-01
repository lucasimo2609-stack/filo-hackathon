"""Agente kit sulla piattaforma: il workflow (qui finto, con le stesse richieste del vero) legge da Filo e chiede il passo;
Filo controlla con regole fisse. Workflow giù o passo non applicato: Filo lo fa in locale. Al cloud niente nome né indirizzo."""
import json
import re

import pytest

from app import db, flussi, flussi_kit, kit
from test_flussi import acceso, aspetta, flusso, piattaforma  # noqa: F401
from test_kit_visita import _apri, prenotazione, trascrittore  # noqa: F401
from test_web import client, login, tok  # noqa: F401


def _caso_kit(con):
    con.execute("DELETE FROM kit_bookings")
    con.execute("INSERT INTO cases (id, run_id, patient_id, role, kind, score, summary, action, evidence, payload, status) "
                "VALUES ('CK1','r','P05','segreteria','kit',5,'udito','kit','[]',?, 'pending')", (json.dumps({"appointment": "A-05"}),))


def _approva_kit():
    from app import actions
    with db.tx() as con:
        _caso_kit(con)
    return actions.execute("CK1", {"username": "m.bianchi", "name": "Marta", "role": "segreteria"})


def _prenotato():
    with db.tx() as con:
        return dict(con.execute("SELECT * FROM kit_bookings WHERE appointment_id='A-05'").fetchone())


def test_kit_scelto_dall_agente(acceso, monkeypatch):
    visto = {}
    def passi(tipo, ref, get, post):
        visto["dati"] = get(f"/filo/kit/casi/{ref}/visita")
        return post(f"/filo/kit/casi/{ref}/riserva", {"kit": "K2", "consiglio": "Parlare lentamente, guardando la persona."})
    piattaforma(monkeypatch, passi)
    righe = _approva_kit()
    aspetta()
    b = _prenotato()
    assert "agente kit" in righe[0] and flusso("CK1")["stato"] == "applicato"
    assert b["kit_id"] == "K2" and "Parlare lentamente" in b["nota"]
    d = json.dumps(visto["dati"], ensure_ascii=False)
    assert "Tonin" not in d and "Bruno" not in d and "@" not in d and [k["kit"] for k in visto["dati"]["kit_liberi"]] == ["K1", "K2", "K3"]


def test_kit_inventato_o_consiglio_con_dati_corretto_da_filo(acceso, monkeypatch):
    piattaforma(monkeypatch, lambda tipo, ref, get, post: post(f"/filo/kit/casi/{ref}/riserva",
                                                               {"kit": "K9", "consiglio": "Chiamare la figlia al 3331234567"}))
    _approva_kit()
    aspetta()
    b = _prenotato()
    assert b["kit_id"] == "K1" and "333" not in b["nota"]
    with db.tx() as con:
        log = " ".join(r[0] for r in con.execute("SELECT text FROM action_log WHERE case_id='CK1' AND username='agente kit'"))
    assert "non libero o inesistente" in log and "consiglio scartato" in log


def test_workflow_giu_prenota_filo(acceso, monkeypatch):
    def giu(tipo, fid, ref):
        raise RuntimeError("piattaforma irraggiungibile")
    monkeypatch.setattr(flussi, "_esegui_workflow", giu)
    _approva_kit()
    aspetta()
    assert _prenotato()["kit_id"] == "K1" and flusso("CK1")["stato"] == "locale"


def test_spento_come_prima():
    righe = _approva_kit()
    assert righe[0].startswith("Kit 1 riservato") and _prenotato()["kit_id"] == "K1"


def test_permesso_di_un_altro_flusso_rifiutato(acceso, monkeypatch):
    esiti = {}
    def passi(tipo, ref, get, post):
        esiti["altro"] = post("/filo/kit/casi/ALTRO/riserva", {"kit": "K2"})
        return {}
    piattaforma(monkeypatch, passi)
    _approva_kit()
    aspetta()
    assert "permesso" in json.dumps(esiti["altro"]) and flusso("CK1")["stato"] == "locale"


# ------------------------------------------------------------------ trascrizione alla persona

def _visita_chiusa(client, prenotazione):
    vid, pag = _apri(client, prenotazione)
    csrf = tok(pag.text)
    client.post(f"/kit/visita/{vid}/audio", data={"csrf_token": csrf, "seq": 1}, files={"audio": ("f", b"x", "audio/webm")})
    client.post(f"/kit/visita/{vid}/audio", data={"csrf_token": csrf, "seq": 2}, files={"audio": ("f", b"x", "audio/webm")})
    client.post(f"/kit/visita/{vid}/chiudi", data={"csrf_token": csrf})
    return vid, csrf


def _mails(vid):
    with db.tx() as con:
        return [r[0] for r in con.execute("SELECT body FROM outbox WHERE case_id=?", (vid,))]


@pytest.fixture
def due_frasi(monkeypatch):
    from app import kit_visita
    frasi = iter(["prenda la pastiglia da 5 mg ogni mattina dopo colazione", "il signor Tonin torna tra tre mesi"])
    monkeypatch.setenv("ELEVENLABS_API_KEY", "finta")
    monkeypatch.setattr(kit_visita, "_chiedi", lambda audio, mime: next(frasi))


def test_trascrizione_resa_leggibile_dall_agente(client, prenotazione, due_frasi, acceso, monkeypatch):
    visto = {}
    def passi(tipo, ref, get, post):
        visto["dati"] = get(f"/filo/kit/visite/{ref}/trascrizione")
        return post(f"/filo/kit/visite/{ref}/invia", {"testo": "Prenda la pastiglia da 5 mg ogni mattina, dopo colazione.\n"
                                                                "Il signor [persona] torna tra tre mesi."})
    piattaforma(monkeypatch, passi)
    vid, csrf = _visita_chiusa(client, prenotazione)
    client.post(f"/kit/visita/{vid}/invia", data={"csrf_token": csrf, "email": "nuova@esempio.it"})
    aspetta()
    d = json.dumps(visto["dati"], ensure_ascii=False)
    assert "Tonin" not in d and "@" not in d and "[persona]" in d  # al cloud niente nome né indirizzo
    m = _mails(vid)  # Filo rimette il nome e manda il testo dell'agente
    assert len(m) == 1 and "dopo colazione.\nIl signor Tonin torna tra tre mesi." in m[0] and flusso(vid)["stato"] == "applicato"


def test_nome_tolto_dall_agente_scartato(client, prenotazione, due_frasi, acceso, monkeypatch):
    piattaforma(monkeypatch, lambda tipo, ref, get, post: post(f"/filo/kit/visite/{ref}/invia", {
        "testo": "Prenda la pastiglia da 5 mg ogni mattina, dopo colazione. Il signore torna tra tre mesi."}))
    vid, csrf = _visita_chiusa(client, prenotazione)
    client.post(f"/kit/visita/{vid}/invia", data={"csrf_token": csrf, "email": "nuova@esempio.it"})
    aspetta()
    m = _mails(vid)
    assert len(m) == 1 and re.search(r"\[\d\d:\d\d\] il signor Tonin torna", m[0])  # parte la trascrizione così com'è


def test_dose_cambiata_dall_agente_scartata(client, prenotazione, due_frasi, acceso, monkeypatch):
    piattaforma(monkeypatch, lambda tipo, ref, get, post: post(f"/filo/kit/visite/{ref}/invia", {
        "testo": "Prenda la pastiglia da 50 mg ogni mattina, dopo colazione. Il signor [persona] torna tra tre mesi."}))
    vid, csrf = _visita_chiusa(client, prenotazione)
    client.post(f"/kit/visita/{vid}/invia", data={"csrf_token": csrf, "email": "nuova@esempio.it"})
    aspetta()
    m = _mails(vid)
    assert len(m) == 1 and "50 mg" not in m[0] and "da 5 mg" in m[0]


def test_controllo_del_testo():
    frasi = ["prenda la pastiglia da 5 mg ogni mattina dopo colazione", "torna tra tre mesi per il controllo"]
    ok = "Prenda la pastiglia da 5 mg ogni mattina, dopo colazione. Torna tra tre mesi per il controllo."
    assert flussi_kit.controlla_testo(frasi, ok) == ok
    assert flussi_kit.controlla_testo(frasi, "Prenda la pastiglia ogni mattina. Torna per il controllo.") is None  # riassunto
    assert flussi_kit.controlla_testo(frasi, ok + " Non serve altro, stia tranquillo e riposi bene ogni giorno.") is None  # aggiunta
    assert flussi_kit.controlla_testo(frasi, ok.replace("tre mesi", "due settimane")) is None  # cambiato
    assert flussi_kit.controlla_testo(frasi, "") is None


def test_invio_workflow_giu_parte_lo_stesso(client, prenotazione, due_frasi, acceso, monkeypatch):
    def giu(tipo, fid, ref):
        raise RuntimeError("piattaforma irraggiungibile")
    monkeypatch.setattr(flussi, "_esegui_workflow", giu)
    vid, csrf = _visita_chiusa(client, prenotazione)
    client.post(f"/kit/visita/{vid}/invia", data={"csrf_token": csrf, "email": "nuova@esempio.it"})
    aspetta()
    assert len(_mails(vid)) == 1 and flusso(vid)["stato"] == "locale"


def test_ricomponi_nome_solo_se_i_segnaposto_tornano():
    assert flussi_kit.ricomponi("Il signor [persona] torna.", ["Tonin"]) == "Il signor Tonin torna."
    assert flussi_kit.ricomponi("[persona] [persona], torni tra un mese.", ["Bruno", "Tonin"]) == "Bruno Tonin, torni tra un mese."
    assert flussi_kit.ricomponi("Il signore torna.", ["Tonin"]) is None  # segnaposto tolto: il nome sparirebbe
    assert flussi_kit.ricomponi("[persona] e [persona] tornano.", ["Tonin"]) is None  # segnaposto in più: chi sarebbe?
    assert flussi_kit.ricomponi("Chiami il [telefono].", []) is None


def test_nessun_kit_libero():
    dati = {"kit_liberi": []}
    assert flussi_kit.controlla_scelta(dati, None, "")[0] is None
    assert "nessun kit libero" in flussi_kit.controlla_scelta(dati, "K1", "")[2]
