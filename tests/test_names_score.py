"""Tests críticos de normalización de nombres y parseo de marcadores."""
from betbot.canonical.names import canonical_key, split_last_initials
from betbot.canonical.score import (ParsedScore, infer_retired, parse_numeric_sets,
                                    parse_score_string)


def test_canonical_key_variants():
    assert canonical_key("Federer R.") == "federer_r"
    assert canonical_key("Federer R. ") == "federer_r"          # espacio final (MLT)
    assert canonical_key("O Connell C.") == "o_connell_c"
    assert canonical_key("O'Connell C.") == "o_connell_c"
    assert canonical_key("Pliskova Ka.") == "pliskova_ka"
    assert canonical_key("De Minaur A.") == "de_minaur_a"
    assert canonical_key("Báez S.") == "baez_s"                  # acentos
    assert canonical_key("Auger-Aliassime F.") == "auger_aliassime_f"
    assert canonical_key(None) == ""


def test_split_last_initials():
    assert split_last_initials("federer_r") == ("federer", "r")
    assert split_last_initials("pliskova_ka") == ("pliskova", "ka")
    assert split_last_initials("de_minaur_a") == ("de_minaur", "a")
    assert split_last_initials("nadal") == ("nadal", "")


def test_parse_numeric_complete_bo3():
    ps = parse_numeric_sets([(6, 4), (7, 6), (None, None), (None, None), (None, None)])
    assert (ps.sets_w, ps.sets_l) == (2, 0)
    assert (ps.games_w, ps.games_l) == (13, 10)
    assert ps.set1_w == 1 and ps.set1_completed
    assert not infer_retired(ps, 3)


def test_parse_numeric_retirement_mid_set2():
    ps = parse_numeric_sets([(6, 4), (3, 1), (None, None), (None, None), (None, None)])
    assert (ps.sets_w, ps.sets_l) == (1, 0)   # el set 2 (3-1) no está completado
    assert ps.set1_w == 1
    assert infer_retired(ps, 3)


def test_parse_numeric_retirement_mid_set1():
    ps = parse_numeric_sets([(3, 2), (None, None), (None, None), (None, None), (None, None)])
    assert ps.set1_w is None and not ps.set1_completed
    assert infer_retired(ps, 3)


def test_parse_score_string_oriented():
    ps = parse_score_string("2-6 4-6")            # jugador de referencia pierde 0-2
    assert (ps.sets_w, ps.sets_l) == (0, 2)
    assert ps.set1_w == 0
    ps2 = parse_score_string("7-6(5) 6-7(3) 7-5")
    assert (ps2.sets_w, ps2.sets_l) == (2, 1)
    assert ps2.games_w == 20 and ps2.games_l == 18
    ps3 = parse_score_string("")
    assert ps3.n_sets == 0


def test_long_fifth_set_completed():
    ps = parse_numeric_sets([(6, 4), (4, 6), (6, 3), (3, 6), (70, 68)])
    assert ps.sets_w == 3 and ps.sets_l == 2
    assert not infer_retired(ps, 5)


def test_parsed_score_defaults():
    ps = ParsedScore()
    assert infer_retired(ps, 3)
