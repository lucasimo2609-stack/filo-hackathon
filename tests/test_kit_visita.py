"""Visita col kit: trascrizione dal vivo, cartella, mail alla persona, schermo del tablet."""
import re

import pytest

from app import db, kit, kit_visita
from tests.test_web import client, login, tok  # noqa: F401


@pytest.fixture
def prenotazione():
    with db.tx() as con:
        kit.assicura(con)
        con.execute("DELETE FROM kit_bookings")
        a = dict(con.execute("SELECT * FROM appointments WHERE id='A-05'").fetchone())
        p = dict(con.execute("SELECT * FROM patients WHERE id='P05'").fetchone())
        kit.riserva(con, "CX", p, a, {"username": "m.bianchi", "name": "Marta"})
        return con.execute("SELECT id FROM kit_bookings WHERE status='riservato'").fetchone()["id"]


@pytest.fixture
def trascrittore(monkeypatch):
    frasi = iter(["Prenda la pastiglia ogni mattina.", "Sottotitoli a cura di QTSS", "Ci vediamo tra tre mesi."])
    monkeypatch.setenv("ELEVENLABS_API_KEY", "finta")
    monkeypatch.setattr(kit_visita, "_chiedi", lambda audio, mime: next(frasi))


def _apri(client, bid):
    login(client, "e.gatti", "medico")
    home = client.get("/cruscotto").text
    assert f'action="/kit/{bid}/visita"' in home  # il tasto è sul cruscotto del medico
    r = client.post(f"/kit/{bid}/visita", data={"csrf_token": tok(home)}, follow_redirects=False)
    vid = r.headers["location"].rsplit("/", 1)[1]
    return vid, client.get(f"/kit/visita/{vid}")


def test_visita_completa(client, prenotazione, trascrittore):
    vid, pag = _apri(client, prenotazione)
    assert pag.status_code == 200 and "microphone=(self)" in pag.headers["permissions-policy"]
    csrf = tok(pag.text)
    for n in (3, 1, 2):  # arrivano fuori ordine
        r = client.post(f"/kit/visita/{vid}/audio", data={"csrf_token": csrf, "seq": n}, files={"audio": ("f", b"x" * 100, "audio/webm")})
        assert r.status_code == 200
    segs = client.get(f"/kit/visita/{vid}/testo").json()["segmenti"]
    # seq 3 -> "Prenda...", seq 1 -> frase inventata (scartata), seq 2 -> "Ci vediamo..."; in pagina vanno in ordine di seq
    assert [(s["seq"], s["text"][:5]) for s in segs] == [(2, "Ci ve"), (3, "Prend")]
    schermo = re.search(r"/kit/schermo/([\w-]+)", pag.text).group(1)
    anonimo = type(client)(client.app)
    assert len(anonimo.get(f"/kit/schermo/{schermo}/testo").json()["segmenti"]) == 2
    # fine visita: documento in cartella, schermo spento
    client.post(f"/kit/visita/{vid}/chiudi", data={"csrf_token": csrf})
    assert anonimo.get(f"/kit/schermo/{schermo}/testo").status_code == 404
    assert anonimo.get(f"/kit/schermo/{schermo}").status_code == 404
    with db.tx() as con:
        nota = con.execute("SELECT * FROM notes WHERE patient_id='P05' AND kind='trascrizione_visita'").fetchone()
    assert nota and "Ci vediamo tra tre mesi." in nota["text"] and "QTSS" not in nota["text"]
    assert "Trascrizione della visita" in client.get(f"/pazienti/P05?doc={nota['id']}").text
    # dopo la chiusura non si aggiunge altro audio
    r = client.post(f"/kit/visita/{vid}/audio", data={"csrf_token": csrf, "seq": 9}, files={"audio": ("f", b"x", "audio/webm")})
    assert r.status_code == 409


