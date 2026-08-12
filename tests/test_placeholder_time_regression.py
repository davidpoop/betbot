"""Regresión del fallo de horas PLACEHOLDER (2026-08-05).

Casos reales observados en la máquina del usuario: dos partidos WTA de Toronto
llegaron desde `api.wtatennis.com` como

    status=scheduled  scheduled_at_utc=2026-08-06T03:59:00Z
    event_id=wta:LS052:20260806:jovic_i__linette_m
    event_id=wta:LS061:20260806:krejcikova_b__samsonova_l

03:59 UTC son las 23:59 de Toronto: fin del día local, es decir "tengo la fecha
pero no el horario". Sus horas REALES, corroboradas por dos espejos
independientes de The Odds API, eran 18:00Z y 21:00Z del día ANTERIOR — o sea
que la marca provisional situaba los partidos hasta 10 h más tarde de lo que
empezaban, y un encuentro ya comenzado seguía pareciendo prepartido.
"""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from betbot.config import load_config
from betbot.feeds.base import (evidence_of_play, is_placeholder_time,
                               mark_placeholder_times)
from betbot.feeds.commence import CommenceIndex, CommenceRecord, cross_check
from betbot.feeds.wta_official import WtaOfficialCalendar
from betbot.prematch import gate

# instante de la observación real del usuario
NOW = datetime(2026, 8, 5, 8, 30, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()
LO, HI = NOW - timedelta(days=1), NOW + timedelta(hours=48)
PLACEHOLDER = "2026-08-06T03:59:00Z"
FRESH_OK = {"ATP": NOW.date(), "WTA": NOW.date()}
STALE = {"ATP": datetime(2026, 8, 3).date(), "WTA": datetime(2026, 8, 3).date()}


@pytest.fixture()
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["paths"]["ledger_dir"] = str(tmp_path / "ledger")
    # tests deterministas: el contraste con fuentes externas se prueba aparte
    c.setdefault("feeds", {})["commence_crosscheck"] = False
    return c


def _ls052(**kw):
    """Fila real LS052: Jović vs Linette."""
    base = {"MatchID": "LS052", "EventID": "0LS", "EventYear": 2026, "RoundID": "R32",
            "DrawMatchType": "S", "DrawLevelType": "P",
            "PlayerIDA": "329340", "PlayerIDB": "317891",
            "PlayerNameFirstA": "Iva", "PlayerNameLastA": "Jovic",
            "PlayerNameFirstB": "Magda", "PlayerNameLastB": "Linette",
            "MatchState": "U", "MatchTimeStamp": PLACEHOLDER,
            "NotBeforeISOTime": "", "TournamentName": "Toronto",
            "CourtName": "Grandstand", "Winner": "",
            "ScoreSet1A": 0, "ScoreSet1B": 0, "MatchTimeTotal": ""}
    base.update(kw)
    return base


def _ls061(**kw):
    """Fila real LS061: Samsonova vs Krejcikova."""
    base = dict(_ls052(), MatchID="LS061", CourtName="Court 1",
                PlayerNameFirstA="Liudmila", PlayerNameLastA="Samsonova",
                PlayerNameFirstB="Barbora", PlayerNameLastB="Krejcikova",
                PlayerIDA="317481", PlayerIDB="311470")
    base.update(kw)
    return base


def _odds_index(fetched_at="2026-08-05T02:33:11Z"):
    """Índice REAL de horas independientes (valores verificados el 2026-08-05
    en dos espejos de The Odds API que coinciden al segundo)."""
    idx = CommenceIndex()
    idx.sources_ok.append("odds_api_mirror:alienorsutinn (42 eventos)")
    for pair, hora, eid in ((("jovic_i", "linette_m"), "2026-08-05T18:00:00Z",
                             "a09c42ca23d870bd4c22fc5c6cc7b612"),
                            (("krejcikova_b", "samsonova_l"), "2026-08-05T21:00:00Z",
                             "83963b351438e21ba56380e3f234182c")):
        rec = CommenceRecord(pair=tuple(sorted(pair)),
                             commence_utc=datetime.fromisoformat(hora.replace("Z", "+00:00")),
                             event_id=eid, source="odds_api_mirror:alienorsutinn",
                             sport_key="tennis_wta_canadian_open",
                             fetched_at=fetched_at)
        idx.by_pair.setdefault(rec.pair, []).append(rec)
        idx.covered_tours.add("tennis_wta_canadian_open")
    return idx


# --------------------------------------------------------------------------
# 1. la hora 23:59 local / 03:59 UTC nunca es una hora confirmada
# --------------------------------------------------------------------------

def test_placeholder_time_detected_by_minute_pattern():
    dt = datetime(2026, 8, 6, 3, 59, tzinfo=timezone.utc)
    bad, why = is_placeholder_time(dt)
    assert bad and ":59" in why
    # 23:59 UTC (mismo patrón en otra zona) también
    assert is_placeholder_time(datetime(2026, 8, 5, 23, 59, tzinfo=timezone.utc))[0]
    # medianoche exacta: fecha convertida a datetime
    assert is_placeholder_time(datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc))[0]
    # una hora de verdad NO se marca
    assert not is_placeholder_time(datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc))[0]
    assert not is_placeholder_time(datetime(2026, 8, 5, 21, 30, tzinfo=timezone.utc))[0]
    assert not is_placeholder_time(datetime(2026, 6, 3, 9, 10, tzinfo=timezone.utc))[0]


