"""Liste di lavoro: scala di priorità a parole (non solo colore), regola fissa che alza il livello, azione dalla riga, scarto con motivo."""
import pytest
from fastapi.testclient import TestClient

from app import auth, db, ui
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


def test_scala_a_cinque_livelli():
    assert [ui.level(s)["label"] for s in (100, 85, 84, 65, 64, 40, 39, 20, 19, 0)] == \
        ["Immediata", "Immediata", "Urgente", "Urgente", "Differibile", "Differibile", "Minore", "Minore", "Non urgente", "Non urgente"]


def test_regola_fissa_alza_almeno_a_urgente():
    c = {"score": 30, "action": "Pagina per il medico. Regola locale fissa: anticoagulante insieme ad antinfiammatorio."}
    assert ui.level_of(c)["label"] == "Urgente" and ui.level_of(c)["raised"]
    assert ui.level_of({"score": 90, "action": "Regola locale fissa"})["label"] == "Immediata"  # non abbassa mai
    assert ui.level_of({"score": 30, "action": "altro"})["label"] == "Minore"


def test_il_livello_si_legge_anche_senza_colore(client, run):
    login(client, "m.bianchi", "segreteria")
    page = client.get("/cruscotto").text
    assert "Priorità: immediata" in page or "Priorità: urgente" in page  # testo per lo screen reader
    assert 'class="prio-label"' in page and "Immediata" in page


def test_via_libera_dalla_riga(client, run):
    login(client, "m.bianchi", "segreteria")
    page = client.get("/cruscotto").text
    with db.tx() as con:
        cid = con.execute("SELECT id FROM cases WHERE kind='kit' AND status='pending'").fetchone()[0]
    assert f'action="/caso/{cid}/approva"' in page  # il comando sta nella riga, non solo nella scheda
    client.post(f"/caso/{cid}/approva", data={"csrf_token": tok(page)})
    with db.tx() as con:
        assert con.execute("SELECT status FROM cases WHERE id=?", (cid,)).fetchone()[0] == "ok"


def test_scarto_con_motivo_da_elenco(client, run):
    login(client, "m.bianchi", "segreteria")
    with db.tx() as con:
        cid = con.execute("SELECT id FROM cases WHERE kind='kit' AND status='pending'").fetchone()[0]
    page = client.get(f"/caso/{cid}").text
    assert "Già gestito in altro modo" in page
    client.post(f"/caso/{cid}/scarta", data={"csrf_token": tok(page), "tipo": "Già gestito in altro modo", "motivo": "kit portato dalla figlia"})
    with db.tx() as con:
        txt = con.execute("SELECT text FROM action_log WHERE case_id=?", (cid,)).fetchone()[0]
    assert "Già gestito in altro modo: kit portato dalla figlia" in txt
