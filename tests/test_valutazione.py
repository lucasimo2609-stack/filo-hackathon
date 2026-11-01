"""Valutazione del medico a fine visita: urgenza, motivi facoltativi."""
import html
import json

import pytest
from fastapi.testclient import TestClient

from app import auth, db, gateway
from app.auth import hash_password
from app.main import _used_codes, app
from test_web import PW, login, tok


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


def case(pid, kind):
    with db.tx() as con:
        return dict(con.execute("SELECT * FROM cases WHERE patient_id=? AND kind=?", (pid, kind)).fetchone())


def parts(c):
    return dict(json.loads(c["parts"]))


def valuta(client, pid, urgenza, motivi, **extra):
    page = client.get(f"/pazienti/{pid}").text
    data = {"csrf_token": tok(page), "urgenza": urgenza, "motivi": motivi, **extra}
    r = client.post(f"/pazienti/{pid}/valutazione", data=data)
    r.page = html.unescape(r.text)  # gli apostrofi nell'HTML arrivano come &#39;
    return r


def saved(pid):
    with db.tx() as con:
        return con.execute("SELECT COUNT(*) FROM visit_assessments WHERE patient_id=?", (pid,)).fetchone()[0]


def test_valutazione_entra_nel_punteggio_del_medico(client, run):
    login(client, "e.gatti", "medico")
    prima = case("P01", "cure")
    assert "Valutazione del medico" not in parts(prima)  # taratura: prima non c'è
    r = valuta(client, "P01", "alta", ["peggioramento", "politerapia", "cadute"], nota="Rivedere tra una settimana")
    assert "Valutazione registrata" in r.text and saved("P01") == 1
    dopo = case("P01", "cure")
    assert parts(dopo)["Valutazione del medico"] == 20 and dopo["score"] == min(100, prima["score"] + 20)
    pagina = client.get(f"/caso/{dopo['id']}").text
    assert "Rischio di caduta" in pagina and "Rivedere tra una settimana" in pagina


def test_resta_dopo_la_prossima_elaborazione_e_si_puo_cambiare(client, run):
    login(client, "e.gatti", "medico")
    valuta(client, "P01", "alta", ["peggioramento", "politerapia", "cadute"])
    gateway.run("t")
    assert parts(case("P01", "cure"))["Valutazione del medico"] == 20
    valuta(client, "P01", "bassa", ["solo", "accesso", "comunicazione"])
    assert parts(case("P01", "cure"))["Valutazione del medico"] == 0 and saved("P01") == 2


def test_basta_l_urgenza_i_motivi_sono_facoltativi(client, run):
    login(client, "e.gatti", "medico")
    r = valuta(client, "P01", "media", [])
    assert "Valutazione registrata" in r.text and saved("P01") == 1
    assert parts(case("P01", "cure"))["Valutazione del medico"] == 10


def test_motivo_o_urgenza_non_previsti_rifiutati(client, run):
    login(client, "e.gatti", "medico")
    assert "non previsto" in valuta(client, "P01", "alta", ["peggioramento", "cadute", "inventato"]).page
    assert "Scegli l'urgenza" in valuta(client, "P01", "altissima", ["peggioramento", "cadute", "solo"]).page
    assert "non è di questo paziente" in valuta(client, "P01", "alta", ["peggioramento", "cadute", "solo"], visita="A-05").page
    assert saved("P01") == 0


def test_l_accettazione_non_la_registra_e_non_la_vede(client, run):
    login(client, "e.gatti", "medico")
    valuta(client, "P01", "alta", ["peggioramento", "cadute", "solo"])
    client.cookies.clear(); _used_codes.clear()
    login(client, "m.bianchi", "segreteria")
    page = client.get("/cruscotto").text
    r = client.post("/pazienti/P01/valutazione", data={"csrf_token": tok(page), "urgenza": "bassa", "motivi": ["solo", "accesso", "cadute"]})
    assert r.status_code == 403 and saved("P01") == 1
    gateway.run("t")
    t = case("P01", "trasporto")
    assert "Valutazione del medico" not in parts(t)
    pagina = client.get(f"/caso/{t['id']}").text
    assert "Rischio di caduta" not in pagina and "Valutazione del medico" not in pagina
