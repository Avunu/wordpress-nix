"""PHP's ``unserialize`` for the values WordPress keeps in options: strings, numbers,
booleans, null and arrays (``active_plugins``, ``template``, capabilities).

Objects are refused (``O:``/``C:``): an option never needs one to be read here, and a
dump is untrusted input. Lengths count bytes, so the input is bytes or UTF-8 text.
"""


class PhpSerialError(ValueError):
    pass


def loads(data: bytes | str):
    raw = data.encode() if isinstance(data, str) else data
    value, end = _value(raw, 0)
    if end != len(raw):
        raise PhpSerialError(f"trailing data at {end}")
    return value


def _until(raw: bytes, i: int, ch: bytes) -> tuple[bytes, int]:
    j = raw.find(ch, i)
    if j < 0:
        raise PhpSerialError(f"expected {ch!r} after {i}")
    return raw[i:j], j + 1


def _value(raw: bytes, i: int):
    if i + 1 >= len(raw) + 1 or i >= len(raw):
        raise PhpSerialError("truncated")
    kind = raw[i : i + 1]
    if kind == b"N":
        if raw[i : i + 2] != b"N;":
            raise PhpSerialError(f"bad null at {i}")
        return None, i + 2
    if raw[i + 1 : i + 2] != b":":
        raise PhpSerialError(f"bad value at {i}")
    i += 2
    if kind == b"b":
        text, i = _until(raw, i, b";")
        return text == b"1", i
    if kind == b"i":
        text, i = _until(raw, i, b";")
        return int(text), i
    if kind == b"d":
        text, i = _until(raw, i, b";")
        return float(text), i
    if kind == b"s":
        text, i = _until(raw, i, b":")
        n = int(text)
        if raw[i : i + 1] != b'"' or raw[i + 1 + n : i + 3 + n] != b'";':
            raise PhpSerialError(f"bad string at {i}")
        return raw[i + 1 : i + 1 + n].decode("utf-8", errors="replace"), i + 3 + n
    if kind == b"a":
        text, i = _until(raw, i, b":")
        n = int(text)
        if raw[i : i + 1] != b"{":
            raise PhpSerialError(f"bad array at {i}")
        i += 1
        out: dict = {}
        for _ in range(n):
            key, i = _value(raw, i)
            if not isinstance(key, (int, str)):
                raise PhpSerialError("array key is not an int or a string")
            out[key], i = _value(raw, i)
        if raw[i : i + 1] != b"}":
            raise PhpSerialError(f"unterminated array at {i}")
        return out, i + 1
    raise PhpSerialError(f"unsupported type {kind!r} at {i - 2}")


def as_list(value) -> list:
    """An unserialized PHP list (``a:2:{i:0;…}``) as a Python list of its values."""
    if isinstance(value, dict):
        return [value[k] for k in sorted(value, key=lambda k: (isinstance(k, str), k))]
    if isinstance(value, list):
        return value
    return []


def mysql_unescape(text: str) -> str:
    """A value as mysqldump quotes it (backslash escapes) back to its text."""
    out = []
    i = 0
    table = {"0": "\0", "n": "\n", "r": "\r", "t": "\t", "Z": "\x1a", "b": "\b"}
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            n = text[i + 1]
            out.append(table.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)
