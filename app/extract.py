"""Lettore LOCALE: trasforma le note in segnali di una lista chiusa (lista bianca).

Due motori:
- modello locale (llama.cpp o simili, compatibile OpenAI) se è acceso;
- regole con vocabolario chiuso, sempre disponibili.
Qualunque cosa produca il modello, passa da `validate()`: escono solo campi e valori previsti.
Il testo libero non lascia mai questo modulo.
"""
from __future__ import annotations

import json
import re
import urllib.request

from .config import LOCAL_LLM_ENABLED, LOCAL_LLM_MODEL, LOCAL_LLM_URL

DRUG_CLASSES = ["anticoagulante", "antinfiammatorio", "antipertensivo", "antidiabetico", "ormone_tiroideo", "diuretico", "cortisonico", "broncodilatatore"]
SPECIALTIES = ["cardiologia", "ortopedia", "medicina", "medicina interna", "pneumologia", "diabetologia", "endocrinologia", "geriatria", "infermiere di famiglia", "CUP", "altro"]
CONDITIONS = ["ipertensione", "diabete", "fibrillazione atriale", "scompenso cardiaco", "BPCO", "artrosi", "ipotiroidismo", "amiloidosi cardiaca"]
SERVICES = ["tac_torace", "ecocardiogramma", "rmn_ginocchio", "visita_cardiologica"]
REASONS = ["effetti_collaterali", "costo", "dimenticanza", "altro"]

# Vocabolario chiuso: campo -> funzione che dice se il valore è ammesso
VOCAB = {
    "farmaco": lambda v: isinstance(v, str) and v.count("|") == 1 and v.split("|")[0] in DRUG_CLASSES and v.split("|")[1] in SPECIALTIES,
    "principio_attivo": lambda v: isinstance(v, str) and v.count("|") == 1 and v.split("|")[0] in DRUG_CLASSES and v.split("|")[1] in PRINCIPI,
    "condizione": lambda v: v in CONDITIONS,
    "aderenza": lambda v: v == "sospesa",
    "aderenza_giorni": lambda v: str(v).isdigit() and 0 < int(v) <= 365,
    "aderenza_motivo": lambda v: v in REASONS,
    "aderenza_terapia": lambda v: v in DRUG_CLASSES,
    "richiesta": lambda v: isinstance(v, str) and v.count("|") == 1 and v.split("|")[0] in SERVICES and v.split("|")[1] in SPECIALTIES,
    "udito": lambda v: v in ("supporto_richiesto", "apparecchio"),
    "vive_solo": lambda v: v == "sì",
    "senza_auto": lambda v: v == "sì",
    "sospeso_dal_medico": lambda v: v in DRUG_CLASSES,
}

DRUGS = {
    r"warfarin|apixaban|rivaroxaban|anticoagulant": "anticoagulante",
    r"ibuprofen|ketoprofen|diclofenac|antinfiammator|fans\b": "antinfiammatorio",
    r"ramipril|amlodipin|enalapril|pillole per la pressione|antipertensiv": "antipertensivo",
    r"metformin|insulin": "antidiabetico",
    r"levotiroxin": "ormone_tiroideo",
    r"furosemide|diuretic": "diuretico",
    r"cortison|prednison": "cortisonico",
    r"inalatori|broncodilatat": "broncodilatatore",
}
# Principi attivi riconosciuti per nome (le parole generiche come "anticoagulante" non ne danno uno). Nome in italiano.
NOMI = {
    r"warfarin": ("anticoagulante", "warfarin"), r"apixaban": ("anticoagulante", "apixaban"), r"rivaroxaban": ("anticoagulante", "rivaroxaban"),
    r"ibuprofen": ("antinfiammatorio", "ibuprofene"), r"ketoprofen": ("antinfiammatorio", "ketoprofene"), r"diclofenac": ("antinfiammatorio", "diclofenac"),
    r"ramipril": ("antipertensivo", "ramipril"), r"amlodipin": ("antipertensivo", "amlodipina"), r"enalapril": ("antipertensivo", "enalapril"),
    r"metformin": ("antidiabetico", "metformina"), r"insulin": ("antidiabetico", "insulina"), r"levotiroxin": ("ormone_tiroideo", "levotiroxina"),
    r"furosemide": ("diuretico", "furosemide"), r"prednison": ("cortisonico", "prednisone"),
}
PRINCIPI = {n for _, n in NOMI.values()}