def test_invio_mail_indirizzo_in_anagrafica_o_nuovo(client, prenotazione, trascrittore):
    vid, pag = _apri(client, prenotazione)
    csrf = tok(pag.text)
    # prima della chiusura non si manda
    client.post(f"/kit/visita/{vid}/invia", data={"csrf_token": csrf, "email": "a@b.it"})
    with db.tx() as con:
        assert not con.execute("SELECT 1 FROM outbox WHERE case_id=?", (vid,)).fetchone()
    client.post(f"/kit/visita/{vid}/audio", data={"csrf_token": csrf, "seq": 1}, files={"audio": ("f", b"x", "audio/webm")})
    client.post(f"/kit/visita/{vid}/chiudi", data={"csrf_token": csrf})
    pag = client.get(f"/kit/visita/{vid}").text
    with db.tx() as con:
        in_anagrafica = con.execute("SELECT email FROM patients WHERE id='P05'").fetchone()["email"]
    assert f'value="{in_anagrafica}"' in pag  # l'indirizzo già inserito è proposto
    client.post(f"/kit/visita/{vid}/invia", data={"csrf_token": csrf, "email": in_anagrafica})
    client.post(f"/kit/visita/{vid}/invia", data={"csrf_token": csrf, "email": "non-una-mail"})
    client.post(f"/kit/visita/{vid}/invia", data={"csrf_token": csrf, "email": "nuova@esempio.it", "salva": "1"})
    with db.tx() as con:
        mails = [dict(r) for r in con.execute("SELECT recipient, body FROM outbox WHERE case_id=? ORDER BY id", (vid,))]
        nuova = con.execute("SELECT email FROM patients WHERE id='P05'").fetchone()["email"]
        log = [r[0] for r in con.execute("SELECT text FROM action_log WHERE case_id=? AND username='agente kit'", (vid,))]
    assert [m["recipient"] for m in mails] == [in_anagrafica, "nuova@esempio.it"]
    assert "Prenda la pastiglia ogni mattina." in mails[0]["body"] and mails[0]["body"].count("può contenere errori") == 1
    assert nuova == "nuova@esempio.it" and len(log) == 2


def test_permessi(client, prenotazione, trascrittore):
    vid, pag = _apri(client, prenotazione)
    client.post("/logout", data={"csrf_token": tok(pag.text)})
    login(client, "m.bianchi", "segreteria")
    assert client.get(f"/kit/visita/{vid}").status_code == 403  # l'accettazione non legge la visita
    assert client.get("/kit/schermo/codice-inventato/testo").status_code == 404
    r = client.get("/cruscotto")
    assert "microphone=()" in r.headers["permissions-policy"]  # il microfono resta chiuso altrove


def test_frasi_inventate_sul_silenzio():
    for t in ["Sottotitoli a cura di QTSS", "Grazie per la visione!", "  "]:
        assert kit_visita._INVENTATE.search(t)
    assert not kit_visita._INVENTATE.search("Prenda la pastiglia ogni mattina.")


def test_pezzo_vuoto_e_silenzio(monkeypatch):
    import httpx
    monkeypatch.setenv("FW_KEY_WHISPER", "")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "finta")
    risposta = httpx.Response(400, text='{"detail":{"code":"invalid_audio"}}', request=httpx.Request("POST", "https://x"))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: risposta)
    assert kit_visita.trascrivi(b"", "audio/webm") == ""
    altro = httpx.Response(401, text="no", request=httpx.Request("POST", "https://x"))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: altro)
    with pytest.raises(httpx.HTTPStatusError):  # un errore vero resta un errore e la pagina lo dice
        kit_visita.trascrivi(b"x", "audio/webm")


def test_una_visita_aperta_per_kit(client, prenotazione, trascrittore):
    vid, pag = _apri(client, prenotazione)
    r = client.post(f"/kit/{prenotazione}/visita", data={"csrf_token": tok(pag.text)}, follow_redirects=False)
    assert r.headers["location"].endswith(vid)  # il secondo clic riprende la stessa visita, non ne apre un'altra
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM kit_visite WHERE booking_id=?", (prenotazione,)).fetchone()[0] == 1


def test_scheda_vecchia_non_impedisce_l_accesso(client):
    """Una scheda di Filo rimasta aperta chiede /novita senza sessione mentre qualcuno sta entrando: l'accesso deve riuscire."""
    r = client.get("/login")
    assert client.get("/novita", follow_redirects=False).status_code == 303  # la scheda vecchia
    r = client.post("/login", data={"role": "medico", "csrf_token": tok(r.text)}, follow_redirects=False)
    assert r.headers["location"] == "/cruscotto"


def test_due_copie_di_filo_non_si_buttano_fuori(tmp_path):
    """Stesso browser, stesso localhost, porte diverse: ogni copia (database diverso) ha il suo cookie di sessione."""
    import subprocess
    import sys
    nome = lambda db: subprocess.run([sys.executable, "-c", "from app.config import SESSION_COOKIE; print(SESSION_COOKIE)"],
                                     capture_output=True, text=True, env={**__import__("os").environ, "FILO_DB": str(db), "FILO_COOKIE": ""},
                                     cwd=str(__import__("pathlib").Path(__file__).parent.parent)).stdout.strip()
    a, b = nome(tmp_path / "a.db"), nome(tmp_path / "b.db")
    assert a.startswith("filo_") and b.startswith("filo_") and a != b and a == nome(tmp_path / "a.db")
