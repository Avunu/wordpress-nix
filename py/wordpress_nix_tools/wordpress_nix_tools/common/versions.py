"""Version strings as WordPress and Composer compare them.

``compare`` is PHP's ``version_compare`` for the forms plugins use (``1.2``,
``1.2.3.4``, ``6.7-beta2``, ``v2.0``): numeric parts compare as numbers, a missing
part is lower than ``0`` only when the other side has a pre-release, and
``dev < alpha < beta < RC < (release) < pl``.
"""

import re

_SPECIAL = {"dev": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2, "rc": 3, "#": 4, "pl": 5, "p": 5}
_SPLIT = re.compile(r"[-_+.]")


def _canon(version: str) -> list[str]:
    v = version.strip().lstrip("vV")
    # PHP inserts a separator between a digit and a letter, both ways.
    v = re.sub(r"(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)", ".", v)
    return [p for p in _SPLIT.split(v) if p]


def _part(p: str) -> tuple[int, int]:
    if p.isdigit():
        return (1, int(p))
    return (0, _SPECIAL.get(p.lower(), -1))


def compare(a: str, b: str) -> int:
    """-1, 0 or 1, as PHP's version_compare."""
    pa, pb = _canon(a), _canon(b)
    for i in range(max(len(pa), len(pb))):
        if i >= len(pa):
            x, y = (0, _SPECIAL["#"]), _part(pb[i])
            # A missing part against a number: the longer version is newer.
            if y[0] == 1:
                return -1
        elif i >= len(pb):
            x, y = _part(pa[i]), (0, _SPECIAL["#"])
            if x[0] == 1:
                return 1
        else:
            x, y = _part(pa[i]), _part(pb[i])
        if x != y:
            return -1 if x < y else 1
    return 0


def at_least(version: str, floor: str) -> bool:
    return compare(version, floor) >= 0


def normalize(version: str) -> str:
    """A lock or header version without a leading v."""
    return version.strip().lstrip("vV")


def caret(version: str) -> str:
    """``^x.y`` for an installed version: the loosened constraint (§8.5 step 11)."""
    parts = [p for p in re.split(r"[.]", normalize(version).split("-")[0]) if p.isdigit()]
    if not parts:
        return f"^{normalize(version)}"
    return "^" + ".".join(parts[:2] if len(parts) > 1 else parts)


def major(version: str) -> int | None:
    parts = _canon(version)
    return int(parts[0]) if parts and parts[0].isdigit() else None