def test_identical_time_shared_by_several_courts_is_placeholder():
    """Partidos en pistas distintas no pueden empezar todos al mismo segundo."""
    from betbot.feeds.base import FeedMatch
    t = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)   # hora "limpia"
    ms = [FeedMatch(date=t.date(), tour="WTA", tournament="Toronto",
                    player1=f"A{i}", player2=f"B{i}", scheduled_at_utc=t,
                    status="scheduled", authoritative=True) for i in range(3)]
    mark_placeholder_times(ms)
    assert all(m.time_precision == "date_only" for m in ms)
    assert all("comparten exactamente" in m.time_note for m in ms)


def test_ls052_and_ls061_time_marked_provisional_and_excluded(cfg):
    out, _, _ = WtaOfficialCalendar.parse_matches([_ls052(), _ls061()], NOW_ISO, LO, HI)
    assert len(out) == 2
    for m in out:
        assert m.scheduled_at_utc == datetime(2026, 8, 6, 3, 59, tzinfo=timezone.utc)
        assert m.time_precision == "date_only"      # NO es "Inicio confirmado"
        assert ":59" in m.time_note or "comparten" in m.time_note
    res = gate(out, cfg, now=NOW, completed_idx={}, fresh_until=FRESH_OK)
    assert res.eligible == []
    assert res.counts()["hora_provisional"] == 2


# --------------------------------------------------------------------------
# 2. contraste con The Odds API
# --------------------------------------------------------------------------

def test_ls052_conflict_with_independent_commence_time(cfg):
    """El calendario dice 03:59Z del día 6; la fuente independiente, 18:00Z del
    día 5. Diferencia de 599 min -> exclusión fail-closed."""
    idx = _odds_index()
    declarada = datetime(2026, 8, 6, 3, 59, tzinfo=timezone.utc)
    r = cross_check(("jovic_i", "linette_m"), declarada, idx, NOW,
                    tournament_hint="tennis_wta_canadian_open")
    assert r["verdict"] == "conflicto_horario"
    assert r["delta_minutes"] == 599.0
    assert r["commence_utc"].startswith("2026-08-05T18:00")
    assert r["event_id"] == "a09c42ca23d870bd4c22fc5c6cc7b612"   # id de The Odds API


def test_ls061_conflict_with_independent_commence_time():
    idx = _odds_index()
    declarada = datetime(2026, 8, 6, 3, 59, tzinfo=timezone.utc)
    r = cross_check(("krejcikova_b", "samsonova_l"), declarada, idx, NOW,
                    tournament_hint="tennis_wta_canadian_open")
    assert r["verdict"] == "conflicto_horario" and r["delta_minutes"] == 419.0


def test_conflict_excludes_even_with_an_exact_looking_time(cfg):
    """Aunque la hora pareciera exacta, la discrepancia material excluye."""
    out, _, _ = WtaOfficialCalendar.parse_matches(
        [_ls052(MatchTimeStamp="2026-08-06T04:00:00Z")], NOW_ISO, LO, HI)
    m = out[0]
    m.time_precision = "exact"                    # simula hora de aspecto válido
    m.time_note = ""
    res = gate([m], cfg, now=NOW, completed_idx={}, fresh_until=FRESH_OK,
               commence_idx=_odds_index())
    assert res.eligible == []
    assert res.counts()["conflicto_de_fuentes"] == 1
    assert "18:00" in list(res.details.values())[0]


