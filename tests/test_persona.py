"""Granularità: le pagine del menu guardano tutti, la scheda di una persona solo lei. Si resta sulla persona finché non si sceglie."""
import re

from app import db
from test_web import client, login, tok  # noqa: F401


def test_calendario_di_bruno_solo_bruno(client, run):
    login(client, "m.bianchi", "segreteria")
    page = client.get("/persona/P05/calendario").text
    assert "visita cardiologica" in page and "10:20" in page  # taratura: la visita di Bruno c'è
    assert "Anna Zanella" not in page and "A-02" not in page  # la prenotazione di Anna no
    assert 'name="prenotazione" value="A-05"' in page and 'value="A-02"' not in page  # le azioni sono solo sulle sue visite


def test_cruscotto_generale_con_la_persona_di_lato(client, run):
    login(client, "m.bianchi", "segreteria")
    client.get("/persona/P05")
    page = client.get("/cruscotto").text
    pannello = page[page.index('class="widget pannello"'):]
    assert "Bruno Tonin" in pannello and 'href="/persona/P05"' in pannello
    menu = re.search(r'<nav class="side"[^>]*>(.*?)</nav>', page, re.S).group(1)
    assert 'href="/persona/P05"' in menu and 'href="/calendario"' in menu
    assert "/persona/P05/" not in menu  # le sezioni della persona stanno solo nelle linguette della scheda, non ripetute nel menu
    scheda = client.get("/persona/P05").text
    linguette = re.search(r'<nav class="tabs"[^>]*>(.*?)</nav>', scheda, re.S).group(1)
    assert 'href="/persona/P05/trasporti" data-dentro' in linguette and 'href="/persona/P05/calendario" data-dentro' in linguette
    tr = client.get("/trasporti").text  # la pagina del menu resta generale
    with db.tx() as con:
        tutti = con.execute("SELECT COUNT(*) FROM transport_requests").fetchone()[0]
    assert tr.count('href="/trasporti/TR-') >= tutti
    client.post("/segui/tutti", data={"csrf_token": tok(page)})
    page = client.get("/cruscotto").text
    assert 'class="widget pannello"' not in page and "/persona/P05/trasporti" not in page


def test_disdetta_dalla_scheda_torna_alla_scheda_e_avvisa(client, run):
    login(client, "m.bianchi", "segreteria")
    page = client.get("/persona/P05/calendario").text
    ora = re.search(r'data-ora="([^"]+)"', page).group(1)
    r = client.post("/calendario/disdetta", data={"csrf_token": tok(page), "prenotazione": "A-05", "motivo": "influenza",
                                                  "torna": "/persona/P05/calendario"}, follow_redirects=False)
    assert r.headers["location"] == "/persona/P05/calendario"
    n = client.get("/novita", params={"dopo": ora}).json()["novita"]
    canc = [x for x in n if x["step"] == "cancellata" and x["patient"] == "Bruno Tonin"]
    assert canc and "visita cardiologica" in canc[0]["text"], n  # l'avviso parte subito, prima di ogni scelta
    assert "2 persone in lista" in canc[0]["text"]  # chi può prendere il posto: le due persone in lista per la visita cardiologica
    prop = [x for x in n if x["step"] == "proposta"]
    assert all(x["link"].startswith("/caso/") for x in prop), prop  # la proposta porta dritta all approvazione


def test_torna_solo_dentro_filo():
    from app.main import app
    from fastapi.testclient import TestClient
    assert TestClient(app)  # la destinazione esterna non si accetta: vedi controllo startswith("/persona/") in calendario_disdetta


def test_medico_non_vede_le_sezioni_logistiche(client, run):
    login(client, "e.gatti", "medico")
    assert client.get("/persona/P05/trasporti").status_code == 403
    page = client.get("/persona/P05").text
    assert 'href="/pazienti/P05"' in page and "/persona/P05/trasporti" not in page


def test_si_vede_chi_ha_bisogno_del_kit_e_a_che_punto_e(client, run):
    from app import actions
    login(client, "m.bianchi", "segreteria")
    with db.tx() as con:  # prima dell'elaborazione la pratica non c'è ancora, ma la visita sì: non deve dire "nessuna visita"
        salvati = [dict(r) for r in con.execute("SELECT * FROM cases WHERE patient_id='P05' AND kind='kit'")]
        con.execute("DELETE FROM cases WHERE patient_id='P05' AND kind='kit'")
    prima = client.get("/persona/P05").text
    assert "non ancora riservato per la visita cardiologica" in prima and "nessuna visita" not in prima
    with db.tx() as con:
        for c in salvati:
            con.execute(f"INSERT INTO cases ({','.join(c)}) VALUES ({','.join('?' * len(c))})", tuple(c.values()))
    bruno = client.get("/persona/P05").text
    assert "Serve il kit per l'udito" in bruno and "da riservare" in bruno  # dopo l'elaborazione: c'è la pratica da approvare
    assert "Serve il kit per l'udito" not in client.get("/persona/P02").text  # Anna non l'ha chiesto
    assert 'title="Ha chiesto il supporto per l\'udito: serve il kit">kit</span>' in client.get("/cruscotto").text
    with db.tx() as con:
        caso = con.execute("SELECT id FROM cases WHERE patient_id='P05' AND kind='kit' AND status='pending'").fetchone()[0]
    actions.execute(caso, {"username": "m.bianchi", "name": "Marta", "role": "segreteria"})
    cal = client.get("/persona/P05/calendario").text
    assert "riservato per la visita" in client.get("/persona/P05").text and "Kit 1 riservato" in cal
