"""Parser for a pasted dbt model.

Extracts Jinja (config / ref / source / var / this / macros), slices the SQL
into clauses with a small depth-aware scanner, detects the row grain,
aggregation shape, join fanout risk, incremental logic and the schema.yml
documentation that ships with the model.

All masking is length-preserving so every byte offset in the parsed SQL maps
back to the same offset in the original text - that keeps line numbers and
evidence snippets accurate.
"""

import re

from . import yamlite

MARK = "\ue000"

JINJA_RE = re.compile(r"\{\{(.*?)\}\}", re.S)
BLOCK_RE = re.compile(r"\{%(.*?)%\}", re.S)
WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
STRING_ARGS_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")

AGG_FUNCS = (
    "sum", "count", "avg", "min", "max", "median", "stddev", "variance",
    "percentile_cont", "percentile_disc", "array_agg", "string_agg",
    "bool_and", "bool_or", "any_value", "approx_count_distinct", "grouping",
)

SQL_KEYWORDS = {
    "select", "from", "where", "group", "by", "order", "having", "limit",
    "offset", "qualify", "window", "union", "intersect", "except", "with",
    "join", "inner", "left", "right", "full", "outer", "cross", "natural",
    "on", "using", "as", "and", "or", "not", "in", "is", "null", "case",
    "when", "then", "else", "end", "distinct", "all", "asc", "desc",
    "over", "partition", "rows", "range", "between", "like", "ilike",
    "cast", "interval", "current_date", "current_timestamp", "true", "false",
    "filter", "within", "unbounded", "preceding", "following", "lateral",
    "values", "insert", "update", "delete", "create", "table",
}

RESERVED_COLUMNS = {
    "order", "group", "user", "users", "date", "timestamp", "table", "end",
    "all", "select", "from", "where", "level", "partition", "index", "check",
    "constraint", "column", "values", "key", "primary", "references",
    "default", "unique", "like", "in", "between", "case", "when", "then",
    "else", "offset", "rows", "range", "current_date", "current_time",
    "current_timestamp", "current_user", "session_user", "grant", "revoke",
    "comment", "cascade", "restrict", "natural", "full", "outer", "cross",
    "interval", "true", "false", "null", "asc", "desc", "distinct", "case",
}

MANY_SIDE_HINTS = (
    "event", "order", "item", "line", "transaction", "payment", "session",
    "click", "visit", "log", "fact", "entrie", "record", "subscription",
    "invoice", "message", "ticket", "activit", "history", "detail",
    "movement", "comment", "review", "charge", "shipment", "revenue_",
    "call_", "page_", "visit_", "purchase", "basket_", "membership",
)

ONE_SIDE_HINTS = ("dim_", "dim.", "dimension", "dim", "stg_", "stg.", "seed")


def blank(text):
    """Replace every non-newline character with a space (length preserving)."""
    return re.sub(r"[^\n]", " ", text)


def split_top(text, sep=","):
    """Split on top-level occurrences of sep, respecting brackets and quotes."""
    parts = []
    depth = 0
    quote = None
    cur = []
    for ch in text or "":
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            cur.append(ch)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return parts


def strip_comments(sql):
    """Remove -- and /* */ comments, preserving offsets."""
    out = list(sql)
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql[i:i + 2] == "--":
            j = sql.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if ch == "/" and sql[i:i + 2] == "/*":
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def string_args(text):
    """Pull the quoted arguments out of a Jinja call body."""
    out = []
    for m in STRING_ARGS_RE.finditer(text or ""):
        out.append(m.group(1) if m.group(1) is not None else m.group(2))
    return out


def parse_literal(value):
    v = (value or "").strip()
    if v.startswith("[") and v.endswith("]"):
        body = v[1:-1].strip()
        if not body:
            return []
        return [parse_literal(p) for p in split_top(body)]
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    low = v.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null":
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def parse_config_kwargs(text):
    """Parse the keyword arguments of a {{ config(...) }} call."""
    start = text.find("(")
    end = text.rfind(")")
    if start == -1 or end == -1 or end < start:
        return {}
    body = text[start + 1:end]
    out = {}
    for part in split_top(body):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip().lower()
        if not key:
            continue
        out[key] = parse_literal(val.strip())
    return out


