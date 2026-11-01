"""Il cancello: dal cloud non deve mai uscire niente che identifichi una persona."""
import json
import re

from app import db, gateway, seed


def _outgoing():
    with db.tx() as con:
        return [r["payload"] for r in con.execute("SELECT payload FROM packets_out")]


def test_nessun_dato_identificativo_esce(run):
    """Spie: nomi, vie, telefoni, medici, id interni e testo delle note non devono comparire nei pacchetti."""
    out = "\n".join(_outgoing()).lower()
    assert out, "nessun pacchetto uscito"
    spie = []
    for p in seed.PATIENTS:
        pid, name, year, addr, phone, quart, _, _, _, _, mmg = p
        spie += [pid.lower(), *name.lower().split(), addr.lower(), phone.lower(), phone.replace(" ", ""), quart.lower(), mmg.lower(), str(year)]
    for n in seed.NOTES:
        spie += [n[0].lower(), n[5].lower()[:40]]
    spie += ["marco", "torino", "347", "ferramenta", "falegname", "piazza ferretto"]
    trovate = [s for s in set(spie) if len(s) >= 3 and re.search(r"(?<![a-zà-ù])" + re.escape(s) + r"(?![a-zà-ù])", out)]
    assert not trovate, f"dati identificativi usciti: {trovate}"


def test_solo_campi_ammessi(run):
    ammessi = {"codice", "tipo", "farmaci", "classe", "principio_attivo", "prescritto_da", "condizioni", "terapia", "giorni_sospensione", "motivo", "visita", "caldo_quel_giorno",
               "giorni_alla_visita", "fascia_eta", "distretto", "vive_solo", "senza_auto", "sciopero_quel_giorno", "esame",
               "richiesto_da", "intervallo_giorni", "giorni_al_posto", "candidati", "priorita", "giorni_attesa", "fragilita",
               "servizi_possibili", "id", "nome", "regole", "costo",
               "calendario", "prenotazione_esistente", "posti_liberi_30_giorni"}

    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)
    for p in _outgoing():
        extra = set(keys(json.loads(p))) - ammessi
        assert not extra, f"campi non previsti: {extra}"


def test_malattia_rara_generalizzata(run):
    out = "\n".join(_outgoing())
    assert "amiloidosi" not in out.lower()
    with db.tx() as con:
        g = con.execute("SELECT * FROM gate_checks WHERE patient_id='P05' AND agent='cure'").fetchone()
    assert g["k"] < 10 and g["generalized"]


def test_ogni_pacchetto_trasporti_ha_almeno_k_simili(run):
    with db.tx() as con:
        for g in con.execute("SELECT * FROM gate_checks WHERE agent='trasporti'"):
            assert g["k"] >= 10


def test_controllo_locale_corregge_il_servizio(run):
    """L'agente finto propone sempre T1 (STACCO, 3 giorni di preavviso): per la visita di domani il controllo locale deve cambiarlo."""
    with db.tx() as con:
        c = con.execute("SELECT * FROM cases WHERE kind='trasporto' AND patient_id='P05'").fetchone()
    assert "controllo locale" in c["action"] and "Croce Verde" in c["action"]


def test_testo_estratto_solo_da_lista_chiusa():
    from app.extract import extract
    note = {"id": "X", "kind": "nota_reparto", "source": "medicina",
            "text": "Il sig. Mario Bianchi, via Roma 3, tel 333 1112223, vive solo e prende warfarin."}
    sig, _ = extract(note, use_llm=False)
    blob = json.dumps([{"field": x["field"], "value": x["value"]} for x in sig]).lower()  # la frase di fonte resta in ospedale
    assert "mario" not in blob and "roma" not in blob and "333" not in blob
    assert ("vive_solo", "sì") in {(x["field"], x["value"]) for x in sig}
    assert any(x["snippet"] and "vive solo" in x["snippet"] for x in sig)


def test_le_frasi_di_fonte_non_escono(run):
    out = "\n".join(_outgoing()).lower()
    with db.tx() as con:
        frasi = [r[0].lower() for r in con.execute("SELECT DISTINCT snippet FROM signals WHERE snippet IS NOT NULL")]
    assert frasi and not [f for f in frasi if len(f) > 12 and f in out]


def test_controllo_delle_risposte():
    from app.gateway import check_output
    ok, _ = check_output("cure", {"da_verificare": "false", "gravita": "3", "spiegazione": "x"}, {})
    assert ok == {"da_verificare": False, "gravita": 3, "spiegazione": "x", "fonte": []}
    assert check_output("cure", {"da_verificare": True, "gravita": "alta"}, {})[0] is None
    assert check_output("aderenza", {"rilevante": True, "urgenza": 7}, {})[0] is None
    t, _ = check_output("trasporti", {"serve_trasporto": True, "servizio": "T2", "partenza": "domattina"}, {})
    assert t["partenza"] is None
    sent = {"tipo": "posto_liberato", "candidati": [{"codice": "F-aa"}, {"codice": "F-bb"}]}
    assert check_output("agende", {"codice_scelto": "P03"}, sent)[0] is None
    assert check_output("agende", {"codice_scelto": "F-bb"}, sent)[0]["codice_scelto"] == "F-bb"
    assert check_output("cure", {"errore": "timeout"}, {})[0] is None
