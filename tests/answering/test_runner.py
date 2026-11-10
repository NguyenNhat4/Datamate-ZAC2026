"""Synthetic checks for the runner, answer contract, scoring and model adapter; no model server is used."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from smolagents.models import ChatMessage, MessageRole
from tabular_agent.cli import parser, run, time_limit, QuestionDeadlineExceeded
from tabular_agent.contracts import validate_answer
from tabular_agent.evaluate import match, score
from tabular_agent.model import create_model


def write_split(root):
    split = root / 'split'
    business = split / 'data' / 'shop'
    business.mkdir(parents=True)
    (split / 'calendar.csv').write_text('date\n')
    (split / 'rulebook.md').write_text('Current rules')
    (business / 'README.md').write_text('Business notes')
    questions = [{'id': 'q1', 'business_id': 'shop', 'question': 'Q?', 'as_of_date': '2026-01-01'},
                 {'id': 'q2', 'business_id': 'other', 'question': 'Q?', 'as_of_date': '2026-01-01'}]
    (split / 'data' / 'other').mkdir()
    (split / 'questions.jsonl').write_text(''.join(json.dumps(q) + '\n' for q in questions))
    return split


class RunnerTests(unittest.TestCase):
    def test_question_deadline_cannot_be_swallowed_by_agent_exception_handler(self):
        with self.assertRaises(QuestionDeadlineExceeded):
            with time_limit(0.02):
                try:
                    time.sleep(0.2)
                except Exception:
                    self.fail('agent caught the deadline')

    def test_answer_contract(self):
        self.assertEqual(validate_answer({'type': 'number', 'answer': 2}), {'type': 'number', 'answer': 2})
        for value in (True, float('nan'), '2'):
            with self.assertRaises(ValueError):
                validate_answer({'type': 'number', 'answer': value})
        with self.assertRaises(ValueError):
            validate_answer({'type': 'date', 'answer': '2026-02-31'})

    def test_scoring_aliases_rows_and_penalty(self):
        self.assertTrue(match({'type': 'string', 'answer': 'CÀ PHÊ'}, {'type': 'string', 'answer': 'Cà phê'}))
        self.assertTrue(match({'type': 'list', 'answer': ['B']}, {'type': 'list', 'answer': ['A'], 'alias': [['B']]}))
        report = score([{'id': 'a'}, {'id': 'b'}], [{'id': 'a', 'type': 'number', 'answer': 1}],
                       [{'id': 'a', 'type': 'number', 'answer': 1}, {'id': 'b', 'type': 'number', 'answer': 2}])
        self.assertEqual(report['penalized_accuracy'], 0.5)

    def test_truncated_model_response_is_rejected(self):
        model = create_model('stub', 'http://127.0.0.1:18234/v1')
        incomplete = ChatMessage(role=MessageRole.ASSISTANT, content='```sql\nSELECT',
                                 raw={'choices': [{'finish_reason': 'length'}]})
        try:
            with patch('smolagents.OpenAIModel.generate', return_value=incomplete):
                with self.assertRaisesRegex(ValueError, 'Incomplete model response'):
                    model.generate([])
        finally:
            model.client.close()

    def test_predict_answers_with_sql_and_abstains_without_a_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            split = write_split(root)
            out = root / 'out'
            args = parser().parse_args(['predict', '--split', str(split), '--output', str(out), '--model', 'stub',
                                        '--prepared', str(root / 'bundles')])
            with patch('tabular_agent.cli.create_model', return_value=object()), \
                 patch('tabular_agent.cli.find_bundles', return_value={'shop': root / 'bundles' / 'shop'}), \
                 patch('tabular_agent.cli.answer_question_sql', return_value={'type': 'number', 'answer': 2}) as answer, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run(args), 0)
            rows = [json.loads(line) for line in (out / 'submission.jsonl').read_text().splitlines()]
            self.assertEqual(rows[0], {'id': 'q1', 'type': 'number', 'answer': 2})
            self.assertEqual(rows[1], {'id': 'q2', 'type': 'abstain', 'reason': 'No prepared data for this business.'})
            self.assertEqual(answer.call_count, 1)
            self.assertEqual(answer.call_args.args[3], root / 'bundles' / 'shop')
            self.assertEqual(answer.call_args.args[4], 'Current rules')
            self.assertEqual(answer.call_args.kwargs['max_steps'], 6)
            manifest = json.loads((out / 'manifest.json').read_text())
            self.assertEqual((manifest['agent_mode'], manifest['max_steps'], manifest['temperature']), ('sql', 6, 0.0))
            last = json.loads((out / 'logs' / '000000.jsonl').read_text().splitlines()[-1])
            self.assertEqual((last['stage'], last['id'], last['answer']['answer']), ('question_timing', 'q1', 2))

    def test_intents_only_abstains_without_calling_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            split = write_split(root)
            out = root / 'out'
            args = parser().parse_args(['predict', '--split', str(split), '--output', str(out), '--model', 'stub',
                                        '--prepared', str(root / 'bundles'), '--intents-only'])
            with patch('tabular_agent.cli.create_model', return_value=object()), \
                 patch('tabular_agent.cli.find_bundles', return_value={'shop': root / 'bundles' / 'shop'}), \
                 patch('tabular_agent.cli.answer_by_intent', return_value=None), \
                 patch('tabular_agent.cli.answer_question_sql') as model_path, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run(args), 0)
            rows = [json.loads(line) for line in (out / 'submission.jsonl').read_text().splitlines()]
            self.assertEqual(rows[0]['type'], 'abstain')
            self.assertIn('intents-only', rows[0]['reason'])
            model_path.assert_not_called()
            self.assertTrue(json.loads((out / 'manifest.json').read_text())['intents_only'])
            both = parser().parse_args(['predict', '--split', str(split), '--output', str(root / 'out2'), '--model', 'stub',
                                        '--prepared', str(root / 'bundles'), '--intents-only', '--no-intents'])
            with self.assertRaisesRegex(ValueError, 'exclude each other'):
                run(both)

    def test_bench_and_predict_need_prepared_bundles(self):
        for command in ('bench', 'predict'):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                parser().parse_args([command])
        self.assertIsNone(parser().parse_args(['doctor']).prepared)


if __name__ == '__main__':
    unittest.main()
