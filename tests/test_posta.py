"""Flusso con la posta vera, con l'invio sostituito da una finta casella."""
import time

import pytest

from app import db, posta, trasporti
from app.voce import chiamata as voce_ch

SERV, FAM = "croceverdissima@outlook.it", "famigliapaziente@outlook.it"


@pytest.fixture
def casella(monkeypatch):
    for k, v in {"SMTP_HOST": "smtp.example", "SMTP_USER": "filo@example.com", "SMTP_PASSWORD": "x",
                 "SMTP_FROM": "filo@example.com", "MAIL_SERVIZIO_PROVA": SERV, "MAIL_FAMIGLIA_PROVA": FAM, "MAIL_PROVA": ""}.items():
        monkeypatch.setenv(k, v)
    inviate = []
    monkeypatch.setattr(posta, "invia", lambda dest, ogg, testo, html=None, ics=None, msgid=None, in_reply_to=None, references=None:
                        inviate.append(dict(dest=dest, ogg=ogg, testo=testo, html=html, msgid=msgid, irt=in_reply_to)) or dest)
    posta.contatti_demo()
    yield inviate
    import subprocess, sys
    subprocess.run([sys.executable, "-m", "app.seed", "--reset"], check=True, capture_output=True)


def _aspetta(inviate, n):
    for _ in range(50):
        if len(inviate) >= n:
            return
        time.sleep(0.05)
    raise AssertionError(f"attese {n} mail, partite {len(inviate)}")


@pytest.mark.parametrize("testo,atteso", [
    ("Ok, passiamo alle 9:15. Il volontario suona al citofono.", ("si", "09:15")),
    ("Sì nessun problema, alle 8", ("si", "08:00")),
    ("No, purtroppo siamo al completo", ("no", None)),
    ("Non siamo disponibili domani", ("no", None)),
    ("Vi richiamo io", (None, None)),
])
def test_interpreta(testo, atteso):
    assert posta.interpreta(testo) == atteso


def test_pulisci_toglie_la_citazione():
    t = "ok alle 9:00\n\nIl giorno gio 23 ott Centrale ha scritto:\n> no non serve"
    assert posta.pulisci(t) == "ok alle 9:00"


def test_giro_completo(casella):
    user = {"username": "m.bianchi", "name": "Marta Bianchi"}
    with db.tx() as con:
        rid = trasporti.start(con, None, "P05", "A-05", "T2", user)
    _aspetta(casella, 1)
    rich = casella[0]
    assert rich["dest"] == SERV and rid in rich["ogg"]

    assert posta.gestisci_risposta(rid, SERV, "<r1@outlook>", "Ok, passiamo alle 9:15. Il volontario suona al citofono.\n> citazione") == "si"
    assert posta.gestisci_risposta(rid, SERV, "<r1@outlook>", "ok") == "già letta"
    with db.tx() as con:
        r = con.execute("SELECT * FROM transport_requests WHERE id=?", (rid,)).fetchone()
        call = con.execute("SELECT * FROM calls WHERE ref_id=?", (rid,)).fetchone()
    assert r["status"] == "attesa_paziente" and r["pickup"] == "09:15"
    assert call and call["created_by"] == "agente" and call["status"] == "pronta"

    # Bruno dice sì al telefono
    trasporti.esito_chiamata(rid, "serve", voce_ch.AGENTE)
    _aspetta(casella, 3)
    al_servizio = [m for m in casella if m["dest"] == SERV][-1]
    alla_famiglia = [m for m in casella if m["dest"] == FAM][-1]
    assert al_servizio["irt"] == "<r1@outlook>" and "SERVE" in al_servizio["testo"] and rid in al_servizio["ogg"]
    assert "suona al citofono" in alla_famiglia["testo"] and "09:15" in alla_famiglia["testo"]
    with db.tx() as con:
        appunti = [e["text"] for e in con.execute("SELECT text FROM transport_events WHERE request_id=? AND step='appunto'", (rid,))]
    assert appunti and "confermato" in appunti[-1]


def test_risposta_non_chiara_va_all_accettazione(casella):
    with db.tx() as con:
        rid = trasporti.start(con, None, "P05", "A-05", "T2", {"username": "m.bianchi", "name": "Marta"})
    assert posta.gestisci_risposta(rid, SERV, "<r2@x>", "ok, passiamo alle 6:00") == "da leggere"  # fuori finestra
    with db.tx() as con:
        assert con.execute("SELECT status FROM transport_requests WHERE id=?", (rid,)).fetchone()[0] == "attesa_servizio"