def test_commence_time_already_past_excludes(cfg):
    """Si la hora independiente RECIENTE ya pasó, manda sobre el calendario.

    (Precisión posterior: una hora PROGRAMADA caduca — el snapshot debe ser
    reciente para poder concluir; con snapshot viejo protege el cinturón de
    hora provisional, ver el test siguiente.)"""
    later = datetime(2026, 8, 5, 19, 0, tzinfo=timezone.utc)   # tras las 18:00Z
    out, _, _ = WtaOfficialCalendar.parse_matches(
        [_ls052(MatchTimeStamp="2026-08-06T04:00:00Z")], later.isoformat(),
        later - timedelta(days=1), later + timedelta(hours=48))
    m = out[0]
    m.time_precision, m.time_note = "exact", ""     # estado recién observado
    idx = _odds_index(fetched_at="2026-08-05T18:30:00Z")       # snapshot reciente
    res = gate([m], cfg, now=later, completed_idx={}, fresh_until=FRESH_OK,
               commence_idx=idx)
    assert res.eligible == []
    # con hora primaria declarada y discrepancia material, la exclusión precisa
    # es el conflicto entre fuentes (ambas horas quedan en el detalle)
    assert res.counts()["conflicto_de_fuentes"] == 1
    assert "18:00" in list(res.details.values())[0]


def test_real_placeholder_case_still_blocked_with_stale_mirror(cfg):
    """El caso REAL Jović–Linette con el espejo viejo (16 h): la hora
    programada caducada ya no bloquea por sí sola, pero el partido SIGUE
    excluido porque 03:59/04:00 es una hora provisional de fin de día local.
    La protección crítica no depende del espejo."""
    later = datetime(2026, 8, 5, 19, 0, tzinfo=timezone.utc)
    out, _, _ = WtaOfficialCalendar.parse_matches(
        [_ls052(MatchTimeStamp="2026-08-06T03:59:00Z")], later.isoformat(),
        later - timedelta(days=1), later + timedelta(hours=48))
    m = out[0]
    res = gate([m], cfg, now=later, completed_idx={}, fresh_until=FRESH_OK,
               commence_idx=_odds_index())               # espejo de 16 h
    assert res.eligible == []
    assert res.counts()["hora_provisional"] == 1


def test_absent_from_covered_tournament_is_not_a_conflict(cfg):
    """AUSENCIA != CONTRADICCIÓN (corregido tras el caso real Jodar–Nakashima):
    que el índice cubra el torneo pero no liste el partido puede ser snapshot
    incompleto, ventana distinta o cobertura parcial. No se marca conflicto; la
    corroboración queda registrada como ausente y deciden las demás puertas."""
    idx = _odds_index()
    from betbot.feeds.base import FeedMatch
    m = FeedMatch(date=NOW.date(), tour="WTA", tournament="tennis_wta_canadian_open",
                  player1="Otra J.", player2="Rival M.",
                  scheduled_at_utc=NOW + timedelta(hours=5), status="scheduled",
                  source="wta_official", source_updated_at=NOW_ISO, authoritative=True,
                  trust_tier="schedule_only")
    res = gate([m], cfg, now=NOW, completed_idx={}, fresh_until=FRESH_OK,
               commence_idx=idx)
    assert res.counts().get("conflicto_de_fuentes", 0) == 0
    assert len(res.eligible) == 1
    conf = res.confirmations[m.pair_key]
    assert conf["commence_check"] == "no_corroborado"
    assert conf["corroboration"] == "ausente_en_secundaria"


def test_matching_commence_time_confirms(cfg):
    """Cuando ambas fuentes coinciden, el partido SÍ se confirma y queda
    registrado con el id y la hora independientes."""
    idx = _odds_index()
    from betbot.feeds.base import FeedMatch
    m = FeedMatch(date=NOW.date(), tour="WTA", tournament="tennis_wta_canadian_open",
                  player1="Jovic I.", player2="Linette M.",
                  scheduled_at_utc=datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc),
                  status="scheduled", source="wta_official", source_updated_at=NOW_ISO,
                  authoritative=True, trust_tier="schedule_only")
    res = gate([m], cfg, now=NOW, completed_idx={}, fresh_until=STALE, commence_idx=idx)
    assert len(res.eligible) == 1
    conf = res.confirmations[m.pair_key]
    assert conf["commence_check"] == "coincide"
    assert conf["commence_utc"].startswith("2026-08-05T18:00")
    assert conf["commence_event_id"] == "a09c42ca23d870bd4c22fc5c6cc7b612"


# --------------------------------------------------------------------------
# 3. el marcador se impone a MatchState
# --------------------------------------------------------------------------

