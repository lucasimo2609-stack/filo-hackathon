"""Cervello della voce: un servizio SEPARATO da Filo, l'unico da esporre su internet (tunnel) per ElevenLabs.

    uvicorn app.voce.server:app --port 8010

ElevenLabs lo usa come "Custom LLM" (formato OpenAI Chat Completions, in streaming). Per ogni turno:
1. controlla il codice segreto;
2. trova la chiamata (call_id passato all'avvio), registra la frase della persona;
3. se nella frase c'è un segnale d'allarme risponde con un testo FISSO e avvisa il medico, senza modello;
4. altrimenti costruisce QUI il prompt (solo ciò che la fase della chiamata permette) e chiede a Gemma 4 su Fastweb;
5. gli strumenti di Filo li esegue Filo, con i controlli; solo quelli di sistema (chiudi, segreteria) tornano a ElevenLabs.
Le schermate di Filo NON sono servite da qui.
"""
from __future__ import annotations

import threading

import asyncio

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AsyncOpenAI

from .. import db
from ..config import ELEVEN_WEBHOOK_SECRET, FW_BASE_URL, FW_KEYS, VOCE_MODEL, voce_token
from . import chiamata as ch

app = FastAPI(title="Filo - voce", docs_url=None, redoc_url=None, openapi_url=None)

from .. import flussi as _flussi  # noqa: E402  regia sulla piattaforma: /filo/... (permesso firmato per ogni flusso)
app.include_router(_flussi.router())
from .. import flussi_kit as _flussi_kit  # noqa: E402  agente kit sulla piattaforma: /filo/kit/...
app.include_router(_flussi_kit.router())
_client: AsyncOpenAI | None = None
MAX_GIRI = 3


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        key = FW_KEYS.get(VOCE_MODEL) or FW_KEYS.get("google/gemma-4-26B-A4B-it")
        _client = AsyncOpenAI(base_url=FW_BASE_URL, api_key=key, timeout=30, max_retries=1)
    return _client


def _auth(request: Request):
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not got or not secrets.compare_digest(got, voce_token()):
        raise HTTPException(401, "non autorizzato")


def _chunk(cid_resp, delta: dict, finish=None) -> str:
    return "data: " + json.dumps({"id": cid_resp, "object": "chat.completion.chunk", "created": int(time.time()), "model": "filo-voce",
                                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}, ensure_ascii=False) + "\n\n"


_MOJI = re.compile(r"[\xc2-\xf4][\x80-\xbf]+")


class Accenti:
    """La AI Factory, in streaming, a volte manda i caratteri accentati come byte singoli (es. "Ã " al posto di "à").
    Ripara le sequenze riconoscibili e tiene da parte l'ultimo carattere se potrebbe essere l'inizio di una sequenza spezzata."""

    def __init__(self):
        self.buf = ""

    @staticmethod
    def _fix(t: str) -> str:
        def rep(m):
            try:
                return m.group(0).encode("latin-1").decode("utf-8")
            except UnicodeDecodeError:
                return m.group(0)
        return _MOJI.sub(rep, t)

    def feed(self, t: str) -> str:
        self.buf += t
        cut = len(self.buf)
        while cut > 0 and "\x80" <= self.buf[cut - 1] <= "\xbf" or (cut > 0 and "\xc2" <= self.buf[cut - 1] <= "\xf4"):
            cut -= 1
            if cut == 0 or not ("\x80" <= self.buf[cut - 1] <= "\xbf" or "\xc2" <= self.buf[cut - 1] <= "\xf4"):
                break
        out, self.buf = self._fix(self.buf[:cut]), self.buf[cut:]
        return out

    def flush(self) -> str:
        out, self.buf = self._fix(self.buf), ""
        return out


def _call_id(body: dict) -> str | None:
    extra = body.get("elevenlabs_extra_body") or {}
    if isinstance(extra, dict) and re.fullmatch(r"CH-[0-9a-f]{6}", str(extra.get("call_id", ""))):
        return extra["call_id"]
    for m in body.get("messages", []):
        if m.get("role") == "system":
            found = re.search(r"CH-[0-9a-f]{6}", str(m.get("content", "")))
            if found:
                return found.group(0)
    return None


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


@app.get("/salute")
def salute():
    # quale database legge: Filo lo confronta col proprio. Se sono diversi, ogni chiamata cade alla prima risposta ("chiamata sconosciuta")
    from ..config import DB_PATH
    return {"ok": True, "db": hashlib.sha256(str(DB_PATH.resolve()).encode()).hexdigest()[:12]}


DICHIARA = re.compile(r"\b(segn[oaei]|registr|annot|preso nota|confermat|confermo|prenotat|organizzat)", re.I)