def test_no_annulla_e_avvisa(casella):
    with db.tx() as con:
        rid = trasporti.start(con, None, "P05", "A-05", "T2", {"username": "m.bianchi", "name": "Marta"})
    posta.gestisci_risposta(rid, SERV, "<r3@x>", "ok alle 9")
    trasporti.esito_chiamata(rid, "non_serve", voce_ch.AGENTE)
    _aspetta(casella, 3)
    assert "NON serve" in [m for m in casella if m["dest"] == SERV][-1]["testo"]
    assert any(m["dest"] == FAM for m in casella)


def test_mai_mail_vere_a_indirizzi_finti(casella):
    assert posta.indirizzo("servizi@croceverde.example") is None
    assert posta.indirizzo("famigliapaziente@outlook.it (per la figlia Paola)") == FAM


def test_risposta_doppia_non_disturba(casella):
    with db.tx() as con:
        rid = trasporti.start(con, None, "P05", "A-05", "T2", {"username": "m.bianchi", "name": "Marta"})
    posta.gestisci_risposta(rid, SERV, "<d1@x>", "ok alle 9:00")
    assert posta.gestisci_risposta(rid, SERV, "<d2@x>", "ok alle 9:00") == "doppione"
    with db.tx() as con:
        assert not con.execute("SELECT 1 FROM transport_events WHERE request_id=? AND step='appunto'", (rid,)).fetchone()


def test_kit_riservato_senza_sovrapposizioni(casella, monkeypatch):
    from app import kit
    monkeypatch.setenv("MAIL_AMBULATORI_PROVA", "coordinamentoambulatori@outlook.it")
    with db.tx() as con:
        kit.assicura(con)
        a = dict(con.execute("SELECT * FROM appointments WHERE id='A-05'").fetchone())
        p = dict(con.execute("SELECT * FROM patients WHERE id='P05'").fetchone())
        primo = kit.riserva(con, "C1", p, a, {"username": "m.bianchi", "name": "Marta"})
        di_nuovo = kit.riserva(con, "C1", p, a, {"username": "m.bianchi", "name": "Marta"})
        # altre tre visite alla stessa ora: due trovano un kit, la terza no
        esiti = []
        for i in range(3):
            con.execute("INSERT INTO appointments VALUES (?,?,?,?,?,?,?)", (f"AX{i}", "P01", "visita", "cardiologia", a["at"], "pad. B", "prenotata"))
            esiti.append(kit.riserva(con, "C2", p, dict(con.execute("SELECT * FROM appointments WHERE id=?", (f"AX{i}",)).fetchone()),
                                     {"username": "m.bianchi", "name": "Marta"})[0])
    assert primo[0].startswith("Kit 1") and "già riservato" in di_nuovo[0]
    assert esiti[0].startswith("Kit 2") and esiti[1].startswith("Kit 3") and "Nessun kit libero" in esiti[2]
    _aspetta(casella, 3)
    m = [x for x in casella if x["dest"] == "coordinamentoambulatori@outlook.it"]
    assert m and "Kit 1" in m[0]["ogg"] and "entro le 09:50" in m[0]["testo"] and "ipoacusia" not in m[0]["testo"]


def test_la_risposta_arriva_dalla_casella_con_il_codice_vero(casella, monkeypatch):
    """La risposta passa dalla casella IMAP, con l'oggetto della nostra richiesta: il codice pratica va riconosciuto
    qualunque sia la sua lunghezza (il 23/10 il codice era passato a 8 caratteri e la casella cercava ancora 5)."""
    import email.message
    user = {"username": "m.bianchi", "name": "Marta Bianchi"}
    with db.tx() as con:
        rid = trasporti.start(con, None, "P05", "A-05", "T2", user)
    _aspetta(casella, 1)
    m = email.message.EmailMessage()
    m["From"], m["Subject"], m["Message-ID"] = f"Croce Verdissima <{SERV}>", "R: " + casella[0]["ogg"], "<casella1@outlook>"
    m.set_content("Ok, passiamo alle 9:15.")
    lette = []

    class Finta:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def select(self, *a, **k): return "OK", [b"1"]
        def search(self, *a): return "OK", [b"1"]
        def fetch(self, num, what): return "OK", [(b"1 (BODY[] {1})", m.as_bytes())]
        def store(self, num, *a): lette.append(num)

    monkeypatch.setattr(posta.imaplib, "IMAP4_SSL", Finta)
    assert posta.leggi_casella() == 1 and lette == [b"1"]
    with db.tx() as con:
        arrivata = con.execute("SELECT rid, esito FROM mail_in WHERE msgid='<casella1@outlook>'").fetchone()
        r = con.execute("SELECT status, pickup FROM transport_requests WHERE id=?", (rid,)).fetchone()
    assert arrivata and arrivata["rid"] == rid and arrivata["esito"] == "si"
    assert r["status"] == "attesa_paziente" and r["pickup"] == "09:15"
