"""Agente vocale: controlli fissi di Filo e cervello della voce, con un modello finto (niente rete)."""
import json

import pytest
from fastapi.testclient import TestClient

from app import actions, db, gateway, trasporti
from app.config import voce_token
from app.voce import chiamata as ch
from app.voce import server

U = {"username": "m.bianchi", "name": "Marta Bianchi"}


def _call_trasporto():
    gateway.run("t")
    with db.tx() as con:
        cid = con.execute("SELECT id FROM cases WHERE kind='trasporto' AND patient_id='P05'").fetchone()[0]
    actions.execute(cid, U)
    with db.tx() as con:
        rid = con.execute("SELECT id FROM transport_requests WHERE patient_id='P05'").fetchone()[0]
    trasporti.risposta_servizio(rid, True, "09:20", U)
    with db.tx() as con:
        return ch.crea(con, "P05", "trasporto", rid, U), rid


def test_prima_dell_identita_il_modello_non_sa_nulla_della_visita():
    cid, _ = _call_trasporto()
    with db.tx() as con:
        c = ch.carica(con, cid)
    p = ch.prompt(c).lower()
    assert "anno di nascita" in p
    for parola in ("cardiolog", "10:20", "croce verde", "padiglione", "pad. b", "1935"):
        assert parola not in p
    assert [t["function"]["name"] for t in ch.strumenti(c)] == ["verifica_identita", "passa_operatore"]


def test_identita_controllata_da_filo():
    cid, _ = _call_trasporto()
    assert ch.esegui(cid, "conferma_trasporto", {"serve": True})["errore"]  # non permesso prima dell'identità
    assert ch.esegui(cid, "verifica_identita", {"anno_nascita": 1950})["esito"] == "non corrisponde"
    assert ch.esegui(cid, "verifica_identita", {"anno_nascita": "boh"})["esito"] == "non capito"
    assert ch.esegui(cid, "verifica_identita", {"anno_nascita": 1951})["esito"] == "non confermata"
    with db.tx() as con:
        assert ch.carica(con, cid)["status"] == "identita_non_confermata"


def test_trasporto_confermato_dalla_chiamata_avanza_la_pratica():
    cid, rid = _call_trasporto()
    assert ch.esegui(cid, "verifica_identita", {"anno_nascita": 1935})["esito"] == "confermata"
    with db.tx() as con:
        assert "cardiolog" in ch.prompt(ch.carica(con, cid)).lower()
    assert ch.esegui(cid, "conferma_trasporto", {"serve": True, "orario_partenza": "03:00"})["esito"] == "orario non possibile"
    assert ch.esegui(cid, "conferma_trasporto", {"serve": True, "orario_partenza": "9.15"})["esito"] == "registrato"
    ch.concludi(cid)
    with db.tx() as con:
        r = con.execute("SELECT status, pickup FROM transport_requests WHERE id=?", (rid,)).fetchone()
        assert r["status"] == "confermato" and r["pickup"] == "09:15"
        assert con.execute("SELECT COUNT(*) FROM outbox WHERE case_id=? AND ics IS NOT NULL", (rid,)).fetchone()[0] == 1
    assert ch.concludi(cid) == []  # una seconda chiusura non rifà nulla


@pytest.mark.parametrize("frase,atteso", [
    ("ho un dolore forte al petto da stamattina", "dolore al petto"),
    ("mi manca il fiato", "fatica a respirare"),
    ("ieri sono caduta in bagno", "caduta"),
    ("sto bene, grazie", None),
    ("ho preso le pastiglie per la pressione", None),
])
def test_segnali_allarme(frase, atteso):
    assert ch.allarme_in(frase) == atteso


def test_controllo_registra_per_il_medico():
    with db.tx() as con:
        cid = ch.crea(con, "P02", "controllo", None, {"username": "e.gatti", "name": "Dott.ssa Elena Gatti"})
    ch.esegui(cid, "verifica_identita", {"anno_nascita": 1947})
    assert ch.esegui(cid, "registra_stato", {"come_sta": "benone", "medicine": "le_prende", "vuole_richiamata_medico": False})["errore"]
    assert ch.esegui(cid, "registra_stato", {"come_sta": "peggio", "medicine": "non_le_prende", "disturbi": "capogiri",
                                             "vuole_richiamata_medico": True})["esito"] == "registrato"
    ch.concludi(cid)
    with db.tx() as con:
        c = con.execute("SELECT * FROM cases WHERE id=?", (cid + "-ST",)).fetchone()
    assert c["role"] == "medico" and c["score"] == 70 and "capogiri" in c["summary"]


# ------------------------------------------------------------------ cervello della voce con modello finto

class _D:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _TC:
    def __init__(self, i, name, args):
        self.index, self.id, self.function = i, f"c{i}", _Fn(name, args)


class _Chunk:
    def __init__(self, d):
        self.choices = [type("C", (), {"delta": d})()]


class FakeLLM:
    """Primo giro: chiama verifica_identita; secondo giro: parla."""

    def __init__(self):
        self.calls = []
        self.chat = self
        self.completions = self

    async def create(self, **kw):
        self.calls.append(kw)
        tools_done = any(m.get("role") == "tool" for m in kw["messages"])

        async def gen():
            if not tools_done:
                yield _Chunk(_D(tool_calls=[_TC(0, "verifica_identita", '{"anno_nascita": 1935}')]))
            else:
                for piece in ["Grazie. ", "Domani ha la visita.".encode().decode("latin-1")]:  # accenti rotti come dalla AI Factory
                    yield _Chunk(_D(content=piece))
        return gen()


