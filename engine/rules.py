"""Review rules for a dbt model.

Every rule returns findings that carry the evidence that triggered them, why
the issue matters to someone who has to trust this model's numbers, and a
concrete fix. Rules are grouped onto five axes:

    grain       grain and aggregation correctness
    perf        materialisation, incremental logic and cost
    lineage     refs, sources, environment portability
    testing     schema.yml documentation and data tests
    conventions naming, formatting and hygiene
"""

import re

from . import parser as P

AXES = {
    "grain": "Grain & correctness",
    "perf": "Incremental & performance",
    "lineage": "Lineage & portability",
    "testing": "Testing & documentation",
    "conventions": "Conventions & hygiene",
}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

SNOWFLAKE = re.compile(r"^[a-z][a-z0-9_]*$")
PREFIXES = ("stg_", "int_", "fct_", "dim_", "mart_", "fct", "dim")


def _ev(brief, pos, fallback=""):
    number, text = P.line_of(brief["sql_raw"], pos)
    if number:
        return "line %d: %s" % (number, text)
    return fallback


def _finding(fid, axis, severity, title, evidence, why, fix, confidence="high"):
    return {
        "id": fid,
        "axis": axis,
        "axis_label": AXES[axis],
        "severity": severity,
        "title": title,
        "evidence": evidence,
        "why": why,
        "fix": fix,
        "confidence": confidence,
    }


def _grain_column_names(brief):
    grain = brief["grain"]
    names = []
    for key in grain.get("keys", []):
        tail = key.split(".")[-1].strip().lower()
        if tail:
            names.append(tail)
    return names


def _test_names(tests):
    out = []
    for t in tests or []:
        out.append((P.test_name(t) or "").lower())
    return out


