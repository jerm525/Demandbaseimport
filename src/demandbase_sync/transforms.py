"""
Named transform library used by the field mapping engine (see mapping.py).

Each transform is registered under a short name. Transform specs in the
mapping YAML look like:

    transform: none
    transform: "date_format:%Y-%m-%d"
    transform: decimal_round_2
    transform: bool_to_yn
    transform: bool_to_truefalse
    transform: "truncate:255"
    transform: "lookup_map:{'Open':'O','Closed':'C'}"

New transforms can be added anywhere in the codebase (not just here) via the
`register_transform` decorator -- the pipeline code in mapping.py never needs
to change to support a new transform name.
"""
from __future__ import annotations

import ast
import datetime as _dt
from typing import Any, Callable, Dict

TransformFn = Callable[[Any, str], Any]

_REGISTRY: Dict[str, TransformFn] = {}


def register_transform(name: str) -> Callable[[TransformFn], TransformFn]:
    def _decorator(fn: TransformFn) -> TransformFn:
        _REGISTRY[name] = fn
        return fn

    return _decorator


class TransformError(ValueError):
    """Raised when a transform cannot be applied to a given value."""


def apply_transform(spec: str, value: Any) -> Any:
    """Parse a transform spec string and apply it to `value`.

    A spec is either a bare name ("none", "decimal_round_2", ...) or a
    "name:arg" pair (e.g. "date_format:%Y-%m-%d", "truncate:255").
    """
    if value is None:
        return None

    if ":" in spec:
        name, _, arg = spec.partition(":")
    else:
        name, arg = spec, None

    name = name.strip()
    fn = _REGISTRY.get(name)
    if fn is None:
        raise TransformError(
            f"Unknown transform '{name}'. Registered transforms: {sorted(_REGISTRY)}"
        )
    return fn(value, arg)


# ---------------------------------------------------------------------------
# Built-in transforms
# ---------------------------------------------------------------------------


@register_transform("none")
def _none(value: Any, arg: str | None) -> Any:
    return value


@register_transform("date_format")
def _date_format(value: Any, arg: str | None) -> str:
    if not arg:
        raise TransformError("date_format requires a strftime pattern, e.g. date_format:%Y-%m-%d")
    if isinstance(value, str):
        # Accept common source shapes: ISO datetime or date-only strings.
        try:
            parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TransformError(f"Cannot parse '{value}' as a date/datetime") from exc
    elif isinstance(value, _dt.datetime):
        parsed = value
    elif isinstance(value, _dt.date):
        parsed = _dt.datetime(value.year, value.month, value.day)
    else:
        raise TransformError(f"date_format cannot handle value of type {type(value)}")
    return parsed.strftime(arg)


@register_transform("decimal_round_2")
def _decimal_round_2(value: Any, arg: str | None) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise TransformError(f"Cannot round '{value}' to a decimal") from exc


_TRUTHY = {True, 1, "1", "true", "True", "TRUE", "y", "Y", "yes", "Yes", "YES"}
_FALSY = {False, 0, "0", "false", "False", "FALSE", "n", "N", "no", "No", "NO"}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise TransformError(f"Cannot interpret '{value}' as a boolean")


@register_transform("bool_to_yn")
def _bool_to_yn(value: Any, arg: str | None) -> str:
    return "Y" if _coerce_bool(value) else "N"


@register_transform("bool_to_truefalse")
def _bool_to_truefalse(value: Any, arg: str | None) -> str:
    return "true" if _coerce_bool(value) else "false"


@register_transform("truncate")
def _truncate(value: Any, arg: str | None) -> str:
    if not arg:
        raise TransformError("truncate requires a max length, e.g. truncate:255")
    max_len = int(arg)
    text = str(value)
    return text[:max_len]


@register_transform("lookup_map")
def _lookup_map(value: Any, arg: str | None) -> Any:
    if not arg:
        raise TransformError("lookup_map requires a mapping literal, e.g. lookup_map:{'A':'B'}")
    try:
        mapping = ast.literal_eval(arg)
    except (ValueError, SyntaxError) as exc:
        raise TransformError(f"Could not parse lookup_map argument '{arg}'") from exc
    if not isinstance(mapping, dict):
        raise TransformError("lookup_map argument must evaluate to a dict")
    if value not in mapping:
        raise TransformError(f"Value '{value}' has no entry in lookup_map {mapping}")
    return mapping[value]


def registered_transform_names() -> list[str]:
    return sorted(_REGISTRY)
