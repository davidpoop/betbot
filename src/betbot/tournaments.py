"""Registro canónico de torneos ATP/WTA: superficie, nivel y alias.

Motivo: Toronto/Montreal salían con `superficie_estimada` aunque el Masters de
Canadá es inequívocamente Hard. La heurística estacional queda SOLO para
torneos que el registro no reconoce.

Jerarquía de confianza de la superficie (columna `surface_source`):
    official            — la publica la propia fuente del calendario
    tournament_registry — el torneo se identifica de forma inequívoca aquí
    inferred            — heurística estacional (guess_surface)
    unknown             — sin información
El aviso `superficie_estimada` solo acompaña a inferred/unknown.

El registro cubre los torneos estables del calendario (Grand Slams, Masters/
1000, 500 y los 250 habituales): sus superficies no cambian de un año a otro
salvo anuncio mayor. Un torneo no reconocido NO inventa superficie: cae a la
heurística con su aviso. Cada entrada: superficie, nivel y localización.
"""
from __future__ import annotations

import re
import unicodedata

# entrada: (superficie, nivel, localización). Los alias van aparte.
_T = {
    # ---- Grand Slams (ambos circuitos) ----
    "australian open": ("Hard", "grand_slam", "Melbourne"),
    "roland garros": ("Clay", "grand_slam", "París"),
    "french open": ("Clay", "grand_slam", "París"),
    "wimbledon": ("Grass", "grand_slam", "Londres"),
    "us open": ("Hard", "grand_slam", "Nueva York"),
    # ---- Masters 1000 / WTA 1000 ----
    "indian wells": ("Hard", "1000", "Indian Wells"),
    "bnp paribas open": ("Hard", "1000", "Indian Wells"),
    "miami": ("Hard", "1000", "Miami"),
    "monte carlo": ("Clay", "1000", "Montecarlo"),
    "madrid": ("Clay", "1000", "Madrid"),
    "rome": ("Clay", "1000", "Roma"),
    "roma": ("Clay", "1000", "Roma"),
    "italian open": ("Clay", "1000", "Roma"),
    "canadian open": ("Hard", "1000", "Toronto/Montreal"),
    "canada masters": ("Hard", "1000", "Toronto/Montreal"),
    "national bank open": ("Hard", "1000", "Toronto/Montreal"),
    "toronto": ("Hard", "1000", "Toronto"),
    "montreal": ("Hard", "1000", "Montreal"),
    "cincinnati": ("Hard", "1000", "Cincinnati"),
    "western & southern": ("Hard", "1000", "Cincinnati"),
    "shanghai": ("Hard", "1000", "Shanghái"),
    "paris masters": ("Hard", "1000", "París (indoor)"),
    "paris bercy": ("Hard", "1000", "París (indoor)"),
    "rolex paris masters": ("Hard", "1000", "París (indoor)"),
    "wuhan": ("Hard", "1000", "Wuhan"),
    "beijing": ("Hard", "1000", "Pekín"),
    "china open": ("Hard", "1000", "Pekín"),
    "dubai": ("Hard", "500/1000", "Dubái"),
    "doha": ("Hard", "500/1000", "Doha"),
    "qatar open": ("Hard", "500/1000", "Doha"),
    # ---- 500 y 250 habituales ----
    "rotterdam": ("Hard", "500", "Róterdam (indoor)"),
    "acapulco": ("Hard", "500", "Acapulco"),
    "barcelona": ("Clay", "500", "Barcelona"),
    "queens": ("Grass", "500", "Londres"),
    "queen's": ("Grass", "500", "Londres"),
    "halle": ("Grass", "500", "Halle"),
    "hamburg": ("Clay", "500", "Hamburgo"),
    "washington": ("Hard", "500", "Washington"),
    "citi open": ("Hard", "500", "Washington"),
    "mubadala citi": ("Hard", "500", "Washington"),
    "tokyo": ("Hard", "500", "Tokio"),
    "vienna": ("Hard", "500", "Viena (indoor)"),
    "basel": ("Hard", "500", "Basilea (indoor)"),
    "stuttgart": ("Grass", "250/500", "Stuttgart"),          # ATP hierba (jun)
    "stuttgart wta": ("Clay", "500", "Stuttgart"),           # WTA tierra (abr)
    "eastbourne": ("Grass", "250", "Eastbourne"),
    "mallorca": ("Grass", "250", "Mallorca"),
    "bad homburg": ("Grass", "500", "Bad Homburg"),
    "s-hertogenbosch": ("Grass", "250", "Hertogenbosch"),
    "hertogenbosch": ("Grass", "250", "Hertogenbosch"),
    "gstaad": ("Clay", "250", "Gstaad"),
    "kitzbuhel": ("Clay", "250", "Kitzbühel"),
    "bastad": ("Clay", "250", "Båstad"),
    "umag": ("Clay", "250", "Umag"),
    "bucharest": ("Clay", "250", "Bucarest"),
    "geneva": ("Clay", "250", "Ginebra"),
    "lyon": ("Clay", "250", "Lyon"),
    "estoril": ("Clay", "250", "Estoril"),
    "marrakech": ("Clay", "250", "Marrakech"),
    "houston": ("Clay", "250", "Houston"),
    "brisbane": ("Hard", "250/500", "Brisbane"),
    "adelaide": ("Hard", "250/500", "Adelaida"),
    "auckland": ("Hard", "250", "Auckland"),
    "hong kong": ("Hard", "250", "Hong Kong"),
    "los cabos": ("Hard", "250", "Los Cabos"),
    "delray beach": ("Hard", "250", "Delray Beach"),
    "dallas": ("Hard", "250/500", "Dallas (indoor)"),
    "buenos aires": ("Clay", "250", "Buenos Aires"),
    "rio": ("Clay", "500", "Río de Janeiro"),
    "santiago": ("Clay", "250", "Santiago"),
    "marseille": ("Hard", "250", "Marsella (indoor)"),
    "montpellier": ("Hard", "250", "Montpellier (indoor)"),
    "metz": ("Hard", "250", "Metz (indoor)"),
    "atlanta": ("Hard", "250", "Atlanta"),
    "newport": ("Grass", "250", "Newport"),
    "winston-salem": ("Hard", "250", "Winston-Salem"),
    "cleveland": ("Hard", "250", "Cleveland"),
    "monterrey": ("Hard", "500", "Monterrey"),
    "guadalajara": ("Hard", "500", "Guadalajara"),
    "san diego": ("Hard", "500", "San Diego"),
    "charleston": ("Clay", "500", "Charleston"),
    "strasbourg": ("Clay", "500", "Estrasburgo"),
    "rabat": ("Clay", "250", "Rabat"),
    "nottingham": ("Grass", "250", "Nottingham"),
    "birmingham": ("Grass", "250", "Birmingham"),
    "berlin": ("Grass", "500", "Berlín"),
    "seoul": ("Hard", "500", "Seúl"),
    "ningbo": ("Hard", "500", "Ningbo"),
    "linz": ("Hard", "500", "Linz (indoor)"),
    "ostrava": ("Hard", "500", "Ostrava (indoor)"),
    "abu dhabi": ("Hard", "500", "Abu Dabi"),
    "merida": ("Hard", "250", "Mérida"),
    "austin": ("Hard", "250", "Austin"),
    "bogota": ("Clay", "250", "Bogotá"),
    "iasi": ("Clay", "250", "Iași"),
    "palermo": ("Clay", "250", "Palermo"),
    "prague": ("Clay", "250", "Praga"),
    "warsaw": ("Clay", "250", "Varsovia"),
    "cluj": ("Hard", "250", "Cluj (indoor)"),
    "hobart": ("Hard", "250", "Hobart"),
}


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", s).strip()


