import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tabular_agent.agent import Agent
from tabular_agent.contracts import validate_answer, read_jsonl, parse_json
from tabular_agent.evaluate import match, normalize, score
from tabular_agent.model import LocalModel
from tabular_agent.cli import safe_sources
from tabular_agent.sandbox import read_bounded
from tabular_agent.planning import validate_plan, validate_evidence
from tabular_agent.splits import business_partition, select_questions
from tabular_agent.diagnostics import trace_signals
from tabular_agent.calibration import SETTINGS, check_compatible, fit_policy, permits


class Contracts(unittest.TestCase):
    def test_types(self):
        for kind, answer in [('number', 1.5), ('percent', -12.4), ('string', 'Đào'),
                             ('date', '2026-02'), ('boolean', True), ('list', ['A', 2]),
                             ('table', {'columns': ['x'], 'rows': [[1]]})]:
            self.assertEqual(validate_answer({'type': kind, 'answer': answer})['answer'], answer)
        for obj in [{'type': 'number', 'answer': True}, {'type': 'number', 'answer': float('nan')},
                    {'type': 'number', 'answer': '2'}, {'type': 'date', 'answer': '2026-02-30'},
                    {'type': 'list', 'answer': [None]}, {'type': 'unanswerable', 'reason': []},
                    {'type': 'table', 'answer': {'columns': ['a'], 'rows': [[1, 2]]}}]:
            with self.assertRaises(ValueError):
                validate_answer(obj)

    def test_json(self):
        self.assertEqual(parse_json('```json\n{"a":1}\n```'), {'a': 1})
        with self.assertRaises(ValueError):
            parse_json('{"x":NaN}')
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'x'
            p.write_text('{"id":"a"}\n{"id":"a"}\n')
            with self.assertRaises(ValueError):
                read_jsonl(p)

    def test_symlink_output_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp)
            (p / 'secret').write_text('secret')
            (p / 'output').symlink_to(p / 'secret')
            with self.assertRaises(OSError):
                read_bounded(p / 'output', 20)


class Scoring(unittest.TestCase):
    def test_normalization_and_alias(self):
        self.assertEqual(normalize('  CÀ-PHÊ! '), 'cà phê')
        self.assertTrue(match({'type': 'string', 'answer': 'sp01'},
                              {'type': 'string', 'answer': 'Cà phê', 'alias': ['SP01']}))
        self.assertFalse(match({'type': 'string', 'answer': 'Ca phe'}, {'type': 'string', 'answer': 'Cà phê'}))

    def test_numbers_no_relative_tolerance(self):
        self.assertFalse(match({'type': 'number', 'answer': 1000000001}, {'type': 'number', 'answer': 1000000000}))
        self.assertTrue(match({'type': 'number', 'answer': 0.1 + 0.2}, {'type': 'number', 'answer': .3}))
        self.assertFalse(match({'type': 'number', 'answer': 2}, {'type': 'percent', 'answer': 2}))
        self.assertTrue(match({'type': 'percent', 'answer': 1.234}, {'type': 'percent', 'answer': 1.23}))

    def test_list_order_and_alias(self):
        ref = {'type': 'list', 'answer': ['A', 'B'], 'alias': [['01'], ['02']]}
        self.assertTrue(match({'type': 'list', 'answer': ['01', 'B']}, ref))
        self.assertFalse(match({'type': 'list', 'answer': ['B', 'A']}, ref))

    def test_table_reorder_alias_and_fallback(self):
        ref = {'type': 'table', 'answer': {'columns': ['name', 'amount'], 'rows': [['A', 10], ['B', 20]]}, 'alias': [['01'], ['02']]}
        self.assertTrue(match({'type': 'table', 'answer': {'columns': ['amount', 'name'], 'rows': [[20, '02'], [10, 'A']]}}, ref))
        self.assertTrue(match({'type': 'table', 'answer': {'columns': ['customer', 'debt'], 'rows': [['B', 20], ['A', 10]]}}, ref))
        self.assertFalse(match({'type': 'table', 'answer': {'columns': ['name', 'amount'], 'rows': [['A', 10], ['A', 10]]}}, ref))

    def test_pa_missing_invalid_and_unanswerable(self):
        qs = [{'id': str(i), 'business_id': 'b'} for i in range(5)]
        refs = [{'id': str(i), 'type': 'number', 'answer': 2} for i in range(4)] + [{'id': '4', 'type': 'unanswerable', 'answer': None}]
        preds = [{'id': '0', 'type': 'number', 'answer': 2}, {'id': '1', 'type': 'number', 'answer': 3},
                 {'id': '2', 'type': 'number', 'answer': True}, {'id': '4', 'type': 'unanswerable'}]
        r = score(qs, preds, refs)
        self.assertEqual((r['correct'], r['wrong'], r['abstain'], r['invalid'], r['pa']), (2, 2, 1, 1, 0))
        self.assertEqual(score(qs, [], refs)['pa'], 0)
        with self.assertRaises(ValueError):
            score(qs, preds + [preds[0]], refs)


