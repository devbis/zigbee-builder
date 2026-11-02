"""Small parsers: durations, pins."""
import re

_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|min|m|h|d)?\s*$")
_MUL = {"ms": 0.001, "s": 1, "min": 60, "m": 60, "h": 3600, "d": 86400, None: 1}


def seconds(v) -> int:
    """'30s', '5min', '1h', or a bare number of seconds -> int seconds."""
    if isinstance(v, (int, float)):
        return int(v)
    m = _DUR.match(str(v))
    if not m:
        raise ValueError(f"bad duration {v!r}")
    return int(round(float(m.group(1)) * _MUL[m.group(2)]))


def millis(v) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    m = _DUR.match(str(v))
    if not m:
        raise ValueError(f"bad duration {v!r}")
    return int(round(float(m.group(1)) * _MUL[m.group(2)] * 1000))
