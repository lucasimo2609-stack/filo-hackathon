"""Importazione dei documenti originali: tutto locale, paziente riconosciuto dal codice fiscale."""
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from app import db, gateway, importa

ROOT = Path(__file__).resolve().parent.parent
ZIP = ROOT / "esempi" / "importazione_esempio_40_pazienti.zip"


@pytest.mark.skipif(not ZIP.exists(), reason="pacchetto di esempio non generato")
def test_pacchetto_di_esempio():
    r = importa.import_zip(ZIP.read_bytes(), ZIP.name, "test")
    assert r["pazienti_nuovi"] == 40
    assert r["doppioni"] == 1
    assert r["da_smistare"] == 1 and r["errori"] == 0
    with db.tx() as con:
        assert con.execute("SELECT COUNT(*) FROM notes WHERE patient_id='P06' AND origin LIKE 'pacchetto%'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM notes WHERE patient_id='P07' AND origin LIKE 'pacchetto%'").fetchone()[0] == 1
        # ogni documento importato ha l'originale con la sua impronta
        for n in con.execute("SELECT file, sha256 FROM notes WHERE origin LIKE 'pacchetto%'"):
            assert importa.verify(n["file"], n["sha256"])


@pytest.mark.skipif(not ZIP.exists(), reason="pacchetto di esempio non generato")
def test_dopo_l_importazione_al_cloud_non_esce_nessun_nome():
    importa.import_zip(ZIP.read_bytes(), ZIP.name, "test")
    gateway.run("test")
    with db.tx() as con:
        out = "\n".join(r[0] for r in con.execute("SELECT payload FROM packets_out")).lower()
        nomi = [r[0].lower() for r in con.execute("SELECT name FROM patients WHERE origin LIKE 'pacchetto%'")]
        cfs = [r[0].lower() for r in con.execute("SELECT cf FROM patients")]
    assert nomi
    for n in nomi:
        for parte in n.split():
            if len(parte) > 3:
                assert not re.search(rf"\b{re.escape(parte)}\b", out), parte
    assert not [c for c in cfs if c in out]


def test_documento_di_un_altro_paziente_non_viene_agganciato():
    src = ROOT / "esempi" / "archivio_originali" / "E-061.pdf"  # di Giuseppe Rossato
    if not src.exists():
        pytest.skip("originali non generati")
    with db.tx() as con:
        con.execute("DELETE FROM notes WHERE id='E-061'")
        r = importa.ingest(con, src.read_bytes(), "referto.pdf", "caricato a mano", "test", forced_pid="P02")
    assert r["esito"] == "da smistare" and "non è quello di" in r["motivo"]


def test_originale_alterato_viene_segnalato():
    with db.tx() as con:
        n = con.execute("SELECT file, sha256 FROM notes WHERE file IS NOT NULL LIMIT 1").fetchone()
    if not n:
        pytest.skip("originali non generati")
    p = importa.ARCHIVE / n["file"]
    assert importa.verify(n["file"], n["sha256"])
    p.chmod(0o640); p.write_bytes(p.read_bytes() + b"%modificato")
    assert not importa.verify(n["file"], n["sha256"])


def test_cda2_xml():
    xml = b"""<ClinicalDocument xmlns="urn:hl7-org:v3"><title>Referto di laboratorio</title><effectiveTime value="20261020"/>
<recordTarget><patientRole><id root="2.16.840.1.113883.2.9.4.3.2" extension="ZNLNNA47L42L736S"/><patient><name><given>Anna</given><family>Zanella</family></name>
<birthTime value="19470702"/></patient></patientRole></recordTarget>
<component><structuredBody><component><section><text>Potassio 5,1 mmol/l</text></section></component></structuredBody></component></ClinicalDocument>"""
    text, how = importa.read_text(xml, ".xml")
    h = importa.parse(text)
    assert how == "CDA2 (XML)" and h["date"] == "2026-10-20" and "Potassio" in h["body"]