class EvaluationSplits(unittest.TestCase):
    def test_business_partitions_are_disjoint_and_stable(self):
        qs = [{'id': f'{i}-{j}', 'business_id': f'shop-{i}'} for i in range(80) for j in range(3)]
        partitions = {p: select_questions(qs, partition=p) for p in ('dev', 'validation', 'holdout')}
        businesses = [{q['business_id'] for q in values} for values in partitions.values()]
        self.assertTrue(all(businesses))
        self.assertTrue(all(not a & b for i, a in enumerate(businesses) for b in businesses[i + 1:]))
        self.assertEqual(sum(map(len, partitions.values())), len(qs))
        self.assertEqual(business_partition('shop-1'), business_partition('shop-1'))

    def test_balanced_sample_covers_businesses_before_repeating(self):
        qs = [{'id': f'{i}-{j}', 'business_id': f'shop-{i}'} for i in range(5) for j in range(20)]
        sample = select_questions(qs, limit=5, seed=7, sample='balanced')
        self.assertEqual(len({q['business_id'] for q in sample}), 5)
        self.assertEqual(sample, select_questions(qs, limit=5, seed=7, sample='balanced'))

    def test_trace_diagnostics_are_per_question_and_nonexclusive(self):
        trace = {'events': [{'response': '{"action":"python"}',
                            'observation': {'ok': True, 'result_validation_error': 'Independent result disagrees'}},
                           {'response': '{"action":"python"}', 'stopped': 'repeated_action'}]}
        signals = trace_signals(trace, {'type': 'abstain'})
        self.assertEqual(signals, ['repeated_action_stop', 'result_or_evidence_invalid', 'verification_disagreement'])

    def test_comparison_rejects_different_source_or_question_sets(self):
        import contextlib
        import io
        import runpy
        compare = runpy.run_path(str(Path(__file__).resolve().parent.parent / 'scripts' / 'compare-runs.py'))['main']
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = {'selected_questions_sha256': 'questions', 'rulebook_sha256': 'rules',
                      'agent_source_sha256': 'code', 'dependencies': {}, 'python': 'same',
                      'arguments': {**{key: None for key in ('schema_mode', 'verification', 'max_steps',
                          'max_executions', 'question_timeout', 'exec_timeout', 'model_timeout', 'max_tokens',
                          'context', 'seed', 'abstention_policy', 'cache_type_k', 'cache_type_v')}},
                      'model': {'model': 'local'}}
            report = {'status': 'completed', 'total': 1, 'correct': 1, 'wrong': 0, 'abstain': 0,
                      'pa': 1, 'coverage': 1, 'answered_accuracy': 1, 'total_wall_seconds': 1}
            for name in ('a', 'b'):
                directory = root / name
                directory.mkdir()
                (directory / 'config.json').write_text(json.dumps(config))
                (directory / 'report.json').write_text(json.dumps(report))
            with contextlib.redirect_stdout(io.StringIO()):
                compare([str(root / 'a'), str(root / 'b')])
            (root / 'b' / 'config.json').write_text(json.dumps({**config, 'agent_source_sha256': 'changed'}))
            with self.assertRaisesRegex(ValueError, 'agent_source_sha256'):
                compare([str(root / 'a'), str(root / 'b')])


