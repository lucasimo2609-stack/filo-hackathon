"""Database locale dell'ospedale (SQLite). Qui stanno nomi, note originali e tabella delle chiavi."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY, name TEXT, role TEXT CHECK(role IN ('segreteria','medico')),
  pw_salt BLOB, pw_hash BLOB, totp_secret TEXT, failed INTEGER DEFAULT 0, locked_until TEXT,
  title TEXT, first_name TEXT, last_name TEXT, qualifica TEXT, struttura TEXT, reparto TEXT, theme TEXT
);
CREATE TABLE IF NOT EXISTS patients (
  id TEXT PRIMARY KEY, name TEXT, birth_year INTEGER, address TEXT, phone TEXT,
  quartiere TEXT, distretto TEXT, lives_alone INTEGER, has_car INTEGER, hearing_support INTEGER,
  mmg TEXT, birth_date TEXT, sex TEXT, cf TEXT UNIQUE, consenso_fse INTEGER DEFAULT 0, origin TEXT DEFAULT 'anagrafe',
  email TEXT, contatto TEXT
);
CREATE TABLE IF NOT EXISTS notes (
  id TEXT PRIMARY KEY, patient_id TEXT, kind TEXT, source TEXT, date TEXT, text TEXT, title TEXT, author TEXT,
  file TEXT, sha256 TEXT, mime TEXT, origin TEXT, imported_at TEXT, imported_by TEXT, read_with TEXT
);
CREATE TABLE IF NOT EXISTS inbox (
  id TEXT PRIMARY KEY, file TEXT, sha256 TEXT, mime TEXT, original_name TEXT, text TEXT, reason TEXT, origin TEXT,
  imported_at TEXT, imported_by TEXT, status TEXT DEFAULT 'da smistare', read_with TEXT
);
CREATE TABLE IF NOT EXISTS import_jobs (
  id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, started_by TEXT, origin TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS appointments (
  id TEXT PRIMARY KEY, patient_id TEXT, service TEXT, specialty TEXT, at TEXT, place TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS waitlist (
  id TEXT PRIMARY KEY, patient_id TEXT, service TEXT, priority TEXT, since TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS freed_slots (
  id TEXT PRIMARY KEY, service TEXT, at TEXT, place TEXT, freed_at TEXT, assigned_to TEXT
);
CREATE TABLE IF NOT EXISTS external_events (
  id TEXT PRIMARY KEY, kind TEXT, date TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS transport_services (
  id TEXT PRIMARY KEY, name TEXT, distretti TEXT, rules TEXT, notice_days INTEGER, email TEXT,
  booking TEXT, response TEXT, cost TEXT
);
CREATE TABLE IF NOT EXISTS residents (
  age INTEGER, quartiere TEXT, distretto TEXT, condition TEXT, lives_alone INTEGER, has_car INTEGER
);
CREATE TABLE IF NOT EXISTS signals (
  patient_id TEXT, field TEXT, value TEXT, evidence_id TEXT, extractor TEXT, snippet TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, started_by TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS vault (
  code TEXT, run_id TEXT, patient_id TEXT, agent TEXT, fields TEXT, evidence TEXT, created_at TEXT,
  PRIMARY KEY (run_id, code)
);
CREATE TABLE IF NOT EXISTS packets_out (
  run_id TEXT, batch INTEGER, seq INTEGER, code TEXT, agent TEXT, payload TEXT, sent_at TEXT, backend TEXT
);
CREATE TABLE IF NOT EXISTS results_in (
  run_id TEXT, code TEXT, agent TEXT, output TEXT, received_at TEXT, ms INTEGER
);
CREATE TABLE IF NOT EXISTS gate_checks (
  run_id TEXT, patient_id TEXT, agent TEXT, k INTEGER, l INTEGER, generalized TEXT
);
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, run_id TEXT, patient_id TEXT, role TEXT, kind TEXT, score INTEGER, parts TEXT,
  confidence TEXT, tag TEXT, summary TEXT, action TEXT, evidence TEXT, role_note TEXT, payload TEXT,
  status TEXT DEFAULT 'pending', decided_by TEXT, decided_at TEXT, ckey TEXT
);
CREATE INDEX IF NOT EXISTS cases_ckey ON cases(ckey);
CREATE TABLE IF NOT EXISTS answers (
  hash TEXT PRIMARY KEY, agent TEXT, day TEXT, output TEXT, backend TEXT
);
CREATE TABLE IF NOT EXISTS used_codes (username TEXT, code TEXT, ts REAL, PRIMARY KEY (username, code));
CREATE TABLE IF NOT EXISTS sessions (sid TEXT PRIMARY KEY, username TEXT, created_at TEXT, last_seen REAL);
CREATE TABLE IF NOT EXISTS visit_assessments (
  id TEXT PRIMARY KEY, patient_id TEXT, at TEXT, doctor TEXT, urgency TEXT, flags TEXT, note TEXT, appointment_id TEXT
);
CREATE TABLE IF NOT EXISTS transport_requests (
  id TEXT PRIMARY KEY, case_id TEXT, patient_id TEXT, appointment_id TEXT, service_id TEXT, status TEXT,
  pickup TEXT, tried TEXT, created_at TEXT, updated_at TEXT, visit_at TEXT, reply_by TEXT, next_call_at TEXT
);
CREATE TABLE IF NOT EXISTS transport_events (request_id TEXT, ts TEXT, who TEXT, step TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS kits (id TEXT PRIMARY KEY, label TEXT, contenuto TEXT);
CREATE TABLE IF NOT EXISTS kit_bookings (id TEXT PRIMARY KEY, kit_id TEXT, appointment_id TEXT, patient_id TEXT, at TEXT, status TEXT, ts TEXT, nota TEXT);
CREATE TABLE IF NOT EXISTS mail_threads (rid TEXT, msgid TEXT, ts TEXT, verso TEXT);
CREATE TABLE IF NOT EXISTS mail_in (msgid TEXT PRIMARY KEY, rid TEXT, ts TEXT, sender TEXT, text TEXT, esito TEXT);
CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY, patient_id TEXT, kind TEXT, ref_id TEXT, role TEXT, status TEXT, channel TEXT,
  identity_ok INTEGER DEFAULT 0, identity_fails INTEGER DEFAULT 0, alarm INTEGER DEFAULT 0, outcome TEXT,
  conversation_id TEXT, created_by TEXT, created_at TEXT, started_at TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS call_events (call_id TEXT, ts TEXT, kind TEXT, text TEXT, clinical INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS call_turns (call_id TEXT, ts TEXT, speaker TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS action_log (ts TEXT, username TEXT, case_id TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS access_log (ts TEXT, username TEXT, action TEXT, target TEXT);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, case_id TEXT, role TEXT, recipient TEXT, subject TEXT, body TEXT, ics TEXT,
  html TEXT, sender TEXT, sent_to TEXT
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


@contextmanager
def tx():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


# colonne aggiunte dopo la prima versione: un database già esistente si aggiorna da solo
_ADDED = {"kit_visite": ["invio_a TEXT"], "transport_requests": ["visit_at TEXT", "reply_by TEXT", "next_call_at TEXT"], "cases": ["ckey TEXT"],
          "users": ["title TEXT", "first_name TEXT", "last_name TEXT", "qualifica TEXT", "struttura TEXT", "reparto TEXT", "theme TEXT"]}


def init():
    with tx() as con:
        for table, cols in _ADDED.items():
            have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            for c in cols:
                if have and c.split()[0] not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {c}")
        con.executescript(SCHEMA)
        from . import posti  # tabelle delle proposte dei posti liberati (con gli indici unici che tengono un posto a una persona)
        con.executescript(posti.SCHEMA)
        from . import flussi  # flussi sulla piattaforma: chi ha fatto ogni passo (workflow o riserva locale)
        con.executescript(flussi.SCHEMA)
        from . import calendario  # agende e prenotazioni: lo strumento Calendario dell'agente Agende
        con.executescript(calendario.SCHEMA)
        from . import modifiche  # modifiche alle prenotazioni in attesa della conferma del paziente
        con.executescript(modifiche.SCHEMA)
        from . import kit_visita  # la visita col kit: trascrizione dal vivo e invio alla persona
        con.executescript(kit_visita.SCHEMA)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_access(username: str, action: str, target: str = ""):
    with tx() as con:
        con.execute("INSERT INTO access_log VALUES (?,?,?,?)", (now(), username, action, target))


def log_action(username: str, case_id: str, text: str):
    with tx() as con:
        con.execute("INSERT INTO action_log VALUES (?,?,?,?)", (now(), username, case_id, text))


def jl(v):
    return json.loads(v) if v else None
