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

import prefix_stability  # noqa: E402

FILES = SKILL / "evals" / "files"
STABLE = "You are a careful assistant for the billing team. Answer from the policy only."


def run(argv):
    """Run the CLI. Return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = prefix_stability.main([str(arg) for arg in argv])
        except SystemExit as exc:  # argparse usage errors
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def analyse(bodies):
    group = [("capture.jsonl:%d" % index, body) for index, body in enumerate(bodies, 1)]
    return prefix_stability.analyse(group, 4.0)


def chat(system, user):
    return {"model": "m", "system": system, "messages": [{"role": "user", "content": user}]}


class LeavesTests(unittest.TestCase):
    def test_order_is_tools_then_system_then_messages_whatever_the_body_order(self):
        body = {"messages": [{"role": "user", "content": "hi"}], "system": "sys",
                "tools": [{"name": "t"}], "model": "m", "max_tokens": 5}
        self.assertEqual(prefix_stability.leaves(body), [
            ("tools", "tools[0].name", "t"),
            ("system", "system", "sys"),
            ("conversation", "messages[0].role", "user"),
            ("conversation", "messages[0].content", "hi"),
        ])

    def test_cache_control_markers_are_ignored(self):
        marked = {"system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}]}
        plain = {"system": [{"type": "text", "text": "s"}]}
        self.assertEqual(prefix_stability.leaves(marked), prefix_stability.leaves(plain))

    def test_leading_system_and_developer_messages_count_as_system(self):
        body = {"messages": [{"role": "developer", "content": "a"}, {"role": "user", "content": "b"},
                             {"role": "system", "content": "c"}]}
        sections = [leaf[0] for leaf in prefix_stability.leaves(body) if leaf[1].endswith("content")]
        self.assertEqual(sections, ["system", "conversation", "conversation"])

    def test_other_provider_field_names_are_read(self):
        gemini = {"systemInstruction": {"parts": [{"text": "s"}]},
                  "contents": [{"role": "user", "parts": [{"text": "q"}]}]}
        self.assertEqual([leaf[:2] for leaf in prefix_stability.leaves(gemini)], [
            ("system", "systemInstruction.parts[0].text"),
            ("conversation", "contents[0].role"),
            ("conversation", "contents[0].parts[0].text")])
        responses = {"instructions": "s", "input": "q"}
        self.assertEqual(prefix_stability.leaves(responses),
                         [("system", "instructions", "s"), ("conversation", "input", "q")])

    def test_numbers_and_empty_containers_become_text(self):
        body = {"tools": [{"name": "t", "limit": 3, "extra": {}}]}
        self.assertEqual([leaf[2] for leaf in prefix_stability.leaves(body)], ["t", "3", "{}"])


class AnalyseTests(unittest.TestCase):
    def test_timestamp_in_system_prompt_is_high_with_path_offset_and_hint(self):
        bodies = [chat("Now: 2026-11-03T09:%02d:00Z. %s" % (minute, STABLE), "question %d" % minute)
                  for minute in (10, 21, 32, 43)]
        result = analyse(bodies)
        divergence = result["divergence"]
        self.assertEqual(result["status"], "unstable")
        self.assertEqual(result["finding"]["severity"], "high")
        self.assertEqual((divergence["path"], divergence["section"]), ("system", "system"))
        self.assertEqual(divergence["offset"], len("Now: 2026-11-03T09:"))
        self.assertEqual(divergence["distinct"], 4)
        self.assertEqual(divergence["hint"], "a timestamp or date")
        self.assertEqual(result["shared_chars"], len("Now: 2026-11-03T09:"))
        self.assertEqual(divergence["samples"][0]["ref"], "capture.jsonl:1")
        self.assertIn(">>>10:00Z", divergence["samples"][0]["text"])

    def test_potential_prefix_counts_everything_except_the_differing_span(self):
        bodies = [chat("Now: 2026-11-03T09:%02d:00Z. %s" % (minute, STABLE), "same question")
                  for minute in (10, 21, 32)]
        result = analyse(bodies)
        whole = len("Now: 2026-11-03T09:10:00Z. " + STABLE) + len("user") + len("same question")
        self.assertEqual(result["potential_chars"], whole - len("10"))
        self.assertEqual(result["potential_tokens_estimate"], int(round((whole - 2) / 4.0)))

    def test_uuid_and_long_id_hints(self):
        ids = ["7f3c9a52-1b0e-4c57-9d2a-3e41c8a0b6f1", "c2a81e07-66d4-4f3b-8a15-0b9d7e2f4c88",
               "0d5b7f91-e3a2-4a6c-b0f4-91c3d2e5a7b0"]
        result = analyse([chat("trace %s\n%s" % (value, STABLE), "q") for value in ids])
        self.assertEqual(result["divergence"]["hint"], "a UUID")
        result = analyse([chat("session 17%05d77 %s" % (n, STABLE), "q") for n in (11, 22, 33)])
        self.assertEqual(result["divergence"]["hint"], "a long number or ID")

    def test_plain_text_difference_has_no_hint(self):
        result = analyse([chat("Hello %s. %s" % (name, STABLE), "q") for name in ("Ann", "Bo", "Cy")])
        self.assertIsNone(result["divergence"]["hint"])
        self.assertEqual(result["finding"]["severity"], "high")

    def test_a_few_variants_are_medium(self):
        bodies = [chat("Tenant: %s. %s" % (tenant, STABLE), "q%d" % index)
                  for index, tenant in enumerate(["acme", "acme", "globex", "globex", "acme"])]
        result = analyse(bodies)
        self.assertEqual(result["divergence"]["distinct"], 2)
        self.assertEqual(result["finding"]["severity"], "medium")
        self.assertIn("2 variants across 5 requests", result["finding"]["summary"])

    def test_two_requests_that_differ_are_medium_not_high(self):
        result = analyse([chat("A " + STABLE, "q"), chat("B " + STABLE, "q")])
        self.assertEqual(result["finding"]["severity"], "medium")

    def test_reordered_json_keys_in_tools_are_detected(self):
        first = {"model": "m", "tools": [{"name": "lookup", "description": "Find an order"}],
                 "messages": [{"role": "user", "content": "a"}]}
        second = {"model": "m", "tools": [{"description": "Find an order", "name": "lookup"}],
                  "messages": [{"role": "user", "content": "b"}]}
        result = analyse([first, second, first])
        self.assertEqual(result["divergence"]["kind"], "structure")
        self.assertEqual(result["divergence"]["section"], "tools")
        self.assertEqual(result["divergence"]["hint"], "a different JSON key order")
        self.assertEqual(result["finding"]["severity"], "high")
        self.assertIn("key order", result["finding"]["summary"])

    def test_different_tool_lists_are_a_tools_finding_without_the_key_order_hint(self):
        one_tool = {"model": "m", "tools": [{"name": "a"}], "system": STABLE,
                    "messages": [{"role": "user", "content": "q"}]}
        two_tools = dict(one_tool, tools=[{"name": "a"}, {"name": "b"}])
        result = analyse([one_tool, two_tools, one_tool])
        self.assertEqual(result["divergence"]["section"], "tools")
        self.assertIsNone(result["divergence"]["hint"])
        self.assertEqual(result["finding"]["severity"], "medium")

    def test_difference_only_in_the_conversation_is_ok(self):
        result = analyse([chat(STABLE, "2026-11-03T09:%02d:00Z what is my balance" % minute)
                          for minute in (1, 2, 3)])
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["finding"])
        self.assertEqual(result["divergence"]["section"], "conversation")
        self.assertEqual(result["shared_chars"], len(STABLE) + len("user") + len("2026-11-03T09:0"))

    def test_identical_requests(self):
        result = analyse([chat(STABLE, "q")] * 3)
        self.assertEqual(result["status"], "identical")
        self.assertIsNone(result["finding"])
        self.assertEqual(result["shared_share"], 1.0)

    def test_a_growing_conversation_is_append_only(self):
        first = chat(STABLE, "q1")
        second = chat(STABLE, "q1")
        second["messages"] = second["messages"] + [{"role": "assistant", "content": "a1"},
                                                   {"role": "user", "content": "q2"}]
        result = analyse([first, second])
        self.assertEqual(result["status"], "append-only")
        self.assertIsNone(result["finding"])

    def test_openai_style_system_message_is_treated_as_system(self):
        bodies = [{"model": "m", "messages": [
            {"role": "system", "content": "Request-ID: %d%s" % (n, STABLE)},
            {"role": "user", "content": "q"}]} for n in (1, 2, 3)]
        result = analyse(bodies)
        self.assertEqual(result["divergence"]["path"], "messages[0].content")
        self.assertEqual(result["finding"]["severity"], "high")


class TempDirCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, rows, name="requests.jsonl"):
        path = self.dir / name
        lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


class CliTests(TempDirCase):
    def test_wrapped_and_bare_bodies_are_both_accepted(self):
        bare = dict(chat(STABLE, "one"), route="r")
        path = self.write([{"route": "r", "body": chat(STABLE, "two")},
                           {"route": "r", "request": chat(STABLE, "three")}, bare])
        code, out, _ = run([path, "--json"])
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(len(report["routes"]), 1)
        self.assertEqual(report["routes"][0]["requests"], 3)
        self.assertEqual(report["routes"][0]["model"], "m")

    def test_routes_and_models_are_compared_separately(self):
        other_model = dict(chat("different " + STABLE, "q"), model="n")
        path = self.write([{"route": "a", "body": chat(STABLE, "1")},
                           {"route": "a", "body": chat(STABLE, "2")},
                           {"route": "b", "body": chat("other prompt", "1")},
                           {"route": "b", "body": chat("other prompt", "2")},
                           {"route": "a", "body": other_model}])
        code, out, _ = run([path, "--json"])
        report = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual([(r["route"], r["model"], r["status"]) for r in report["routes"]],
                         [("a", "m", "ok"), ("a", "n", "skipped"), ("b", "m", "ok")])

    def test_route_filter(self):
        path = self.write([{"route": "a", "body": chat("x1 " + STABLE, "1")},
                           {"route": "a", "body": chat("x2 " + STABLE, "2")},
                           {"route": "b", "body": chat(STABLE, "1")},
                           {"route": "b", "body": chat(STABLE, "2")}])
        self.assertEqual(run([path])[0], 1)
        self.assertEqual(run([path, "--route", "b"])[0], 0)
        code, _, err = run([path, "--route", "missing"])
        self.assertEqual(code, 2)
        self.assertIn('no requests found for route "missing"', err)

    def test_fail_on_levels(self):
        path = self.write([{"route": "a", "body": chat("x1 " + STABLE, "1")},
                           {"route": "a", "body": chat("x2 " + STABLE, "2")}])
        self.assertEqual(run([path])[0], 1)
        self.assertEqual(run([path, "--fail-on", "high"])[0], 0)
        self.assertEqual(run([path, "--fail-on", "never"])[0], 0)

    def test_chars_per_token_changes_only_the_estimate(self):
        path = self.write([{"route": "a", "body": chat(STABLE, "1")},
                           {"route": "a", "body": chat(STABLE, "2")}])
        four = json.loads(run([path, "--json"])[1])["routes"][0]
        two = json.loads(run([path, "--json", "--chars-per-token", "2"])[1])["routes"][0]
        self.assertEqual(four["shared_chars"], two["shared_chars"])
        self.assertEqual(two["shared_tokens_estimate"], int(round(four["shared_chars"] / 2.0)))

    def test_bad_input_is_an_error_with_file_and_line_and_no_traceback(self):
        path = self.write([{"route": "a", "body": chat(STABLE, "1")}, "{broken",
                           {"route": "a", "note": "not a request"}, ["a", "list"]])
        code, out, err = run([path])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("requests.jsonl:2: not valid JSON", err)
        self.assertIn("requests.jsonl:3: no prompt fields found", err)
        self.assertIn("requests.jsonl:4: line is not a JSON object", err)
        self.assertNotIn("Traceback", err)

    def test_missing_file_empty_file_and_usage_errors_exit_two(self):
        self.assertEqual(run([self.dir / "nope.jsonl"])[0], 2)
        self.assertEqual(run([self.write([""])])[0], 2)
        self.assertEqual(run([])[0], 2)
        self.assertEqual(run([self.write([""]), "--chars-per-token", "0"])[0], 2)

    def test_help_documents_examples_and_exit_codes(self):
        code, out, _ = run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("Examples:", out)
        self.assertIn("Exit codes:", out)


class FixtureTests(unittest.TestCase):
    def report(self, name):
        code, out, _ = run([FILES / name / "requests.jsonl", "--json"])
        report = json.loads(out)
        return code, report, dict((entry["route"], entry) for entry in report["routes"])

    def test_broken_cache_finds_the_timestamp_in_the_system_prompt(self):
        code, report, routes = self.report("broken-cache")
        self.assertEqual(code, 1)
        self.assertEqual([(f["severity"], f["route"], f["path"]) for f in report["findings"]],
                         [("high", "support-chat", "system[0].text")])
        support = routes["support-chat"]
        self.assertEqual(support["divergence"]["hint"], "a timestamp or date")
        self.assertEqual(support["divergence"]["distinct"], 6)
        self.assertEqual(support["divergence"]["offset"], 81)
        self.assertIn("Current time: 2026-11-03T09:>>>14:07Z", support["divergence"]["samples"][0]["text"])
        self.assertGreater(support["potential_chars"], 2 * support["shared_chars"])
        self.assertEqual(report["findings"][0]["evidence"][0],
                         str(FILES / "broken-cache" / "requests.jsonl") + ":1")
        self.assertEqual(routes["faq-answer"]["status"], "ok")
        self.assertIsNone(routes["faq-answer"]["finding"])

    def test_mixed_waste_finds_the_request_id_in_the_system_message(self):
        code, report, routes = self.report("mixed-waste")
        self.assertEqual(code, 1)
        self.assertEqual([(f["severity"], f["route"], f["path"]) for f in report["findings"]],
                         [("high", "search-answer", "messages[0].content")])
        search = routes["search-answer"]
        self.assertEqual(search["divergence"]["hint"], "a UUID")
        self.assertEqual(search["divergence"]["offset"], len("Request-ID: "))
        self.assertEqual(search["divergence"]["section"], "system")
        self.assertEqual(routes["classify-ticket"]["status"], "ok")

    def test_healthy_requests_pass_even_with_a_timestamp_in_the_user_message(self):
        code, report, routes = self.report("healthy")
        self.assertEqual(code, 0)
        self.assertEqual(report["findings"], [])
        support = routes["support-chat"]
        self.assertEqual(support["status"], "ok")
        self.assertEqual(support["divergence"]["section"], "conversation")
        self.assertGreater(support["shared_share"], 0.9)

    def test_text_output_names_evidence_and_is_deterministic(self):
        argv = [FILES / "broken-cache" / "requests.jsonl"]
        code, out, _ = run(argv)
        self.assertEqual(code, 1)
        self.assertIn("first difference: system[0].text, character 81 (section: system)", out)
        self.assertIn("[high] unstable-prefix", out)
        self.assertIn("Findings: 1 unstable prefix(es).", out)
        self.assertEqual(run(argv), (code, out, ""))


if __name__ == "__main__":
    unittest.main()
