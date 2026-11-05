import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "llm-cost-audit"
sys.path.insert(0, str(SKILL / "scripts"))

import usage_report  # noqa: E402

FILES = SKILL / "evals" / "files"
EXAMPLE_PRICES = str(SKILL / "assets" / "prices.example.json")


def run(argv):
    """Run the CLI. Return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = usage_report.main([str(arg) for arg in argv])
        except SystemExit as exc:  # argparse usage errors
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def run_json(argv):
    code, out, err = run(list(argv) + ["--json"])
    return code, json.loads(out)


def finding_ids(report):
    return [(f["severity"], f["id"], f["route"]) for f in report["findings"]]


class NormaliseTests(unittest.TestCase):
    def counts(self, record, **kwargs):
        result = usage_report.normalise(record, **kwargs)
        return (result["shape"], result["input"], result["cache_read"],
                result["cache_write"], result["output"])

    def test_neutral_flat_record(self):
        record = {"route": "r", "model": "m", "input_tokens": 10, "cache_read_tokens": 20,
                  "cache_write_tokens": 30, "output_tokens": 40}
        self.assertEqual(self.counts(record), ("neutral", 10, 20, 30, 40))

    def test_anthropic_input_tokens_already_exclude_cached_tokens(self):
        record = {"usage": {"input_tokens": 100, "cache_creation_input_tokens": 2000,
                            "cache_read_input_tokens": 3000, "output_tokens": 50}}
        self.assertEqual(self.counts(record), ("anthropic", 100, 3000, 2000, 50))

    def test_anthropic_one_hour_cache_writes_are_recorded(self):
        record = {"usage": {"input_tokens": 1, "cache_creation_input_tokens": 900,
                            "cache_read_input_tokens": 0, "output_tokens": 1,
                            "cache_creation": {"ephemeral_5m_input_tokens": 400,
                                               "ephemeral_1h_input_tokens": 500}}}
        self.assertEqual(usage_report.normalise(record)["cache_write_1h"], 500)

    def test_openai_chat_prompt_tokens_include_cached_tokens(self):
        record = {"usage": {"prompt_tokens": 1000, "completion_tokens": 70, "total_tokens": 1070,
                            "prompt_tokens_details": {"cached_tokens": 600}}}
        self.assertEqual(self.counts(record), ("openai-chat", 400, 600, 0, 70))

    def test_openai_chat_without_details_has_no_cache_reads(self):
        record = {"usage": {"prompt_tokens": 1000, "completion_tokens": 70,
                            "prompt_tokens_details": None}}
        self.assertEqual(self.counts(record), ("openai-chat", 1000, 0, 0, 70))

    def test_openai_responses_input_tokens_include_cached_tokens(self):
        record = {"usage": {"input_tokens": 900, "output_tokens": 30,
                            "input_tokens_details": {"cached_tokens": 512}}}
        self.assertEqual(self.counts(record), ("openai-responses", 388, 512, 0, 30))

    def test_gemini_usage_metadata(self):
        record = {"modelVersion": "example-large",
                  "usageMetadata": {"promptTokenCount": 5000, "cachedContentTokenCount": 4096,
                                    "candidatesTokenCount": 100, "thoughtsTokenCount": 250,
                                    "totalTokenCount": 5350}}
        self.assertEqual(self.counts(record), ("gemini", 904, 4096, 0, 350))
        self.assertEqual(usage_report.normalise(record)["model"], "example-large")

    def test_prompt_tokens_plus_cache_fields_always_sum_to_the_prompt(self):
        record = {"usage": {"prompt_tokens": 1234, "completion_tokens": 1,
                            "prompt_tokens_details": {"cached_tokens": 1024}}}
        result = usage_report.normalise(record)
        self.assertEqual(result["input"] + result["cache_read"] + result["cache_write"], 1234)

    def test_cached_tokens_above_prompt_tokens_is_rejected(self):
        record = {"usage": {"prompt_tokens": 10, "completion_tokens": 1,
                            "prompt_tokens_details": {"cached_tokens": 11}}}
        with self.assertRaises(ValueError):
            usage_report.normalise(record)

    def test_negative_fractional_and_non_numeric_counts_are_rejected(self):
        for bad in (-1, 1.5, "12", True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                usage_report.normalise({"input_tokens": bad, "output_tokens": 1})

    def test_record_without_token_fields_is_rejected(self):
        with self.assertRaises(ValueError):
            usage_report.normalise({"route": "r", "message": "started"})
        with self.assertRaises(ValueError):
            usage_report.normalise(["not", "an", "object"])

    def test_route_field_can_be_a_dotted_path_and_defaults_are_named(self):
        record = {"metadata": {"feature": "search"}, "input_tokens": 1, "output_tokens": 1}
        result = usage_report.normalise(record, route_field="metadata.feature")
        self.assertEqual(result["route"], "search")
        fallback = usage_report.normalise(record)
        self.assertEqual((fallback["route"], fallback["model"]), ("(no route)", "(unknown)"))


class CostTests(unittest.TestCase):
    PRICE = {"input": 2.0, "cache_write": 3.0, "cache_read": 0.5, "output": 10.0}

    def test_cost_is_tokens_times_price_per_million(self):
        totals = {"calls": 1, "input": 1000000, "cache_write": 2000000,
                  "cache_read": 4000000, "output": 500000}
        self.assertAlmostEqual(usage_report.cost_of(totals, self.PRICE, "m"), 2 + 6 + 2 + 5)

    def test_missing_cache_price_is_an_error_only_when_such_tokens_exist(self):
        price = {"input": 2.0, "output": 10.0}
        plain = {"calls": 1, "input": 1000000, "cache_write": 0, "cache_read": 0, "output": 0}
        self.assertAlmostEqual(usage_report.cost_of(plain, price, "m"), 2.0)
        cached = dict(plain, cache_read=5)
        with self.assertRaises(usage_report.InputError):
            usage_report.cost_of(cached, price, "m")

    def test_cache_read_share_is_cache_reads_over_all_prompt_tokens(self):
        totals = {"calls": 2, "input": 100, "cache_write": 300, "cache_read": 600, "output": 999}
        self.assertEqual(usage_report.cache_read_share(totals), 0.6)
        empty = {"calls": 1, "input": 0, "cache_write": 0, "cache_read": 0, "output": 5}
        self.assertIsNone(usage_report.cache_read_share(empty))


class TempDirCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name, rows):
        path = self.dir / name
        lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def prices(self, **table):
        base = {"unit": "per_million_tokens", "currency": "TEST",
                "models": {"m": {"input": 1.0, "cache_write": 2.0, "cache_read": 0.1, "output": 4.0}}}
        base.update(table)
        return self.write("prices.json", [base])


class PriceTableTests(TempDirCase):
    def log(self):
        return self.write("usage.jsonl", [
            {"route": "a", "model": "m", "input_tokens": 1000000, "output_tokens": 500000}])

    def test_no_price_table_means_no_cost_anywhere_in_the_output(self):
        code, out, _ = run([self.log()])
        self.assertEqual(code, 0)
        self.assertIn("cost: not computed", out)
        self.assertNotIn("cost (", out)
        _, report = run_json([self.log()])
        self.assertIsNone(report["prices"])
        self.assertNotIn("cost", report["routes"][0])

    def test_cost_uses_the_supplied_table(self):
        code, report = run_json([self.log(), "--prices", self.prices()])
        self.assertEqual(code, 0)
        self.assertAlmostEqual(report["routes"][0]["cost"], 1.0 + 2.0)
        self.assertEqual(report["prices"]["currency"], "TEST")
        self.assertFalse(report["prices"]["placeholder"])

    def test_placeholder_table_is_labelled_in_text_and_json(self):
        prices = self.prices(placeholder=True)
        _, out, _ = run([self.log(), "--prices", prices])
        self.assertIn("PLACEHOLDER PRICES", out)
        self.assertIn("cost (PLACEHOLDER)", out)
        _, report = run_json([self.log(), "--prices", prices])
        self.assertTrue(report["prices"]["placeholder"])

    def test_shipped_example_table_is_marked_as_placeholder(self):
        table = json.loads(Path(EXAMPLE_PRICES).read_text(encoding="utf-8"))
        self.assertIs(table["placeholder"], True)
        self.assertIn("PLACEHOLDER", table["_readme"])
        self.assertTrue(usage_report.load_prices(EXAMPLE_PRICES)["placeholder"])

    def test_model_missing_from_table_is_an_input_error_naming_the_model(self):
        prices = self.prices(models={"other": {"input": 1, "output": 1}})
        code, out, err = run([self.log(), "--prices", prices])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn('"m"', err)

    def test_model_names_are_matched_exactly_not_by_prefix(self):
        prices = self.prices(models={"m-mini": {"input": 1, "output": 1}})
        self.assertEqual(run([self.log(), "--prices", prices])[0], 2)

    def test_bad_price_tables_are_input_errors(self):
        bad_tables = [
            {"currency": "X", "models": {"m": {"input": 1, "output": 1}}},
            {"unit": "per_1k_tokens", "models": {"m": {"input": 1, "output": 1}}},
            {"unit": "per_million_tokens", "models": {}},
            {"unit": "per_million_tokens", "models": {"m": {"input": 1}}},
            {"unit": "per_million_tokens", "models": {"m": {"input": -1, "output": 1}}},
            {"unit": "per_million_tokens", "models": {"m": {"input": "1.0", "output": 1}}},
        ]
        for index, table in enumerate(bad_tables):
            path = self.write("bad-%d.json" % index, [table])
            code, _, err = run([self.log(), "--prices", path])
            self.assertEqual(code, 2, table)
            self.assertIn("error", err)
        code, _, _ = run([self.log(), "--prices", self.write("broken.json", ["{not json"])])
        self.assertEqual(code, 2)
        self.assertEqual(run([self.log(), "--prices", self.dir / "missing.json"])[0], 2)

    def test_one_hour_cache_writes_raise_a_note_when_priced(self):
        log = self.write("usage.jsonl", [{"route": "a", "model": "m", "usage": {
            "input_tokens": 5, "cache_creation_input_tokens": 700, "cache_read_input_tokens": 0,
            "output_tokens": 5, "cache_creation": {"ephemeral_1h_input_tokens": 700}}}])
        _, report = run_json([log, "--prices", self.prices()])
        self.assertTrue(any("1-hour" in note for note in report["notes"]))


class FindingRuleTests(TempDirCase):
    def calls(self, count, route="r", **usage):
        base = {"input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 1}
        base.update(usage)
        return [dict(base, route=route, model="m") for _ in range(count)]

    def ids(self, rows, *extra):
        _, report = run_json([self.write("usage.jsonl", rows)] + list(extra))
        return finding_ids(report)

    def test_cache_writes_with_no_reads_are_high(self):
        self.assertEqual(self.ids(self.calls(2, cache_write_tokens=3000)),
                         [("high", "cache-writes-never-read", "r")])

    def test_a_single_cache_write_is_not_flagged(self):
        self.assertEqual(self.ids(self.calls(1, cache_write_tokens=3000)), [])

    def test_cache_writes_that_are_read_back_are_not_flagged(self):
        rows = self.calls(1, cache_write_tokens=3000) + self.calls(4, cache_read_tokens=3000)
        self.assertEqual(self.ids(rows), [])

    def test_long_uncached_input_needs_enough_calls_and_enough_tokens(self):
        self.assertEqual(self.ids(self.calls(5, input_tokens=1000)),
                         [("medium", "uncached-long-input", "r")])
        self.assertEqual(self.ids(self.calls(4, input_tokens=1000)), [])
        self.assertEqual(self.ids(self.calls(5, input_tokens=999)), [])
        self.assertEqual(self.ids(self.calls(5, input_tokens=999), "--min-input-tokens", "500"),
                         [("medium", "uncached-long-input", "r")])
        self.assertEqual(self.ids(self.calls(4, input_tokens=1000), "--min-calls", "4"),
                         [("medium", "uncached-long-input", "r")])

    def test_duplicates_need_two_repeats_and_five_percent_of_calls(self):
        def rows(total, repeats):
            out = self.calls(total, input_tokens=10)
            for index, row in enumerate(out):
                row["request_hash"] = "same" if index <= repeats else "unique-%d" % index
            return out
        self.assertEqual(self.ids(rows(10, 2)), [("medium", "duplicate-requests", "r")])
        self.assertEqual(self.ids(rows(10, 1)), [])
        self.assertEqual(self.ids(rows(41, 2)), [])

    def test_duplicate_tokens_count_only_the_repeats(self):
        rows = self.calls(3, input_tokens=100, output_tokens=7)
        for row in rows:
            row["request_hash"] = "h"
        _, report = run_json([self.write("usage.jsonl", rows)])
        finding = report["findings"][0]
        self.assertEqual(finding["tokens_involved"], 2 * 107)
        self.assertEqual(finding["evidence"]["records"], 2)
        self.assertIn("lines 2, 3", finding["evidence"]["first_at"])

    def test_the_same_hash_on_two_routes_is_not_a_duplicate(self):
        rows = self.calls(3, route="a", input_tokens=10) + self.calls(3, route="b", input_tokens=10)
        for index, row in enumerate(rows):
            row["request_hash"] = "h%d" % (index % 3)
        self.assertEqual(self.ids(rows), [])

    def test_missing_request_hash_is_noted_not_flagged(self):
        _, report = run_json([self.write("usage.jsonl", self.calls(3, input_tokens=10))])
        self.assertEqual(report["findings"], [])
        self.assertTrue(any("request_hash" in note for note in report["notes"]))

    def test_output_heavy_is_info_and_does_not_fail_the_run(self):
        path = self.write("usage.jsonl", self.calls(5, input_tokens=100, output_tokens=100))
        code, report = run_json([path])
        self.assertEqual(finding_ids(report), [("info", "output-heavy", "r")])
        self.assertEqual(code, 0)
        self.assertEqual(run([path, "--fail-on", "info"])[0], 1)

    def test_spend_concentration_needs_three_routes(self):
        big = self.calls(2, route="big", input_tokens=500)
        small = self.calls(2, route="s1", input_tokens=100) + self.calls(2, route="s2", input_tokens=100)
        self.assertEqual(self.ids(big + small), [("info", "spend-concentration", "big")])
        self.assertEqual(self.ids(big + small[:2]), [])

    def test_findings_are_per_route_and_model(self):
        rows = self.calls(5, input_tokens=2000)
        for row in rows[:3]:
            row["model"] = "other"
        self.assertEqual(self.ids(rows), [])

    def test_fail_on_levels(self):
        path = self.write("usage.jsonl", self.calls(5, input_tokens=2000))
        self.assertEqual(run([path])[0], 1)
        self.assertEqual(run([path, "--fail-on", "high"])[0], 0)
        self.assertEqual(run([path, "--fail-on", "never"])[0], 0)


class FixtureTests(unittest.TestCase):
    def test_broken_cache_reports_writes_never_read_on_support_chat_only(self):
        code, report = run_json([FILES / "broken-cache" / "usage.jsonl"])
        self.assertEqual(code, 1)
        self.assertEqual(finding_ids(report), [("high", "cache-writes-never-read", "support-chat")])
        finding = report["findings"][0]
        self.assertEqual(finding["tokens_involved"], 125040)
        self.assertEqual(finding["evidence"]["records"], 24)
        self.assertIn("usage.jsonl lines 1, 3, 5", finding["evidence"]["first_at"])
        self.assertIsNone(finding["cost_involved"])
        self.assertEqual(report["shapes"], {"anthropic": 48})
        shares = dict((row["name"], row["cache_read_share"]) for row in report["routes"])
        self.assertEqual(shares, {"support-chat": 0.0, "faq-answer": 0.925})

    def test_mixed_waste_reports_every_planted_defect_in_rank_order(self):
        code, report = run_json([FILES / "mixed-waste" / "usage.jsonl"])
        self.assertEqual(code, 1)
        self.assertEqual(finding_ids(report), [
            ("medium", "uncached-long-input", "search-answer"),
            ("medium", "duplicate-requests", "classify-ticket"),
            ("info", "spend-concentration", "search-answer"),
            ("info", "output-heavy", "release-notes"),
        ])
        by_id = dict((f["id"], f) for f in report["findings"])
        self.assertEqual(by_id["uncached-long-input"]["tokens_involved"], 212055)
        self.assertIn("14 of 20 calls", by_id["duplicate-requests"]["summary"])
        self.assertEqual(by_id["output-heavy"]["tokens_involved"], 23716)
        self.assertEqual(report["shapes"], {"openai-chat": 70})
        routes = dict((row["name"], row) for row in report["routes"])
        self.assertEqual(routes["autocomplete"]["cache_read"], 22528)
        self.assertNotIn("autocomplete", [f["route"] for f in report["findings"]])

    def test_mixed_waste_with_prices_ranks_by_cost_and_labels_the_placeholder(self):
        code, report = run_json([FILES / "mixed-waste" / "usage.jsonl", "--prices", EXAMPLE_PRICES])
        self.assertEqual(code, 1)
        self.assertTrue(report["prices"]["placeholder"])
        self.assertEqual([f["id"] for f in report["findings"]], [
            "uncached-long-input", "duplicate-requests", "spend-concentration", "output-heavy"])
        costs = [f["cost_involved"] for f in report["findings"]]
        self.assertAlmostEqual(costs[0], 212055 * 1000.0 / 1e6)
        self.assertTrue(all(cost is not None for cost in costs))

    def test_healthy_log_has_no_findings_with_or_without_prices(self):
        for extra in ([], ["--prices", EXAMPLE_PRICES]):
            code, report = run_json([FILES / "healthy" / "usage.jsonl"] + extra)
            self.assertEqual(code, 0, extra)
            self.assertEqual(report["findings"], [], extra)
            self.assertEqual(report["shapes"], {"neutral": 76})
        code, out, _ = run([FILES / "healthy" / "usage.jsonl"])
        self.assertIn("Findings: none.", out)

    def test_output_is_deterministic(self):
        argv = [FILES / "mixed-waste" / "usage.jsonl", "--prices", EXAMPLE_PRICES]
        self.assertEqual(run(argv), run(argv))

    def test_several_files_are_summed(self):
        one = FILES / "healthy" / "usage.jsonl"
        _, single = run_json([one])
        _, double = run_json([one, one])
        self.assertEqual(double["records"], 2 * single["records"])


class InputErrorTests(TempDirCase):
    def test_missing_file_is_an_input_error(self):
        code, out, err = run([self.dir / "nope.jsonl"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("cannot read", err)

    def test_invalid_lines_stop_the_run_and_name_file_and_line(self):
        path = self.write("usage.jsonl", [
            {"route": "a", "model": "m", "input_tokens": 1, "output_tokens": 1},
            "{broken", {"route": "a", "note": "no tokens here"}])
        code, out, err = run([path])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("usage.jsonl:2: not valid JSON", err)
        self.assertIn("usage.jsonl:3: no recognised token fields", err)
        self.assertNotIn("Traceback", err)

    def test_skip_invalid_reports_what_was_left_out(self):
        path = self.write("usage.jsonl", [
            {"route": "a", "model": "m", "input_tokens": 1, "output_tokens": 1}, "{broken"])
        code, report = run_json([path, "--skip-invalid"])
        self.assertEqual(code, 0)
        self.assertEqual(report["records"], 1)
        self.assertIn("1 line(s) skipped", report["notes"][0])

    def test_empty_log_is_an_input_error(self):
        self.assertEqual(run([self.write("usage.jsonl", [""])])[0], 2)

    def test_usage_errors_exit_two(self):
        self.assertEqual(run([])[0], 2)
        self.assertEqual(run([self.write("u.jsonl", [""]), "--min-calls", "0"])[0], 2)

    def test_help_documents_examples_and_exit_codes(self):
        code, out, _ = run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("Examples:", out)
        self.assertIn("Exit codes:", out)


class ValidateRepoTests(unittest.TestCase):
    def test_repository_passes_its_own_validator(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import validate
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = validate.main(["validate.py", str(ROOT)])
        self.assertEqual(code, 0, buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
