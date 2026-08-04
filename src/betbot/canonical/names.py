"""Normalización de nombres estilo Tennis-Data ("Federer R.", "O Connell C.").

Clave canónica: apellidos + iniciales, minúsculas, sin acentos ni puntuación,
separados por '_'. Ej.: "Federer R. " -> "federer_r"; "Pliskova Ka." -> "pliskova_ka";
"De Minaur A." -> "de_minaur_a"; "O'Connell C." -> "o_connell_c".

Limitación documentada: la identidad se basa en el nombre tal y como lo
desambigua Tennis-Data (usa iniciales dobles cuando hay colisión, p.ej.
"Kuznetsov An."/"Kuznetsov Al."). Colisiones residuales quedan como riesgo
aceptado del MVP y se auditan con el informe de cuarentena.
"""
from __future__ import annotations

import re
import unicodedata

_PUNCT = re.compile(r"[.\'\-]")
_WS = re.compile(r"\s+")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def canonical_key(raw: str) -> str:
    """Clave canónica de jugador a partir del nombre crudo de la fuente."""
    if raw is None:
        return ""
    s = strip_accents(str(raw)).strip()
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip().lower()
    if not s:
        return ""
    return "_".join(s.split(" "))


def resolve_full_name(raw: str, registry: set) -> str:
    """Resuelve un nombre (estilo TD "Fritz T." o completo "Taylor Fritz") a una
    clave presente en `registry`. Sin coincidencia, devuelve la mejor clave
    candidata en forma TD (apellido + inicial) para que la cuarentena muestre
    una sugerencia razonable — nunca acepta aproximaciones en silencio.

    Nombres completos: se prueban los cortes "nombre(s) | apellido(s)" de
    izquierda a derecha ("Juan Martin del Potro" -> martin_del_potro_j,
    del_potro_j, potro_j) con inicial simple y doble ("Karolina Pliskova" ->
    pliskova_k, pliskova_ka), y se acepta el primero presente en el registro.
    """
    direct = canonical_key(raw)
    if not direct or direct in registry:
        return direct
    tokens = direct.split("_")
    if len(tokens) < 2:
        return direct
    first = tokens[0]
    candidates = []
    for i in range(1, len(tokens)):
        surname = "_".join(tokens[i:])
        # inicial simple y doble del primer nombre ("Karolina" -> k, ka) y, con
        # varios nombres de pila, iniciales encadenadas ("Jan Lennard" -> j_l,
        # convención Tennis-Data "Struff J.L.")
        inis = [first[:1], first[:2]]
        if i > 1:
            inis.insert(1, "_".join(t[:1] for t in tokens[:i]))
        for ini in inis:
            if ini and f"{surname}_{ini}" not in candidates:
                candidates.append(f"{surname}_{ini}")
    for c in candidates:
        if c in registry:
            return c
    return candidates[0]


def extend_initials(key: str, registry: set) -> str:
    """Extensión determinista de iniciales: si `key` ("struff_j") no está en el
    registro pero EXACTAMENTE una clave lo extiende con más iniciales
    ("struff_j_l"), la devuelve. Con cero o varias candidatas devuelve `key`
    sin cambios — nunca una aproximación difusa."""
    if not key or key in registry:
        return key
    pref = key + "_"
    cands = [k for k in registry
             if k.startswith(pref) and all(len(t) <= 2 for t in k[len(pref):].split("_"))]
    return cands[0] if len(cands) == 1 else key


def split_last_initials(key: str) -> tuple[str, str]:
    """Divide una clave canónica en (apellido_compuesto, iniciales).

    Tennis-Data pone las iniciales al final ("federer_r", "de_minaur_a",
    "haddad_maia_b"). Heurística: el último token es la inicial (<=3 chars);
    el resto es apellido. Si no hay token corto final, todo es apellido.
    """
    parts = key.split("_")
    if len(parts) >= 2 and len(parts[-1]) <= 3:
        return "_".join(parts[:-1]), parts[-1]
    return key, ""
