import datetime as dt

import pytest

from demandbase_sync.transforms import TransformError, apply_transform


def test_none_passthrough():
    assert apply_transform("none", "hello") == "hello"


def test_date_format():
    assert apply_transform("date_format:%Y-%m-%d", dt.datetime(2026, 1, 5)) == "2026-01-05"
    assert apply_transform("date_format:%Y-%m-%d", "2026-01-05T10:00:00Z") == "2026-01-05"


def test_date_format_bad_value():
    with pytest.raises(TransformError):
        apply_transform("date_format:%Y-%m-%d", "not-a-date")


def test_decimal_round_2():
    assert apply_transform("decimal_round_2", 10.005) == 10.0 or apply_transform("decimal_round_2", 10.005) == 10.01
    assert apply_transform("decimal_round_2", "12.3456") == 12.35


def test_bool_to_yn():
    assert apply_transform("bool_to_yn", True) == "Y"
    assert apply_transform("bool_to_yn", False) == "N"
    assert apply_transform("bool_to_yn", "true") == "Y"


def test_bool_to_truefalse():
    assert apply_transform("bool_to_truefalse", True) == "true"
    assert apply_transform("bool_to_truefalse", 0) == "false"


def test_truncate():
    assert apply_transform("truncate:5", "abcdefgh") == "abcde"


def test_lookup_map():
    assert apply_transform("lookup_map:{'Open': 'O', 'Closed': 'C'}", "Open") == "O"
    with pytest.raises(TransformError):
        apply_transform("lookup_map:{'Open': 'O'}", "Unknown")


def test_none_value_short_circuits():
    assert apply_transform("decimal_round_2", None) is None


def test_unknown_transform_raises():
    with pytest.raises(TransformError):
        apply_transform("not_a_real_transform", "x")
