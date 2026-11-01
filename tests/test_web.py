"""Accesso con doppia autenticazione e separazione dei ruoli."""
import re

import pyotp
import pytest
from fastapi.testclient import TestClient

from app import db
from app.auth import hash_password
from app.main import app, _used_codes

PW = "prova-Prova-123"


@pytest.fixture
def client():
    secrets = {}
    with db.tx() as con:
        for u in ("m.bianchi", "e.gatti"):
            salt, h = hash_password(PW)
            con.execute("UPDATE users SET pw_salt=?, pw_hash=?, failed=0, locked_until=NULL WHERE username=?", (salt, h, u))
            secrets[u] = con.execute("SELECT totp_secret FROM users WHERE username=?", (u,)).fetchone()[0]
    _used_codes.clear()
    c = TestClient(app)
    c.secrets = secrets
    return c


def tok(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(c, user, role):
    """Accesso della demo: si sceglie il ruolo, si entra con il suo utente di prova (niente password né codice)."""
    r = c.get("/login")
    r = c.post("/login", data={"role": role, "csrf_token": tok(r.text)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cruscotto", r.text[:300]
    assert {"segreteria": "m.bianchi", "medico": "e.gatti"}[role] == user  # il ruolo decide l'utente


def test_senza_accesso_si_torna_al_login(client):
    r = client.get("/cruscotto", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_si_entra_con_un_clic_come_utente_del_ruolo(client):
    r = client.get("/login")
    assert 'name="password"' not in r.text and 'name="username"' not in r.text
    client.post("/login", data={"role": "medico", "csrf_token": tok(r.text)})
    assert "Elena Gatti" in client.get("/cruscotto").text


def test_il_nome_utente_passato_a_mano_non_conta(client):
    r = client.get("/login")
    client.post("/login", data={"role": "segreteria", "username": "e.gatti", "password": "x", "csrf_token": tok(r.text)})
    page = client.get("/cruscotto").text
    assert "Marta Bianchi" in page and "Elena Gatti" not in page


def test_ruolo_inesistente_non_entra(client):
    r = client.get("/login")
    r = client.post("/login", data={"role": "amministratore", "csrf_token": tok(r.text)})
    assert "non disponibile" in r.text and client.get("/cruscotto", follow_redirects=False).status_code == 303


def test_il_vecchio_secondo_passo_riporta_all_accesso(client):
    for metodo in (client.get, client.post):
        r = metodo("/login/codice", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_csrf_obbligatorio(client):
    r = client.post("/login", data={"role": "segreteria"})
    assert r.status_code == 400


def test_accettazione_non_vede_casi_clinici(client, run):
    login(client, "m.bianchi", "segreteria")
    html = client.get("/cruscotto").text
    assert "Visita a rischio" in html
    for parola in ("warfarin", "ibuprofene", "anticoagulant", "ramipril", "amiloidosi", "Cure che si incrociano"):
        assert parola.lower() not in html.lower()
    with db.tx() as con:
        clin = con.execute("SELECT id FROM cases WHERE role='medico' LIMIT 1").fetchone()[0]
    assert client.get(f"/caso/{clin}").status_code == 403
    el = client.get("/elaborazione").text
    assert "Ricomposizione" not in el


def test_medico_firma_con_codice(client, run):
    login(client, "e.gatti", "medico")
    html = client.get("/cruscotto").text
    assert "Cure che si incrociano" in html and "Visita a rischio" not in html
    with db.tx() as con:
        cid = con.execute("SELECT id FROM cases WHERE role='medico' AND kind='cure' LIMIT 1").fetchone()[0]
    page = client.get(f"/caso/{cid}").text
    r = client.post(f"/caso/{cid}/approva", data={"csrf_token": tok(page), "code": "000000"}, follow_redirects=False)
    assert r.headers["location"] == f"/caso/{cid}" and "error" not in r.headers["location"]  # il messaggio non viaggia nell'indirizzo
    assert "Codice non valido" in client.get(f"/caso/{cid}").text
    _used_codes.clear()
    r = client.post(f"/caso/{cid}/approva", data={"csrf_token": tok(page), "code": pyotp.TOTP(client.secrets["e.gatti"]).now()}, follow_redirects=False)
    assert r.headers["location"] == "/cruscotto" and "Fatto:" in client.get("/cruscotto").text
    with db.tx() as con:
        assert con.execute("SELECT status FROM cases WHERE id=?", (cid,)).fetchone()[0] == "ok"
        assert con.execute("SELECT COUNT(*) FROM outbox WHERE case_id=?", (cid,)).fetchone()[0] >= 1


def _approva_trasporto(client, pid):
    login(client, "m.bianchi", "segreteria")
    with db.tx() as con:
        cid = con.execute("SELECT id FROM cases WHERE kind='trasporto' AND patient_id=?", (pid,)).fetchone()[0]
    page = client.get(f"/caso/{cid}").text
    r = client.post(f"/caso/{cid}/approva", data={"csrf_token": tok(page)}, follow_redirects=False)
    assert r.headers["location"].startswith("/trasporti/TR-")
    return r.headers["location"]


def test_trasporto_flusso_completo(client, run):
    url = _approva_trasporto(client, "P10")
    page = client.get(url).text
    assert "Richiesta al servizio" in page and "STACCO" in page
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM outbox WHERE subject LIKE 'Richiesta di disponibilit%'").fetchone()[0] == 1
    client.post(url + "/risposta", data={"csrf_token": tok(page), "esito": "disponibile", "partenza": "13:50"})
    page = client.get(url).text
    assert "Le serve un passaggio" in page and "13:50" in page
    client.post(url + "/chiamata", data={"csrf_token": tok(page), "esito": "serve"})
    page = client.get(url).text
    assert "Trasporto confermato" in page
    with db.tx() as con:
        m = con.execute("SELECT * FROM outbox WHERE ics IS NOT NULL ORDER BY id DESC").fetchone()
    assert m["subject"].startswith("Conferma trasporto e promemoria prenotazione n. 2026")
    assert m["recipient"].startswith("chiara.scarpa@example.com")
    prev = client.get(f"/posta/{m['id']}/anteprima")
    assert prev.status_code == 200 and "13:50" in prev.text and "2 GIORNI LAVORATIVI" in prev.text and "Trasporto organizzato" in prev.text
    assert "default-src 'none'" in prev.headers["content-security-policy"]
    r = client.get(f"/posta/{m['id']}/calendario.ics")
    assert r.status_code == 200 and "BEGIN:VEVENT" in r.text


def test_servizio_non_disponibile_passa_al_successivo(client, run):
    url = _approva_trasporto(client, "P10")  # visita tra 5 giorni: STACCO, poi Croce Verde, poi associazione di quartiere (solo Nord: P10 è Sud)
    page = client.get(url).text
    client.post(url + "/risposta", data={"csrf_token": tok(page), "esito": "non_disponibile"})
    page = client.get(url).text
    assert "servizio successivo" in page and "Croce Verde" in page
    client.post(url + "/risposta", data={"csrf_token": tok(page), "esito": "non_disponibile"})
    assert "Serve l&#39;accettazione" in client.get(url).text or "serve l'accettazione" in client.get(url).text.lower()


def test_tre_chiamate_senza_risposta_tornano_all_accettazione(client, run):
    url = _approva_trasporto(client, "P01")
    page = client.get(url).text
    client.post(url + "/risposta", data={"csrf_token": tok(page), "esito": "disponibile", "partenza": "10:00"})
    for _ in range(3):
        client.post(url + "/chiamata", data={"csrf_token": tok(page), "esito": "non_risponde"})
    assert "Tre tentativi" in client.get(url).text


def test_pagine_riservate_per_ruolo(client, run):
    login(client, "m.bianchi", "segreteria")
    assert client.get("/pazienti").status_code == 403
    assert client.get("/pazienti/P01").status_code == 403
    client.cookies.clear()
    _used_codes.clear()
    login(client, "e.gatti", "medico")
    assert client.get("/trasporti").status_code == 403


def test_medico_vede_cartella_e_documenti(client, run):
    login(client, "e.gatti", "medico")
    html = client.get("/cruscotto").text
    assert "Da firmare" in html and "/pazienti/P01?doc=E-093" in html
    page = client.get("/pazienti/P01?doc=E-061").text
    assert "Lettera di dimissione" in page and "warfarin" in page and "segnalato" in page
    assert len(set(re.findall(r"/pazienti/P\d+", client.get("/pazienti").text))) == 10  # la barra del paziente seguito non conta


def test_ogni_scheda_aperta_finisce_nel_registro(client, run):
    login(client, "m.bianchi", "segreteria")
    with db.tx() as con:
        cid = con.execute("SELECT id FROM cases WHERE role='segreteria' LIMIT 1").fetchone()[0]
    client.get(f"/caso/{cid}")
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM access_log WHERE action='apre la scheda' AND target LIKE ?", (cid + "%",)).fetchone()[0] == 1


def test_ripristina_demo(client):
    login(client, "m.bianchi", "segreteria")
    with db.tx() as con:
        n_pazienti = con.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
        con.execute("INSERT INTO outbox (ts, case_id, role, recipient, subject, body) VALUES ('x','x','segreteria','x','sporco','x')")
        con.execute("DELETE FROM patients WHERE id='P01'")
    assert client.post("/ripristina", data={}).status_code == 400  # senza CSRF niente
    r = client.post("/ripristina", data={"csrf_token": tok(client.get("/cruscotto").text)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cruscotto"  # il messaggio non viaggia più nell'indirizzo
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == n_pazienti
        assert con.execute("SELECT 1 FROM patients WHERE id='P01'").fetchone()
        assert not con.execute("SELECT 1 FROM outbox WHERE subject='sporco'").fetchone()
    assert client.get("/cruscotto", follow_redirects=False).status_code == 200  # utenti intatti: la sessione resta valida


def test_kit_visibile_a_medico_e_accettazione(client):
    from app import kit
    with db.tx() as con:
        kit.assicura(con)
        a = dict(con.execute("SELECT * FROM appointments WHERE id='A-05'").fetchone())
        p = dict(con.execute("SELECT * FROM patients WHERE id='P05'").fetchone())
        kit.riserva(con, "CX", p, a, {"username": "m.bianchi", "name": "Marta"})
    login(client, "m.bianchi", "segreteria")
    assert "Appunti dell'agente" not in client.get("/cruscotto").text  # il cruscotto mostra solo cosa c'è da fare
    reg = client.get("/registro").text
    assert "Appunti dell'agente" in reg and "kit per la visita" in reg  # gli appunti stanno nel registro
    client.post("/logout", data={"csrf_token": tok(client.get("/cruscotto").text)})
    login(client, "e.gatti", "medico")
    assert "Visite accessibili" in client.get("/cruscotto").text
    assert "Visita accessibile: Kit 1" in client.get("/pazienti/P05").text
    import subprocess, sys
    subprocess.run([sys.executable, "-m", "app.seed", "--reset"], check=True, capture_output=True)
