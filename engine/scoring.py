"""Scoring: five axes, profile weights, structural caps and the fix plan."""

AXIS_ORDER = ["grain", "perf", "lineage", "testing", "conventions"]

AXIS_LABELS = {
    "grain": "Grain & correctness",
    "perf": "Incremental & performance",
    "lineage": "Lineage & portability",
    "testing": "Testing & documentation",
    "conventions": "Conventions & hygiene",
}

PROFILES = {
    "general": {
        "label": "General review",
        "weights": {"grain": 25, "perf": 20, "lineage": 20, "testing": 20, "conventions": 15},
    },
    "grain_audit": {
        "label": "Grain audit",
        "weights": {"grain": 40, "perf": 15, "lineage": 15, "testing": 15, "conventions": 15},
    },
    "incremental_review": {
        "label": "Incremental & performance review",
        "weights": {"grain": 20, "perf": 35, "lineage": 20, "testing": 15, "conventions": 10},
    },
    "new_model_onboarding": {
        "label": "New model onboarding",
        "weights": {"grain": 20, "perf": 15, "lineage": 20, "testing": 30, "conventions": 15},
    },
    "legacy_refactor": {
        "label": "Legacy refactor",
        "weights": {"grain": 25, "perf": 25, "lineage": 25, "testing": 15, "conventions": 10},
    },
}

SEVERITY_PENALTY = {"critical": 25, "high": 12, "medium": 5, "low": 2, "info": 0}

CAPS = [
    {"finding": "G02", "axis": "grain", "axis_cap": 40, "overall_cap": 55,
     "note": "count(*) over a joined model can silently over-count"},
    {"finding": "G01", "axis": "grain", "axis_cap": 45, "overall_cap": 70,
     "note": "aggregates with no group by leave the grain undefined"},
    {"finding": "G06", "axis": "grain", "axis_cap": 40, "overall_cap": 60,
     "note": "a WHERE filter on the null side of a LEFT JOIN hides unmatched rows"},
    {"finding": "G03", "axis": "grain", "axis_cap": 40, "overall_cap": 58,
     "note": "two detail tables joined in one model fan out"},
    {"finding": "G12", "axis": "lineage", "axis_cap": 20, "overall_cap": 25,
     "note": "a self-referencing model is a cycle dbt cannot build"},
    {"finding": "P01", "axis": "perf", "axis_cap": 35, "overall_cap": 42,
     "note": "an incremental model without a guard doubles in size every run"},
    {"finding": "P02", "axis": "perf", "axis_cap": 45, "overall_cap": 62,
     "note": "an incremental model without a unique_key cannot merge reliably"},
    {"finding": "P04", "axis": "perf", "axis_cap": 60, "overall_cap": 72,
     "note": "select * freezes the source shape into the model"},
    {"finding": "P08", "axis": "perf", "axis_cap": 60, "overall_cap": 70,
     "note": "a LIMIT in a model silently truncates the data"},
    {"finding": "L02", "axis": "lineage", "axis_cap": 25, "overall_cap": 50,
     "note": "a hard-coded database.schema table only runs in one environment"},
    {"finding": "L01", "axis": "lineage", "axis_cap": 30, "overall_cap": 55,
     "note": "a hard-coded table name is invisible to the lineage graph"},
    {"finding": "L03", "axis": "lineage", "axis_cap": 35, "overall_cap": 65,
     "note": "a model with no ref or source has no build order"},
    {"finding": "T01", "axis": "testing", "axis_cap": 25, "overall_cap": 65,
     "note": "a model missing from schema.yml has no documentation and no tests"},
    {"finding": "T03", "axis": "testing", "axis_cap": 45, "overall_cap": 78,
     "note": "a model with no tests has nothing protecting its shape"},
    {"finding": "T04", "axis": "testing", "axis_cap": 50, "overall_cap": 78,
     "note": "the grain column has no unique test"},
    {"finding": "C06", "axis": "conventions", "axis_cap": 10, "overall_cap": 30,
     "note": "a credential in a model file is exposed to everyone with repo access"},
]

VERDICT_BANDS = [
    (90, "production_ready", "Ready to run. The grain is declared, the lineage is explicit and the model is tested."),
    (78, "mostly_ready", "Sound with a few gaps. Fix the high findings before this model becomes someone's source of truth."),
    (65, "needs_attention", "Usable, but something important is missing. Read the high findings before you trust the numbers."),
    (45, "tight_but_usable", "Fragile. The grain or the lineage is not solid enough to build on yet."),
    (0, "needs_rework", "This model should not be trusted in its current shape. Start with the critical and high findings."),
]


def verdict_for(score):
    for threshold, name, description in VERDICT_BANDS:
        if score >= threshold:
            return name, description
    return "needs_rework", VERDICT_BANDS[-1][2]


