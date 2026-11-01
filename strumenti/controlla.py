"""Controllo prima della prova dal vivo: posta (invio e lettura) e numero di telefono su ElevenLabs.

    .venv/bin/python -m strumenti.controlla
"""
import imaplib
import os
import smtplib
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402  (carica il .env)


def posta():
    host, user, pw = os.getenv("SMTP_HOST"), os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
    try:
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(user, pw)
        print("Invio mail: ok")
    except Exception as e:
        print(f"Invio mail: NON va ({str(e)[:150]})")
    try:
        imap = os.getenv("IMAP_HOST") or host.replace("smtp.", "imap.", 1)
        with imaplib.IMAP4_SSL(imap, 993) as im:
            im.login(user, pw)
            im.select("INBOX")
            print("Lettura mail: ok")
    except Exception as e:
        print(f"Lettura mail: NON va ({str(e)[:150]})")


def telefono():
    from app.voce import telefono as t
    try:
        t.main()
    except SystemExit as e:
        print(f"Telefono: {e}")
    except Exception as e:
        print(f"Telefono: NON va (codice {getattr(e, 'status_code', '?')}): {getattr(e, 'body', None) or str(e)[-400:]}")


if __name__ == "__main__":
    posta()
    telefono()