def test_score_overrides_matchstate_u():
    """MatchState U pero con sets marcados: el partido está jugado."""
    played = _ls052(MatchState="U", ScoreSet1A=6, ScoreSet1B=3, Winner="A",
                    MatchTimeTotal="01:24", MatchTimeStamp="2026-08-05T18:00:00Z")
    out, _, _ = WtaOfficialCalendar.parse_matches([played], NOW_ISO, LO, HI)
    assert out[0].status == "completed"
    assert "evidencia de partido jugado" in out[0].time_note


def test_source_own_flags_mark_time_as_not_firm(cfg):
    """La WTA publica `Unscheduled` e `isEstimatedStartTime`: cuando los marca,
    su propia hora no es firme aunque parezca exacta."""
    for flag in ("Unscheduled", "isEstimatedStartTime"):
        row = _ls052(MatchTimeStamp="2026-08-05T18:00:00Z", **{flag: True})
        out, _, _ = WtaOfficialCalendar.parse_matches([row], NOW_ISO, LO, HI)
        m = out[0]
        assert m.time_precision == "date_only", flag
        assert flag.lower()[:6] in m.time_note.lower()
        res = gate([m], cfg, now=NOW, completed_idx={}, fresh_until=FRESH_OK)
        assert res.counts()["hora_provisional"] == 1
    # sin esas banderas y con hora limpia, la hora sí es exacta
    ok, _, _ = WtaOfficialCalendar.parse_matches(
        [_ls052(MatchTimeStamp="2026-08-05T18:00:00Z")], NOW_ISO, LO, HI)
    assert ok[0].time_precision == "exact"


def test_wta_empty_string_scores_are_not_evidence():
    """En la WTA los campos sin jugar llegan como CADENA VACÍA (verificado sobre
    un payload real: los 17 campos de marcador son "" en los partidos por jugar)."""
    vacio = {f"ScoreSet{i}{s}": "" for i in range(1, 6) for s in ("A", "B")}
    vacio.update({f"ScoreTbSet{i}": "" for i in range(1, 6)})
    vacio.update({"Winner": "", "MatchTimeTotal": "", "PointA": "", "PointB": "",
                  "ScoreString": "", "ResultString": "", "Serve": ""})
    assert evidence_of_play(vacio)[0] is False
    # Winner "0" tampoco decide (convención real de la fuente)
    assert evidence_of_play(dict(vacio, Winner="0"))[0] is False
    # duración a cero tampoco
    assert evidence_of_play(dict(vacio, MatchTimeTotal="00:00:00"))[0] is False


def test_wta_real_score_fields_are_evidence():
    base = {f"ScoreSet{i}{s}": "" for i in range(1, 6) for s in ("A", "B")}
    for campo, valor in (("ScoreString", "6-4,7-6(4)"),
                         ("ResultString", "[6]A. Smith d E. Schoppe 6-4,7-6(4)"),
                         ("ScoreSet1A", "6"), ("Winner", "A"),
                         ("MatchTimeTotal", "01:24:00"), ("PointA", "30"),
                         ("Serve", "A")):
        ok, why = evidence_of_play(dict(base, **{campo: valor}))
        assert ok, campo
        assert any(campo in w for w in why)


def test_evidence_of_play_detects_generic_fields():
    assert evidence_of_play({"ScoreSet1A": 0, "ScoreSet1B": 0, "Winner": ""})[0] is False
    assert evidence_of_play({"MatchState": "U"})[0] is False
    assert evidence_of_play({"ScoreSet1A": 6})[0] is True
    assert evidence_of_play({"Winner": "A"})[0] is True
    assert evidence_of_play({"MatchTimeTotal": "01:24"})[0] is True
    # anidado y con nombres de otra fuente
    ok, why = evidence_of_play({"status": {"periodScores": [{"home_score": 6}]}})
    assert ok and any("periodScores" in w for w in why)
    # un cero o un vacío no son evidencia
    assert evidence_of_play({"duration": "00:00", "result": "-"})[0] is False


def test_played_match_excluded_even_if_state_says_scheduled(cfg):
    played = _ls061(MatchState="U", ScoreSet1A=7, ScoreSet1B=5,
                    MatchTimeStamp="2026-08-05T21:00:00Z")
    out, _, _ = WtaOfficialCalendar.parse_matches([played], NOW_ISO, LO, HI)
    res = gate(out, cfg, now=NOW, completed_idx={}, fresh_until=FRESH_OK)
    assert res.eligible == []
    assert res.counts()["evidencia_de_resultado"] == 1


# --------------------------------------------------------------------------
# 4. frescura de resultados
# --------------------------------------------------------------------------

