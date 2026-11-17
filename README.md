# dbt-model-forge

**A local-first review workbench for dbt models.** Paste a model, get the row grain it actually produces, the ways it can quietly produce wrong numbers, and the concrete fix for each one - then export the review as Markdown for a pull request.

No API keys. No network calls. No LLM. The model SQL is parsed and analysed in-process; nothing leaves the machine.

---

## The problem it solves

Most dbt bugs are not syntax bugs. They are semantic ones, and they all look fine in review:

- a `count(*)` after a join to a detail table counts joined rows, so a customer with three orders is counted three times
- a `LEFT JOIN` with a predicate on the right-hand relation in `WHERE` is silently an `INNER JOIN`
- an `incremental` model with no `is_incremental()` guard rebuilds and appends the whole table on every run
- a hard-coded `analytics.raw.orders` is invisible to dbt: no lineage, no stubbing, no portability
- a `LIMIT` in a model silently truncates the data for everyone downstream
- an aggregate with no `group by` leaves the grain undefined, and an undefined grain is how duplicated measurements happen

dbt itself will not catch any of these. A reviewer will catch them if they are lucky, or a dashboard will, three weeks later, when a total looks wrong and nobody knows why.

**dbt-model-forge reads the model the way an experienced analytics engineer would**, and tells you what it found, why it matters, and what to change.

---

## What you get

**A five-axis scorecard** (0-100, weighted by the review profile you pick):

| Axis | What it measures |
| --- | --- |
| Grain & correctness | What one row actually means, and whether the numbers can be inflated by a join |
| Incremental & performance | Materialisation choice, incremental safety, scan cost |
| Lineage & portability | ref()/source() usage, environment portability, var defaults |
| Testing & documentation | schema.yml entry, grain tests, relationships tests, freshness |
| Conventions & hygiene | Naming, reserved words, magic numbers, secrets, formatting |

**Structural caps.** A model cannot score well by skipping the hard part. An incremental model with no guard caps performance at 35 and the overall score at 42. A hard-coded `database.schema` table caps lineage at 25. `count(*)` over a joined model caps grain at 40. Sixteen caps in total, each with a plain-English reason.

**60+ rules**, every one carrying the evidence that triggered it, why it matters to someone who has to trust the numbers, and a concrete fix.

**The grain, explained.** Whether the grain is declared (via `group by` or `unique_key`) or merely implied, plus the fanout risk from each join.

**A recommended shape**: the materialisation to use and why, a ready-to-paste `config()` block, the tests to add, a `schema.yml` skeleton and a shape checklist.

**A prioritised fix plan**, with the estimated score change per step and an explicit note when a step is blocked by a structural cap (fix the cap first, then the rest).

**Exports** to Markdown and JSON, a local review history with star ratings and notes, and a REST API.

---

## Quick start

`bash
cd dbt-model-forge
python3 -m pip install -r requirements.txt
python3 app.py
`

Open http://127.0.0.1:5057, paste a model, press **Review the model**.

Or use the API:

`bash
cat > payload.json <<'JSON'
{
  "sql": "{{ config(materialized='incremental', unique_key='order_id') }} select order_id from {{ ref('stg_orders') }}",
  "yaml": "",
  "model_name": "fct_orders",
  "profile": "general",
  "save": true
}
JSON

curl -s -X POST http://127.0.0.1:5057/api/analyze \
  -H 'Content-Type: application/json' \
  -d @payload.json
`

Run the tests:

`bash
python3 -m unittest discover -s tests -v
`

---

## The six built-in samples

Each sample is a real model file under `samples/`, loaded from the sample picker in the UI. The scores below are the actual output of the engine.

| Sample | Verdict | Score | Findings | What it demonstrates |
| --- | --- | --- | --- | --- |
| `clean_orders` | production_ready | 100.0 | 0 | The control case: incremental with a guard, a `unique_key`, an explicit column list, a documented grain and real tests |
| `fanout_dashboard` | tight_but_usable | 55.0 | 8 | Joins orders *and* order items, then counts and sums - the classic double-counting bug, plus a grain column with no unique test |
| `incremental_no_guard` | needs_rework | 42.0 | 5 | Declared incremental with no `is_incremental()` guard and no `unique_key`, so every run appends the whole table again |
| `hardcoded_legacy` | tight_but_usable | 50.0 | 8 | Literal `database.schema` tables, `select *`, a hard-coded date and a `LIMIT` that silently truncates the data |
| `left_join_filter` | tight_but_usable | 60.0 | 5 | A `LEFT JOIN` whose right-hand relation is filtered in `WHERE`, quietly dropping unmatched orders |
| `untested_view` | tight_but_usable | 50.0 | 12 | An upper-case view over literal tables, no schema.yml at all, a reserved word as a column name and a magic number in `HAVING` |

