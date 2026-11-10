import json
from pathlib import Path
import tempfile
import unittest
from tabular_agent.contracts import validate_answer, read_jsonl
from tabular_agent.evaluate import match, normalize, score
from tabular_agent.cli import parser, select_questions


class Contracts(unittest.TestCase):

    def test_types(self):
        for kind, answer in [('number', 1.5), ('percent', -12.4), ('string', 'Đào'), ('date', '2026-02'), ('boolean', True), ('list', ['A', 2]), ('table', {'columns': ['x'], 'rows': [[1]]})]:
            self.assertEqual(validate_answer({'type': kind, 'answer': answer})['answer'], answer)
        for obj in [{'type': 'number', 'answer': True}, {'type': 'number', 'answer': float('nan')}, {'type': 'number', 'answer': '2'}, {'type': 'date', 'answer': '2026-02-30'}, {'type': 'list', 'answer': [None]}, {'type': 'unanswerable', 'reason': []}, {'type': 'table', 'answer': {'columns': ['a'], 'rows': [[1, 2]]}}]:
            with self.assertRaises(ValueError):
                validate_answer(obj)

    def test_json(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / 'x'
            p.write_text('{"id":"a"}\n{"id":"a"}\n')
            with self.assertRaises(ValueError):
                read_jsonl(p)

class Scoring(unittest.TestCase):

    def test_normalization_and_alias(self):
        self.assertEqual(normalize('  CÀ-PHÊ! '), 'cà phê')
        self.assertTrue(match({'type': 'string', 'answer': 'sp01'}, {'type': 'string', 'answer': 'Cà phê', 'alias': ['SP01']}))
        self.assertFalse(match({'type': 'string', 'answer': 'Ca phe'}, {'type': 'string', 'answer': 'Cà phê'}))

    def test_numbers_no_relative_tolerance(self):
        self.assertFalse(match({'type': 'number', 'answer': 1000000001}, {'type': 'number', 'answer': 1000000000}))
        self.assertTrue(match({'type': 'number', 'answer': 0.1 + 0.2}, {'type': 'number', 'answer': 0.3}))
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
        preds = [{'id': '0', 'type': 'number', 'answer': 2}, {'id': '1', 'type': 'number', 'answer': 3}, {'id': '2', 'type': 'number', 'answer': True}, {'id': '4', 'type': 'unanswerable'}]
        r = score(qs, preds, refs)
        self.assertEqual((r['correct'], r['wrong'], r['abstain'], r['penalized_accuracy']), (2, 2, 1, 0))
        self.assertEqual(score(qs, [], refs)['penalized_accuracy'], 0)
        with self.assertRaises(ValueError):
            score(qs, preds + [preds[0]], refs)

class EvaluationSplits(unittest.TestCase):

    def test_business_partitions_are_disjoint_and_stable(self):
        qs = [{'id': f'{i}-{j}', 'business_id': f'shop-{i}', 'question': 'q', 'as_of_date': '2026-01-01'} for i in range(80) for j in range(3)]
        partitions = {p: select_questions(qs, parser().parse_args(['bench', '--prepared', 'bundles', '--partition', p, '--limit', '0'])) for p in ('dev', 'validation', 'holdout')}
        businesses = [{q['business_id'] for q in values} for values in partitions.values()]
        self.assertTrue(all(businesses))
        self.assertTrue(all((not a & b for i, a in enumerate(businesses) for b in businesses[i + 1:])))
        self.assertEqual(sum(map(len, partitions.values())), len(qs))

    def test_balanced_sample_covers_businesses_before_repeating(self):
        qs = [{'id': f'{i}-{j}', 'business_id': f'shop-{i}', 'question': 'q', 'as_of_date': '2026-01-01'} for i in range(5) for j in range(20)]
        sample = select_questions(qs, parser().parse_args(['bench', '--prepared', 'bundles', '--limit', '5', '--seed', '7', '--sample', 'balanced']))
        self.assertEqual(len({q['business_id'] for q in sample}), 5)
        self.assertEqual(sample, select_questions(qs, parser().parse_args(['bench', '--prepared', 'bundles', '--limit', '5', '--seed', '7', '--sample', 'balanced'])))