def classify(expr):
    """Classify a Jinja expression."""
    e = (expr or "").strip()
    low = e.lower()
    if low.startswith("config("):
        return "config"
    if re.match(r"^ref\s*\(", low):
        return "ref"
    if re.match(r"^source\s*\(", low):
        return "source"
    if re.match(r"^var\s*\(", low):
        return "var"
    if low == "this":
        return "this"
    if "dbt_utils." in e:
        return "dbt_utils"
    return "macro"


def extract_jinja(sql):
    """Return every Jinja expression with its offset, kind and payload."""
    out = []
    for m in JINJA_RE.finditer(sql):
        expr = m.group(1).strip()
        kind = classify(expr)
        payload = None
        if kind == "config":
            payload = parse_config_kwargs(expr)
        elif kind in ("ref", "source", "var"):
            start = expr.find("(")
            end = expr.rfind(")")
            body = expr[start + 1:end] if start != -1 and end != -1 else ""
            payload = string_args(body)
        out.append({
            "start": m.start(),
            "end": m.end(),
            "expr": expr,
            "kind": kind,
            "payload": payload,
        })
    return out


def mask_jinja(sql):
    """Replace Jinja expressions with same-length placeholder characters."""
    out = list(sql)
    for m in JINJA_RE.finditer(sql):
        for k in range(m.start(), m.end()):
            out[k] = MARK
    return "".join(out)


def strip_jinja_blocks(sql):
    """Remove {% ... %} tags but keep the SQL they wrap."""
    out = list(sql)
    for m in BLOCK_RE.finditer(sql):
        for k in range(m.start(), m.end()):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


def top_level_words(sql):
    """Return (word, start, end) for every word outside strings at depth 0."""
    out = []
    depth = 0
    quote = None
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if quote:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth -= 1
            i += 1
            continue
        if depth == 0 and (ch.isalpha() or ch == "_"):
            m = WORD_RE.match(sql, i)
            if m:
                out.append((m.group(0).lower(), m.start(), m.end()))
                i = m.end()
                continue
        i += 1
    return out


def line_of(text, pos):
    """Return (line_number, line_text) for an offset in text."""
    if pos is None or pos < 0:
        return 0, ""
    pos = min(pos, len(text))
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    number = text.count("\n", 0, start) + 1
    return number, text[start:end].strip()


def is_many_side(name):
    """Heuristic: does this relation look like a detail/fact table (many rows)?"""
    seg = (name or "").lower().split(".")[-1]
    seg = re.sub(r"[^a-z0-9_]", "", seg)
    if not seg:
        return False
    for hint in ONE_SIDE_HINTS:
        if seg.startswith(hint) or seg == hint.rstrip("."):
            return False
    for hint in MANY_SIDE_HINTS:
        if hint in seg:
            return True
    return False


def parse_alias_and_expr(item):
    """Split a select-list item into (expression, alias)."""
    text = item.strip()
    if not text:
        return "", None
    m = re.search(r"\s+as\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", text, re.I)
    if m:
        return text[:m.start()].strip(), m.group(1)
    m = re.search(r"(\s|^)([A-Za-z_][A-Za-z0-9_]*)\s*$", text)
    if m:
        tail = m.group(2)
        if tail.lower() not in SQL_KEYWORDS:
            expr = text[:m.start(2)].strip()
            if expr:
                return expr, tail
    m = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.([A-Za-z_][A-Za-z0-9_]*)", text)
    if m:
        return text, m.group(1)
    return text, None


def detect_aggregates(expr):
    """Return aggregate function names used in an expression."""
    found = []
    for m in re.finditer(r"\b(" + "|".join(AGG_FUNCS) + r")\s*\(", expr, re.I):
        name = m.group(1).lower()
        if name not in found:
            found.append(name)
    return found


def extract_references(expr):
    """Return qualified refs (table, column) and bare column refs in an expression."""
    qualified = []
    bare = []
    masked = re.sub(r"'[^']*'", " ", expr)
    for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", masked):
        qualified.append((m.group(1).lower(), m.group(2).lower()))
    no_qual = re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", " ", masked)
    for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", no_qual):
        word = m.group(1).lower()
        if word in SQL_KEYWORDS:
            continue
        bare.append(word)
    return qualified, bare


