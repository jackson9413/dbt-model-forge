"""Top level: parse a model, run the rules, score it, build the blueprint."""

from . import blueprint as bp
from . import parser as P
from . import rules as R
from . import scoring as S


def analyze(sql, yaml_text=None, model_name=None, profile="general"):
    """Review a dbt model and return the full result document."""
    brief = P.parse_model(sql, yaml_text, model_name)
    findings = R.build_findings(brief)
    score = S.score(brief, findings, profile)
    plan = bp.build(brief, findings)

    relations = []
    if brief["from_relation"]:
        relations.append({
            "role": "from",
            "kind": brief["from_relation"].get("kind"),
            "name": brief["from_relation"].get("name"),
            "alias": brief["from_relation"].get("alias"),
        })
    for join in brief["joins"]:
        rel = join.get("relation") or {}
        relations.append({
            "role": "join",
            "join_type": join.get("type"),
            "kind": rel.get("kind"),
            "name": rel.get("name"),
            "alias": rel.get("alias"),
            "on": join.get("on") or join.get("using") or "",
        })

    columns = []
    for col in brief["columns"]:
        columns.append({
            "name": col["alias"] or col["expr"],
            "expression": col["expr"],
            "aggregates": col["aggregates"],
            "is_star": col["is_star"],
        })

    return {
        "model": brief["name"],
        "profile": profile,
        "profile_label": score["profile_label"],
        "materialization": brief["materialization"],
        "config": brief["config"],
        "tags": brief["tags"],
        "grain": brief["grain"],
        "fanout": brief["fanout"],
        "ambiguous": brief["ambiguous"],
        "left_join_filters": brief["left_join_filters"],
        "columns": columns,
        "dependencies": {
            "refs": brief["refs"],
            "sources": brief["sources"],
            "relations": relations,
            "vars": brief["vars"],
        },
        "incremental": {
            "guard": brief["has_incremental_guard"],
            "unique_key": brief["unique_key"],
            "filter": brief["incremental_filter"],
        },
        "stats": {
            "lines": brief["line_count"],
            "longest_line": brief["longest_line"],
            "column_count": len(columns),
            "join_count": len(brief["joins"]),
            "ctes": brief["shape"]["ctes"],
        },
        "score": score,
        "findings": findings,
        "blueprint": plan,
        "notes": score["notes"],
    }
