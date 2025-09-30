from __future__ import annotations

import datetime as _dt
import decimal as _dec
import uuid as _uuid
from typing import Any, Iterable, List

try:  # SQLAlchemy optional typing imports
    from sqlalchemy.engine import Row, RowMapping  # type: ignore
except Exception:  # pragma: no cover - environment without SA types
    Row = object  # type: ignore
    RowMapping = object  # type: ignore


def _cell(value: Any) -> Any:
    """Coerce a single value to a JSON-friendly primitive."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, _dec.Decimal):
        try:
            return int(value) if value == int(value) else float(value)
        except Exception:
            return float(value)
    if isinstance(value, _uuid.UUID):
        return str(value)
    try:
        # Fallback best-effort
        return str(value)
    except Exception:  # pragma: no cover - defensive
        return None


def mapping_to_primitive_dict(mapping: Any) -> dict:
    """Convert a SQLAlchemy Row/RowMapping or plain dict values to primitives."""

    if isinstance(mapping, dict):
        return {k: _cell(v) for k, v in mapping.items()}
    # SQLAlchemy Row exposes ._mapping that is Mapping-like
    m = getattr(mapping, "_mapping", None)
    if m is not None:
        return {k: _cell(v) for k, v in dict(m).items()}
    # Attempt attribute-style keys (rare)
    try:
        return {k: _cell(getattr(mapping, k)) for k in mapping.keys()}  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        return {str(i): _cell(v) for i, v in enumerate(list(mapping))}
    except Exception:
        return {"value": _cell(mapping)}


def rows_to_lists(rows: Iterable[Any]) -> List[List[Any]]:
    """Normalize rows to a list-of-lists with primitive cell types."""

    out: List[List[Any]] = []
    for r in rows:
        # Prefer sequence layout
        if isinstance(r, (list, tuple)):
            out.append([_cell(x) for x in r])
            continue
        # Row / RowMapping → list of values in column order if possible
        m = getattr(r, "_mapping", None)
        if m is not None:
            out.append([_cell(v) for v in list(m.values())])
            continue
        if isinstance(r, dict):
            out.append([_cell(v) for v in list(r.values())])
            continue
        # Fallback best-effort
        out.append([_cell(r)])
    return out