def analyse_sql(sql, jinja):
    """Slice the SQL into clauses and describe its shape."""
    words = top_level_words(sql)
    res = {
        "words": words,
        "select_list": "",
        "select_start": 0,
        "select_end": 0,
        "from_source": None,
        "joins": [],
        "where": "",
        "group_by": [],
        "having": "",
        "order_by": "",
        "limit": "",
        "qualify": "",
        "unions": 0,
        "union_all": 0,
        "ctes": [],
        "has_select_star": False,
    }

    select_idx = None
    for i, (word, start, end) in enumerate(words):
        if word == "select":
            select_idx = i
            break
    if select_idx is None:
        return res

    sel_end = words[select_idx][2]
    from_idx = None
    for i in range(select_idx + 1, len(words)):
        if words[i][0] == "from":
            from_idx = i
            break

    clause_words = {
        "join", "on", "using", "where", "group", "order", "having", "limit",
        "qualify", "window", "union", "intersect", "except", "with",
    }

    clause_start_idx = from_idx if from_idx is not None else select_idx

    def next_clause_after(i):
        j = i
        while j < len(words):
            if words[j][0] in clause_words:
                return j
            j += 1
        return len(words)

    def end_of(idx):
        return words[idx][1] if idx < len(words) else len(sql)

    res["select_start"] = sel_end
    if from_idx is not None:
        res["select_list"] = sql[sel_end:words[from_idx][1]]
        res["select_end"] = words[from_idx][1]
        stop = next_clause_after(from_idx + 1)
        res["from_source"] = {
            "text": sql[words[from_idx][2]:end_of(stop)],
            "start": words[from_idx][2],
            "end": end_of(stop),
        }
    else:
        res["select_list"] = sql[sel_end:].strip()

    join_idxs = [i for i, w in enumerate(words) if w[0] == "join"]
    for ji in join_idxs:
        jtype = "inner"
        prev = words[ji - 1][0] if ji > 0 else ""
        if prev in ("left", "right", "full", "cross", "inner", "outer", "natural"):
            jtype = "natural" if prev == "natural" else prev
        if prev == "outer" and ji > 1 and words[ji - 2][0] in ("left", "right", "full"):
            jtype = words[ji - 2][0]
        stop = next_clause_after(ji + 1)
        source_text = sql[words[ji][2]:end_of(stop)]
        on_text = ""
        using_text = ""
        on_idx = None
        for k in range(ji + 1, stop):
            if words[k][0] in ("on", "using"):
                on_idx = k
                break
        if on_idx is None and stop < len(words) and words[stop][0] in ("on", "using"):
            on_idx = stop
        if on_idx is not None:
            nxt = next_clause_after(on_idx + 1)
            if words[on_idx][0] == "on":
                on_text = sql[words[on_idx][2]:end_of(nxt)].strip()
            else:
                using_text = sql[words[on_idx][2]:end_of(nxt)].strip()
        res["joins"].append({
            "type": jtype,
            "source_text": source_text,
            "on": on_text,
            "using": using_text,
            "start": words[ji][2],
        })

    for name, word_key in (("where", "where"), ("having", "having"), ("order_by", "order"), ("limit", "limit"), ("qualify", "qualify")):
        for i, w in enumerate(words):
            if i < clause_start_idx:
                continue
            if w[0] != word_key:
                continue
            if word_key == "order" and i + 1 < len(words) and words[i + 1][0] == "by":
                continue
            if word_key == "limit":
                stop = i + 1
                while stop < len(words) and words[stop][0] not in clause_words:
                    stop += 1
                res[name] = sql[words[i][2]:end_of(stop)].strip()
                break
            stop = next_clause_after(i + 1)
            res[name] = sql[words[i][2]:end_of(stop)].strip()
            res[name + "_start"] = words[i][2]
            break

    for i, w in enumerate(words):
        if i < clause_start_idx:
            continue
        if w[0] == "group" and i + 1 < len(words) and words[i + 1][0] == "by":
            stop = next_clause_after(i + 2)
            text = sql[words[i + 1][2]:end_of(stop)] if i + 1 < len(words) else ""
            res["group_by"] = [p.strip() for p in split_top(text) if p.strip()]
            res["group_start"] = words[i + 2][2] if i + 2 < len(words) else 0
            break

    for i, w in enumerate(words):
        if i < clause_start_idx:
            continue
        if w[0] == "union":
            res["unions"] += 1
            if i + 1 < len(words) and words[i + 1][0] == "all":
                res["union_all"] += 1

    for i, w in enumerate(words):
        if w[0] != "with":
            continue
        j = i + 1
        while j < len(words) and words[j][0] != "select":
            if words[j][0] == "as":
                name = words[j - 1][0]
                res["ctes"].append(name)
            j += 1
        break

    for item in split_top(res["select_list"]):
        text = item.strip()
        if text == "*" or re.search(r"\b\w+\.\*$", text):
            res["has_select_star"] = True
    return res