class PlanEvidence(unittest.TestCase):
    def test_rejects_wrong_sources_and_out_of_period(self):
        plan = {'metric': 'revenue', 'period': {'start': '2025-06-01', 'end': '2025-06-30'},
                'sources': ['sales.xlsx'], 'grain': 'order', 'filters': ['valid orders'],
                'unit': 'VND', 'risk': 'high'}
        valid = validate_plan(plan, ['sales.xlsx'])
        with self.assertRaises(ValueError):
            validate_plan({**plan, 'sources': ['other.xlsx']}, ['sales.xlsx'])
        with self.assertRaises(ValueError):
            validate_evidence({'sources': ['sales.xlsx'], 'row_count': 3, 'date_min': '2025-05-31',
                               'date_max': '2025-06-25', 'checks': {'status': 'valid'}}, valid)
        with self.assertRaises(ValueError):
            validate_evidence({'sources': ['sales.xlsx'], 'row_count': 0, 'checks': {'status': 'valid'}}, valid)

    def test_evidence_date_accepts_only_unambiguous_slash_dates(self):
        plan = validate_plan({'metric': 'total', 'period': {'start': '2025-07-24', 'end': '2025-07-24'},
                              'sources': ['sales.xlsx'], 'grain': 'order', 'filters': [], 'unit': 'VND', 'risk': 'high'}, ['sales.xlsx'])
        evidence = {'sources': ['sales.xlsx'], 'row_count': 2, 'date_min': '24/07/2025',
                    'date_max': '24/07/2025', 'checks': {'status': 'valid'}}
        self.assertEqual(validate_evidence(evidence, plan), evidence)
        with self.assertRaises(ValueError):
            validate_evidence({**evidence, 'date_min': '07/08/2025'}, plan)


