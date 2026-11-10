"""Synthetic checks for how the Streamlit UI shows a finished run; no model is invoked."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


APP = Path(__file__).resolve().parents[2] / 'streamlit_app.py'


class FinishedProcess:
    returncode = 0

    def poll(self):
        return self.returncode


class UIResults(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.split = root / 'split'
        self.output = root / 'run'
        (self.output / 'logs').mkdir(parents=True)
        self.split.mkdir()
        # No prepared bundles: the UI must not depend on this machine's artifacts.
        environment = patch.dict(os.environ, {'UI_PREPARED': str(root / 'no-bundles')})
        environment.start()
        self.addCleanup(environment.stop)
        self.questions = [
            {'id': 'q1', 'business_id': 'shop', 'question': 'First question', 'as_of_date': '2025-01-31'},
            {'id': 'q2', 'business_id': 'shop', 'question': 'Second question', 'as_of_date': '2025-01-31'},
        ]
        self.write_jsonl(self.split / 'questions.jsonl', self.questions)
        self.write_jsonl(self.output / 'questions.jsonl', self.questions)
        self.write_jsonl(self.output / 'submission.jsonl', [
            {'id': 'q1', 'type': 'number', 'answer': 1},
            {'id': 'q2', 'type': 'abstain', 'reason': 'fixture'},
        ])
        timings = [{'id': 'q1', 'seconds': 1.5, 'model_calls': 2},
                   {'id': 'q2', 'seconds': 0.5, 'model_calls': 1}]
        (self.output / 'timings.json').write_text(json.dumps(timings), encoding='utf-8')
        self.write_jsonl(self.output / 'logs' / '000000.jsonl', [
            {'stage': 'schema', 'text': 'semantic.orders (one row per order)'},
            {'stage': 'memory_step', 'step': '1', 'model_output': '```sql\nSELECT 1\n```', 'error': None,
             'token_usage': {}},
            {'stage': 'sql', 'sql': 'SELECT 1'},
            {'stage': 'observation', 'text': 'Result:\n1'},
            {'stage': 'question_timing', 'id': 'q1', 'seconds': 1.5, 'answer': {'type': 'number', 'answer': 1}},
        ])

    @staticmethod
    def write_jsonl(path, rows):
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def render(self):
        app = AppTest.from_file(str(APP))
        app.session_state['run'] = {'path': str(self.output), 'process': FinishedProcess(), 'log': io.StringIO()}
        app.run(timeout=20)
        app.text_input[0].set_value(str(self.split)).run(timeout=20)
        self.assertFalse(app.exception)
        return app

    def test_finished_run_shows_answers_timings_and_queries(self):
        app = self.render()
        headings = [item.value for item in app.subheader]
        self.assertIn('First question', headings)
        self.assertIn('Second question', headings)
        self.assertIn('Actions and observations', headings)
        self.assertNotIn('Internal score', headings)
        self.assertIn('1.50s · 2 model calls', [item.value for item in app.caption])
        self.assertIn('SELECT 1', [item.value for item in app.code])
        self.assertIn('Final answer: {"type": "number", "answer": 1}', [item.value for item in app.caption])

    def test_scored_run_shows_the_internal_score(self):
        report = {'evaluator': 'internal-not-official', 'total': 2, 'correct': 1, 'wrong': 0, 'abstain': 1,
                  'penalized_accuracy': 0.5, 'questions': []}
        (self.output / 'score.json').write_text(json.dumps(report), encoding='utf-8')
        app = self.render()
        self.assertIn('Internal score', [item.value for item in app.subheader])


if __name__ == '__main__':
    unittest.main()