def jinja_at(jinja, start, end):
    """Return the Jinja expression overlapping an offset range, if any."""
    for j in jinja:
        if j["start"] < end and start < j["end"]:
            return j
    return None


def relation_from_text(text, start, jinja):
    """Describe a FROM/JOIN relation: kind (ref/source/literal/cte), name, alias."""
    raw = (text or "").strip()
    if not raw:
        return {"kind": "unknown", "name": "", "alias": None, "literal": False}
    lead = len(text) - len(text.lstrip())
    abs_start = start + lead
    hit = jinja_at(jinja, abs_start, abs_start + len(raw))
    if hit is not None:
        payload = hit["payload"] or []
        local_end = hit["end"] - abs_start
        rest = raw[local_end:].strip()
        alias = None
        am = re.match(r"^(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*)$", rest, re.I)
        if am and am.group(1).lower() not in SQL_KEYWORDS:
            alias = am.group(1)
        if hit["kind"] == "ref":
            name = payload[-1] if payload else hit["expr"]
            return {"kind": "ref", "name": name, "alias": alias, "literal": False, "expr": hit["expr"]}
        if hit["kind"] == "source":
            name = payload[-1] if payload else hit["expr"]
            src = payload[0] if len(payload) > 1 else ""
            return {"kind": "source", "name": name, "source": src, "alias": alias, "literal": False, "expr": hit["expr"]}
        if hit["kind"] == "this":
            return {"kind": "this", "name": "this", "alias": alias, "literal": False, "expr": hit["expr"]}
        return {"kind": "macro", "name": hit["expr"], "alias": alias, "literal": False, "expr": hit["expr"]}
    if raw.startswith("("):
        return {"kind": "subquery", "name": raw[:40], "alias": None, "literal": False}
    m = re.match(r"^([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*)", raw)
    if not m:
        return {"kind": "unknown", "name": raw, "alias": None, "literal": True}
    name = m.group(1)
    alias = None
    rest = raw[m.end():].strip()
    am = re.match(r"^(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*)$", rest, re.I)
    if am and am.group(1).lower() not in SQL_KEYWORDS:
        alias = am.group(1)
    return {"kind": "literal", "name": name, "alias": alias, "literal": True}


def find_model_entry(yml, model_name):
    """Locate a model inside a parsed schema.yml document."""
    if not isinstance(yml, dict):
        return None
    models = yml.get("models")
    if not isinstance(models, list):
        return None
    for entry in models:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        alias = entry.get("alias")
        if model_name and (name == model_name or alias == model_name):
            return entry
    return None


def collect_tests(entry):
    """Return (model_level_tests, column -> [tests]) for a schema.yml model entry."""
    model_tests = []
    col_tests = {}
    if not isinstance(entry, dict):
        return model_tests, col_tests
    for key in ("data_tests", "tests"):
        val = entry.get(key)
        if isinstance(val, list):
            for item in val:
                model_tests.append(item)
    columns = entry.get("columns")
    if isinstance(columns, list):
        for col in columns:
            if not isinstance(col, dict):
                continue
            cname = col.get("name")
            found = []
            for key in ("data_tests", "tests"):
                val = col.get(key)
                if isinstance(val, list):
                    found.extend(val)
            if cname:
                col_tests[str(cname).lower()] = found
    return model_tests, col_tests


def test_name(test):
    """Extract the test type from a schema.yml test entry."""
    if isinstance(test, str):
        return test
    if isinstance(test, dict):
        for key in test:
            return key
    return None