class Calibration(unittest.TestCase):
    def test_validation_policy_and_compatibility(self):
        arguments = {name: 1 for name in SETTINGS}
        arguments.update({'partition': 'validation', 'abstention_policy': None,
                          'split': str(Path(__file__).resolve().parent.parent / 'data' / 'train')})
        config = {'arguments': arguments, 'model': {'model': 'local', 'weight_sha256_provided': 'a' * 64},
                  'agent_source_sha256': 'source', 'selected_questions_sha256': 'questions'}
        details = [{'id': str(i), 'outcome': 'correct' if i < 10 else 'wrong'} for i in range(20)]
        report = {'status': 'completed', 'evaluator': 'internal-v1-not-official', 'questions': details}
        logs = {str(i): {'independent_result_matched': i < 10, 'diagnostic_signals': []} for i in range(20)}
        policy = fit_policy(config, report, logs)
        self.assertEqual(policy['policy'], 'verified_only')
        self.assertTrue(permits(policy['policy'], logs['0'], []))
        self.assertFalse(permits(policy['policy'], logs['15'], []))
        check_compatible(policy, config)
        with self.assertRaises(ValueError):
            check_compatible(policy, {**config, 'model': {'model': 'local', 'weight_sha256_provided': 'b' * 64}})
        with self.assertRaises(ValueError):
            fit_policy({**config, 'arguments': {**arguments, 'partition': 'holdout'}}, report, logs)
        with self.assertRaises(ValueError):
            fit_policy({**config, 'arguments': {**arguments, 'split': 'data/public_test'}}, report, logs)
        with self.assertRaises(ValueError):
            fit_policy(config, {**report, 'questions': details[:5]}, logs)

    def test_calibration_cli_writes_policy_without_question_answers(self):
        import contextlib
        import io
        from tabular_agent.cli import main
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            logs = root / 'logs'
            logs.mkdir()
            arguments = {name: 1 for name in SETTINGS}
            arguments.update({'partition': 'validation', 'abstention_policy': None,
                              'split': str(Path(__file__).resolve().parent.parent / 'data' / 'train')})
            config = {'arguments': arguments, 'model': {'model': 'local', 'weight_sha256_provided': 'a' * 64},
                      'agent_source_sha256': 'source', 'selected_questions_sha256': 'questions'}
            details = [{'id': str(i), 'outcome': 'correct' if i < 10 else 'wrong'} for i in range(20)]
            (root / 'config.json').write_text(json.dumps(config))
            (root / 'report.json').write_text(json.dumps({'status': 'completed', 'evaluator': 'internal-v1-not-official', 'questions': details}))
            for i in range(20):
                (logs / f'{i}.json').write_text(json.dumps({'id': str(i), 'independent_result_matched': i < 10,
                                                          'diagnostic_signals': []}))
            target = root / 'policy.json'
            with patch('sys.argv', ['cli', 'calibrate', '--run', str(root), '--output', str(target)]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            policy = json.loads(target.read_text())
            self.assertEqual(policy['policy'], 'verified_only')
            self.assertNotIn('answer', policy)


class ScriptModel:
    timeout = 2
    def __init__(self, actions):
        self.actions = iter(actions)
        self.contexts = []
    def complete(self, messages, timeout=None):
        self.contexts.append(json.dumps(messages))
        action = next(self.actions)
        return (action if isinstance(action, str) else json.dumps(action)), {}


class FakeSandbox:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []
    def run(self, action, *args, **kwargs):
        self.calls.append(action)
        return next(self.results)


class AgentLoop(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'source.txt').write_text('synthetic source')
        self.q = {'id': 'hidden-question-id', 'business_id': 'hidden-business-id', 'question': 'Tổng?', 'as_of_date': '2026-01-01'}
        self.plan = {'metric': 'total', 'period': None, 'sources': ['source.txt'], 'grain': 'item',
                     'filters': [], 'unit': 'count', 'risk': 'normal'}
        self.evidence = {'sources': ['source.txt'], 'row_count': 2, 'checks': {'unit': 'count'}}

    def tearDown(self):
        self.temp.cleanup()

    def solve(self, model, sandbox, **kw):
        return Agent(model, sandbox, **kw).solve(self.q, self.root, self.root / 'calendar.csv', 'current rules')

    def test_execute_with_plan_evidence_and_review(self):
        ans = {'type': 'number', 'answer': 3}
        model = ScriptModel([{'action': 'plan', 'plan': self.plan}, {'action': 'python', 'code': 'result=...'},
                             {'action': 'review', 'verdict': 'accept', 'findings': 'Checked rows and units'}])
        sandbox = FakeSandbox([{'ok': True, 'result': ans, 'evidence': self.evidence}])
        result, trace = self.solve(model, sandbox)
        self.assertEqual(result, ans)
        self.assertEqual(trace['python_executions'], 1)
        self.assertNotIn('hidden-question-id', model.contexts[0])
        self.assertNotIn('hidden-business-id', model.contexts[0])

    def test_repeat_gets_one_repair_opportunity(self):
        model = ScriptModel([{'action': 'plan', 'plan': self.plan}, {'action': 'python', 'code': 'bad'},
                             {'action': 'python', 'code': 'bad'}, {'action': 'python', 'code': 'fixed'},
                             {'action': 'review', 'verdict': 'accept', 'findings': 'Checked count'}])
        sandbox = FakeSandbox([{'ok': False, 'error': 'bad format'},
                               {'ok': True, 'result': {'type': 'number', 'answer': 3}, 'evidence': self.evidence}])
        result, trace = self.solve(model, sandbox)
        self.assertEqual(result['answer'], 3)
        self.assertEqual(len(sandbox.calls), 2)
        self.assertIn('Identical action not rerun', trace['events'][2]['observation']['error'])

    def test_repeat_stops_when_no_repair(self):
        model = ScriptModel([{'action': 'plan', 'plan': self.plan}] + [{'action': 'python', 'code': 'bad'}] * 3)
        sandbox = FakeSandbox([{'ok': False, 'error': 'bad'}])
        result, trace = self.solve(model, sandbox)
        self.assertEqual(result['type'], 'abstain')
        self.assertEqual(len(sandbox.calls), 1)
        self.assertEqual(trace['events'][-1]['stopped'], 'repeated_action')

    def test_high_risk_needs_matching_independent_calculation(self):
        plan = {**self.plan, 'risk': 'high'}
        ans = {'type': 'number', 'answer': 3}
        model = ScriptModel([{'action': 'plan', 'plan': plan}, {'action': 'python', 'code': 'compute A'},
                             {'action': 'review', 'verdict': 'accept', 'findings': 'checked'},
                             {'action': 'verify', 'code': 'compute B'},
                             {'action': 'review', 'verdict': 'accept', 'findings': 'checked rules'}])
        sandbox = FakeSandbox([{'ok': True, 'result': ans, 'evidence': self.evidence}] * 2)
        result, trace = self.solve(model, sandbox)
        self.assertEqual(result, ans)
        self.assertTrue(trace['independent_result_matched'])
        self.assertIn('needs an independent', trace['events'][2]['observation']['error'])

    def test_disagreement_cannot_be_accepted(self):
        plan = {**self.plan, 'risk': 'high'}
        model = ScriptModel([{'action': 'plan', 'plan': plan}, {'action': 'python', 'code': 'compute A'},
                             {'action': 'verify', 'code': 'compute B'},
                             {'action': 'review', 'verdict': 'accept', 'findings': 'checked'}])
        sandbox = FakeSandbox([{'ok': True, 'result': {'type': 'number', 'answer': 3}, 'evidence': self.evidence},
                               {'ok': True, 'result': {'type': 'number', 'answer': 4}, 'evidence': self.evidence}])
        result, trace = self.solve(model, sandbox, max_steps=4)
        self.assertEqual(result['type'], 'abstain')
        self.assertIn('disagrees', trace['events'][2]['observation']['result_validation_error'])

    def test_missing_result_reported_immediately(self):
        model = ScriptModel([{'action': 'plan', 'plan': self.plan}, {'action': 'python', 'code': 'print(3)'},
                             {'action': 'final', 'answer': {'type': 'abstain'}}])
        result, trace = self.solve(model, FakeSandbox([{'ok': True, 'result': None, 'stdout': '3'}]))
        self.assertEqual(result['type'], 'abstain')
        self.assertIn('printing alone', trace['events'][1]['observation']['result_validation_error'])

    def test_repair_review_after_invalid_result_can_continue(self):
        answer = {'type': 'number', 'answer': 3}
        model = ScriptModel([{'action': 'plan', 'plan': self.plan}, {'action': 'python', 'code': 'bad'},
                             {'action': 'review', 'verdict': 'repair', 'findings': 'Convert text numbers'},
                             {'action': 'python', 'code': 'fixed'},
                             {'action': 'review', 'verdict': 'accept', 'findings': 'Checked units and sum'}])
        sandbox = FakeSandbox([{'ok': True, 'result': {'type': 'number', 'answer': '12'}, 'evidence': self.evidence},
                               {'ok': True, 'result': answer, 'evidence': self.evidence}])
        result, trace = self.solve(model, sandbox)
        self.assertEqual(result, answer)
        self.assertIn('parse each numeric value', trace['events'][1]['observation']['result_validation_error'])
        self.assertIn('write corrected Python', trace['events'][2]['observation']['error'])

    def test_repeated_repair_review_stops(self):
        repair = {'action': 'review', 'verdict': 'repair', 'findings': 'Convert text numbers'}
        model = ScriptModel([repair] * 3)
        result, trace = self.solve(model, FakeSandbox([]))
        self.assertEqual(result['type'], 'abstain')
        self.assertEqual(trace['events'][-1]['stopped'], 'repeated_review')

    def test_rejected_candidate_cannot_be_accepted_without_recomputation(self):
        model = ScriptModel([{'action': 'plan', 'plan': self.plan}, {'action': 'python', 'code': 'first'},
                             {'action': 'review', 'verdict': 'repair', 'findings': 'Wrong filter'},
                             {'action': 'review', 'verdict': 'accept', 'findings': 'looks fine'}])
        sandbox = FakeSandbox([{'ok': True, 'result': {'type': 'number', 'answer': 3}, 'evidence': self.evidence}])
        result, trace = self.solve(model, sandbox, max_steps=4)
        self.assertEqual(result['type'], 'abstain')
        self.assertIn('execute corrected Python', trace['events'][-1]['observation']['error'])

    def test_evidence_outside_period_cannot_be_accepted(self):
        plan = {**self.plan, 'period': {'start': '2026-01-01', 'end': '2026-01-31'}}
        evidence = {**self.evidence, 'date_min': '2025-12-31', 'date_max': '2026-01-05'}
        model = ScriptModel([{'action': 'plan', 'plan': plan}, {'action': 'python', 'code': 'compute'},
                             {'action': 'review', 'verdict': 'accept', 'findings': 'checked'}])
        result, trace = self.solve(model, FakeSandbox([{'ok': True, 'result': {'type': 'number', 'answer': 3}, 'evidence': evidence}]), max_steps=3)
        self.assertEqual(result['type'], 'abstain')
        self.assertIn('outside', trace['events'][1]['observation']['result_validation_error'])

    def test_truncation_stops_without_growing_context(self):
        class Truncated(ScriptModel):
            def complete(self, messages, timeout=None):
                self.contexts.append(json.dumps(messages))
                return 'x' * 12000, {'finish_reason': 'length'}
        model = Truncated([])
        result, trace = self.solve(model, FakeSandbox([]))
        self.assertEqual(result['type'], 'abstain')
        self.assertEqual(len(model.contexts), 2)
        self.assertNotIn('x' * 100, model.contexts[-1])
        self.assertEqual(trace['python_executions'], 0)

    def test_profile_cached_and_question_context_has_no_ids(self):
        (self.root / 'sales.csv').write_text('amount\n1\n')
        model = ScriptModel([{'action': 'final', 'answer': {'type': 'abstain'}}] * 2)
        sandbox = FakeSandbox([{'ok': True, 'data': {'columns': ['amount']}, 'elapsed_seconds': 2.4}])
        agent = Agent(model, sandbox)
        for _ in range(2):
            agent.solve(self.q, self.root, self.root / 'calendar.csv', 'rules')
        self.assertEqual(len(sandbox.calls), 1)
        self.assertIn('amount', model.contexts[0])
        self.assertNotIn('hidden-question-id', model.contexts[0])
        self.assertNotIn('elapsed_seconds', model.contexts[0])

    def test_file_aliases_route_exact_names_to_inspection_and_python(self):
        plan = {**self.plan, 'sources': ['F1']}
        evidence = {**self.evidence, 'sources': ['F1']}
        model = ScriptModel([{'action': 'inspect', 'file': 'F1'}, {'action': 'plan', 'plan': plan},
                             {'action': 'python', 'code': 'read_table("F1")'},
                             {'action': 'review', 'verdict': 'accept', 'findings': 'Checked count'}])
        sandbox = FakeSandbox([{'ok': True, 'data': {'text': 'data'}},
                               {'ok': True, 'result': {'type': 'number', 'answer': 3}, 'evidence': evidence}])
        result, _ = self.solve(model, sandbox)
        self.assertEqual(result['answer'], 3)
        self.assertEqual(sandbox.calls[0]['file'], 'source.txt')
        self.assertEqual(sandbox.calls[1]['file_aliases']['F1'], 'source.txt')

    def test_column_error_reports_inspected_header_to_repair(self):
        plan = {**self.plan, 'sources': ['F1']}
        model = ScriptModel([{'action': 'inspect', 'file': 'F1', 'sheet': 'July'},
                             {'action': 'plan', 'plan': plan}, {'action': 'python', 'code': 'bad header'},
                             {'action': 'final', 'answer': {'type': 'abstain'}}])
        sandbox = FakeSandbox([{'ok': True, 'data': {'header_candidates': [
                                   {'row': 3, 'columns': ['Ngày', 'Tên hàng hóa']}]}},
                               {'ok': False, 'error': "KeyError: 'Tên hàng hóa'"}])
        _, trace = self.solve(model, sandbox)
        hint = trace['events'][2]['observation']['repair_hint']['tables'][0]
        self.assertEqual((hint['source'], hint['sheet'], hint['header_row']), ('F1', 'July', 3))
        self.assertIn('Tên hàng hóa', hint['columns'])

    def test_column_error_points_to_dataframe_that_retains_it(self):
        model = ScriptModel([{'action': 'plan', 'plan': self.plan}, {'action': 'python', 'code': 'bad join'},
                             {'action': 'final', 'answer': {'type': 'abstain'}}])
        failure = {'ok': False, 'error': "KeyError: 'product_id'", 'variables': {
            'details': {'kind': 'dataframe', 'rows': 2, 'columns': ['product_id', 'order_id']},
            'merged': {'kind': 'dataframe', 'rows': 2, 'columns': ['order_id']}}}
        _, trace = self.solve(model, FakeSandbox([failure]))
        hint = trace['events'][1]['observation']['repair_hint']
        self.assertEqual(hint['column_in_variables'], ['details'])
        self.assertIn('retain', hint['column_message'])

    def test_pandas_header_argument_reports_correct_api(self):
        model = ScriptModel([{'action': 'plan', 'plan': self.plan}, {'action': 'python', 'code': 'bad header argument'},
                             {'action': 'final', 'answer': {'type': 'abstain'}}])
        failure = {'ok': False, 'error': "TypeError: read_csv() got an unexpected keyword argument 'header_row'"}
        _, trace = self.solve(model, FakeSandbox([failure]))
        self.assertIn('pandas uses header=', trace['events'][1]['observation']['repair_hint']['message'])

    def test_empty_dated_selection_prompts_format_check(self):
        plan = {**self.plan, 'period': {'start': '2025-04-01', 'end': '2025-04-30'}}
        model = ScriptModel([{'action': 'plan', 'plan': plan},
                             {'action': 'python', 'purpose': 'inspect', 'code': 'inspect filter'},
                             {'action': 'final', 'answer': {'type': 'abstain'}}])
        diagnostic = {'ok': True, 'result': None, 'variables': {
            'source': {'kind': 'dataframe', 'rows': 10, 'columns': ['date']},
            'selected': {'kind': 'dataframe', 'rows': 0, 'columns': ['date']}}}
        _, trace = self.solve(model, FakeSandbox([diagnostic]))
        self.assertIn('parse dates', trace['events'][1]['observation']['empty_selection_hint'])

    def test_cannot_invent_final(self):
        model = ScriptModel([{'action': 'final', 'answer': {'type': 'number', 'answer': 99}}])
        result, _ = self.solve(model, FakeSandbox([]), max_steps=1)
        self.assertEqual(result['type'], 'abstain')

    def test_malformed_recovers(self):
        model = ScriptModel(['bad JSON', {'action': 'final', 'answer': {'type': 'abstain'}}])
        result, trace = self.solve(model, FakeSandbox([]))
        self.assertEqual(result['type'], 'abstain')
        self.assertIn('observation', trace['events'][0])

    def test_preview_alone_cannot_prove_unanswerable(self):
        ans = {'type': 'unanswerable', 'reason': 'Missing cost'}
        model = ScriptModel([{'action': 'final', 'answer': ans}, {'action': 'inspect'}, {'action': 'final', 'answer': ans}])
        result, trace = self.solve(model, FakeSandbox([{'ok': True, 'data': {'files': []}}]), max_steps=3)
        self.assertEqual(result['type'], 'abstain')
        self.assertFalse(trace['events'][0]['observation']['ok'])
        self.assertIn('preview cannot prove absence', trace['events'][2]['observation']['error'])

    def test_execution_failure_is_not_missing_data(self):
        final = {'action': 'final', 'answer': {'type': 'unanswerable', 'reason': 'Missing'}}
        model = ScriptModel([{'action': 'inspect'}, {'action': 'plan', 'plan': self.plan},
                             {'action': 'python', 'code': 'bad'}, final, final])
        sandbox = FakeSandbox([{'ok': True, 'data': {}}, {'ok': False, 'error': 'bad'}])
        result, trace = self.solve(model, sandbox)
        self.assertEqual(result['type'], 'abstain')
        self.assertIn('not evidence of missing data', trace['events'][-2]['observation']['error'])
        self.assertEqual(trace['events'][-1]['stopped'], 'unsupported_unanswerable')

    def test_unsupported_missing_claim_can_be_repaired(self):
        answer = {'type': 'number', 'answer': 3}
        model = ScriptModel([{'action': 'inspect'}, {'action': 'plan', 'plan': self.plan}, {'action': 'python', 'code': 'bad'},
                             {'action': 'final', 'answer': {'type': 'unanswerable', 'reason': 'Missing'}},
                             {'action': 'python', 'code': 'fixed'},
                             {'action': 'review', 'verdict': 'accept', 'findings': 'Checked rows'}])
        sandbox = FakeSandbox([{'ok': True, 'data': {}}, {'ok': False, 'error': 'bad'},
                               {'ok': True, 'result': answer, 'evidence': self.evidence}])
        result, trace = self.solve(model, sandbox)
        self.assertEqual(result, answer)
        self.assertIn('repair Python or abstain', trace['events'][3]['observation']['error'])

    def test_successful_diagnostics_can_establish_missing_data(self):
        answer = {'type': 'unanswerable', 'reason': 'Requested dates outside available coverage'}
        absence = {'sources': ['source.txt'], 'searched_rows': 2, 'missing': 'date coverage',
                   'checks': {'method': 'full scan'}, 'search_complete': True}
        model = ScriptModel([{'action': 'plan', 'plan': self.plan}, {'action': 'python', 'purpose': 'inspect', 'code': 'print(coverage)'},
                             {'action': 'final', 'answer': answer}])
        result, _ = self.solve(model, FakeSandbox([{'ok': True, 'result': None, 'stdout': 'coverage', 'evidence': absence}]))
        self.assertEqual(result, answer)


class Transport(unittest.TestCase):
    def test_remote_endpoints_rejected(self):
        for url in ['https://example.com', 'http://user:secret@localhost', 'file:///tmp/model']:
            with self.assertRaises(ValueError):
                LocalModel(base_url=url)

    def test_adapter_payloads(self):
        for backend in ['ollama', 'openai']:
            model = LocalModel(backend=backend, model='local-test')
            response = {'message': {'content': '{}'}} if backend == 'ollama' else {'choices': [{'message': {'content': '{}'}}]}
            with patch.object(model, 'request', return_value=response) as request:
                self.assertEqual(model.complete([])[0], '{}')
                payload = request.call_args.args[1]
                self.assertFalse(payload['stream'])
                self.assertEqual(payload['model'], 'local-test')

    def test_auto_model_selection(self):
        model = LocalModel(backend='openai')
        with patch.object(model, 'request', return_value={'data': [{'id': 'one'}]}):
            self.assertEqual(model.check()['model'], 'one')
