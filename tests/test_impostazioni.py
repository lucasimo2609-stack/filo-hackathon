"""Rotellina delle impostazioni: profilo, aspetto (chiaro/scuro), log. "Ospedale e cloud" e "Registro" non sono più nel menu."""
import re

import pytest
from fastapi.testclient import TestClient

from app import auth, db
from app.auth import hash_password
from app.main import _used_codes, app
from test_web import PW, login, tok


@pytest.fixture
def client():
    secrets_ = {}
    with db.tx() as con:
        for u in ("m.bianchi", "e.gatti"):
            salt, h = hash_password(PW)
            con.execute("UPDATE users SET name=?, pw_salt=?, pw_hash=?, failed=0, locked_until=NULL, theme=NULL, first_name=NULL WHERE username=?",
                        ("Dott.ssa Elena Gatti" if u == "e.gatti" else "Marta Bianchi", salt, h, u))
            secrets_[u] = con.execute("SELECT totp_secret FROM users WHERE username=?", (u,)).fetchone()[0]
    _used_codes.clear()
    getattr(auth, "_pw_fails", {}).clear()
    c = TestClient(app)
    c.secrets = secrets_
    return c


def nav(html):
    return re.search(r"<nav class=\"side\"[^>]*>(.*?)</nav>", html, re.S).group(1)  # il menu laterale


def test_menu_senza_ospedale_e_cloud_ne_registro_e_con_la_rotellina(client, run):
    for user, role in (("m.bianchi", "segreteria"), ("e.gatti", "medico")):
        client.cookies.clear(); _used_codes.clear()
        login(client, user, role)
        page = client.get("/cruscotto").text
        assert "Posta in uscita" in nav(page)  # taratura: il menu c'è
        assert "Ospedale e cloud" not in nav(page) and "Registro" not in nav(page) and "/elaborazione" not in page
        assert 'href="/impostazioni" class="gear' in page


def test_vecchi_indirizzi_rimandano_alle_impostazioni(client, run):
    login(client, "e.gatti", "medico")
    assert client.get("/registro", follow_redirects=False).headers["location"] == "/impostazioni/log"
    assert client.get("/elaborazione", follow_redirects=False).headers["location"] == "/impostazioni/invii"
    assert "Accessi" in client.get("/impostazioni/log").text and "Controllo di anonimato" in client.get("/impostazioni/invii").text


def test_elabora_torna_al_cruscotto_con_il_riepilogo(client):
    login(client, "m.bianchi", "segreteria")
    page = client.get("/cruscotto").text
    r = client.post("/elabora", data={"csrf_token": tok(page)})
    assert r.url.path == "/cruscotto" and "Novità elaborate" in r.text


def test_profilo_salvato_e_nome_aggiornato(client):
    login(client, "e.gatti", "medico")
    page = client.get("/impostazioni").text
    assert 'value="Elena"' in page and 'value="Gatti"' in page  # prima volta: ricavati dal nome mostrato
    r = client.post("/impostazioni/profilo", data={"csrf_token": tok(page), "title": "Dott.ssa", "first_name": "Elena", "last_name": "Gatti Rossi",
                                                    "qualifica": "Medicina interna", "struttura": "Ospedale Laguna", "reparto": "COT"})
    assert "Profilo salvato" in r.text and "Dott.ssa Elena Gatti Rossi" in r.text and 'value="Medicina interna"' in r.text
    with db.tx() as con:
        u = con.execute("SELECT name, qualifica, struttura, reparto FROM users WHERE username='e.gatti'").fetchone()
    assert tuple(u) == ("Dott.ssa Elena Gatti Rossi", "Medicina interna", "Ospedale Laguna", "COT")


def test_profilo_senza_cognome_o_con_titolo_inventato_rifiutato(client):
    login(client, "e.gatti", "medico")
    page = client.get("/impostazioni").text
    r = client.post("/impostazioni/profilo", data={"csrf_token": tok(page), "title": "", "first_name": "Elena", "last_name": "  "})
    assert "obbligatori" in r.text
    r = client.post("/impostazioni/profilo", data={"csrf_token": tok(page), "title": "Prof.", "first_name": "Elena", "last_name": "Gatti"})
    assert "non previsto" in r.text
    with db.tx() as con:
        assert con.execute("SELECT name FROM users WHERE username='e.gatti'").fetchone()[0] == "Dott.ssa Elena Gatti"


def test_tema_scuro_per_utente(client):
    login(client, "m.bianchi", "segreteria")
    page = client.get("/impostazioni").text
    assert 'data-theme="auto"' in page
    r = client.post("/impostazioni/tema", data={"csrf_token": tok(page), "theme": "dark"})
    assert 'data-theme="dark"' in r.text
    assert 'data-theme="dark"' in client.get("/cruscotto").text  # vale su tutte le pagine
    assert client.post("/impostazioni/tema", data={"csrf_token": tok(page), "theme": "viola"}).status_code == 400
    client.cookies.clear(); _used_codes.clear()
    login(client, "e.gatti", "medico")
    assert 'data-theme="auto"' in client.get("/cruscotto").text  # il tema è di chi lo sceglie
