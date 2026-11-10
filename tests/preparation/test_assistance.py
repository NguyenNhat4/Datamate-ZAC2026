"""Preparation-model boundaries; all model calls use explicit in-process stubs."""
from contextlib import contextmanager
import copy
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from tabular_agent.preparation import PreparationConfig, PreparationSpec, prepare_business, profile_business
from tabular_agent.preparation.assistance import LocalProposalAdapter, assist_source, assist_table
from tabular_agent.preparation.readers import RawRow, RawTable, SourceInfo, discover_sources


class ProposalStub:
    def __init__(self, replies, *, calls=0, model="stub"):
        self.replies = list(replies)
        self.calls = calls
        self.requests = []
        self.configuration = {"model": model, "protocol": "stub-spec-v1", "seed": 42}

    def propose(self, context, feedback=None):
        self.calls += 1
        self.requests.append(copy.deepcopy({"context": context, "feedback": feedback}))
        value = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def completion(content, finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content))])


@contextmanager
def mocked_transport(content='{"grain":"unknown","roles":{},"evidence":[]}', *, finish="stop"):
    request = Mock(return_value=completion(content, finish))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)), close=Mock())
    # Deliberately omit model_id and max_tokens: those are framework details.
    model = SimpleNamespace(client=client)
    with patch("tabular_agent.model.create_model", return_value=model) as factory:
        yield factory, model, request


