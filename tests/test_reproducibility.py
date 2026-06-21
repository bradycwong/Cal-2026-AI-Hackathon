from backend.reproducibility import check


def test_check_returns_none_without_expected_volume():
    assert check({}, "added 200 uL lysis buffer") is None
    assert check({"reagent": "lysis buffer"}, "added 200 uL lysis buffer") is None


def test_check_returns_none_without_logged_volume():
    assert check({"volume_ul": 200}, "pellet looks cloudy") is None


def test_check_accepts_ascii_and_spelled_units():
    assert check({"volume_ul": 200}, "added 200 uL lysis buffer") == {
        "parameter": "volume_ul",
        "expected": 200,
        "logged": 200,
        "unit": "uL",
        "status": "ok",
    }
    assert check({"volume_ul": 200}, "added 200 microliters lysis buffer")["status"] == "ok"
    assert check({"volume_ul": 200}, "added 200 microlitres lysis buffer")["status"] == "ok"


def test_check_flags_mismatch():
    assert check({"volume_ul": 200}, "added 250 uL lysis buffer") == {
        "parameter": "volume_ul",
        "expected": 200,
        "logged": 250,
        "unit": "uL",
        "status": "mismatch",
    }


def test_check_preserves_decimal_values():
    flag = check({"volume_ul": 1.5}, "added 1.25 uL template")
    assert flag["expected"] == 1.5
    assert flag["logged"] == 1.25
    assert flag["status"] == "mismatch"


def test_check_ignores_non_numeric_expected_volume():
    assert check({"volume_ul": "about 200"}, "added 200 uL lysis buffer") is None
