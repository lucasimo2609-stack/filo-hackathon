"""Scrive nel .env le chiavi per la prova dal vivo, senza mostrarle a schermo.

    python3 strumenti/chiavi.py

Controlla il formato di quello che incolli: se sbagli campo te lo dice e non scrive niente.
"""
import getpass
import re
from pathlib import Path

ENV = Path(__file__).resolve().parent.parent / ".env"

CAMPI = [
    ("TWILIO_ACCOUNT_SID", "Twilio Account SID (inizia con AC)", False, r"AC[0-9a-f]{32}"),
    ("TWILIO_AUTH_TOKEN", "Twilio Auth Token (32 caratteri)", True, r"[0-9a-f]{32}"),
    ("SMTP_PASSWORD", "Password per le app di Gmail (16 lettere)", True, r"[a-z]{16}"),
]


def main():
    valori = {}
    for chiave, domanda, nascosto, formato in CAMPI:
        for _ in range(3):
            v = (getpass.getpass(domanda + ": ") if nascosto else input(domanda + ": ")).strip().replace(" ", "")
            if not v:
                print("  vuoto: lo salto")
                break
            if re.fullmatch(formato, v):
                valori[chiave] = v
                print("  ok")
                break
            print(f"  non sembra giusto ({len(v)} caratteri): riprova, incolla solo il valore")
    if not valori:
        print("Niente da scrivere.")
        return
    testo = ENV.read_text(encoding="utf-8")
    for k, v in valori.items():
        riga = f"{k}={v}"
        testo = re.sub(rf"(?m)^{k}=.*$", lambda _m: riga, testo) if re.search(rf"(?m)^{k}=", testo) else testo.rstrip("\n") + "\n" + riga + "\n"
    ENV.write_text(testo, encoding="utf-8")
    ENV.chmod(0o600)
    print("Scritto nel .env: " + ", ".join(valori) + ". Fatto.")


if __name__ == "__main__":
    main()
