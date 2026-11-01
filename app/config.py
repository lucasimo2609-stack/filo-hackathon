"""Configurazione di Filo. Tutto si regola da .env (chiavi) e da qui (interruttori)."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("FILO_DATA", ROOT / "data"))
OUTBOX = DATA / "posta_in_uscita"
DB_PATH = Path(os.getenv("FILO_DB", DATA / "filo.db"))
# Un cookie di sessione per ogni copia di Filo: i browser legano i cookie al nome (localhost), non alla porta, e due copie
# aperte nello stesso browser (8000, 8001...) con lo stesso nome si buttavano fuori a vicenda.
SESSION_COOKIE = os.getenv("FILO_COOKIE") or "filo_" + __import__("hashlib").sha1(str(DB_PATH.resolve()).encode()).hexdigest()[:8]

load_dotenv(ROOT / ".env")

# --- Fastweb AI Factory (API dirette) ---------------------------------------
FW_BASE_URL = os.getenv("FW_BASE_URL", "https://spod.ai-factory.fastweb.it/v1")
FW_KEYS = {
    "google/gemma-4-26B-A4B-it": os.getenv("FW_KEY_GEMMA4"),
    "Qwen/Qwen3.5-397B-A17B-FP8": os.getenv("FW_KEY_QWEN35_397B"),
}

# --- Piattaforma FastwebAI Agents (Sim) -------------------------------------
SIM_BASE_URL = os.getenv("SIM_BASE_URL", "https://platform.fastwebai-agents.it")
SIM_API_KEY = os.getenv("SIM_API_KEY")  # da creare in Settings > Sim Keys

# Interruttore per agente: "api" = chiamata diretta al modello Fastweb,
# "sim" = flusso pubblicato sulla piattaforma (serve l'id del flusso).
# Se "sim" fallisce, Filo ripiega da solo su "api" e lo scrive nel registro.
AGENTS = {
    "cure":      {"backend": os.getenv("AGENT_CURE_BACKEND", "api"),      "sim_workflow": os.getenv("SIM_WF_CURE"),      "model": "google/gemma-4-26B-A4B-it"},
    "trasporti": {"backend": os.getenv("AGENT_TRASPORTI_BACKEND", "api"), "sim_workflow": os.getenv("SIM_WF_TRASPORTI"), "model": "google/gemma-4-26B-A4B-it"},
    "aderenza":  {"backend": os.getenv("AGENT_ADERENZA_BACKEND", "api"),  "sim_workflow": os.getenv("SIM_WF_ADERENZA"),  "model": "google/gemma-4-26B-A4B-it"},
    "agende":    {"backend": os.getenv("AGENT_AGENDE_BACKEND", "api"),    "sim_workflow": os.getenv("SIM_WF_AGENDE"),    "model": "google/gemma-4-26B-A4B-it"},
}

# --- Lettore locale (llama.cpp o qualsiasi server compatibile OpenAI in locale) --
LOCAL_LLM_URL = os.getenv("LOCAL_LLM_URL", "http://127.0.0.1:8080/v1")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "local")
LOCAL_LLM_ENABLED = os.getenv("LOCAL_LLM_ENABLED", "auto")  # auto | on | off

# --- Cancello ----------------------------------------------------------------
K_MIN = int(os.getenv("FILO_K_MIN", "10"))          # persone minime con la stessa combinazione
L_MIN = int(os.getenv("FILO_L_MIN", "3"))           # malattie diverse minime nel gruppo
BATCH_SIZE = int(os.getenv("FILO_BATCH_SIZE", "6"))
ATTESTATION_MODE = os.getenv("FILO_ATTESTATION", "simulated")  # simulated | report | off
ATTESTATION_REPORT = os.getenv("FILO_ATTESTATION_REPORT")  # file JSON del report della AI Factory (modalità "report")
# Modelli ammessi per dati sanitari: solo quelli che girano in area UE nella AI Factory (verificato il 22/10).
EU_MODELS = {m.strip() for m in os.getenv("FILO_EU_MODELS", "google/gemma-4-26B-A4B-it").split(",") if m.strip()}
FIXED_IDS_FOR_TEST = False  # solo per il test dell'attaccante: NON usare mai in esercizio

# --- Sessioni ------------------------------------------------------------------
_secret_file = DATA / ".session_secret"


def session_secret() -> str:
    env = os.getenv("FILO_SESSION_SECRET")
    if env:
        return env
    DATA.mkdir(parents=True, exist_ok=True)
    if not _secret_file.exists():
        _secret_file.write_text(secrets.token_hex(32))
        _secret_file.chmod(0o600)
    return _secret_file.read_text().strip()


IMPORT_DIR = Path(os.getenv("FILO_IMPORT_DIR", DATA / "import"))  # es. cartella SharePoint o Drive sincronizzata

# --- Agente vocale (ElevenLabs per la voce, Fastweb per il ragionamento) -------------
VOCE_MODEL = os.getenv("VOCE_MODEL", "google/gemma-4-26B-A4B-it")
VOCE_PORT = int(os.getenv("VOCE_PORT", "8010"))
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVEN_AGENT_ID = os.getenv("ELEVEN_AGENT_ID")
ELEVEN_PHONE_NUMBER_ID = os.getenv("ELEVEN_PHONE_NUMBER_ID")     # numero Twilio o SIP importato in ElevenLabs
ELEVEN_WEBHOOK_SECRET = os.getenv("ELEVEN_WEBHOOK_SECRET")
VOCE_NUMERO_PROVA = os.getenv("VOCE_NUMERO_PROVA")  # le chiamate vere vanno SOLO qui: i numeri dei pazienti sono finti
NUMERO_CENTRALE = os.getenv("NUMERO_CENTRALE", "041 000 0000")


def voce_token() -> str:
    """Codice che ElevenLabs deve presentare per usare il cervello della voce."""
    env = os.getenv("VOCE_LLM_TOKEN")
    if env:
        return env
    f = DATA / ".voce_token"
    DATA.mkdir(parents=True, exist_ok=True)
    if not f.exists():
        f.write_text(secrets.token_urlsafe(32))
        f.chmod(0o600)
    return f.read_text().strip()


TODAY = os.getenv("FILO_TODAY", "2026-10-22")  # data della "mattina in centrale": i dati sintetici sono costruiti su questo giorno
NEW_DOCS_DAYS = int(os.getenv("FILO_NEW_DOCS_DAYS", "7"))  # "documenti nuovi" = arrivati negli ultimi N giorni
AUTO_RUN_AT = os.getenv("FILO_AUTO_RUN_AT", "")  # es. "07:00": elaborazione automatica ogni mattina (vuoto = solo dal pulsante)
HTTPS_ONLY = os.getenv("FILO_HTTPS", "0") == "1"  # cookie Secure: da accendere quando Filo sta dietro HTTPS