CONDS = {
    r"fibrillazione atriale": "fibrillazione atriale",
    r"amiloidosi cardiaca": "amiloidosi cardiaca",
    r"scompenso": "scompenso cardiaco",
    r"diabete": "diabete",
    r"ipertension": "ipertensione",
    r"bpco": "BPCO",
    r"artrosi": "artrosi",
    r"ipotiroid": "ipotiroidismo",
}
SERVS = {r"tac torace": "tac_torace", r"ecocardiogramm": "ecocardiogramma"}


def _spec(source: str) -> str:
    return source if source in SPECIALTIES else "altro"


def _bounds(text: str, start: int, end: int) -> tuple[int, int]:
    lo = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    his = [i for i in (text.find(".", end), text.find("\n", end)) if i >= 0]
    return lo, (min(his) + 1 if his else len(text))


def _sentence(text: str, start: int, end: int) -> str:
    """La frase del documento che contiene la parola trovata: è la "fonte" che il medico può controllare."""
    lo, hi = _bounds(text, start, end)
    return text[lo:hi].strip()


# Negazioni: se nella stessa frase, PRIMA della parola trovata, c'è uno di questi, il dato non vale
# ("Nega diabete", "Escluso scompenso", "Non assume anticoagulanti").
NEGATION = re.compile(r"\b(nega|negano|negativ[oa] per|esclus[oaie]|esclude|non assume|non ha mai|non presenta|non riferisce|"
                      r"assenza di|no storia di)\b")
# Sospensione decisa da un medico: non è una terapia abbandonata dalla persona.
BY_DOCTOR = re.compile(r"\b(dal (dott|dottor|cardiolog|medic|curante|ortoped|reparto|diabetolog|pneumolog)|"
                       r"su indicazione|si sospende|sospeso per|sospesa per|da sospendere|sospendere)")


def _negated(t: str, start: int) -> bool:
    lo = max(t.rfind(".", 0, start), t.rfind("\n", 0, start), t.rfind(";", 0, start)) + 1
    return bool(NEGATION.search(t[lo:start]))


def rules(note) -> list[dict]:
    raw = note["text"] or ""
    t = raw.lower()
    out = []

    def add(field, value, m=None):
        out.append({"field": field, "value": value, "snippet": _sentence(raw, m.start(), m.end()) if m else None})

    def first(pat):
        """Prima occorrenza non negata."""
        for m in re.finditer(pat, t):
            if not _negated(t, m.start()):
                return m
        return None

    for pat, cls in DRUGS.items():
        if m := first(pat):
            add("farmaco", f"{cls}|{_spec(note['source'])}", m)
    for pat, (cls, nome) in NOMI.items():
        if m := first(pat):
            add("principio_attivo", f"{cls}|{nome}", m)
    for pat, cond in CONDS.items():
        if m := first(pat):
            add("condizione", cond, m)
    if note["kind"] == "richiesta":
        for pat, sv in SERVS.items():
            if m := first(pat):
                add("richiesta", f"{sv}|{_spec(note['source'])}", m)
    for m0 in re.finditer(r"non (le |li |la |lo )?prend|sospes[oa]|smesso di prendere", t):
        lo, hi = _bounds(t, m0.start(), m0.end())
        sent = t[lo:hi]
        drug = next((cls for pat, cls in DRUGS.items() if re.search(pat, sent)), None)
        if BY_DOCTOR.search(sent):  # decisione del medico: si registra che quel farmaco non va più contato
            if drug:
                add("sospeso_dal_medico", drug, m0)
            continue
        add("aderenza", "sospesa", m0)
        m = re.search(r"da (\d+) giorni", t)
        days = int(m.group(1)) if m else 7 if re.search(r"da (una|1) settiman", t) else 14 if "due settimane" in t else None
        if days:
            add("aderenza_giorni", str(days), m0)
        if m := re.search(r"girare la testa|capogir|vertigin|nausea|stomaco|effett", t):
            add("aderenza_motivo", "effetti_collaterali", m)
        elif m := re.search(r"cost", t):
            add("aderenza_motivo", "costo", m)
        elif m := re.search(r"dimentic", t):
            add("aderenza_motivo", "dimenticanza", m)
        # la classe è quella del farmaco nominato nella stessa frase, non il primo dell'elenco
        drug = drug or next((cls for pat, cls in DRUGS.items() if re.search(pat, t)), None)
        if drug:
            add("aderenza_terapia", drug, m0)
        break
    if m := first(r"supporto per l.udito|chiede.*udito"):
        add("udito", "supporto_richiesto", m)
    elif m := first(r"apparecchio acustico|ipoacusia"):
        add("udito", "apparecchio", m)
    if m := first(r"vive sol"):
        add("vive_solo", "sì", m)
    if m := re.search(r"non guida|non ha l.auto|senza auto", t):
        add("senza_auto", "sì", m)
    return out


