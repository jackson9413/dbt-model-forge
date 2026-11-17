"""Tests for dbt-model-forge.

Run with:  python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine import analyzer, parser, report, yamlite  # noqa: E402

SAMPLES_DIR = os.path.join(ROOT, "samples")


def sample_ids():
    with open(os.path.join(SAMPLES_DIR, "index.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_sample(sample_id):
    folder = os.path.join(SAMPLES_DIR, sample_id)
    with open(os.path.join(folder, "model.sql"), "r", encoding="utf-8") as fh:
        sql = fh.read()
    yml_path = os.path.join(folder, "schema.yml")
    yml = ""
    if os.path.exists(yml_path):
        with open(yml_path, "r", encoding="utf-8") as fh:
            yml = fh.read()
    return sql, yml


def finding_ids(result):
    return set(f["id"] for f in result["findings"])


class TestYamlite(unittest.TestCase):
    def test_block_mapping(self):
        doc = yamlite.parse("a: 1\nb: two\nc: true\n")
        self.assertEqual(doc, {"a": 1, "b": "two", "c": True})

    def test_block_sequence_of_scalars(self):
        doc = yamlite.parse("tests:\n  - unique\n  - not_null\n")
        self.assertEqual(doc, {"tests": ["unique", "not_null"]})

    def test_sequence_of_maps(self):
        doc = yamlite.parse(
            "models:\n"
            "  - name: fct_orders\n"
            "    description: one row per order\n"
            "    columns:\n"
            "      - name: order_id\n"
            "        data_tests:\n"
            "          - unique\n"
            "          - not_null\n"
        )
        self.assertEqual(len(doc["models"]), 1)
        model = doc["models"][0]
        self.assertEqual(model["name"], "fct_orders")
        self.assertEqual(model["description"], "one row per order")
        self.assertEqual(model["columns"][0]["name"], "order_id")
        self.assertEqual(model["columns"][0]["data_tests"], ["unique", "not_null"])

    def test_flow_collections(self):
        doc = yamlite.parse(
            "freshness:\n"
            "  warn_after: {count: 12, period: hour}\n"
            "  values: ['a', 'b']\n"
        )
        self.assertEqual(doc["freshness"]["warn_after"]["count"], 12)
        self.assertEqual(doc["freshness"]["values"], ["a", "b"])

    def test_comments_ignored(self):
        doc = yamlite.parse("a: 1  # trailing comment\n# whole line\nb: 2\n")
        self.assertEqual(doc, {"a": 1, "b": 2})

    def test_empty_document(self):
        self.assertIsNone(yamlite.parse(""))
        self.assertIsNone(yamlite.parse("   \n"))


class TestParser(unittest.TestCase):
    def test_config_kwargs(self):
        sql = "{{ config(materialized='incremental', unique_key='id', tags=['a','b']) }}\nselect 1"
        brief = parser.parse_model(sql)
        self.assertEqual(brief["materialization"], "incremental")
        self.assertEqual(brief["unique_key"], "id")
        self.assertEqual(brief["tags"], ["a", "b"])

    def test_refs_and_sources(self):
        sql = "select * from {{ ref('stg_a') }} join {{ source('raw','b') }} on 1=1"
        brief = parser.parse_model(sql)
        self.assertEqual(brief["refs"][0]["name"], "stg_a")
        self.assertEqual(brief["sources"][0]["name"], "b")
        self.assertEqual(brief["sources"][0]["source"], "raw")

    def test_select_list_and_aliases(self):
        sql = "select a.id, sum(b.amount) as total from x a join y b on a.id = b.id"
        brief = parser.parse_model(sql)
        names = [c["alias"] or c["expr"] for c in brief["columns"]]
        self.assertIn("id", names)
        self.assertIn("total", names)

    def test_join_on_condition(self):
        sql = "select 1 from x a left join y b on a.id = b.id where b.name is not null"
        brief = parser.parse_model(sql)
        self.assertEqual(len(brief["joins"]), 1)
        self.assertEqual(brief["joins"][0]["type"], "left")
        self.assertIn("a.id = b.id", brief["joins"][0]["on"])

    def test_cte_relation_is_not_a_hardcoded_table(self):
        sql = "with cte_a as (select 1 as x) select x from cte_a"
        brief = parser.parse_model(sql)
        self.assertEqual(brief["from_relation"]["kind"], "cte")
        self.assertFalse(brief["from_relation"]["literal"])

    def test_grain_from_group_by(self):
        sql = "select customer_id, sum(x) as s from t group by customer_id"
        brief = parser.parse_model(sql)
        self.assertTrue(brief["grain"]["declared"])
        self.assertEqual(brief["grain"]["method"], "group by")
        self.assertIn("customer_id", brief["grain"]["expression"])

    def test_grain_from_unique_key(self):
        sql = "{{ config(materialized='incremental', unique_key='order_id') }} select order_id from t"
        brief = parser.parse_model(sql)
        self.assertTrue(brief["grain"]["declared"])
        self.assertEqual(brief["grain"]["method"], "unique_key")

    def test_aggregate_without_group_by(self):
        sql = "select count(*) as n from t"
        brief = parser.parse_model(sql)
        self.assertFalse(brief["grain"]["declared"])
        self.assertTrue(brief["has_aggregates"])

    def test_incremental_guard_and_filter(self):
        sql = (
            "{{ config(materialized='incremental', unique_key='id') }}\n"
            "select id from t\n"
            "{% if is_incremental() %}\n"
            "  where updated_at > (select max(updated_at) from {{ this }})\n"
            "{% endif %}\n"
        )
        brief = parser.parse_model(sql)
        self.assertTrue(brief["has_incremental_guard"])
        self.assertIn("updated_at", brief["incremental_filter"])

    def test_left_join_filter_detected(self):
        sql = (
            "select 1 from {{ ref('stg_a') }} a "
            "left join {{ ref('stg_b') }} b on a.id = b.id "
            "where b.name is not null"
        )
        brief = parser.parse_model(sql)
        self.assertEqual(len(brief["left_join_filters"]), 1)
        self.assertIn("name", brief["left_join_filters"][0]["columns"])

    def test_ambiguous_column(self):
        sql = (
            "select id, other from {{ ref('stg_a') }} a "
            "join {{ ref('stg_b') }} b on a.id = b.id "
            "where a.id = 1 and b.id = 2"
        )
        brief = parser.parse_model(sql)
        self.assertTrue(any(item["column"] == "id" for item in brief["ambiguous"]))

    def test_order_by_not_confused_with_alias(self):
        sql = "select sum(x) as order from t group by order"
        brief = parser.parse_model(sql)
        self.assertEqual(brief["shape"]["order_by"], "")


class TestRules(unittest.TestCase):
    def analyze(self, sql, yml="", name=None, profile="general"):
        return analyzer.analyze(sql, yml, name, profile)

    def test_incremental_without_guard_is_critical(self):
        sql = "{{ config(materialized='incremental') }} select id from {{ source('raw','e') }}"
        result = self.analyze(sql)
        ids = finding_ids(result)
        self.assertIn("P01", ids)
        finding = [f for f in result["findings"] if f["id"] == "P01"][0]
        self.assertEqual(finding["severity"], "critical")

    def test_hardcoded_table_is_found(self):
        sql = "select 1 from analytics.raw.orders"
        result = self.analyze(sql)
        self.assertIn("L02", finding_ids(result))

    def test_select_star_and_limit(self):
        sql = "select o.* from analytics.raw.orders o limit 10"
        result = self.analyze(sql)
        ids = finding_ids(result)
        self.assertIn("P04", ids)
        self.assertIn("P08", ids)

    def test_count_star_over_join(self):
        sql = (
            "select count(*) as n from {{ ref('stg_a') }} a "
            "join {{ ref('fct_orders') }} o on a.id = o.customer_id"
        )
        result = self.analyze(sql)
        self.assertIn("G02", finding_ids(result))

    def test_no_tests_flagged_when_schema_present(self):
        sql = "select id from {{ ref('stg_a') }}"
        yml = "models:\n  - name: my_model\n    description: hi\n"
        result = self.analyze(sql, yml, "my_model")
        self.assertIn("T03", finding_ids(result))

    def test_secret_detected(self):
        sql = "select 1 from t where api_key = 'abcdef123456'"
        result = self.analyze(sql)
        self.assertIn("C06", finding_ids(result))

    def test_every_finding_has_evidence_and_fix(self):
        for item in sample_ids():
            sql, yml = load_sample(item["id"])
            result = analyzer.analyze(sql, yml, item["model_name"], "general")
            for finding in result["findings"]:
                self.assertTrue(finding["evidence"].strip(), finding["id"])
                self.assertTrue(finding["fix"].strip(), finding["id"])
                self.assertTrue(finding["why"].strip(), finding["id"])
                self.assertIn(
                    finding["severity"], ("critical", "high", "medium", "low")
                )


class TestScoring(unittest.TestCase):
    def test_structural_cap_on_incremental(self):
        sql = "{{ config(materialized='incremental') }} select id from {{ source('raw','e') }}"
        result = analyzer.analyze(sql, "", None, "general")
        perf = [a for a in result["score"]["axes"] if a["key"] == "perf"][0]
        self.assertLessEqual(perf["score"], 35)

    def test_structural_cap_on_lineage(self):
        sql = "select 1 from analytics.raw.orders"
        result = analyzer.analyze(sql, "", None, "general")
        lineage = [a for a in result["score"]["axes"] if a["key"] == "lineage"][0]
        self.assertLessEqual(lineage["score"], 25)

    def test_testing_axis_off_without_schema(self):
        sql = "select id from {{ ref('stg_a') }}"
        result = analyzer.analyze(sql, "", None, "general")
        testing = [a for a in result["score"]["axes"] if a["key"] == "testing"][0]
        self.assertFalse(testing["assessed"])
        self.assertTrue(any("not assessed" in n for n in result["notes"]))

    def test_profile_changes_the_weighting(self):
        sql = "select id from {{ ref('stg_a') }}"
        general = analyzer.analyze(sql, "", None, "general")
        audit = analyzer.analyze(sql, "", None, "grain_audit")
        self.assertNotEqual(general["score"]["overall"], audit["score"]["overall"])

    def test_verdict_bands(self):
        sql = "{{ config(materialized='incremental') }} select id from {{ source('raw','e') }}"
        self.assertEqual(analyzer.analyze(sql)["score"]["verdict"], "needs_rework")


class TestSampleCalibration(unittest.TestCase):
    EXPECTED = {
        "clean_orders": ("production_ready", 95.0),
        "fanout_dashboard": ("tight_but_usable", 40.0),
        "incremental_no_guard": ("needs_rework", 40.0),
        "hardcoded_legacy": ("tight_but_usable", 40.0),
        "left_join_filter": ("tight_but_usable", 40.0),
        "untested_view": ("tight_but_usable", 40.0),
    }

    def test_samples_score_as_documented(self):
        for item in sample_ids():
            sql, yml = load_sample(item["id"])
            result = analyzer.analyze(sql, yml, item["model_name"], "general")
            verdict, floor = self.EXPECTED[item["id"]]
            self.assertEqual(result["score"]["verdict"], verdict, item["id"])
            self.assertGreaterEqual(result["score"]["overall"], floor, item["id"])

    def test_clean_model_has_no_findings(self):
        sql, yml = load_sample("clean_orders")
        result = analyzer.analyze(sql, yml, "fct_orders", "general")
        self.assertEqual(result["findings"], [])

    def test_fanout_sample_catches_the_fanout(self):
        sql, yml = load_sample("fanout_dashboard")
        result = analyzer.analyze(sql, yml, "fct_customer_orders", "general")
        ids = finding_ids(result)
        self.assertIn("G02", ids)
        self.assertIn("G03", ids)

    def test_left_join_sample_catches_the_filter(self):
        sql, yml = load_sample("left_join_filter")
        result = analyzer.analyze(sql, yml, "fct_orders_enriched", "general")
        self.assertIn("G06", finding_ids(result))


class TestReport(unittest.TestCase):
    def test_markdown_contains_the_sections(self):
        sql, yml = load_sample("fanout_dashboard")
        result = analyzer.analyze(sql, yml, "fct_customer_orders", "general")
        md = report.to_markdown(result)
        for marker in ("# dbt model review", "## Scorecard", "## Findings",
                       "## Recommended shape", "## Fix plan"):
            self.assertIn(marker, md)

    def test_json_export_round_trips(self):
        sql, yml = load_sample("clean_orders")
        result = analyzer.analyze(sql, yml, "fct_orders", "general")
        payload = json.loads(report.to_json(result))
        self.assertEqual(payload["model"], "fct_orders")


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import app
        cls.app = app
        cls.client = app.test_client()

    def test_index_served(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)

    def test_samples_listed(self):
        res = self.client.get("/api/samples")
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(len(res.get_json()), 6)

    def test_sample_loaded(self):
        res = self.client.get("/api/samples/clean_orders")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("select", data["sql"].lower())
        self.assertIn("models:", data["yaml"])

    def test_analyze_endpoint(self):
        res = self.client.post("/api/analyze", json={
            "sql": "select id from {{ ref('stg_a') }}",
            "yaml": "",
            "model_name": "my_model",
            "profile": "general",
            "save": True,
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["model"], "my_model")
        self.assertIn("markdown", data)
        self.assertTrue(data["run_id"])

    def test_analyze_requires_sql(self):
        res = self.client.post("/api/analyze", json={"sql": "  "})
        self.assertEqual(res.status_code, 400)

    def test_run_rating_and_export(self):
        res = self.client.post("/api/analyze", json={
            "sql": "select id from {{ ref('stg_a') }}",
            "yaml": "",
            "model_name": "rated_model",
            "profile": "general",
            "save": True,
        })
        run_id = res.get_json()["run_id"]
        res = self.client.patch("/api/runs/%d" % run_id, json={"rating": 4, "notes": "fixed the grain"})
        self.assertEqual(res.status_code, 200)
        md = self.client.get("/api/export/%d?fmt=md" % run_id)
        self.assertEqual(md.status_code, 200)
        self.assertIn("dbt model review", md.get_data(as_text=True))
        js = self.client.get("/api/export/%d?fmt=json" % run_id)
        self.assertEqual(js.status_code, 200)
        self.assertIn("score", js.get_json())

    def test_runs_listed(self):
        res = self.client.get("/api/runs")
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.get_json(), list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