def parse_model(sql_text, yaml_text=None, model_name=None):
    """Parse a dbt model (SQL plus optional schema.yml) into a structured brief."""
    sql_raw = sql_text or ""
    yaml_raw = yaml_text or ""

    sql_part = sql_raw
    yaml_part = yaml_raw
    lines = sql_raw.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "---":
            sql_part = "\n".join(lines[:i])
            if not yaml_raw:
                yaml_part = "\n".join(lines[i + 1:])
            break

    jinja = extract_jinja(sql_part)
    masked = mask_jinja(sql_part)
    masked = strip_jinja_blocks(masked)
    sql = strip_comments(masked)

    config = {}
    refs = []
    sources = []
    vars_used = []
    macros = []
    for j in jinja:
        if j["kind"] == "config":
            for k, v in (j["payload"] or {}).items():
                config.setdefault(k, v)
        elif j["kind"] == "ref":
            payload = j["payload"] or []
            refs.append({
                "name": payload[-1] if payload else "",
                "package": payload[0] if len(payload) > 1 else None,
                "expr": j["expr"],
            })
        elif j["kind"] == "source":
            payload = j["payload"] or []
            sources.append({
                "name": payload[-1] if payload else "",
                "source": payload[0] if len(payload) > 1 else "",
                "expr": j["expr"],
            })
        elif j["kind"] == "var":
            payload = j["payload"] or []
            vars_used.append({
                "name": payload[0] if payload else "",
                "default": payload[1] if len(payload) > 1 else None,
                "expr": j["expr"],
            })
        elif j["kind"] == "dbt_utils":
            macros.append(j["expr"])
        elif j["kind"] == "macro":
            macros.append(j["expr"])

    shape = analyse_sql(sql, jinja)

    from_rel = None
    if shape["from_source"]:
        from_rel = relation_from_text(
            shape["from_source"]["text"], shape["from_source"]["start"], jinja
        )
    for join in shape["joins"]:
        join["relation"] = relation_from_text(join["source_text"], join["start"], jinja)

    cte_names = {c.lower() for c in shape["ctes"]}
    for rel in [from_rel] + [j.get("relation") for j in shape["joins"]]:
        if not rel:
            continue
        tail = (rel.get("name") or "").split(".")[-1].lower()
        if tail and tail in cte_names:
            rel["kind"] = "cte"
            rel["literal"] = False

    columns = []
    aggregate_functions = []
    qualified_refs = []
    bare_refs = []
    for item in split_top(shape["select_list"]):
        expr, alias = parse_alias_and_expr(item)
        if not expr:
            continue
        aggs = detect_aggregates(expr)
        aggregate_functions.extend(aggs)
        qual, bare = extract_references(expr)
        qualified_refs.extend(qual)
        bare_refs.extend(bare)
        columns.append({
            "expr": expr,
            "alias": alias,
            "aggregates": aggs,
            "is_star": expr.strip() == "*" or bool(re.search(r"\b\w+\.\*$", expr)),
        })

    where_text = shape["where"]
    if where_text:
        qual, bare = extract_references(where_text)
        qualified_refs.extend(qual)
        bare_refs.extend(bare)
    for join in shape["joins"]:
        on_text = join.get("on") or join.get("using") or ""
        if on_text:
            qual, bare = extract_references(on_text)
            qualified_refs.extend(qual)
            bare_refs.extend(bare)

    group_by = []
    for g in shape["group_by"]:
        gexpr, galias = parse_alias_and_expr(g)
        group_by.append(gexpr)

    materialization = str(config.get("materialized") or "view").lower()
    unique_key = config.get("unique_key")
    if isinstance(unique_key, list):
        unique_key = list(unique_key)

    yml_doc = yamlite.parse(yaml_part) if yaml_part.strip() else None
    entry = find_model_entry(yml_doc, model_name)

    detected_name = model_name or config.get("alias") or ""
    if not detected_name:
        m = re.search(r"--\s*(?:model|name)\s*[:=]\s*([\w\-]+)", sql_raw, re.I)
        if m:
            detected_name = m.group(1)
    if not detected_name and isinstance(yml_doc, dict):
        models = yml_doc.get("models")
        if isinstance(models, list) and len(models) == 1 and isinstance(models[0], dict):
            detected_name = str(models[0].get("name") or "")
    if entry is None and detected_name:
        entry = find_model_entry(yml_doc, detected_name)

    model_tests, col_tests = collect_tests(entry)
    yml_sources = []
    if isinstance(yml_doc, dict) and isinstance(yml_doc.get("sources"), list):
        for s in yml_doc["sources"]:
            if isinstance(s, dict):
                yml_sources.append(s)

    brief = {
        "name": detected_name or "unnamed_model",
        "raw_sql": sql_raw,
        "raw_yaml": yaml_part,
        "config": config,
        "materialization": materialization,
        "unique_key": unique_key,
        "tags": config.get("tags") or [],
        "refs": refs,
        "sources": sources,
        "vars": vars_used,
        "macros": macros,
        "has_incremental_guard": any(
            re.search(r"is_incremental\s*\(\s*\)", m.group(1), re.I)
            for m in BLOCK_RE.finditer(sql_raw)
        ),
        "incremental_filter": "",
        "shape": shape,
        "from_relation": from_rel,
        "joins": shape["joins"],
        "columns": columns,
        "aggregate_functions": sorted(set(aggregate_functions)),
        "has_aggregates": bool(aggregate_functions),
        "group_by": group_by,
        "qualified_refs": qualified_refs,
        "bare_refs": bare_refs,
        "yaml_present": bool(yaml_part.strip()),
        "yaml_entry": entry,
        "yaml_doc": yml_doc,
        "model_tests": model_tests,
        "column_tests": col_tests,
        "yml_sources": yml_sources,
        "line_count": len(sql_raw.splitlines()),
        "longest_line": max((len(l) for l in sql_raw.splitlines()), default=0),
        "has_semicolon": bool(re.search(r";\s*$", sql_raw.strip())),
        "has_tabs": "\t" in sql_raw,
        "todo_count": len(re.findall(r"\b(TODO|FIXME|XXX|HACK)\b", sql_raw, re.I)),
        "secret_hits": [
            m.group(0) for m in re.finditer(
                r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|"
                r"private[_-]?key|bearer)\b\s*[:=]\s*['\"][^'\"]{6,}", sql_raw
            )
        ],
        "hardcoded_dates": re.findall(r"'\d{4}-\d{2}-\d{2}(?:[ T][\d:]+)?'", sql_raw),
        "jinja": jinja,
        "sql_clean": sql,
        "sql_raw": sql_raw,
    }

    grain = detect_grain(brief)
    brief["grain"] = grain
    brief["fanout"] = detect_fanout(brief)
    brief["ambiguous"] = detect_ambiguous(brief)
    brief["left_join_filters"] = detect_left_join_filters(brief)
    brief["incremental_filter"] = detect_incremental_filter(brief)
    brief["select_columns"] = [
        c["alias"] or c["expr"] for c in brief["columns"]
    ]
    return brief