def _sse(text):
    out, tools = "", []
    for line in text.splitlines():
        if line.startswith("data: {"):
            d = json.loads(line[6:])["choices"][0]["delta"]
            out += d.get("content") or ""
            tools += [t["function"]["name"] for t in d.get("tool_calls") or []]
    return out, tools


def test_cervello_della_voce_esegue_gli_strumenti_in_locale(monkeypatch):
    cid, _ = _call_trasporto()
    fake = FakeLLM()
    monkeypatch.setattr(server, "client", lambda: fake)
    c = TestClient(server.app)
    body = {"model": "x", "stream": True, "messages": [{"role": "user", "content": "Sono del 1935"}],
            "elevenlabs_extra_body": {"call_id": cid}}
    assert c.post("/v1/chat/completions", json=body).status_code == 401
    r = c.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {voce_token()}"})
    text, tools = _sse(r.text)
    assert "Grazie" in text and tools == []
    assert len(fake.calls) == 2  # strumento eseguito da Filo, poi seconda risposta
    with db.tx() as con:
        assert ch.carica(con, cid)["identity_ok"] == 1


def test_allarme_risposta_fissa_senza_modello(monkeypatch):
    cid, _ = _call_trasporto()
    fake = FakeLLM()
    monkeypatch.setattr(server, "client", lambda: fake)
    r = TestClient(server.app).post("/v1/chat/completions", headers={"Authorization": f"Bearer {voce_token()}"},
                                    json={"messages": [{"role": "user", "content": "non riesco a respirare"}], "elevenlabs_extra_body": {"call_id": cid}})
    text, _ = _sse(r.text)
    assert "112" in text and fake.calls == []
    with db.tx() as con:
        assert con.execute("SELECT role, score FROM cases WHERE id=?", (cid + "-AL",)).fetchone()[:] == ("medico", 100)


def test_riparazione_accenti():
    a = server.Accenti()
    rotto = "leggerà perché è così".encode().decode("latin-1")
    assert "".join(a.feed(ch_) for ch_ in rotto) + a.flush() == "leggerà perché è così"


def test_firma_del_webhook():
    import hashlib, hmac, time
    raw = '{"data":{}}'
    t = str(int(time.time()))
    sig = "t=" + t + ",v0=" + hmac.new(b"segreto", f"{t}.{raw}".encode(), hashlib.sha256).hexdigest()
    assert server.verifica_firma(raw, sig, "segreto")
    assert not server.verifica_firma(raw, sig, "altro")
    assert not server.verifica_firma(raw, "t=1,v0=abc", "segreto")


class FakeDichiara:
    """Il modello dice 'ho segnato tutto' senza chiamare lo strumento; alla richiesta forzata lo chiama."""

    def __init__(self):
        self.calls = []
        self.chat = self
        self.completions = self

    async def create(self, **kw):
        self.calls.append(kw)
        if kw.get("tool_choice"):
            tc = type("TC", (), {"function": type("F", (), {"name": "conferma_trasporto", "arguments": '{"serve": true}'})()})()
            msg = type("M", (), {"tool_calls": [tc], "content": None})()
            return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

        async def gen():
            yield _Chunk(_D(content="Perfetto, ho segnato tutto."))
        return gen()


def test_rete_di_sicurezza_se_dice_di_aver_registrato(monkeypatch):
    cid, rid = _call_trasporto()
    ch.esegui(cid, "verifica_identita", {"anno_nascita": 1935})
    fake = FakeDichiara()
    monkeypatch.setattr(server, "client", lambda: fake)
    TestClient(server.app).post("/v1/chat/completions", headers={"Authorization": f"Bearer {voce_token()}"},
                                json={"messages": [{"role": "user", "content": "Sì mi serve il passaggio"}], "elevenlabs_extra_body": {"call_id": cid}})
    assert any(k.get("tool_choice") for k in fake.calls)
    with db.tx() as con:
        assert ch.carica(con, cid)["outcome"]["trasporto"]["serve"] is True


def test_mail_cup_dopo_posto_accettato_al_telefono():
    with db.tx() as con:
        cid = con.execute("SELECT id FROM cases WHERE kind='posto'").fetchone()
    if cid is None:
        gateway.run("t")
        with db.tx() as con:
            cid = con.execute("SELECT id FROM cases WHERE kind='posto'").fetchone()
    actions.execute(cid[0], U)
    with db.tx() as con:
        a = con.execute("SELECT id, patient_id FROM appointments WHERE status='da confermare'").fetchone()
        call = ch.crea(con, a["patient_id"], "appuntamento", a["id"], U)
    with db.tx() as con:
        anno = con.execute("SELECT birth_year FROM patients WHERE id=?", (a["patient_id"],)).fetchone()[0]
    ch.esegui(call, "verifica_identita", {"anno_nascita": anno})
    ch.esegui(call, "conferma_appuntamento", {"scelta": "accetta", "serve_passaggio": False})
    ch.concludi(call)
    with db.tx() as con:
        m = con.execute("SELECT * FROM outbox WHERE case_id=?", (call,)).fetchone()
    assert m["subject"].startswith("Conferma prenotazione n. 2026") and "@example.com" in m["recipient"]
    for pezzo in ("Codice prenotazione", "Impegnativa (NRE)", "Cosa portare", "2 GIORNI LAVORATIVI", "*****"):
        assert pezzo in m["html"]
