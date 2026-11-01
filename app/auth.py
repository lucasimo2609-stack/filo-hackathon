"""Accesso con doppia autenticazione: password (scrypt) + codice a tempo (TOTP, RFC 6238)."""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta

import pyotp

from . import db

MAX_FAILED = 5
LOCK_MINUTES = 5


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt, h


def _locked(row) -> bool:
    return bool(row["locked_until"]) and datetime.fromisoformat(row["locked_until"]) > datetime.now()


def _fail(username: str, failed: int):
    lock = (datetime.now() + timedelta(minutes=LOCK_MINUTES)).isoformat() if failed + 1 >= MAX_FAILED else None
    with db.tx() as con:
        con.execute("UPDATE users SET failed=?, locked_until=? WHERE username=?",
                    (0 if lock else failed + 1, lock, username))


# Tentativi di password sbagliati, per (utente, postazione). Il blocco non tocca il medico che entra da un'altra postazione:
# chi conosce solo il nome utente non può più chiudergli l'accesso. In memoria: un riavvio azzera i contatori, non i blocchi
# del codice a tempo (che restano nel database).
_pw_fails: dict[tuple[str, str], tuple[int, datetime | None]] = {}
GENERIC = "Utente, ruolo o password non corretti."


def _pw_locked(key) -> bool:
    n, until = _pw_fails.get(key, (0, None))
    return bool(until and until > datetime.now())


def _pw_fail(key):
    n, until = _pw_fails.get(key, (0, None))
    n += 1
    _pw_fails[key] = (0, datetime.now() + timedelta(minutes=LOCK_MINUTES)) if n >= MAX_FAILED else (n, None)


def check_password(username: str, password: str, role: str, client: str = "?") -> tuple[bool, str]:
    """Stessa risposta e stesso tempo che l'utente esista o no, e anche quando la postazione è bloccata."""
    key = (username, client)
    with db.tx() as con:
        row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    salt = row["pw_salt"] if row else None
    _, h = hash_password(password, salt)  # sempre: il tempo di risposta non dice niente
    if _pw_locked(key):
        return False, GENERIC + f" Troppi tentativi da questa postazione: riprova tra {LOCK_MINUTES} minuti."
    if not row or row["role"] != role or not hmac.compare_digest(h, row["pw_hash"]):
        _pw_fail(key)
        return False, GENERIC
    _pw_fails.pop(key, None)
    return True, ""


def check_totp(username: str, code: str) -> tuple[bool, str]:
    with db.tx() as con:
        row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        return False, "Sessione non valida, ricomincia l'accesso."
    if _locked(row):
        return False, f"Troppi tentativi: accesso bloccato per {LOCK_MINUTES} minuti."
    if pyotp.TOTP(row["totp_secret"]).verify((code or "").strip(), valid_window=1):
        with db.tx() as con:
            con.execute("UPDATE users SET failed=0, locked_until=NULL WHERE username=?", (username,))
        return True, ""
    _fail(username, row["failed"])
    return False, "Codice non valido o scaduto."


def get_user(username: str):
    with db.tx() as con:
        return con.execute("SELECT username, name, role, title, first_name, last_name, qualifica, struttura, reparto, theme "
                           "FROM users WHERE username=?", (username,)).fetchone()