def build_findings(brief):
    """Run every rule against a parsed model brief."""
    findings = []
    shape = brief["shape"]
    mat = brief["materialization"]
    grain = brief["grain"]
    joins = brief["joins"]
    sql = brief["sql_raw"]
    select_start = shape.get("select_start") or 0
    select_end = shape.get("select_end") or len(sql)

    # ---------------------------------------------------------------- grain
    if brief["has_aggregates"] and not brief["group_by"]:
        sev = "high" if mat == "incremental" else "medium"
        findings.append(_finding(
            "G01", "grain", sev,
            "Aggregates with no group by",
            _ev(brief, select_start, "select ...")[:240],
            "Without a group by the model collapses to a single row. If that is "
            "not intentional the row grain is undefined and every downstream "
            "join to this model will multiply rows.",
            "Add the group by that defines the grain, or declare the model as a "
            "single-row summary and rename it so the intent is obvious.",
        ))

    for item in brief["fanout"]:
        if item["type"] == "count_star_over_join":
            findings.append(_finding(
                "G02", "grain", "high",
                "count(*) over a joined model",
                _ev(brief, select_start, "count(*)")[:240],
                "count(*) counts rows after the join, so a customer with three "
                "orders is counted three times. The number is wrong and looks "
                "plausible, which is the worst kind of wrong.",
                "Aggregate the base table in a CTE first, then join the "
                "pre-aggregated result, or use count(distinct <base key>).",
            ))
        if item["type"] == "multiple_many_side_joins":
            findings.append(_finding(
                "G03", "grain", "high",
                "Two or more detail tables joined in one model",
                item["detail"],
                "Joining two many-side relations creates a cartesian fanout: "
                "three orders x four refunds is twelve rows, so both sums and "
                "counts are inflated.",
                "Aggregate each detail table in its own CTE at its own grain, "
                "then join the aggregated CTEs to the dimension.",
                "medium",
            ))
        if item["type"] == "aggregate_with_join":
            findings.append(_finding(
                "G14", "grain", "medium",
                "Aggregates computed after a join",
                item["detail"],
                "An aggregate that runs after a join counts the joined rows, "
                "not the rows of the base relation. If the joined relation "
                "can repeat, the measure is inflated.",
                "Aggregate the base relation in its own CTE, then join the "
                "aggregated result.",
                "medium",
            ))
        if item["type"] == "many_side_join_with_aggregates":
            findings.append(_finding(
                "G04", "grain", "medium",
                "Detail table joined while aggregates are computed",
                item["detail"],
                "The joined detail table can repeat the base rows, so any sum "
                "or count in the same select list may be counted more than once.",
                "Pre-aggregate the detail table in a CTE and join the CTE "
                "instead of the raw detail table.",
                "medium",
            ))

    for amb in brief["ambiguous"]:
        findings.append(_finding(
            "G05", "grain", "high",
            "Ambiguous column reference: " + amb["column"],
            _ev(brief, select_start, amb["column"])[:240],
            "The column %s exists on more than one joined relation (%s). SQL "
            "resolves it to one of them silently, so the model can pick the "
            "wrong column without any error." % (
                amb["column"], ", ".join(amb["tables"])),
            "Qualify the column with its relation alias: %s.%s." % (
                amb["tables"][0], amb["column"]),
        ))

    for lj in brief["left_join_filters"]:
        findings.append(_finding(
            "G06", "grain", "high",
            "WHERE predicate on the null-able side of a LEFT JOIN",
            "where " + lj["where"][:200],
            "Filtering a LEFT JOIN's right-hand relation in WHERE removes the "
            "unmatched rows, which silently turns the LEFT JOIN into an INNER "
            "JOIN. The left side loses rows without any warning.",
            "Move the predicate into the ON clause, or keep it in WHERE only "
            "when the right-hand column is guaranteed non-null.",
        ))

    if shape["unions"] > shape["union_all"]:
        findings.append(_finding(
            "G07", "grain", "medium",
            "UNION without UNION ALL",
            "union x%d" % shape["unions"],
            "UNION de-duplicates the combined result, which silently drops rows "
            "that are identical across the branches and forces a sort over the "
            "whole union.",
            "Use UNION ALL when the branches are already disjoint, and add an "
            "explicit dedupe only where you actually need one.",
        ))

    if not grain["declared"]:
        findings.append(_finding(
            "G08", "grain", "high" if (joins or mat == "incremental") else "medium",
            "Row grain is not declared",
            "no group by, no unique_key",
            "Nobody can tell what one row of this model represents. When the "
            "model is joined downstream, an undeclared grain is how duplicated "
            "measurements happen.",
            "Declare the grain: add a group by, or set unique_key in the "
            "config block for incremental models.",
        ))
    elif brief["yaml_present"] and brief["yaml_entry"] is not None:
        desc = (brief["yaml_entry"].get("description") or "").strip()
        if not desc:
            findings.append(_finding(
                "G09", "grain", "medium",
                "Grain is declared but not documented",
                "grain: " + grain["description"],
                "The grain exists in the SQL but nowhere in schema.yml, so the "
                "next analyst has to reverse-engineer it from the SQL.",
                "Describe the model in schema.yml and state the grain in one "
                "sentence: one row per <key>.",
            ))

    for col in brief["columns"]:
        expr = col["expr"]
        if re.search(r"\b(sum|avg)\s*\(", expr, re.I) and not re.search(
            r"coalesce|ifnull|nullif", expr, re.I
        ):
            findings.append(_finding(
                "G10", "grain", "medium",
                "Aggregate over a nullable column with no null handling",
                expr[:200],
                "sum() and avg() skip NULLs. If a missing value means zero "
                "the total silently under-reports, and if it means unknown "
                "the total is right but incomplete - nothing in the model says "
                "which.",
                "Wrap the measure in coalesce(<expr>, 0) when a missing value "
                "means zero, and document the choice.",
            ))
            break

    for item in brief["columns"]:
        if re.match(r"^distinct\b", item["expr"], re.I) and joins:
            findings.append(_finding(
                "G11", "grain", "medium",
                "DISTINCT applied after a join",
                item["expr"][:200],
                "DISTINCT over a joined result hides the fanout instead of "
                "removing it, and it makes the engine sort the whole result.",
                "Remove the DISTINCT and fix the grain at the source.",
            ))
            break

    for ref in brief["refs"]:
        if ref["name"] and ref["name"] == brief["name"]:
            findings.append(_finding(
                "G12", "grain", "critical",
                "Model references itself",
                ref["expr"],
                "A self-reference is a cycle. dbt cannot build this model and "
                "the lineage graph cannot be resolved.",
                "Remove the self-reference, or move the shared logic into a "
                "CTE or a separate model.",
            ))

    if shape["having"] and not brief["group_by"]:
        findings.append(_finding(
            "G13", "grain", "medium",
            "HAVING without a GROUP BY",
            "having " + shape["having"][:200],
            "A HAVING clause filters groups, but there are no groups here, so "
            "it filters a single aggregate row - which is almost never what "
            "was meant.",
            "Add the group by, or move the predicate to WHERE.",
        ))

    # ------------------------------------------------------------ performance
    if mat == "incremental" and not brief["has_incremental_guard"]:
        findings.append(_finding(
            "P01", "perf", "critical",
            "Incremental model with no is_incremental() guard",
            _ev(brief, 0, "{{ config(materialized='incremental') }}")[:240],
            "Every run rebuilds the whole table and appends it to itself, so "
            "the model doubles in size on every run and the numbers drift.",
            "Wrap the row filter in {% if is_incremental() %} and filter on the "
            "updated timestamp of the source.",
        ))

    if mat == "incremental" and not brief["unique_key"]:
        findings.append(_finding(
            "P02", "perf", "high",
            "Incremental model with no unique_key",
            _ev(brief, 0, "materialized='incremental'")[:240],
            "Without a unique_key dbt has to delete and insert whole blocks, "
            "and re-processing the same row can leave duplicates behind.",
            "Set unique_key in the config block to the column that identifies "
            "one row of this model.",
        ))

    if mat == "incremental" and brief["has_incremental_guard"] and not brief["incremental_filter"]:
        findings.append(_finding(
            "P03", "perf", "high",
            "Incremental guard has no row filter",
            _ev(brief, 0, "is_incremental()")[:240],
            "The guard is present but nothing inside it limits the rows, so "
            "the full table is still scanned and rebuilt every run.",
            "Add a WHERE inside the guard, usually "
            "where updated_at > (select max(updated_at) from {{ this }}).",
        ))

    if shape["has_select_star"]:
        findings.append(_finding(
            "P04", "perf", "high",
            "select * in the model",
            _ev(brief, select_start, "select *")[:240],
            "select * freezes whatever the source happens to have today. The "
            "next source column silently appears in the model, the column "
            "order changes, and downstream marts break.",
            "List the columns explicitly, and add a contract test if you want "
            "to know when the source shape changes.",
        ))

    for join in joins:
        if join["type"] == "cross" or (not join["on"] and not join["using"]):
            rel = join.get("relation") or {}
            findings.append(_finding(
                "P05", "perf", "high",
                "Join with no join condition",
                _ev(brief, join["start"], join["source_text"])[:240],
                "A join with no ON clause is a cartesian product: the result "
                "is rows_a * rows_b, which is both wrong and enormous.",
                "Add an explicit join condition, or replace the join with a "
                "cross join and a deliberate row-number generator.",
            ))
            break

    if mat != "incremental" and not shape["where"]:
        findings.append(_finding(
            "P06", "perf", "medium",
            "No row filter anywhere in the model",
            _ev(brief, shape.get("from_source", {}).get("start", 0), "from ...")[:240],
            "The model reads the entire source relation on every run. For a "
            "large fact table that is the difference between seconds and "
            "minutes.",
            "Filter to the window the model actually covers, and use "
            "materialized='incremental' if it needs to keep growing.",
        ))

    if shape["order_by"]:
        findings.append(_finding(
            "P07", "perf", "low",
            "ORDER BY inside the model",
            "order by " + shape["order_by"][:200],
            "dbt does not guarantee row order in a relation, so the sort is "
            "paid for on every run and thrown away by the next reader.",
            "Move the ordering to the query or dashboard that needs it.",
        ))

    if shape["limit"]:
        findings.append(_finding(
            "P08", "perf", "high",
            "LIMIT inside the model",
            "limit " + shape["limit"][:200],
            "A limit in a model silently truncates the data. The model looks "
            "fine and the numbers are quietly wrong for everyone downstream.",
            "Remove the limit. If a sample is genuinely needed, put it in a "
            "separate model with an obvious name.",
        ))

    if mat == "view" and brief["has_aggregates"] and len(joins) >= 2:
        findings.append(_finding(
            "P09", "perf", "medium",
            "View materialisation over joins and aggregates",
            _ev(brief, 0, "materialized='view'")[:240],
            "A view is recomputed on every read, so every downstream query "
            "pays for the joins and the aggregation again.",
            "Materialise as a table if the result is stable, or as an "
            "incremental model if it keeps growing.",
        ))

    if len(joins) >= 4:
        findings.append(_finding(
            "P10", "perf", "medium",
            "More than four joins in one model",
            _ev(brief, shape.get("from_source", {}).get("start", 0), "")[:240],
            "Wide join graphs are slow to plan, hard to review and the usual "
            "home of an accidental fanout.",
            "Split the model into a staging join and an aggregation layer.",
        ))

    if re.search(r"like\s+'%", shape["where"], re.I):
        findings.append(_finding(
            "P11", "perf", "medium",
            "Leading-wildcard LIKE",
            shape["where"][:200],
            "A pattern that starts with % cannot use an index, so the engine "
            "scans the whole relation.",
            "Anchor the pattern or use a full-text search instead.",
        ))

    if re.search(r"\bor\b", shape["where"], re.I):
        findings.append(_finding(
            "P12", "perf", "low",
            "OR in a WHERE clause",
            shape["where"][:200],
            "An OR between two different columns usually stops the engine "
            "from using an index on either of them.",
            "Rewrite as UNION ALL of two filtered selects, or use a column "
            "that holds both cases.",
        ))

    if mat == "incremental" and brief["unique_key"]:
        keys = brief["unique_key"] if isinstance(brief["unique_key"], list) else [brief["unique_key"]]
        for key in keys:
            col = str(key).lower()
            tests = _test_names(brief["column_tests"].get(col))
            if "unique" not in tests:
                findings.append(_finding(
                    "P13", "perf", "medium",
                    "unique_key column is not tested unique",
                    "unique_key = " + str(key),
                    "The unique_key is what tells dbt which row to replace. "
                    "If it is not unique, an incremental merge updates one row "
                    "and leaves the duplicates behind.",
                    "Add a unique test on %s in schema.yml." % col,
                ))
                break

    # ---------------------------------------------------------------- lineage
    literal_relations = []
    if brief["from_relation"] and brief["from_relation"].get("literal"):
        literal_relations.append(("from", brief["from_relation"]))
    for join in joins:
        rel = join.get("relation") or {}
        if rel.get("literal"):
            literal_relations.append(("join", rel))

    if literal_relations:
        names = [rel.get("name") or "" for where, rel in literal_relations]
        dotted = [n for n in names if "." in n]
        evidence = ", ".join(names)
        if dotted:
            findings.append(_finding(
                "L02", "lineage", "critical",
                "Hard-coded table names with database and schema",
                evidence,
                "A literal table name is invisible to dbt: it is not in the "
                "lineage graph, dbt cannot stub it in a test, and the model "
                "only runs against one environment. These names also "
                "hard-code the database and schema, so the model breaks the "
                "moment it runs somewhere else.",
                "Replace every one of them with {{ ref('...') }} for a model "
                "or {{ source('...', '...') }} for a source table.",
            ))
        else:
            findings.append(_finding(
                "L01", "lineage", "high",
                "Hard-coded table names",
                evidence,
                "A literal table name is invisible to dbt: it is not in the "
                "lineage graph, dbt cannot stub it in a test, and the model "
                "only runs against one environment.",
                "Replace every one of them with {{ ref('...') }} for a model "
                "or {{ source('...', '...') }} for a source table.",
            ))

    if not brief["refs"] and not brief["sources"]:
        findings.append(_finding(
            "L03", "lineage", "high",
            "No ref() and no source() anywhere in the model",
            _ev(brief, 0, "")[:240],
            "The model has no declared dependencies, so dbt cannot order the "
            "build, cannot stub it in a test and cannot tell you what breaks "
            "when a source changes.",
            "Point every FROM and JOIN at {{ ref() }} or {{ source() }}.",
        ))

    for var in brief["vars"]:
        if var.get("default") is None:
            findings.append(_finding(
                "L04", "lineage", "medium",
                "var() with no default value",
                var["expr"],
                "A var without a default makes the model fail to compile on "
                "any environment that has not defined the variable, and the "
                "failure looks like a project bug rather than a config gap.",
                "Give the var a default: {{ var('name', 'fallback') }}.",
            ))

    for ref in brief["refs"]:
        if ref.get("package"):
            findings.append(_finding(
                "L05", "lineage", "low",
                "Cross-package ref()",
                ref["expr"],
                "A two-argument ref() depends on a package being installed. If "
                "the dependency is missing the model fails at runtime rather "
                "than at parse time.",
                "Make sure the package is declared in packages.yml and pinned.",
            ))
            break

    if brief["macros"]:
        dbt_utils = [m for m in brief["macros"] if "dbt_utils." in m]
        if dbt_utils:
            findings.append(_finding(
                "L06", "lineage", "low",
                "dbt_utils macro used",
                dbt_utils[0],
                "dbt_utils is a package dependency. If it is not installed in "
                "the target environment the model fails to compile.",
                "Declare dbt_utils in packages.yml and pin the version.",
            ))

    if brief["yaml_present"] and brief["yaml_doc"]:
        doc = brief["yaml_doc"]
        known = set()
        if isinstance(doc.get("models"), list):
            for entry in doc["models"]:
                if isinstance(entry, dict) and entry.get("name"):
                    known.add(str(entry["name"]).lower())
        for ref in brief["refs"]:
            if known and ref["name"] and ref["name"].lower() not in known:
                findings.append(_finding(
                    "L07", "lineage", "low",
                    "ref() target is not in the pasted schema.yml",
                    ref["expr"],
                    "The model this depends on is not documented in the "
                    "schema.yml you pasted. That is not necessarily wrong - the "
                    "upstream model may live in another file - but it means "
                    "the lineage cannot be verified from what you provided.",
                    "Check that the target model exists and has its own "
                    "schema.yml entry.",
                    "medium",
                ))
                break

    if brief["sources"]:
        declared = []
        for src in brief["yml_sources"]:
            if isinstance(src, dict):
                declared.append(str(src.get("name") or "").lower())
        for source in brief["sources"]:
            if declared and source.get("source", "").lower() not in declared:
                findings.append(_finding(
                    "L08", "lineage", "medium",
                    "source() used with no matching source definition",
                    source["expr"],
                    "A source that is not declared in schema.yml has no "
                    "freshness check and no column documentation, so dbt "
                    "cannot tell you when it goes stale.",
                    "Declare the source with a loader and a freshness block.",
                ))
                break

    # --------------------------------------------------------------- testing
    if brief["yaml_present"]:
        entry = brief["yaml_entry"]
        if entry is None:
            findings.append(_finding(
                "T01", "testing", "high",
                "Model is not in schema.yml",
                "schema.yml provided but no entry named " + brief["name"],
                "The model has no documentation and no tests, so nothing "
                "checks its grain, its keys or its column names.",
                "Add a models entry for this model with a description, its "
                "grain column and at least not_null and unique tests.",
            ))
        else:
            desc = (entry.get("description") or "").strip()
            if not desc:
                findings.append(_finding(
                    "T02", "testing", "medium",
                    "No description on the model",
                    "models: name: " + str(entry.get("name")),
                    "The description is where the grain and the business "
                    "meaning are written down. Without it the next analyst "
                    "reads the SQL instead.",
                    "Describe the model in one or two sentences, including "
                    "what one row represents.",
                ))
            all_tests = list(brief["model_tests"])
            for col_tests in brief["column_tests"].values():
                all_tests.extend(col_tests)
            if not all_tests:
                findings.append(_finding(
                    "T03", "testing", "high",
                    "No tests at all",
                    "schema.yml entry with no tests",
                    "Nothing verifies this model's shape. A broken join or a "
                    "renamed column surfaces as wrong numbers in a dashboard "
                    "rather than as a failed test.",
                    "Add not_null on the key columns, unique on the grain "
                    "column, and relationships on every foreign key.",
                ))
            grain_names = _grain_column_names(brief)
            for name in grain_names:
                tests = _test_names(brief["column_tests"].get(name))
                if "unique" not in tests:
                    findings.append(_finding(
                        "T04", "testing", "high",
                        "Grain column %s has no unique test" % name,
                        "grain column: " + name,
                        "The grain column is the one thing that must never "
                        "repeat. Without a unique test, duplication is "
                        "detected by a human noticing a total that looks off.",
                        "Add a unique test on %s." % name,
                    ))
                if "not_null" not in tests:
                    findings.append(_finding(
                        "T05", "testing", "medium",
                        "Grain column %s has no not_null test" % name,
                        "grain column: " + name,
                        "A null grain key means the row cannot be joined "
                        "reliably downstream and it usually means an upstream "
                        "filter dropped something.",
                        "Add a not_null test on %s." % name,
                    ))
                break
            columns = entry.get("columns")
            if isinstance(columns, list) and columns:
                undocumented = [
                    c.get("name") for c in columns
                    if isinstance(c, dict) and not (c.get("description") or "").strip()
                ]
                if len(undocumented) >= max(2, len(columns) // 2):
                    findings.append(_finding(
                        "T06", "testing", "medium",
                        "Most columns have no description",
                        "%d of %d columns undocumented" % (
                            len(undocumented), len(columns)),
                        "Undocumented columns are where the surprising values "
                        "live. The documentation is what stops the next "
                        "person from trusting a column they do not "
                        "understand.",
                        "Describe each column, especially the measures and "
                        "the flags.",
                    ))
            if joins and "relationships" not in [t.lower() for t in _test_names(all_tests)]:
                findings.append(_finding(
                    "T07", "testing", "medium",
                    "No relationships test on the joined keys",
                    _ev(brief, shape.get("from_source", {}).get("start", 0), "")[:240],
                    "The join keys are assumed to match. When an upstream "
                    "model changes, the join silently loses rows instead of "
                    "failing a test.",
                    "Add a relationships test for each joined key, pointing at "
                    "the model it references.",
                ))
        for src in brief["yml_sources"]:
            if not isinstance(src, dict):
                continue
            has_freshness = bool(src.get("freshness"))
            tables = src.get("tables")
            if not has_freshness and isinstance(tables, list):
                has_freshness = any(
                    isinstance(t, dict) and t.get("freshness") for t in tables
                )
            if not has_freshness:
                findings.append(_finding(
                    "T08", "testing", "low",
                    "Source declared without a freshness check",
                    "source: " + str(src.get("name")),
                    "Without a freshness check dbt cannot warn you that the "
                    "data stopped moving.",
                    "Add a loaded_at_field and a freshness block to the source.",
                ))
                break

    if not brief["tags"]:
        findings.append(_finding(
            "T09", "conventions", "low",
            "Model has no tags",
            "no tags in the config block",
            "Tags are how a project selects models for a run, a contract "
            "check or an owner notification. A model without tags is a model "
            "nobody can target.",
            "Add tags to the config block, for example "
            "tags=['daily', 'finance'].",
        ))

    # ----------------------------------------------------------- conventions
    name = brief["name"]
    if name and re.search(r"[A-Z]", name):
        findings.append(_finding(
            "C01", "conventions", "medium",
            "Model name contains upper case",
            "model name: " + name,
            "Mixed-case model names sort badly in the file tree, break "
            "case-sensitive tooling and are hard to match in a ref().",
            "Use lower case with underscores: " + name.lower(),
        ))

    if name and not SNOWFLAKE.match(name):
        findings.append(_finding(
            "C02", "conventions", "low",
            "Model name is not lower_snake_case",
            "model name: " + name,
            "dbt's default naming convention is lower case with underscores. "
            "Anything else needs a custom macro to resolve consistently.",
            "Rename the model to lower_snake_case.",
        ))

    for col in brief["columns"]:
        alias = (col["alias"] or "").lower()
        if alias and alias in P.RESERVED_COLUMNS:
            findings.append(_finding(
                "C03", "conventions", "medium",
                "Column name shadows a reserved word: " + alias,
                col["expr"][:200],
                "Reserved words have to be quoted in every query that uses "
                "them, and an unquoted use fails on some warehouses and "
                "silently means something else on others.",
                "Rename the column to something specific, for example "
                + alias + "_id.",
            ))
            break

    for clause in ("where", "having"):
        text = re.sub(r"'[^']*'", " ", shape.get(clause) or "")
        for m in re.finditer(r"(?<![<>=!])\b(\d{2,})\b", text):
            if int(m.group(1)) in (0, 1):
                continue
            findings.append(_finding(
                "C04", "conventions", "low",
                "Magic number in the " + clause + " clause",
                text[:200],
                "A bare number in a filter has no name and no explanation, so "
                "the next person cannot tell what it means or whether it is "
                "still right.",
                "Move it into a dbt var with a descriptive name and a default.",
            ))
            break
        if findings and findings[-1]["id"] == "C04":
            break

    if brief["hardcoded_dates"]:
        findings.append(_finding(
            "C05", "conventions", "medium",
            "Hard-coded date literal",
            brief["hardcoded_dates"][0],
            "A date literal freezes the model to a point in time. The model "
            "keeps running and quietly returns the same rows forever.",
            "Use {{ var('start_date') }} or an incremental filter on the "
            "source instead.",
        ))

    if brief["secret_hits"]:
        findings.append(_finding(
            "C06", "conventions", "critical",
            "Credential-looking value in the model",
            brief["secret_hits"][0][:120],
            "A password or token in a model file is in git history forever "
            "and is readable by everyone with repo access.",
            "Move it to an environment variable and rotate the value that was "
            "committed.",
        ))

    commented = re.findall(r"^\s*--\s*(select|from|join|where|group by|insert|update)\b",
                           sql, re.I | re.M)
    if commented:
        findings.append(_finding(
            "C07", "conventions", "low",
            "Commented-out SQL left in the file",
            commented[0],
            "Dead SQL in a model file makes the live logic harder to read and "
            "tends to drift out of date without anyone noticing.",
            "Delete it. Git already remembers the old version.",
        ))

    if brief["longest_line"] > 120:
        findings.append(_finding(
            "C08", "conventions", "low",
            "Line longer than 120 characters",
            "longest line: %d characters" % brief["longest_line"],
            "Very long lines make a diff unreadable, which hides real changes "
            "in the review.",
            "Break the expression across lines.",
        ))

    if brief["todo_count"]:
        findings.append(_finding(
            "C09", "conventions", "low",
            "TODO or FIXME left in the model",
            "%d marker(s) found" % brief["todo_count"],
            "A TODO in a model is a promise with no owner and no date, and it "
            "usually outlives the person who wrote it.",
            "Resolve it or turn it into a tracked issue with a link.",
        ))

    if name and not any(name.startswith(p) for p in PREFIXES):
        findings.append(_finding(
            "C10", "conventions", "low",
            "Model name has no layer prefix",
            "model name: " + name,
            "Layer prefixes (stg_, int_, fct_, dim_) are what let a reviewer "
            "read the DAG without opening every file.",
            "Prefix the name with the layer it belongs to.",
        ))

    if re.search(r'"[A-Za-z_][A-Za-z0-9_]*"', brief["sql_clean"]):
        findings.append(_finding(
            "C11", "conventions", "low",
            "Quoted identifier",
            re.search(r'"[A-Za-z_][A-Za-z0-9_]*"', brief["sql_clean"]).group(0),
            "Quoted identifiers are case-sensitive and hide the fact that the "
            "name needs quoting in the first place.",
            "Use an unquoted lower-case name.",
        ))

    if brief["has_semicolon"]:
        findings.append(_finding(
            "C12", "conventions", "low",
            "Trailing semicolon",
            "the file ends with ';'",
            "dbt compiles the file into a larger statement, so a stray "
            "semicolon is a syntax error in the generated SQL.",
            "Remove the semicolon.",
        ))

    if brief["has_tabs"]:
        findings.append(_finding(
            "C13", "conventions", "low",
            "Tab indentation",
            "the file contains tab characters",
            "Tabs and spaces mixed in one file produce an inconsistent diff "
            "that hides real changes.",
            "Use spaces.",
        ))

    for cte in shape["ctes"]:
        if not any(cte.startswith(p) for p in ("stg_", "int_", "base_", "agg_", "fct_", "dim_")):
            findings.append(_finding(
                "C14", "conventions", "low",
                "CTE name has no layer prefix: " + cte,
                "with " + cte + " as (...)",
                "Prefixed CTE names show what each step does at a glance and "
                "keep the naming convention consistent with the models.",
                "Rename the CTE, for example int_" + cte + ".",
            ))
            break

    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["id"]))
    return findings