Per-sample axis scores for the control case: grain 100, performance 100, lineage 100, testing 100, conventions 100.

---

## Review profiles

The same rules, weighted differently:

| Profile | Weights (grain/performance/lineage/testing/conventions) | Use it for |
| --- | --- | --- |
| General review | 25/20/20/20/15 | The default read of a model |
| Grain audit | 40/15/15/15/15 | When the row grain is the thing you are not sure about |
| Incremental & performance | 20/35/20/15/10 | When the model is incremental or expensive |
| New model onboarding | 20/15/20/30/15 | Reviewing a model somebody just wrote |
| Legacy refactor | 25/25/25/15/10 | Taking an old model apart and putting it back together |

---

## What the engine understands

- **Jinja**: `config()` with any keyword arguments, `ref()`, `source()`, `var()`, `this`, custom macros, `dbt_utils` calls, and `{% if is_incremental() %}` blocks (the guard and the row filter inside it are read separately)
- **SQL**: a depth-aware clause scanner that handles CTEs, subqueries, unions, aliases, LEFT/RIGHT/FULL/INNER/CROSS joins, `USING`, `HAVING`, `QUALIFY`, `LIMIT` and window clauses
- **schema.yml**: a purpose-built YAML subset parser (block mappings, block sequences, flow collections, quoted scalars) that reads model descriptions, column descriptions, `data_tests`/`tests`, `accepted_values`, `relationships`, source freshness and sources
- **Grain detection**: from `group by`, from `unique_key`, from an aggregate with no `group by`, or undeclared
- **Fanout heuristics**: detail/fact tables are identified by name shape (`orders`, `order_items`, `events`, `payments`, ...), dimension and staging tables are excluded, and `count(*)`/`count(1)`/`sum()` over a joined result is flagged

Everything is heuristic and says so: each finding carries a confidence level, and the review is a starting point for a human, not a replacement for one.

---

## Tech stack

- **Backend**: Python 3 + Flask 3, SQLite for the review history
- **Engine**: pure Python standard library - no SQLAlchemy, no linters, no AST packages. The YAML parser, the Jinja extractor and the SQL clause scanner are all in `engine/`
- **Frontend**: vanilla HTML/CSS/JS, no build step, no framework. The UI builds DOM nodes directly, so pasted model text is never interpreted as markup
- **Dependencies**: `Flask>=3.0` and nothing else

### Layout

`
dbt-model-forge/
├── app.py                 Flask app and REST API
├── engine/
│   ├── yamlite.py         minimal YAML subset parser
│   ├── parser.py          Jinja extraction + SQL clause scanner + grain detection
│   ├── rules.py           60+ review rules across five axes
│   ├── scoring.py         axis scores, profile weights, structural caps, fix plan
│   ├── blueprint.py       recommended materialisation, config, tests, schema.yml
│   ├── report.py          Markdown and JSON export
│   └── analyzer.py        ties it together
├── samples/               six real sample models (model.sql + schema.yml)
├── static/                dark vanilla-JS UI
├── tests/test_engine.py   43 tests
└── data/                  local SQLite review history (gitignored)
`

### API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/profiles` | The five review profiles |
| GET | `/api/samples` | Sample index |
| GET | `/api/samples/<id>` | One sample (SQL + schema.yml) |
| POST | `/api/analyze` | Review a model. Body: `sql`, `yaml`, `model_name`, `profile`, `save` |
| GET | `/api/runs` | Review history |
| PATCH | `/api/runs/<id>` | Set a star rating or notes |
| GET | `/api/export/<id>?fmt=md|json` | Export a saved review |

---

## Privacy

The review runs entirely on your machine. The model SQL is parsed in-process and stored only if you tick **save to history**, in a local SQLite file under `data/`. There is no telemetry, no outbound request and no third-party service in this project - not even a badge image in this README.

---

## Why local-first

Model SQL is the most sensitive code most data teams have. It contains table names, business logic, metric definitions and occasionally a credential that somebody left behind. Sending it to a hosted reviewer to get a lint report is a bad trade. The rules here are deterministic, so there is nothing a hosted service would do better - and a local tool is one that you can actually extend with your own rules.

## License

MIT.