def detect_grain(brief):
    """Work out the row grain the model produces."""
    group_by = brief["group_by"]
    unique_key = brief["unique_key"]
    materialization = brief["materialization"]
    if group_by:
        exprs = [g.strip() for g in group_by]
        return {
            "declared": True,
            "method": "group by",
            "expression": ", ".join(exprs),
            "keys": exprs,
            "description": "one row per " + ", ".join(exprs),
            "confidence": "high",
        }
    if unique_key:
        keys = unique_key if isinstance(unique_key, list) else [unique_key]
        return {
            "declared": True,
            "method": "unique_key",
            "expression": ", ".join(str(k) for k in keys),
            "keys": [str(k) for k in keys],
            "description": "one row per " + ", ".join(str(k) for k in keys),
            "confidence": "high",
        }
    if brief["has_aggregates"]:
        return {
            "declared": False,
            "method": "aggregate without group by",
            "expression": "",
            "keys": [],
            "description": "a single summary row (no group by)",
            "confidence": "medium",
        }
    if materialization == "incremental":
        return {
            "declared": False,
            "method": "incremental without a key",
            "expression": "",
            "keys": [],
            "description": "unclear: no group by and no unique_key",
            "confidence": "low",
        }
    return {
        "declared": False,
        "method": "pass-through",
        "expression": "",
        "keys": [],
        "description": "one row per input row (not declared)",
        "confidence": "low",
    }