def _strengths(brief, findings):
    ids = {f["id"] for f in findings}
    out = []
    if brief["grain"]["declared"]:
        out.append("Row grain is declared (%s: %s)." % (
            brief["grain"]["method"], brief["grain"]["expression"]))
    if not brief["fanout"]:
        out.append("No join fanout detected.")
    if brief["materialization"] == "incremental" and brief["has_incremental_guard"]:
        out.append("The incremental guard is present.")
    if brief["unique_key"]:
        out.append("unique_key is set, so incremental merges know which row to replace.")
    if brief["refs"] or brief["sources"]:
        out.append("%d ref() and %d source() calls declare the lineage." % (
            len(brief["refs"]), len(brief["sources"])))
    if not brief["shape"]["has_select_star"]:
        out.append("The column list is explicit rather than select *.")
    if not brief["shape"]["limit"]:
        out.append("No LIMIT, so the model returns the full result.")
    if brief["yaml_present"] and brief["yaml_entry"] is not None:
        desc = (brief["yaml_entry"].get("description") or "").strip()
        if desc:
            out.append("The model is described in schema.yml.")
        total = len(brief["model_tests"]) + sum(len(v) for v in brief["column_tests"].values())
        if total:
            out.append("%d data test(s) are declared." % total)
    if brief["tags"]:
        out.append("Tags are set, so the model can be targeted by a selector.")
    return out


def score(brief, findings, profile="general"):
    """Score a model on five axes and build the prioritised fix plan."""
    if profile not in PROFILES:
        profile = "general"
    weights = PROFILES[profile]["weights"]
    assessed = {axis: True for axis in AXIS_ORDER}
    notes = []
    if not brief["yaml_present"]:
        assessed["testing"] = False
        notes.append(
            "Testing and documentation were not assessed: no schema.yml was "
            "provided. The other four axes are weighted up to fill the gap."
        )

    axis_scores = {axis: 100 for axis in AXIS_ORDER}
    counts = {axis: {"critical": 0, "high": 0, "medium": 0, "low": 0} for axis in AXIS_ORDER}
    for finding in findings:
        axis = finding["axis"]
        if not assessed.get(axis, True):
            continue
        axis_scores[axis] -= SEVERITY_PENALTY.get(finding["severity"], 0)
        counts[axis][finding["severity"]] = counts[axis].get(finding["severity"], 0) + 1
    for axis in AXIS_ORDER:
        axis_scores[axis] = max(0, min(100, axis_scores[axis]))

    cap_source = {}
    cap_notes = []
    overall_caps = []
    for cap in CAPS:
        if not any(f["id"] == cap["finding"] for f in findings):
            continue
        axis = cap["axis"]
        if axis in assessed and axis_scores[axis] > cap["axis_cap"]:
            axis_scores[axis] = cap["axis_cap"]
            cap_source[axis] = (cap["axis_cap"], cap["finding"])
            cap_notes.append("%s: %s" % (AXIS_LABELS[axis], cap["note"]))
        overall_caps.append(cap["overall_cap"])

    total_weight = sum(weights[a] for a in AXIS_ORDER if assessed[a]) or 1
    overall = 0.0
    for axis in AXIS_ORDER:
        if not assessed[axis]:
            continue
        overall += axis_scores[axis] * weights[axis] / total_weight
    overall = round(overall, 1)
    if overall_caps:
        capped = min(overall_caps)
        if overall > capped:
            notes.append(
                "The overall score is capped at %d by a structural finding: %s"
                % (capped, cap_notes[0] if cap_notes else "see the findings list")
            )
            overall = float(capped)

    verdict, verdict_text = verdict_for(overall)

    fix_plan = []
    for finding in findings:
        axis = finding["axis"]
        if axis not in assessed:
            gain = 0.0
        elif axis in cap_source:
            gain = 0.0
        else:
            penalty = SEVERITY_PENALTY.get(finding["severity"], 0)
            gain = round(penalty * weights[axis] / total_weight, 1)
        blocked = None
        if axis in cap_source:
            blocked = "Blocked by the structural cap on %s (fix %s first)." % (
                AXIS_LABELS[axis], cap_source[axis][1])
        fix_plan.append({
            "id": finding["id"],
            "severity": finding["severity"],
            "axis": axis,
            "axis_label": AXIS_LABELS[axis],
            "title": finding["title"],
            "fix": finding["fix"],
            "estimated_gain": gain,
            "blocked": blocked,
        })
    fix_plan.sort(key=lambda s: (
        {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(s["severity"], 9),
        -s["estimated_gain"],
    ))
    for i, step in enumerate(fix_plan, 1):
        step["step"] = i

    return {
        "profile": profile,
        "profile_label": PROFILES[profile]["label"],
        "axes": [
            {
                "key": axis,
                "label": AXIS_LABELS[axis],
                "score": axis_scores[axis],
                "weight": weights[axis],
                "assessed": assessed[axis],
                "counts": counts[axis],
                "capped": axis in cap_source,
                "cap_note": cap_notes[[AXIS_LABELS[a] for a in cap_source].index(AXIS_LABELS[axis])] if axis in cap_source else "",
            }
            for axis in AXIS_ORDER
        ],
        "overall": overall,
        "verdict": verdict,
        "verdict_text": verdict_text,
        "assessed_axes": [a for a in AXIS_ORDER if assessed[a]],
        "strengths": _strengths(brief, findings),
        "notes": notes,
        "fix_plan": fix_plan,
    }