class AssistanceTests(unittest.TestCase):
    def setUp(self):
        self.source = SourceInfo("source_one", "opaque.csv", "csv", "source_hash")
        self.config = PreparationConfig(sample_rows=16)
        self.readme = "Column A is the order identifier.\nColumn B is the order date.\n"

    def table(self, rows=None):
        rows = rows or [RawRow(i + 2, [f"ORDER_{i}", "2025-01-01"]) for i in range(40)]
        return RawTable(self.source, "source_one:region_1", ["A", "B"], [1], iter(rows), {})

    def mapping(self):
        return {"grain": "order", "roles": {"order_id": 0, "order_date": 1},
                "evidence": [
                    {"role": "order_id", "position": 0, "source": "readme", "quote": "Column A is the order identifier."},
                    {"role": "order_date", "position": 1, "source": "readme", "quote": "Column B is the order date."},
                ]}

    def test_local_transport_uses_explicit_parameters_without_python_rewriting(self):
        with mocked_transport() as (factory, model, request):
            with LocalProposalAdapter("fixture-model", "http://127.0.0.1:9999/v1", max_tokens=789, timeout=12, seed=7) as adapter:
                result = adapter.propose({"task": "semantics", "labels": ["A", "B"]})
                self.assertEqual(result["grain"], "unknown")
                self.assertEqual(adapter.calls, 1)
                params = request.call_args.kwargs
                self.assertEqual(params["model"], "fixture-model")
                self.assertEqual(params["max_tokens"], 789)
                self.assertEqual(params["seed"], 7)
                self.assertEqual(params["temperature"], 0)
                self.assertIn("untrusted data", params["messages"][0]["content"])
                self.assertEqual(adapter.configuration["max_tokens"], 789)
            model.client.close.assert_called_once()
            adapter.close()
            model.client.close.assert_called_once()
            factory.assert_called_once_with("fixture-model", "http://127.0.0.1:9999/v1", timeout=12, max_tokens=789, seed=7)
            with self.assertRaises(RuntimeError):
                adapter.propose({})

    def test_nonlocal_or_unsafe_endpoints_fail_before_client_creation(self):
        endpoints = ["https://localhost/v1", "http://example.com/v1", "http://localhost.example.com/v1",
                     "http://127.0.0.1@evil.example/v1", "http://user:pass@localhost/v1",
                     "http://localhost/v1?key=secret", "http://localhost/v1#fragment",
                     "http://localhost:999999/v1", "not a URL", None]
        with patch("tabular_agent.model.create_model") as factory:
            for endpoint in endpoints:
                with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                    LocalProposalAdapter("fixture", endpoint)
            factory.assert_not_called()

    def test_ipv6_loopback_is_allowed_and_context_exception_closes_client(self):
        with mocked_transport() as (_, model, _):
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                with LocalProposalAdapter("fixture", "http://[::1]:9999/v1"):
                    raise RuntimeError("test failure")
            model.client.close.assert_called_once()

    def test_incomplete_nonfinite_duplicate_and_code_responses_are_rejected(self):
        contents = ["<code>print('unsafe')</code>", 'Final answer: ```json\n{"grain":"order"}\n```',
                    '{"grain":"order","grain":"unknown"}', '{"value":NaN}', '["not an object"]']
        for content in contents:
            with self.subTest(content=content), mocked_transport(content):
                with LocalProposalAdapter("fixture", "http://localhost/v1") as adapter:
                    with self.assertRaises(ValueError):
                        adapter.propose({})
        for reason in ["length", "tool_calls", None]:
            with self.subTest(reason=reason), mocked_transport("{}", finish=reason):
                with LocalProposalAdapter("fixture", "http://localhost/v1") as adapter:
                    with self.assertRaisesRegex(ValueError, "Incomplete"):
                        adapter.propose({})

    def test_valid_json_fence_returns_an_object(self):
        with mocked_transport('```json\n{"grain":"unknown","roles":{},"evidence":[]}\n```'):
            with LocalProposalAdapter("fixture", "http://localhost/v1") as adapter:
                self.assertEqual(adapter.propose({}), {"grain": "unknown", "roles": {}, "evidence": []})

    def test_semantic_proposal_needs_documentation_and_preserves_all_rows(self):
        table = self.table()
        adapter, spec = ProposalStub([self.mapping()]), PreparationSpec()
        assist_table(table, self.readme, "", spec, adapter, self.config)
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(spec.tables[table.region_id]["roles"], {"order_id": 0, "order_date": 1})
        self.assertEqual(len(list(table.rows)), 40)
        self.assertEqual(len(adapter.requests[0]["context"]["rows"]), 16)
        self.assertNotIn("questions", adapter.requests[0]["context"])
        self.assertNotIn("answers", adapter.requests[0]["context"])

    def test_raw_record_quote_cannot_establish_a_semantic_mapping(self):
        raw_quote = "Column A is the order identifier."
        table = self.table([RawRow(2, [raw_quote, "2025-01-01"])])
        proposal = self.mapping()
        proposal["roles"] = {"order_id": 0}
        proposal["evidence"] = proposal["evidence"][:1]
        adapter, spec = ProposalStub([proposal]), PreparationSpec()
        assist_table(table, "", "", spec, adapter, self.config)
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(spec.tables, {})
        self.assertEqual(len(list(table.rows)), 1)

    def test_each_role_needs_its_own_matching_column_meaning(self):
        invalid = []
        missing = self.mapping(); missing["evidence"].pop(); invalid.append(missing)
        wrong_position = self.mapping(); wrong_position["evidence"][0]["position"] = 1; invalid.append(wrong_position)
        fabricated = self.mapping(); fabricated["evidence"][0]["quote"] = "A is a product identifier."; invalid.append(fabricated)
        ungrounded = self.mapping(); ungrounded["evidence"][0]["quote"] = "Column A stores a value."; invalid.append(ungrounded)
        unsupported = self.mapping(); unsupported["roles"]["python"] = 1; invalid.append(unsupported)
        for proposal in invalid:
            with self.subTest(proposal=proposal):
                adapter, spec = ProposalStub([proposal]), PreparationSpec()
                assist_table(self.table(), self.readme + "Column A stores a value.\n", "", spec, adapter, self.config)
                self.assertEqual(adapter.calls, 2)
                self.assertEqual(spec.tables, {})

    def test_unknown_stays_unresolved_and_saved_spec_needs_no_model(self):
        adapter = ProposalStub([{"grain": "unknown", "roles": {}, "evidence": []}])
        spec = PreparationSpec()
        assist_table(self.table(), self.readme, "", spec, adapter, self.config)
        self.assertEqual(spec.tables, {})
        self.assertEqual(adapter.calls, 1)
        saved = PreparationSpec(tables={"source_one:region_1": {"grain": "order", "roles": {"order_id": 0}}})
        assist_table(self.table(), self.readme, "", saved, adapter, self.config)
        self.assertEqual(adapter.calls, 1)

    def test_retry_is_bounded_and_server_error_text_is_not_reflected(self):
        adapter = ProposalStub([RuntimeError("PRIVATE_SERVER_CONTENT"), self.mapping()])
        spec = PreparationSpec()
        assist_table(self.table(), self.readme, "", spec, adapter, self.config)
        self.assertEqual(adapter.calls, 2)
        self.assertIn("source_one:region_1", spec.tables)
        self.assertNotIn("PRIVATE_SERVER_CONTENT", adapter.requests[1]["feedback"])
        rejecting = ProposalStub([{"grain": "order", "roles": {}, "evidence": [], "python": "arbitrary code"}])
        spec = PreparationSpec()
        assist_table(self.table(), self.readme, "", spec, rejecting, self.config)
        self.assertEqual(rejecting.calls, 2)
        self.assertEqual(spec.tables, {})

    def test_layout_quotes_use_raw_header_strings_not_json_encoded_strings(self):
        header = 'A "code"\nidentifier'
        closed = []

        def iterator(*args, **kwargs):
            def rows():
                try:
                    yield RawRow(1, ["Report title"])
                    yield RawRow(2, [header, "B date"])
                    yield RawRow(3, ["PRIVATE_RECORD", "2025-01-01"])
                finally:
                    closed.append("rows")
            try:
                yield RawTable(self.source, "region_1", ["A", "B"], [1], rows(), {"layout_issues": ["weak_header_evidence"]})
            finally:
                closed.append("iterator")

        adapter = ProposalStub([{"header_rows": [2], "evidence": [{"quote": header}]}])
        spec = PreparationSpec()
        with patch("tabular_agent.preparation.readers.iter_tables", side_effect=iterator):
            assist_source(Path("unused"), self.source, spec, adapter, self.config)
        self.assertEqual(spec.layouts["source_one"], {"header_rows": [2]})
        self.assertEqual(closed, ["rows", "iterator"])
        encoded = json.dumps(header)[1:-1]
        adapter = ProposalStub([{"header_rows": [2], "evidence": [{"quote": encoded}]}])
        spec = PreparationSpec()
        with patch("tabular_agent.preparation.readers.iter_tables", side_effect=iterator):
            assist_source(Path("unused"), self.source, spec, adapter, self.config)
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(spec.layouts, {})

    def test_layout_cannot_cite_an_unselected_row_or_arbitrary_expression(self):
        def iterator(*args, **kwargs):
            yield RawTable(self.source, "region_1", ["A", "B"], [1], iter([
                RawRow(1, ["Report title"]), RawRow(2, ["A code", "B date"])]),
                {"layout_issues": ["weak_header_evidence"]})
        proposals = [
            {"header_rows": [2], "evidence": [{"quote": "Report title"}]},
            {"header_rows": [99], "evidence": [{"quote": "A code"}]},
            {"header_rows": [2], "evidence": [{"quote": "A code"}], "transform": "eval('code')"},
        ]
        for proposal in proposals:
            with self.subTest(proposal=proposal):
                spec, adapter = PreparationSpec(), ProposalStub([proposal])
                with patch("tabular_agent.preparation.readers.iter_tables", side_effect=iterator):
                    assist_source(Path("unused"), self.source, spec, adapter, self.config)
                self.assertEqual(spec.layouts, {})
                self.assertEqual(adapter.calls, 2)

    def test_public_api_fingerprints_adapter_and_counts_only_current_run(self):
        with tempfile.TemporaryDirectory(prefix="assisted-profile-test-") as temp:
            root = Path(temp); split = root / "split"; business = split / "data" / "shop"
            business.mkdir(parents=True)
            (business / "README.md").write_text(self.readme)
            (business / "opaque.csv").write_text("A,B\nORDER_1,2025-01-01\nORDER_2,2025-01-02\n")
            rulebook = split / "rulebook.md"; rulebook.write_text("# Current rules\n")
            calendar = split / "calendar.csv"; calendar.write_text("solar_date,lunar_day\n2025-01-01,2\n")
            (split / "questions.jsonl").write_text("PRIVATE_QUESTION_CONTENT")
            (split / "answers.jsonl").write_text("PRIVATE_REFERENCE_CONTENT")
            source = discover_sources(business)[0]
            spec = PreparationSpec(layouts={source.source_id: {"header_rows": [1], "width": 2}})
            adapter = ProposalStub([self.mapping()], calls=9)
            original_read_text = Path.read_text

            def guarded_read(path, *args, **kwargs):
                if path.name in {"questions.jsonl", "answers.jsonl"}:
                    raise AssertionError("Preparation must not read questions or reference answers")
                return original_read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", guarded_read):
                first = profile_business(business, rulebook, calendar, spec=spec, proposal_adapter=adapter)
                second = profile_business(business, rulebook, calendar, spec=spec, proposal_adapter=adapter)
                self.assertEqual(first.measurements["model_calls"], 1)
                self.assertEqual(second.measurements["model_calls"], 1)
                bundle = prepare_business(business, rulebook, calendar, output=root / "prepared", spec=spec, proposal_adapter=adapter)
                cached = prepare_business(business, rulebook, calendar, output=root / "prepared", spec=spec, proposal_adapter=adapter)
                self.assertEqual(cached.bundle_id, bundle.bundle_id)
                self.assertEqual(cached.measurements["model_calls"], 0)
                self.assertTrue(cached.cache_hit)
                adapter.configuration["seed"] = 43
                changed = prepare_business(business, rulebook, calendar, output=root / "prepared", spec=spec, proposal_adapter=adapter)
                self.assertNotEqual(changed.bundle_id, bundle.bundle_id)
            capture = json.dumps(adapter.requests)
            self.assertNotIn("PRIVATE_REFERENCE_CONTENT", capture)
            self.assertNotIn("PRIVATE_QUESTION_CONTENT", capture)
            self.assertEqual(first.evidence["config"]["model_configuration"]["adapter"]["model"], "stub")


if __name__ == "__main__":
    unittest.main()
