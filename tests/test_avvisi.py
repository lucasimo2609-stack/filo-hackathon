"""La risposta del servizio arriva all'accettazione da sola: la pagina chiede le novità e la pratica la mostra."""
import re

from app import db
from test_web import client, login, tok, _approva_trasporto  # noqa: F401


def _ora(page):
    return re.search(r'data-ora="([^"]+)"', page).group(1)


def test_risposta_del_servizio_compare_tra_le_novita(client, run):
    url = _approva_trasporto(client, "P10")
    page = client.get(url).text
    assert "In attesa della risposta di" in page and "Risposta ricevuta" not in page
    dopo = _ora(page)
    assert not [x for x in client.get("/novita", params={"dopo": dopo}).json()["novita"] if x["link"] == url]  # niente prima della risposta
    client.post(url + "/risposta", data={"csrf_token": tok(page), "esito": "disponibile", "partenza": "13:50"})
    d = client.get("/novita", params={"dopo": dopo}).json()
    n = [x for x in d["novita"] if x["step"] == "risposta servizio"]
    assert len(n) == 1 and n[0]["titolo"] == "Risposta ricevuta" and "13:50" in n[0]["text"] and n[0]["link"] == url
    assert not [x for x in client.get("/novita", params={"dopo": d["ora"][:17] + "99"}).json()["novita"] if x["link"] == url]  # già viste
    page = client.get(url).text
    assert "Risposta ricevuta" in page and "disponibile, partenza alle 13:50" in page
    assert "data-aggiorna" not in page  # niente più ricarica alla cieca


def test_il_medico_non_riceve_novita_dei_trasporti(client, run):
    login(client, "e.gatti", "medico")
    with db.tx() as con:
        con.execute("INSERT INTO transport_events VALUES ('TR-x', ?, 'agente', 'risposta servizio', 'prova')", (db.now(),))
    assert client.get("/novita", params={"dopo": "2000-01-01 00:00:00"}).json()["novita"] == []


def test_voce_su_un_altro_database_si_vede(monkeypatch):
    """Il guasto del 23/10: il cervello della voce girava da un'altra copia di Filo e ogni chiamata cadeva alla prima risposta."""
    import hashlib
    import httpx
    from app import main
    from app.config import DB_PATH
    mio = hashlib.sha256(str(DB_PATH.resolve()).encode()).hexdigest()[:12]

    class R:
        def __init__(self, d): self.d = d
        def json(self): return self.d
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R({"ok": True, "db": mio}))
    assert main.voce_allineata() is None
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R({"ok": True, "db": "000000000000"}))
    assert "altro database" in main.voce_allineata()
    def giu(*a, **k): raise httpx.ConnectError("no")
    monkeypatch.setattr(httpx, "get", giu)
    assert "non risponde" in main.voce_allineata()


def _due_cose():
    with db.tx() as con:
        r = con.execute("SELECT patient_id FROM cases WHERE role='segreteria' AND status='pending' GROUP BY patient_id "
                        "HAVING SUM(kind='trasporto') >= 1 AND SUM(kind='kit') >= 1").fetchone()
        assert r, "nella demo serve una persona con trasporto e kit (Bruno Tonin)"
        pid = r[0]
        tr = con.execute("SELECT id FROM cases WHERE patient_id=? AND kind='trasporto' AND status='pending'", (pid,)).fetchone()[0]
        kit = con.execute("SELECT id FROM cases WHERE patient_id=? AND kind='kit' AND status='pending'", (pid,)).fetchone()[0]
        name = con.execute("SELECT name FROM patients WHERE id=?", (pid,)).fetchone()[0]
    return pid, tr, kit, name


def test_una_persona_una_riga_nel_cruscotto(client, run):
    login(client, "m.bianchi", "segreteria")
    pid, tr, kit, name = _due_cose()
    page = client.get("/cruscotto").text
    dafare = page[page.index('aria-label="Da fare"'):]
    assert dafare.count(f"<b>{name}</b>") == 1  # una sola riga nel "Da fare", non due schede
    assert f'action="/caso/{tr}/approva"' in page and f'action="/caso/{kit}/approva"' in page


def test_via_libera_resta_sulla_schermata_della_persona(client, run):
    login(client, "m.bianchi", "segreteria")
    pid, tr, kit, name = _due_cose()
    page = client.get(f"/caso/{tr}").text
    assert "c'è anche" in page and f'action="/caso/{kit}/approva"' in page  # il kit si decide da qui
    r = client.post(f"/caso/{tr}/approva", data={"csrf_token": tok(page), "torna": tr}, follow_redirects=False)
    assert r.headers["location"] == f"/caso/{tr}"
    page = client.get(r.headers["location"]).text
    assert "Pratica del trasporto aperta: TR-" in page and "Per questa persona resta" in page
    assert f'action="/caso/{kit}/approva"' in page  # il kit resta decidibile dalla stessa schermata, anche a trasporto fatto
    r = client.post(f"/caso/{kit}/approva", data={"csrf_token": tok(page), "torna": tr}, follow_redirects=False)
    assert r.headers["location"] == "/cruscotto"  # niente più da fare per lei: si torna alla lista
    with db.tx() as con:
        assert {s for (s,) in con.execute("SELECT status FROM cases WHERE id IN (?,?)", (tr, kit))} == {"ok"}


def test_torna_non_porta_su_una_scheda_qualsiasi(client, run):
    login(client, "m.bianchi", "segreteria")
    pid, tr, kit, name = _due_cose()
    page = client.get(f"/caso/{tr}").text
    r = client.post(f"/caso/{tr}/approva", data={"csrf_token": tok(page), "torna": "https://esempio.it"}, follow_redirects=False)
    assert r.headers["location"] == f"/caso/{tr}"


def test_scheda_aperta_prima_dell_aggiornamento_chiede_di_ricaricare(client, run):
    """Il 23/10 una scheda con il vecchio script chiedeva /trasporti/novita ogni 4 secondi e riceveva 404 per ore."""
    login(client, "m.bianchi", "segreteria")
    d = client.get("/trasporti/novita", params={"dopo": "2026-10-23 04:20:49"}).json()
    assert [n["titolo"] for n in d["novita"]] == ["Pagina da ricaricare"] and d["ora"]
    assert d["novita"][0]["ts"] == client.get("/trasporti/novita").json()["novita"][0]["ts"]  # ora fissa: l'avviso compare una volta


def test_pagine_chiamate_con_elevenlabs_acceso(client, run, monkeypatch):
    """Il 23/10 l'avviso sulla voce era finito nella route dell'elenco (/chiamate, senza la variabile c): 500 solo con ElevenLabs
    configurato, cioè sul portatile della demo e mai nei test."""
    from app import main
    monkeypatch.setattr(main.voce_el, "configurato", lambda: True)
    monkeypatch.setattr(main, "voce_allineata", lambda: "Il cervello della voce legge un altro database.")
    from app.voce import chiamata as ch
    login(client, "m.bianchi", "segreteria")
    assert client.get("/chiamate").status_code == 200
    with db.tx() as con:
        pid = con.execute("SELECT id FROM patients LIMIT 1").fetchone()[0]
        cid = ch.crea(con, pid, "controllo", None, {"username": "e.gatti", "name": "Elena Gatti"})
    login(client, "e.gatti", "medico")
    assert client.get("/chiamate").status_code == 200
    p = client.get(f"/chiamate/{cid}")
    assert p.status_code == 200 and "La voce non è collegata a questa copia di Filo" in p.text
