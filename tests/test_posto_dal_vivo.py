"""Si libera un posto: proposta all'accettazione, via libera, chiamata, accetta. Ogni schermo lo sa da solo (/novita),
e il medico vede in cima chi è entrato in agenda, con la cartella a un clic. La chiamata parte solo con il via libera."""
import json
import re

from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.voce import chiamata as ch
from test_web import client, login, tok  # noqa: F401


def q(sql, *a):
    with db.tx() as con:
        return [dict(r) for r in con.execute(sql, a)]


def _ora(page):
    return re.search(r'data-ora="([^"]+)"', page).group(1)


def test_disdetta_chiamata_e_vista_del_medico(client, run):
    medico = TestClient(app)
    medico.secrets = client.secrets
    login(medico, "e.gatti", "medico")
    dopo_medico = _ora(medico.get("/cruscotto").text)

    login(client, "m.bianchi", "segreteria")
    page = client.get("/posti").text
    dopo = _ora(page)
    r = client.post("/posti/disdetta-prova", data={"csrf_token": tok(page)}, follow_redirects=False)
    assert r.headers["location"] == "/cruscotto"
    slot = q("SELECT * FROM freed_slots ORDER BY freed_at DESC, rowid DESC LIMIT 1")[0]
    case = q("SELECT * FROM cases WHERE kind='posto' AND status='pending' AND json_extract(payload,'$.slot')=?", slot["id"])
    assert len(case) == 1, "la proposta nasce subito, senza aspettare l'elaborazione"
    case = case[0]
    assert not q("SELECT 1 FROM calls WHERE ref_id IN (SELECT id FROM appointments WHERE at=?)", slot["at"]), "nessuna chiamata senza via libera"
    nov = client.get("/novita", params={"dopo": dopo}).json()["novita"]
    assert any(n["step"] == "proposta" and "/cruscotto" in n["ricarica"] for n in nov)  # il cruscotto si aggiorna e avvisa

    page = client.get("/cruscotto").text
    assert f'action="/caso/{case["id"]}/approva"' in page
    client.post(f"/caso/{case['id']}/approva", data={"csrf_token": tok(page)})
    o = q("SELECT * FROM slot_offers WHERE id=?", json.loads(case["payload"])["offer"])[0]
    assert o["status"] == "in_chiamata" and o["call_id"]

    anno = q("SELECT birth_year FROM patients WHERE id=?", o["patient_id"])[0]["birth_year"]
    ch.esegui(o["call_id"], "verifica_identita", {"anno_nascita": anno})
    ch.esegui(o["call_id"], "conferma_appuntamento", {"scelta": "accetta", "serve_passaggio": False})
    ch.concludi(o["call_id"])
    assert q("SELECT assigned_to FROM freed_slots WHERE id=?", slot["id"])[0]["assigned_to"] == o["patient_id"]

    nov = client.get("/novita", params={"dopo": dopo}).json()["novita"]
    assert any(n["step"] == "accettato" for n in nov)
    nm = medico.get("/novita", params={"dopo": dopo_medico}).json()["novita"]
    assert [n["link"] for n in nm if n["step"] == "nuovo in agenda"] == [f"/pazienti/{o['patient_id']}"]
    assert all(n["step"] == "nuovo in agenda" for n in nm)  # al medico niente trasporti né proposte dell'accettazione
    page = medico.get("/cruscotto").text
    name = q("SELECT name FROM patients WHERE id=?", o["patient_id"])[0]["name"]
    assert "Nuovo in agenda" in page and f'href="/pazienti/{o["patient_id"]}">{name}</a>' in page and "Apri la cartella" in page
    cart = medico.get(f"/pazienti/{o['patient_id']}").text
    assert slot["at"] in cart and "In lista: " + slot["service"] not in cart  # prenotata, e non più in lista d'attesa


def test_disdetta_di_prova_solo_per_l_accettazione(client, run):
    login(client, "e.gatti", "medico")
    page = client.get("/cruscotto").text
    r = client.post("/posti/disdetta-prova", data={"csrf_token": tok(page)}, follow_redirects=False)
    assert r.status_code in (303, 403) and r.headers.get("location", "") != "/cruscotto"