def detect_fanout(brief):
    """Flag joins that can multiply rows and inflate aggregates."""
    findings = []
    joins = brief["joins"]
    many = []
    for join in joins:
        rel = join.get("relation") or {}
        name = rel.get("name") or ""
        if is_many_side(name):
            many.append(join)
    aggs = brief["aggregate_functions"]
    star_count = False
    for col in brief["columns"]:
        if col["aggregates"] and "count" in col["aggregates"]:
            expr = col["expr"].lower()
            if re.search(r"count\s*\(\s*(\*|1)\s*\)", expr):
                star_count = True
    has_distinct_count = any(
        re.search(r"count\s*\(\s*distinct", c["expr"], re.I) for c in brief["columns"]
    )
    if joins and (star_count or (brief["has_aggregates"] and not has_distinct_count)):
        findings.append({
            "type": "aggregate_with_join",
            "detail": "aggregates computed after joining %d relation(s)" % len(joins),
            "many_side": [ (j.get("relation") or {}).get("name") for j in many ],
            "severity_hint": "high" if many else "medium",
        })
    if len(many) >= 2:
        findings.append({
            "type": "multiple_many_side_joins",
            "detail": "joins %d detail/fact tables: %s" % (
                len(many),
                ", ".join((j.get("relation") or {}).get("name") or "?" for j in many),
            ),
            "many_side": [ (j.get("relation") or {}).get("name") for j in many ],
            "severity_hint": "high",
        })
    elif many and brief["has_aggregates"]:
        findings.append({
            "type": "many_side_join_with_aggregates",
            "detail": "detail table %s is joined while aggregates are computed" % (
                (many[0].get("relation") or {}).get("name") or "?"
            ),
            "many_side": [ (j.get("relation") or {}).get("name") for j in many ],
            "severity_hint": "medium",
        })
    if star_count and joins and not has_distinct_count:
        findings.append({
            "type": "count_star_over_join",
            "detail": "count(*) over a joined model counts joined rows, not base rows",
            "many_side": [ (j.get("relation") or {}).get("name") for j in many ],
            "severity_hint": "high",
        })
    return findings


def detect_ambiguous(brief):
    """Find unqualified column names that exist on more than one joined relation."""
    tables = {}
    for table, col in brief["qualified_refs"]:
        tables.setdefault(table, set()).add(col)
    if len(tables) < 2:
        return []
    duplicated = {}
    for table, cols in tables.items():
        for col in cols:
            duplicated.setdefault(col, []).append(table)
    ambiguous = []
    for col in sorted(set(brief["bare_refs"])):
        owners = duplicated.get(col)
        if owners and len(owners) >= 2:
            ambiguous.append({"column": col, "tables": sorted(owners)})
    return ambiguous


def detect_left_join_filters(brief):
    """Find WHERE predicates on the right side of a LEFT JOIN (silently inner)."""
    out = []
    where = brief["shape"]["where"]
    if not where:
        return out
    for join in brief["joins"]:
        if join.get("type") != "left":
            continue
        rel = join.get("relation") or {}
        name = (rel.get("name") or "").split(".")[-1].lower()
        if not name:
            continue
        alias = (rel.get("alias") or name).lower()
        pattern = re.compile(r"\b" + re.escape(alias) + r"\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)")
        hits = []
        for m in pattern.finditer(where):
            hits.append(m.group(1))
        for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)", where):
            if m.group(1).lower() == alias:
                hits.append(m.group(2))
        if hits:
            out.append({
                "table": rel.get("name") or alias,
                "columns": sorted(set(hits)),
                "where": where,
            })
    return out


def detect_incremental_filter(brief):
    """Find the row filter used inside an is_incremental() guard."""
    raw = brief["sql_raw"]
    for block in BLOCK_RE.finditer(raw):
        if not re.search(r"is_incremental\s*\(\s*\)", block.group(1), re.I):
            continue
        tail = raw[block.end():]
        end = re.search(r"\{%\s*endif\s*%\}", tail)
        if end:
            tail = tail[:end.start()]
        wm = re.search(r"\bwhere\b", tail, re.I)
        if not wm:
            return ""
        rest = tail[wm.end():]
        stop = len(rest)
        for kw in ("group by", "order by", "having", "limit", "qualify"):
            km = re.search(r"\b" + kw + r"\b", rest, re.I)
            if km:
                stop = min(stop, km.start())
        return rest[:stop].strip()
    return ""
