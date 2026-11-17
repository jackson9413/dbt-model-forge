"""Build the recommended shape for the model: config block, tests, schema.yml."""

import re

from . import parser as P

BT = chr(96)


def _grain_columns(brief):
    names = []
    for key in brief["grain"].get("keys", []):
        tail = key.split(".")[-1].strip()
        tail = re.sub(r"[^A-Za-z0-9_]", "", tail)
        if tail and tail.lower() not in names:
            names.append(tail.lower())
    return names


def recommend_materialization(brief):
    """Recommend a materialisation and say why."""
    current = brief["materialization"]
    if current == "incremental":
        if not brief["has_incremental_guard"]:
            return "table", (
                "The model is declared incremental but has no is_incremental() "
                "guard, so it is not safe to run incrementally yet. Build it as "
                "a table until the guard is in place."
            )
        return "incremental", (
            "Incremental is the right choice for this model: the guard is in "
            "place and the model keeps growing."
        )
    if brief["has_aggregates"] and brief["joins"]:
        return "table", (
            "The model joins and aggregates. A view recomputes all of that on "
            "every read, so materialise it as a table."
        )
    if brief["grain"]["declared"] and brief["grain"]["method"] == "group by":
        return "table", (
            "The model aggregates to a declared grain, which is exactly what a "
            "table is for."
        )
    return "view", (
        "The model is a pass-through with no aggregation, so a view is "
        "appropriate and keeps the build cheap."
    )


def build(brief, findings):
    """Return the recommended model shape."""
    materialization, reason = recommend_materialization(brief)
    names = _grain_columns(brief)
    key = names[0] if names else "id"

    config_lines = ["{{ config("]
    config_lines.append("    materialized='%s'," % materialization)
    if materialization == "incremental":
        if brief["unique_key"]:
            uk = brief["unique_key"]
            uk = uk if isinstance(uk, list) else [uk]
            rendered = ", ".join(str(u) for u in uk)
            config_lines.append("    unique_key=%s," % rendered)
        else:
            config_lines.append("    unique_key='%s'," % key)
    if not brief["tags"]:
        config_lines.append("    tags=['daily'],")
    config_lines.append(") }}")
    config_block = "\n".join(config_lines)

    tests = []
    if names:
        tests.append({
            "column": names[0],
            "tests": ["not_null", "unique"],
            "reason": "The grain key must be present and unique - that is what "
                      "makes one row mean one thing.",
        })
    for col in brief["columns"]:
        alias = (col["alias"] or "").lower()
        if alias and alias in P.RESERVED_COLUMNS:
            tests.append({
                "column": alias,
                "tests": ["not_null"],
                "reason": "A reserved word as a column name needs a test and "
                          "a rename.",
            })
            break
    for join in brief["joins"]:
        rel = join.get("relation") or {}
        if rel.get("kind") == "ref" and rel.get("name"):
            on = join.get("on") or ""
            m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)", on)
            if m:
                tests.append({
                    "column": m.group(2),
                    "tests": ["relationships"],
                    "reason": "The join key must exist in %s, otherwise the join "
                              "silently loses rows." % rel["name"],
                })
            break

    schema_lines = ["models:", "  - name: %s" % brief["name"]]
    schema_lines.append(
        '    description: "One row per %s."' % (
            brief["grain"]["description"].replace("one row per ", "")
            if brief["grain"]["declared"] else "<state the grain here>")
    )
    schema_lines.append("    columns:")
    for col in brief["columns"][:12]:
        alias = col["alias"] or re.sub(r"[^A-Za-z0-9_]", "_", col["expr"][:30]).strip("_")
        if not alias:
            continue
        schema_lines.append("      - name: %s" % alias)
        schema_lines.append('        description: "TODO"')
        for t in tests:
            if t["column"] == alias.lower():
                schema_lines.append("        data_tests:")
                for name in t["tests"]:
                    schema_lines.append("          - %s" % name)
    schema_yml = "\n".join(schema_lines)

    structure = []
    if brief["grain"]["declared"]:
        structure.append("Keep the declared grain (%s) and document it in "
                         "schema.yml." % brief["grain"]["expression"])
    else:
        structure.append("Decide the grain first: add a group by, or set "
                         "unique_key if the model is incremental.")
    if brief["fanout"]:
        structure.append("Pre-aggregate every detail table in its own CTE, then "
                         "join the aggregated CTEs.")
    if brief["materialization"] == "incremental":
        structure.append("Keep the is_incremental() guard and filter on the "
                         "source updated timestamp inside it.")
    structure.append("Replace every literal table name with ref() or source().")
    structure.append("List the columns explicitly instead of select *.")
    structure.append("Add a unique test on the grain column and a relationships "
                     "test on every joined key.")

    return {
        "materialization": materialization,
        "current_materialization": brief["materialization"],
        "reason": reason,
        "config_block": config_block,
        "tests": tests,
        "schema_yml": schema_yml,
        "structure": structure,
    }
