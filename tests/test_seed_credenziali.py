"""Un database di prova (FILO_DB altrove) non deve sovrascrivere credenziali e QR veri in data/: il 23/10 è successo."""
import os
import subprocess
import sys
from pathlib import Path


def test_credenziali_accanto_al_database(tmp_path):
    dati, prova = tmp_path / "data", tmp_path / "prova"
    dati.mkdir(); prova.mkdir()
    env = {**os.environ, "FILO_DATA": str(dati), "FILO_DB": str(prova / "x.db")}
    r = subprocess.run([sys.executable, "-m", "app.seed", "--reset", "--nuovi-utenti"], cwd=Path(__file__).resolve().parent.parent,
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    assert (prova / "credenziali_demo.txt").exists() and (prova / "qr_m.bianchi.png").exists()
    assert not (dati / "credenziali_demo.txt").exists() and not (dati / "qr_m.bianchi.png").exists()