def test_stale_results_block_schedule_only_source(cfg):
    """Resultados hasta 2026-08-03 y hoy es 08-05: sin fuente live independiente
    no se puede afirmar que un partido de hoy siga por jugar."""
    out, _, _ = WtaOfficialCalendar.parse_matches(
        [_ls052(MatchTimeStamp="2026-08-05T18:00:00Z")], NOW_ISO, LO, HI)
    res = gate(out, cfg, now=NOW, completed_idx={}, fresh_until=STALE)
    assert res.eligible == []
    assert res.counts()["results_feed_stale_pre_match_unverified"] == 1
    assert "2026-08-03" in list(res.details.values())[0]


def test_stale_results_do_not_block_live_verified_source(cfg):
    """ESPN publica evidencia de juego: su estado no depende de la frescura."""
    from betbot.feeds.base import FeedMatch
    m = FeedMatch(date=NOW.date(), tour="WTA", tournament="Toronto",
                  player1="Jovic I.", player2="Linette M.",
                  scheduled_at_utc=NOW + timedelta(hours=5), status="scheduled",
                  source="espn", source_updated_at=NOW_ISO, authoritative=True,
                  trust_tier="live_verified")
    res = gate([m], cfg, now=NOW, completed_idx={}, fresh_until=STALE)
    assert len(res.eligible) == 1


def test_independent_commence_unblocks_stale_results(cfg):
    """Una hora independiente que coincide sustituye a la frescura como prueba."""
    from betbot.feeds.base import FeedMatch
    m = FeedMatch(date=NOW.date(), tour="WTA", tournament="tennis_wta_canadian_open",
                  player1="Jovic I.", player2="Linette M.",
                  scheduled_at_utc=datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc),
                  status="scheduled", source="wta_official", source_updated_at=NOW_ISO,
                  authoritative=True, trust_tier="schedule_only")
    assert len(gate([m], cfg, now=NOW, completed_idx={}, fresh_until=STALE,
                    commence_idx=_odds_index()).eligible) == 1


# --------------------------------------------------------------------------
# 5. identidad estable del evento
# --------------------------------------------------------------------------

def test_event_id_is_stable_across_schedule_changes():
    """Cambiar la hora NO puede crear un evento nuevo que eluda la
    reconciliación con resultados."""
    a, _, _ = WtaOfficialCalendar.parse_matches(
        [_ls052(MatchTimeStamp="2026-08-05T18:00:00Z")], NOW_ISO, LO, HI)
    b, _, _ = WtaOfficialCalendar.parse_matches(
        [_ls052(MatchTimeStamp=PLACEHOLDER)], NOW_ISO, LO, HI)
    assert a[0].event_id == b[0].event_id
    assert a[0].pair_key == b[0].pair_key
    # la identidad usa torneo/año/hueco del cuadro/pareja, sin fecha
    assert a[0].event_id == "wta:0LS:2026:LS052:jovic_i__linette_m"
    assert "20260805" not in a[0].event_id and "20260806" not in b[0].event_id


def test_stable_id_differs_between_the_two_real_matches():
    out, _, _ = WtaOfficialCalendar.parse_matches([_ls052(), _ls061()], NOW_ISO, LO, HI)
    ids = {m.event_id for m in out}
    assert len(ids) == 2
    assert "wta:0LS:2026:LS052:jovic_i__linette_m" in ids
    assert "wta:0LS:2026:LS061:krejcikova_b__samsonova_l" in ids


# --------------------------------------------------------------------------
# 6. la salida no puede decir "Inicio confirmado" en estos casos
# --------------------------------------------------------------------------

def test_report_never_claims_confirmed_start_for_placeholder(cfg):
    from betbot.scan import render_report, run_scan
    from betbot.feeds.base import SourceStatus

    class WtaWithPlaceholders:
        name = "wta_official"
        authoritative = True

        def fetch_matches(self, window_hours):
            ms, _, _ = WtaOfficialCalendar.parse_matches(
                [_ls052(), _ls061()], datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc) - timedelta(days=400),
                datetime.now(timezone.utc) + timedelta(days=400))
            return ms, SourceStatus(name=self.name, ok=True, authoritative=True,
                                    n_items=len(ms))

    res = run_scan(cfg, calendar_sources=[WtaWithPlaceholders()], odds_sources=[],
                   structured_providers=[], log_ledger=False, show_rejected=True)
    txt = render_report(res)
    assert res.summary["calendar_confirmed"] == 0
    assert "Inicio confirmado" not in txt
    assert "hora provisional" in txt.lower() or "provisional" in txt.lower()
