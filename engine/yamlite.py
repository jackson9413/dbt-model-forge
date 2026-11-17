"""Minimal YAML subset parser.

Supports the subset of YAML that appears in dbt schema.yml / packages.yml files:
block mappings, block sequences, plain and quoted scalars, inline flow
collections, comments and null/boolean/number literals. No anchors, no
multi-document streams, no block scalars - those are not needed here and
are tolerated by ignoring the unparseable remainder rather than crashing.
"""

import re

__all__ = ["parse", "YamlError"]


class YamlError(ValueError):
    """Raised when the document cannot be parsed at all."""


def _strip_comment(line):
    out = []
    quote = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def split_flow(text):
    """Split a flow collection body on top-level commas."""
    parts = []
    depth = 0
    quote = None
    cur = []
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
            continue
        if ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return parts


def scalar(text):
    """Convert a scalar token into a Python value."""
    t = (text or "").strip()
    if t == "":
        return None
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        inner = t[1:-1]
        if t[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")
        else:
            inner = inner.replace("''", "'")
        return inner
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d*\.\d+", t):
        return float(t)
    if t.startswith("[") and t.endswith("]"):
        body = t[1:-1].strip()
        if not body:
            return []
        return [scalar(p) for p in split_flow(body)]
    if t.startswith("{") and t.endswith("}"):
        body = t[1:-1].strip()
        if not body:
            return {}
        out = {}
        for part in split_flow(body):
            if ":" in part:
                k, v = part.split(":", 1)
                out[str(scalar(k))] = scalar(v)
        return out
    return t


def _tokenize(text):
    rows = []
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        if line.strip() in ("---", "..."):
            continue
        indent = len(line) - len(line.lstrip(" "))
        rows.append((indent, line.strip()))
    return rows


def _parse_block(rows, i, indent):
    if i >= len(rows):
        return None, i
    if rows[i][1] == "-" or rows[i][1].startswith("- "):
        return _parse_seq(rows, i, indent)
    return _parse_map(rows, i, indent)


def _parse_map(rows, i, indent):
    out = {}
    while i < len(rows):
        ind, content = rows[i]
        if ind < indent:
            break
        if ind > indent:
            i += 1
            continue
        if content == "-" or content.startswith("- "):
            break
        if ":" not in content:
            i += 1
            continue
        key, _, rest = content.partition(":")
        key = str(scalar(key))
        rest = rest.strip()
        i += 1
        if rest == "":
            if i < len(rows) and rows[i][0] > indent:
                val, i = _parse_block(rows, i, rows[i][0])
            elif i < len(rows) and rows[i][0] == indent and (
                rows[i][1] == "-" or rows[i][1].startswith("- ")
            ):
                val, i = _parse_seq(rows, i, indent)
            else:
                val = None
            out[key] = val
        else:
            out[key] = scalar(rest)
    return out, i


def _parse_seq(rows, i, indent):
    items = []
    while i < len(rows):
        ind, content = rows[i]
        if ind != indent:
            break
        if not (content == "-" or content.startswith("- ")):
            break
        item_text = content[1:].strip()
        offset = len(content[1:]) - len(content[1:].lstrip())
        base = indent + offset + 1
        if ":" not in item_text and not item_text.startswith("- "):
            items.append(scalar(item_text))
            i += 1
            continue
        sub = [(base, item_text)]
        i += 1
        while i < len(rows):
            nind, ncontent = rows[i]
            if nind == indent and (ncontent == "-" or ncontent.startswith("- ")):
                break
            if nind < base:
                break
            sub.append((nind, ncontent))
            i += 1
        val, _ = _parse_block(sub, 0, base)
        items.append(val)
    return items, i


def parse(text):
    """Parse a YAML document. Returns None for an empty document."""
    if not text or not text.strip():
        return None
    rows = _tokenize(text)
    if not rows:
        return None
    value, _ = _parse_block(rows, 0, rows[0][0])
    return value
