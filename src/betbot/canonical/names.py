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
