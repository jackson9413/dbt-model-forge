"""Markdown export for a model review."""

import json

BT = chr(96)
FENCE = BT * 3


def _table(rows, headers):
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return out


def to_markdown(result):
    score = result["score"]
    lines = []
    lines.append("# dbt model review: %s" % result["model"])
    lines.append("")
    lines.append("**Verdict:** %s (%s / 100)  " % (score["verdict"], score["overall"]))
    lines.append("**Profile:** %s  " % score["profile_label"])
    lines.append("**Materialisation:** %s  " % result["materialization"])
    lines.append("**Grain:** %s" % result["grain"]["description"])
    lines.append("")

    lines.append("## Scorecard")
    lines.append("")
    rows = []
    for axis in score["axes"]:
        counts = axis["counts"]
        rows.append([
            axis["label"],
            axis["score"] if axis["assessed"] else "not assessed",
            "%d%%" % axis["weight"],
            counts.get("critical", 0),
            counts.get("high", 0),
            counts.get("medium", 0),
            counts.get("low", 0),
        ])
    lines.extend(_table(rows, ["Axis", "Score", "Weight", "Crit", "High", "Med", "Low"]))
    lines.append("")
    if score["strengths"]:
        lines.append("### What is already right")
        lines.append("")
        for strength in score["strengths"]:
            lines.append("- %s" % strength)
        lines.append("")

    lines.append("## Shape")
    lines.append("")
    deps = result["dependencies"]
    reads = ", ".join(
        "%s %s" % (rel["role"], rel.get("name") or "?") for rel in deps["relations"]
    )
    lines.append("- Reads %d relation(s): %s" % (len(deps["relations"]), reads or "-"))
    if deps["refs"]:
        lines.append("- ref(): %s" % ", ".join(r["expr"] for r in deps["refs"]))
    if deps["sources"]:
        lines.append("- source(): %s" % ", ".join(s["expr"] for s in deps["sources"]))
    if result["columns"]:
        lines.append("- Produces %d column(s): %s" % (
            len(result["columns"]),
            ", ".join(c["name"] for c in result["columns"][:20]),
        ))
    inc = result["incremental"]
    if result["materialization"] == "incremental":
        lines.append("- Incremental guard: %s, unique_key: %s, filter: %s" % (
            "present" if inc["guard"] else "MISSING",
            inc["unique_key"] or "MISSING",
            inc["filter"] or "MISSING",
        ))
    lines.append("")

    lines.append("## Findings")
    lines.append("")
    if not result["findings"]:
        lines.append("No findings. This model passed every rule.")
        lines.append("")
    else:
        for severity in ("critical", "high", "medium", "low"):
            group = [f for f in result["findings"] if f["severity"] == severity]
            if not group:
                continue
            lines.append("### %s (%d)" % (severity.capitalize(), len(group)))
            lines.append("")
            for finding in group:
                lines.append("#### %s - %s" % (finding["id"], finding["title"]))
                lines.append("")
                lines.append("- **Axis:** %s" % finding["axis_label"])
                lines.append("- **Evidence:** %s" % finding["evidence"])
                lines.append("- **Why it matters:** %s" % finding["why"])
                lines.append("- **Fix:** %s" % finding["fix"])
                lines.append("")

    lines.append("## Recommended shape")
    lines.append("")
    lines.append("%s" % result["blueprint"]["reason"])
    lines.append("")
    lines.append(FENCE + "sql")
    lines.append(result["blueprint"]["config_block"])
    lines.append(FENCE)
    lines.append("")
    if result["blueprint"]["tests"]:
        lines.append("### Tests to add")
        lines.append("")
        for test in result["blueprint"]["tests"]:
            lines.append("- " + BT + str(test["column"]) + BT + ": "
                         + ", ".join(test["tests"]) + " - " + test["reason"])
        lines.append("")
    lines.append("### schema.yml")
    lines.append("")
    lines.append(FENCE + "yaml")
    lines.append(result["blueprint"]["schema_yml"])
    lines.append(FENCE)
    lines.append("")
    if result["blueprint"]["structure"]:
        lines.append("### Shape checklist")
        lines.append("")
        for step in result["blueprint"]["structure"]:
            lines.append("- [ ] %s" % step)
        lines.append("")

    lines.append("## Fix plan")
    lines.append("")
    for step in score["fix_plan"]:
        gain = "+%.1f" % step["estimated_gain"] if step["estimated_gain"] else "no score gain"
        lines.append("%d. **[%s] %s** - %s" % (
            step["step"], step["severity"], step["title"], step["fix"]))
        lines.append("   - %s (estimated score change %s)" % (step["axis_label"], gain))
        if step["blocked"]:
            lines.append("   - %s" % step["blocked"])
    lines.append("")

    if result.get("notes"):
        lines.append("## Notes")
        lines.append("")
        for note in result["notes"]:
            lines.append("- %s" % note)
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("Generated locally by dbt-model-forge. No data left this machine.")
    lines.append("")
    return "\n".join(lines)


def to_json(result):
    return json.dumps(result, indent=2, default=str)
