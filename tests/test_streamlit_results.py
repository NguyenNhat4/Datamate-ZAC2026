"""Synthetic checks for the Streamlit run results; no model is invoked."""
import io
import json
from pathlib import Path
import tempfile
import unittest

from streamlit.testing.v1 import AppTest


APP = Path(__file__).resolve().parents[1] / 'streamlit_app.py'


class FinishedProcess:
    returncode = 0

    def poll(self):
        return self.returncode


class StreamlitResults(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.split = root / 'split'
        self.output = root / 'run'
        self.split.mkdir()
        self.output.mkdir()
        self.questions = [
            {'id': 'q1', 'business_id': 'shop', 'question': 'First question'},
            {'id': 'q2', 'business_id': 'shop', 'question': 'Second question'},
            {'id': 'q3', 'business_id': 'shop', 'question': 'Third question'},
        ]
        self.write_jsonl(self.split / 'questions.jsonl', self.questions)
        self.write_jsonl(self.output / 'submission.jsonl', [
            {'id': 'q1', 'type': 'number', 'answer': 1},
            {'id': 'q2', 'type': 'number', 'answer': 2},
            {'id': 'q3', 'type': 'abstain', 'reason': 'fixture'},
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

    def test_scored_run_displays_counts_and_individual_outcomes(self):
        report = {
            'evaluator': 'internal-not-official', 'total': 3,
            'correct': 1, 'wrong': 1, 'abstain': 1, 'penalized_accuracy': 0,
            'questions': [
                {'id': 'q1', 'outcome': 'correct'},
                {'id': 'q2', 'outcome': 'wrong'},
                {'id': 'q3', 'outcome': 'abstain'},
            ],
        }
        (self.output / 'score.json').write_text(json.dumps(report), encoding='utf-8')
        app = self.render()
        metrics = {metric.label: metric.value for metric in app.metric}
        for label in ('Total', 'Correct', 'Wrong', 'Abstained'):
            self.assertEqual(metrics[label], '3' if label == 'Total' else '1')
        captions = [item.value for item in app.caption]
        self.assertEqual([value for value in captions if value.startswith('Result: ')],
                         ['Result: Correct', 'Result: Wrong', 'Result: Abstained'])

    def test_completed_unscored_run_explains_missing_references(self):
        app = self.render()
        self.assertIn('No reference answers available; results cannot be scored.',
                      [item.value for item in app.info])
        self.assertFalse(app.metric)
        self.assertFalse(any(item.value.startswith('Result: ') for item in app.caption))


if __name__ == '__main__':
    unittest.main()
