"""Codice fiscale: calcolo (per i dati sintetici) e riconoscimento nei documenti."""
from __future__ import annotations

import re

MESI = "ABCDEHLMPRST"
_ODD = {**dict(zip("0123456789", [1, 0, 5, 7, 9, 13, 15, 17, 19, 21])),
        **dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", [1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18, 20, 11, 3, 6, 8, 12, 14, 16, 10, 22, 25, 24, 23]))}
_EVEN = {**{str(i): i for i in range(10)}, **{c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}}
# Le cifre possono essere sostituite da lettere (omocodia: due persone con lo stesso codice). L->0, M->1 ... V->9.
OMO = "LMNPQRSTUV"
_D = r"[\dLMNPQRSTUV]"
CF_RE = re.compile(rf"\b[A-Z]{{6}}{_D}{{2}}[A-EHLMPRST]{_D}{{2}}[A-Z]{_D}{{3}}[A-Z]\b")
_OMO_POS = (6, 7, 9, 10, 12, 13, 14)


def base(cf: str) -> str:
    """Codice con le cifre originali al posto delle lettere di omocodia (serve per leggere data e comune)."""
    c = list((cf or "").upper())
    for i in _OMO_POS:
        if i < len(c) and c[i] in OMO:
            c[i] = str(OMO.index(c[i]))
    return "".join(c)


def name_code(cognome: str, nome: str) -> str:
    return _part(cognome) + _part(nome, True)


def _cons(s):
    return [c for c in s.upper() if c.isalpha() and c not in "AEIOU"]


def _vow(s):
    return [c for c in s.upper() if c in "AEIOU"]


def _part(s, name=False):
    c = _cons(s)
    if name and len(c) >= 4:
        c = [c[0], c[2], c[3]]
    return "".join((c + _vow(s) + ["X"] * 3)[:3])


def check_char(cf15: str) -> str:
    tot = sum(_ODD[ch] if i % 2 == 0 else _EVEN[ch] for i, ch in enumerate(cf15))
    return chr(ord("A") + tot % 26)


def compute(cognome: str, nome: str, nascita: str, sesso: str, comune: str = "L736") -> str:
    """nascita: AAAA-MM-GG. comune: codice catastale (L736 = Venezia)."""
    y, m, d = nascita.split("-")
    day = int(d) + (40 if sesso.upper() == "F" else 0)
    base = f"{_part(cognome)}{_part(nome, True)}{y[2:]}{MESI[int(m) - 1]}{day:02d}{comune}"
    return base + check_char(base)


def valid(cf: str) -> bool:
    cf = (cf or "").upper()
    return bool(CF_RE.fullmatch(cf)) and check_char(cf[:15]) == cf[15]


def find(text: str) -> list[str]:
    return [c for c in dict.fromkeys(CF_RE.findall((text or "").upper())) if valid(c)]