LLM_PROMPT = """Estrai segnali clinici e sociali dalla nota. Rispondi SOLO con JSON:
{"segnali":[{"field":"...","value":"..."}]}
Campi e valori ammessi (nient'altro, niente nomi, luoghi, numeri di telefono):
- farmaco: "<classe>|<specialità>" con classe in %s e specialità in %s
- condizione: uno tra %s
- aderenza: "sospesa"; aderenza_giorni: numero; aderenza_motivo: uno tra %s; aderenza_terapia: una classe
- richiesta: "<servizio>|<specialità>" con servizio in %s (solo se la nota è una richiesta di esame)
- udito: "supporto_richiesto" o "apparecchio"
- vive_solo: "sì"; senza_auto: "sì"
Per ogni segnale aggiungi "frase": la frase della nota da cui viene, copiata esattamente. Un segnale senza frase viene scartato.
Ignora ciò che la nota nega ("nega", "escluso", "non assume").
Specialità della nota: %s. Tipo di nota: %s."""


def _llm_available() -> bool:
    """off = mai; on = sempre (se non risponde si ripiega sulle regole); auto = una prova veloce. Da chiamare UNA volta per elaborazione."""
    if LOCAL_LLM_ENABLED == "off":
        return False
    if LOCAL_LLM_ENABLED == "on":
        return True
    try:
        with urllib.request.urlopen(LOCAL_LLM_URL.rstrip("/") + "/models", timeout=1.5) as r:
            return r.status == 200
    except Exception:
        return False


def llm(note) -> list[dict] | None:
    prompt = LLM_PROMPT % (DRUG_CLASSES, SPECIALTIES, CONDITIONS, REASONS, SERVICES, note["source"], note["kind"])
    body = json.dumps({"model": LOCAL_LLM_MODEL, "temperature": 0, "max_tokens": 600,
                       "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": note["text"]}]}).encode()
    req = urllib.request.Request(LOCAL_LLM_URL.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            content = json.load(r)["choices"][0]["message"]["content"]
        start, end = content.find("{"), content.rfind("}")
        return json.loads(content[start:end + 1]).get("segnali", [])
    except Exception:
        return None


def validate(signals: list[dict]) -> list[dict]:
    """La lista bianca: scarta tutto ciò che non è un campo previsto con un valore previsto."""
    ok = []
    for s in signals or []:
        f, v = s.get("field"), s.get("value")
        if f in VOCAB and VOCAB[f](v):
            ok.append({"field": f, "value": str(v), "snippet": s.get("snippet") or s.get("frase")})
    return ok


def _anchored(note, sigs: list[dict]) -> list[dict]:
    """Segnali del modello: valgono solo se portano una frase che sta davvero nel documento."""
    text = note["text"] or ""
    ok = []
    for s in sigs:
        frase = (s.get("frase") or s.get("snippet") or "").strip()
        if len(frase) >= 8 and frase in text:
            ok.append({**s, "snippet": frase})
    return ok


def extract(note, use_llm: bool | None = None) -> tuple[list[dict], str]:
    if use_llm is None:
        use_llm = _llm_available()
    if use_llm:
        got = llm(note)
        if got is not None:
            # unione con le regole: il modello aggiunge (solo con la sua frase di fonte), le regole garantiscono il minimo
            by_key = {(s["field"], s["value"]): s for s in validate(_anchored(note, got))}
            by_key.update({(s["field"], s["value"]): s for s in rules(note)})  # le regole hanno la frase esatta
            return [by_key[k] for k in sorted(by_key)], "modello locale + regole"
    return validate(rules(note)), "regole"
