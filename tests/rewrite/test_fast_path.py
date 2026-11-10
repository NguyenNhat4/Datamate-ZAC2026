"""Focused synthetic checks for the single-agent path."""
import unittest
from decimal import Decimal
from unittest.mock import patch
import json
from pathlib import Path
import tempfile

import pandas as pd

from tabular_agent.cli import parser, run
from tabular_agent.tools import date_window, filter_categories, count_entities, count_orders, checked_join, round_decimal


class HelperTests(unittest.TestCase):
    def test_date_windows_inclusive_and_month_end(self):
        self.assertEqual(date_window.forward('2024-03-31', 'previous_month_comparable'),
                         {'start': '2024-02-01', 'end': '2024-02-29', 'inclusive': True})
        self.assertEqual(date_window.forward('2026-01-04', 'last_week')['start'], '2025-12-22')
        self.assertEqual(date_window.forward('2026-01-04', 'last_week')['end'], '2025-12-28')

    def test_checked_category_count_and_rounding(self):
        frame = pd.DataFrame({'id': ['a', 'a', 'b'], 'status': ['done', 'done', 'open']})
        selected = filter_categories.forward(frame, 'status', ['done'])
        self.assertEqual(count_entities.forward(selected, 'id'), 1)
        with self.assertRaisesRegex(ValueError, 'not present'):
            filter_categories.forward(frame, 'status', ['finished'])
        dates = pd.DataFrame({'day': pd.to_datetime(['2025-10-18', None, '2025-10-19'])})
        self.assertEqual(len(filter_categories.forward(dates, 'day', [pd.Timestamp('2025-10-18')])), 1)
        with self.assertRaisesRegex(ValueError, 'day/month/year'):
            filter_categories.forward(pd.DataFrame({'day': ['18/10/2025']}), 'day', ['2025-10-18'])
        self.assertEqual(round_decimal.forward('1.005', 2), Decimal('1.01'))
        with self.assertRaises(ValueError):
            count_entities.forward(pd.DataFrame({'id': [None]}), 'id')

    def test_order_count_requires_resolving_mixed_statuses(self):
        frame = pd.DataFrame({'Mã đơn hàng': ['a', 'a', 'b'],
                              'Trạng thái đơn hàng': ['Đã hoàn thành', 'Đã hoàn thành', 'Đã hủy']})
        with self.assertRaisesRegex(ValueError, 'Order statuses include'):
            count_entities.forward(frame, 'Mã đơn hàng')
        self.assertEqual(count_entities.forward(frame[frame['Trạng thái đơn hàng'] == 'Đã hoàn thành'], 'Mã đơn hàng'), 1)
        self.assertEqual(count_entities.forward(frame, 'Mã đơn hàng', allow_flagged_order_status=True), 2)
        cancelled = frame[frame['Trạng thái đơn hàng'] == 'Đã hủy']
        with self.assertRaisesRegex(ValueError, 'Order statuses include'):
            count_entities.forward(cancelled, 'Mã đơn hàng')
        self.assertEqual(count_entities.forward(cancelled, 'Mã đơn hàng', allow_flagged_order_status=True), 1)

    def test_order_count_filters_dates_and_excludes_whole_returned_orders(self):
        frame = pd.DataFrame({'order': ['a', 'a', 'b', 'c', 'c', 'd', None],
                              'date': ['18/10/2025'] * 5 + ['19/10/2025', None],
                              'status': ['complete', 'complete', 'cancelled', 'complete', 'returned', 'complete', None]})
        args = (frame, 'order', 'date', 'status', '2025-10-18', '2025-10-18', '%d/%m/%Y')
        self.assertEqual(count_orders.forward(*args, exclude_cancelled_and_returned=True), 1)
        self.assertEqual(count_orders.forward(*args, exclude_cancelled_and_returned=False), 3)
        frame.loc[0, 'status'] = None
        with self.assertRaisesRegex(ValueError, 'missing status'):
            count_orders.forward(*args, exclude_cancelled_and_returned=True)

    def test_join_checks_cardinality_and_null_keys(self):
        left = pd.DataFrame({'id': [1, 1], 'value': [2, 3]})
        right = pd.DataFrame({'id': [1], 'name': ['x']})
        self.assertEqual(len(checked_join.forward(left, right, 'id', 'many_to_one')), 2)
        with self.assertRaises(Exception):
            checked_join.forward(left, pd.concat([right, right]), 'id', 'many_to_one')
        with self.assertRaisesRegex(ValueError, 'null'):
            checked_join.forward(pd.DataFrame({'id': [None]}), right, 'id', 'many_to_one')

    def test_cli_defaults_to_single_agent_budgets(self):
        args = parser().parse_args(['predict'])
        self.assertEqual(args.max_steps, 6)
        self.assertEqual(args.limit, 0)
        self.assertEqual(args.question_timeout, 60)
        self.assertFalse(hasattr(args, 'agent_mode'))

    def test_single_mode_saves_timing_without_reference_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            split = root / 'split'
            (split / 'data' / 'shop').mkdir(parents=True)
            (split / 'calendar.csv').write_text('date\n')
            (split / 'rulebook.md').write_text('rules')
            (split / 'questions.jsonl').write_text(json.dumps({'id': 'q', 'business_id': 'shop',
                'question': 'How many?', 'as_of_date': '2026-01-01'}) + '\n')
            out = root / 'run'
            args = parser().parse_args(['predict', '--model', 'stub', '--split', str(split),
                                        '--output', str(out), '--max-steps', '9'])
            with patch('tabular_agent.cli.create_model', return_value=object()), \
                 patch('tabular_agent.cli.source_preview', return_value='synthetic preview'), \
                 patch('tabular_agent.cli.answer_question', return_value={'type': 'number', 'answer': 3}) as answer:
                self.assertEqual(run(args), 0)
            self.assertEqual(answer.call_args.kwargs['max_steps'], 4)
            self.assertEqual(json.loads((out / 'submission.jsonl').read_text())['answer'], 3)
            self.assertEqual(json.loads((out / 'timings.json').read_text())[0]['answer_type'], 'number')
            self.assertEqual(json.loads((out / 'manifest.json').read_text())['agent_mode'], 'single')

    def test_preview_is_reused_for_same_business(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            split = root / 'split'
            (split / 'data' / 'shop').mkdir(parents=True)
            (split / 'calendar.csv').write_text('date\n')
            (split / 'rulebook.md').write_text('rules')
            questions = [{'id': key, 'business_id': 'shop', 'question': 'Q', 'as_of_date': '2026-01-01'}
                         for key in ('a', 'b')]
            (split / 'questions.jsonl').write_text(''.join(json.dumps(q) + '\n' for q in questions))
            out = root / 'run'
            args = parser().parse_args(['predict', '--model', 'stub', '--split', str(split), '--output', str(out),
                                        '--limit', '0'])
            with patch('tabular_agent.cli.create_model', return_value=object()), \
                 patch('tabular_agent.cli.source_preview', return_value='preview') as preview, \
                 patch('tabular_agent.cli.answer_question', return_value={'type': 'abstain'}):
                run(args)
            self.assertEqual(preview.call_count, 1)
            timing = json.loads((out / 'timings.json').read_text())
            self.assertEqual([row['preview_cache_hit'] for row in timing], [False, True])


if __name__ == '__main__':
    unittest.main()