# Grupos de alias: el MISMO evento aparece con nombres distintos según la
# fuente (The Odds API dice "Canadian Open"; la WTA "National Bank Open ...").
# Para casar cobertura-por-torneo entre fuentes hace falta una identidad de
# evento compartida. Solo torneos grandes (los que tienen sport key).
_EVENT_GROUP = {
    "canadian open": "canadian open", "canada masters": "canadian open",
    "national bank open": "canadian open", "toronto": "canadian open",
    "montreal": "canadian open",
    "cincinnati": "cincinnati", "western & southern": "cincinnati",
    "indian wells": "indian wells", "bnp paribas open": "indian wells",
    "rome": "rome", "roma": "rome", "italian open": "rome",
    "paris masters": "paris masters", "paris bercy": "paris masters",
    "rolex paris masters": "paris masters",
    "french open": "roland garros", "roland garros": "roland garros",
    "china open": "beijing", "beijing": "beijing",
    "qatar open": "doha", "doha": "doha",
}


def canonical_event(tour: str, tournament: str) -> str:
    """Identidad de evento estable entre fuentes: clave del registro si se
    reconoce (pasada por los grupos de alias), o el nombre normalizado."""
    _surf, _src, info = resolve_surface(tour, tournament)
    key = info.get("canonical") or _norm(tournament)
    return _EVENT_GROUP.get(key, key)


def resolve_surface(tour: str, tournament: str) -> tuple[str | None, str, dict]:
    """(superficie, surface_source, info). Sin invenciones: torneo no
    reconocido -> (None, "unknown", {}) y la heurística decide con su aviso."""
    n = _norm(tournament)
    if not n:
        return None, "unknown", {}
    # desambiguación por circuito ANTES que nada: el mismo nombre puede tener
    # superficie distinta por tour (Stuttgart: ATP hierba jun / WTA tierra abr)
    if tour == "WTA" and "stuttgart" in n:
        surf, level, loc = _T["stuttgart wta"]
        return surf, "tournament_registry", {"canonical": "stuttgart wta",
                                             "level": level, "location": loc}
    # coincidencia exacta primero; después por subcadena, la más ESPECÍFICA
    # (la clave más larga contenida en el nombre)
    if n in _T:
        surf, level, loc = _T[n]
        return surf, "tournament_registry", {"canonical": n, "level": level,
                                             "location": loc}
    hits = [(k, v) for k, v in _T.items() if k in n]
    if hits:
        k, (surf, level, loc) = max(hits, key=lambda kv: len(kv[0]))
        return surf, "tournament_registry", {"canonical": k, "level": level,
                                             "location": loc}
    return None, "unknown", {}