async def _forza_esito(cid: str, tool_name: str, msgs: list) -> tuple[bool, list]:
    """Rete di sicurezza: il modello ha chiuso o detto 'ho segnato' senza registrare l'esito. Filo gli chiede SOLO lo strumento.
    Ritorna (registrato, messaggi da aggiungere alla conversazione se Filo ha rifiutato, per far correggere l'agente)."""
    with db.tx() as con:
        cur = ch.carica(con, cid)
    tool = next((t for t in ch.strumenti(cur) if t["function"]["name"] == tool_name), None)
    if not tool:
        return False, []
    nudge = {"role": "user", "content": f"(nota di sistema, non detta dalla persona) Non hai ancora registrato la risposta: "
                                        f"chiama ora {tool_name} con quello che ha detto la persona nella conversazione."}
    try:
        r = await client().chat.completions.create(model=VOCE_MODEL, messages=[{"role": "system", "content": ch.prompt(cur)}] + msgs + [nudge],
                                                   tools=[tool], tool_choice={"type": "function", "function": {"name": tool_name}},
                                                   temperature=0, max_tokens=200)
        tc = (r.choices[0].message.tool_calls or [None])[0]
        raw = tc.function.arguments if tc else "{}"
        args = json.loads(raw or "{}")
    except Exception:
        return False, []
    res = ch.esegui(cid, tool_name, args if isinstance(args, dict) else {})
    ok = res.get("esito") == "registrato"
    with db.tx() as con:
        ch.evento(con, cid, "controllo", "Filo ha chiesto al modello di registrare l'esito prima di chiudere: "
                  + ("fatto." if ok else f"rifiutato da Filo ({res.get('esito') or res.get('errore')}): l'agente deve correggersi con la persona."))
    if ok:
        return True, []
    call_id = f"forza_{uuid.uuid4().hex[:6]}"
    return False, [{"role": "assistant", "content": None,
                    "tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": raw or "{}"}}]},
                   {"role": "tool", "tool_call_id": call_id, "content": json.dumps(
                       {**res, "attenzione": "NON hai ancora confermato nulla: correggi quello che hai detto alla persona e trova con lei una soluzione valida."},
                       ensure_ascii=False)}]


