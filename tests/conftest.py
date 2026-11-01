"""I test girano su una cartella dati temporanea e con agenti finti: niente rete, niente chiavi."""
import os
import sys
import tempfile
from pathlib import Path

# Una sola cartella per tutta la sessione, anche se questo file viene importato due volte (es. come "tests.conftest"):
# altrimenti FILO_DB punterebbe a una cartella nuova e i sottoprocessi (Ripristina demo) userebbero un altro database.
_tmp = os.environ.get("FILO_TEST_TMP") or tempfile.mkdtemp(prefix="filo-test-")
os.environ["FILO_TEST_TMP"] = _tmp
os.environ["FILO_DATA"] = _tmp
os.environ["FILO_DB"] = str(Path(_tmp) / "filo.db")
os.environ["LOCAL_LLM_ENABLED"] = "off"
# I test non devono MAI mandare mail vere, leggere caselle vere o telefonare: il .env vero si ignora per queste voci
# (load_dotenv non sovrascrive variabili già impostate, anche se vuote).
for _k in ("MAIL_PROVA", "MAIL_SERVIZIO_PROVA", "MAIL_FAMIGLIA_PROVA", "MAIL_AMBULATORI_PROVA", "SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
           "SMTP_FROM", "IMAP_HOST", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_NUMERO", "ELEVENLABS_API_KEY", "ELEVEN_AGENT_ID",
           "ELEVEN_PHONE_NUMBER_ID", "VOCE_NUMERO_PROVA", "FILO_FLUSSI", "FILO_URL_PUBBLICO"):
    os.environ[_k] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app import agents, db, gateway, seed  # noqa: E402


def _rete_vietata(*a, **k):
    raise RuntimeError("chiamata vera a un agente durante i test: va sostituita con un finto")


# A livello di modulo, non con monkeypatch: un monkeypatch.undo() in un test non la toglie.
agents._sim_post = _rete_vietata
agents._client = _rete_vietata


def fake_call(agent, payload):
    if agent == "cure":
        return {"da_verificare": True, "gravita": 3, "spiegazione": "Combinazione da verificare."}, "finto", 1
    if agent == "aderenza":
        return {"rilevante": True, "urgenza": 3, "spiegazione": "Terapia sospesa."}, "finto", 1
    if agent == "trasporti":
        return {"serve_trasporto": True, "servizio": "T1", "partenza": None, "motivo": "test"}, "finto", 1
    if payload.get("tipo") == "posto_liberato":
        return {"codice_scelto": payload["candidati"][0]["codice"], "motivo": "test"}, "finto", 1
    return {"doppione": True, "spiegazione": "Stesso esame."}, "finto", 1


ECO = "Ospedale Laguna, pad. C"


def libera_posto(service, at, place=ECO, fs_id="FS-2"):
    """Come una disdetta vera: il posto (occupato da una prenotazione di prova) torna libero nel calendario e va alla lista."""
    from app import calendario
    with db.tx() as con:
        s = calendario.posto(con, service=service, at=at, place=place)
        assert s, f"nessun posto {service} {at} {place} nel calendario"
        con.execute("UPDATE cal_slots SET status='libero', occupant=NULL, version=version+1 WHERE id=?", (s["id"],))
        con.execute("INSERT INTO freed_slots VALUES (?,?,?,?,?,NULL)", (fs_id, service, at, place, "2026-10-22 08:00"))
    return s["id"]


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(agents, "call", fake_call)
    monkeypatch.setattr(sys, "argv", ["seed", "--reset"])
    seed.main()
    yield


@pytest.fixture
def run():
    return gateway.run("test")