async def _completions(request: Request):
    _auth(request)
    body = await request.json()
    cid = _call_id(body)
    with db.tx() as con:
        c = ch.carica(con, cid) if cid else None
    if not c:
        raise HTTPException(400, "chiamata sconosciuta")

    turns = [m for m in body.get("messages", []) if m.get("role") in ("user", "assistant") and _text(m.get("content")).strip()]
    last_user = next((_text(m["content"]) for m in reversed(turns) if m["role"] == "user"), "")
    if last_user:
        ch.turno(cid, "persona", last_user)
    system_tools = [t for t in (body.get("tools") or []) if t.get("function", {}).get("name") not in ch.LOCALI]
    rid = "chatcmpl-" + uuid.uuid4().hex[:12]

    async def gen():
        yield _chunk(rid, {"role": "assistant", "content": "... "})  # copre l'attesa del modello (indicazione di ElevenLabs)
        label = ch.allarme_in(last_user) if last_user else None
        if label and not c["alarm"]:
            ch.segna_allarme(cid, label, last_user)
            ch.turno(cid, "agente", ch.RISPOSTA_ALLARME)
            yield _chunk(rid, {"content": ch.RISPOSTA_ALLARME})
            yield _chunk(rid, {}, "stop")
            yield "data: [DONE]\n\n"
            return
        msgs = None
        for _ in range(MAX_GIRI):
            with db.tx() as con:
                cur = ch.carica(con, cid)
            sys = ch.prompt(cur)
            if cur["alarm"]:
                sys += ("\n\nATTENZIONE: in questa chiamata è già stato segnalato un allarme e l'infermiere è stato avvisato. "
                        "Non fare altre domande: rassicura, ricorda il 112 se sta male, saluta e chiudi la chiamata.")
            if msgs is None:
                msgs = [{"role": m["role"], "content": _text(m["content"])} for m in turns]
            tools = ch.strumenti(cur) + system_tools
            extra = {"chat_template_kwargs": {"enable_thinking": False}} if VOCE_MODEL.startswith("Qwen/") else None
            text, calls, acc = "", {}, Accenti()
            try:
                stream = await client().chat.completions.create(model=VOCE_MODEL, messages=[{"role": "system", "content": sys}] + msgs,
                                                                tools=tools or None, stream=True, temperature=0.2, max_tokens=220,
                                                                extra_body=extra)
                async for part in stream:
                    if not part.choices:
                        continue
                    d = part.choices[0].delta
                    if d.content:
                        piece = acc.feed(d.content)
                        if piece:
                            text += piece
                            yield _chunk(rid, {"content": piece})
                    for tc in d.tool_calls or []:
                        slot = calls.setdefault(tc.index, {"id": tc.id or f"call_{uuid.uuid4().hex[:8]}", "name": "", "args": ""})
                        if tc.function and tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["args"] += tc.function.arguments
            except Exception:  # modello non raggiungibile: frase di cortesia, la Centrale richiama
                fallback = "Mi scusi, ho un problema sulla linea. La richiameremo noi tra poco. Arrivederci."
                with db.tx() as con:
                    ch.evento(con, cid, "errore", "Il modello non ha risposto: chiamata da ripetere.")
                yield _chunk(rid, {"content": fallback})
                break
            tail = acc.flush()
            if tail:
                text += tail
                yield _chunk(rid, {"content": tail})
            if text.strip():
                ch.turno(cid, "agente", text.strip())
            if not calls:
                with db.tx() as con:
                    missing = ch.manca_esito(ch.carica(con, cid))
                if missing and DICHIARA.search(text):  # ha detto "ho segnato" senza farlo
                    msgs.append({"role": "assistant", "content": text})
                    ok, fix = await _forza_esito(cid, missing, msgs)
                    if not ok and fix:  # Filo ha rifiutato: l'agente si corregge subito, nella stessa risposta
                        msgs.extend(fix)
                        yield _chunk(rid, {"content": " "})
                        continue
                break
            local = [v for v in calls.values() if v["name"] in ch.LOCALI]
            remote = [v for v in calls.values() if v["name"] not in ch.LOCALI]
            with db.tx() as con:
                missing = ch.manca_esito(ch.carica(con, cid))
            if missing and any(v["name"] == "end_call" for v in remote):  # vuole chiudere senza esito: prima lo registra
                ok, fix = await _forza_esito(cid, missing, msgs + ([{"role": "assistant", "content": text}] if text.strip() else []))
                if not ok and fix:  # rifiutato: niente chiusura, l'agente deve correggersi
                    if text.strip():
                        msgs.append({"role": "assistant", "content": text})
                    msgs.extend(fix)
                    yield _chunk(rid, {"content": " "})
                    continue
            if any(v["name"] == "end_call" for v in remote):
                with db.tx() as con:
                    ch.evento(con, cid, "chiusura", "L'agente ha salutato e chiude la chiamata.")
                    tel = con.execute("SELECT channel FROM calls WHERE id=?", (cid,)).fetchone()
                if tel and tel[0] == "telefono":  # al telefono nessuno preme "fine": chiude Filo, dopo i saluti
                    asyncio.get_running_loop().call_later(8, lambda: threading.Thread(target=ch.concludi, args=(cid,), daemon=True).start())
            if remote:  # strumenti di sistema (chiudi la chiamata, segreteria...): li esegue ElevenLabs
                for i, v in enumerate(remote):
                    yield _chunk(rid, {"tool_calls": [{"index": i, "id": v["id"], "type": "function",
                                                       "function": {"name": v["name"], "arguments": v["args"] or "{}"}}]})
                yield _chunk(rid, {}, "tool_calls")
                yield "data: [DONE]\n\n"
                return
            msgs.append({"role": "assistant", "content": text or None,
                         "tool_calls": [{"id": v["id"], "type": "function", "function": {"name": v["name"], "arguments": v["args"] or "{}"}} for v in local]})
            for v in local:
                try:
                    args = json.loads(v["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = ch.esegui(cid, v["name"], args if isinstance(args, dict) else {})
                msgs.append({"role": "tool", "tool_call_id": v["id"], "content": json.dumps(result, ensure_ascii=False)})
            if text.strip():
                yield _chunk(rid, {"content": " "})
        yield _chunk(rid, {}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.post("/v1/chat/completions")
async def completions_v1(request: Request):
    return await _completions(request)


@app.post("/chat/completions")
async def completions(request: Request):
    return await _completions(request)


def verifica_firma(raw: str, header: str, secret: str, tolleranza_s: int = 1800) -> bool:
    parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
    t, v0 = parts.get("t"), parts.get("v0")
    if not (t and v0 and secret) or not t.isdigit() or int(t) < time.time() - tolleranza_s:
        return False
    digest = hmac.new(secret.encode(), f"{t}.{raw}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, v0)


@app.post("/fine-chiamata")
async def fine_chiamata(request: Request):
    """Webhook di ElevenLabs a fine chiamata (telefono). Firmato: senza firma valida non si tocca nulla."""
    raw = (await request.body()).decode()
    if not verifica_firma(raw, request.headers.get("elevenlabs-signature", ""), ELEVEN_WEBHOOK_SECRET or ""):
        raise HTTPException(401, "firma non valida")
    ev = json.loads(raw)
    data = ev.get("data", {})
    init = data.get("conversation_initiation_client_data") or {}
    cid = ((init.get("dynamic_variables") or {}).get("call_id")) or ((init.get("custom_llm_extra_body") or {}).get("call_id"))
    if cid and re.fullmatch(r"CH-[0-9a-f]{6}", cid):
        ch.concludi(cid, data.get("conversation_id"))
    return JSONResponse({"ok": True})
