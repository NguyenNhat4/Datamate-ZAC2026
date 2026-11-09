"""SQL answering loop with a scripted model and an invented bundle; no real model is called."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import duckdb

from tabular_agent.sql_agent import answer_question_sql


def sql(text):
    return "```sql\n" + text + "\n```"


def final(answer):
    return "```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```"


class Scripted:
    """Return prepared replies in order and remember the feedback each one answered."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.feedback = []

    def generate(self, messages, **kwargs):
        self.feedback.append(messages[-1].content[0]["text"])
        return SimpleNamespace(content=self.replies.pop(0), token_usage=None)


class SqlAnswering(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.bundle = Path(folder.name) / "bundle"
        self.business = Path(folder.name) / "business"
        self.bundle.mkdir()
        self.business.mkdir()
        (self.business / "README.md").write_text("Invented shop.", encoding="utf-8")
        db = duckdb.connect(str(self.bundle / "data.duckdb"))
        for schema in ("clean", "semantic", "private"):
            db.execute("CREATE SCHEMA " + schema)
        db.execute("CREATE TABLE clean.sales(order_id VARCHAR, order_date DATE, product_name VARCHAR, line_amount DECIMAL(20,0))")
        db.executemany("INSERT INTO clean.sales VALUES (?,?,?,?)",
                       [("1", "2025-04-01", "Trà vải", 100), ("2", "2025-04-02", "Cà phê", 50), ("3", "2025-06-30", "Trà vải", 70)])
        db.execute("CREATE VIEW semantic.eligible_sales_lines AS SELECT * FROM clean.sales")
        db.execute("CREATE TABLE private.audit(note VARCHAR)")
        db.close()
        columns = [{"name": "order_id", "type": "VARCHAR", "role": "order_id", "meaning": "order"},
                   {"name": "order_date", "type": "DATE", "role": "order_date", "meaning": "order date"},
                   {"name": "product_name", "type": "VARCHAR", "role": "product_name", "meaning": "product"},
                   {"name": "line_amount", "type": "DECIMAL(20,0)", "role": "line_amount", "meaning": "amount"}]
        schema = {"relations": [{"name": "semantic.eligible_sales_lines", "grain": "order_item", "columns": columns},
                                {"name": "clean.sales", "grain": "order_item", "columns": columns}],
                  "capabilities": {}, "temporal_coverage": {}}
        (self.bundle / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
        self.question = {"id": "q", "business_id": "invented", "question": "Doanh thu trà vải?", "as_of_date": "2025-06-30"}

    def ask(self, *replies):
        model = Scripted(*replies)
        answer = answer_question_sql(model, self.question, self.business, self.bundle, "Rules.", max_steps=6)
        return answer, model

    def test_answer_comes_from_a_query_and_numbers_are_normalized(self):
        answer, model = self.ask(
            final({"type": "number", "answer": 5}),
            sql("SELECT SUM(line_amount) FROM semantic.eligible_sales_lines WHERE name_like(product_name, 'tra vai')"),
            final({"type": "number", "answer": "170"}))
        self.assertEqual(answer, {"type": "number", "answer": 170})
        self.assertIn("Run a query first", model.feedback[1])
        self.assertIn("170", model.feedback[2])

    def test_unanswerable_after_an_empty_result_becomes_an_abstention(self):
        empty = sql("SELECT SUM(line_amount) FROM semantic.eligible_sales_lines "
                    "WHERE product_name = 'tra vai' AND order_date BETWEEN DATE '2025-05-01' AND DATE '2025-05-31'")
        answer, model = self.ask(empty, final({"type": "unanswerable", "reason": "no data"}),
                                 final({"type": "unanswerable", "reason": "still no data"}))
        self.assertEqual(answer["type"], "abstain")
        self.assertIn("2025-04-01 to 2025-06-30", model.feedback[2])

    def test_retry_after_rejected_unanswerable_can_still_answer(self):
        answer, _ = self.ask(
            sql("SELECT SUM(line_amount) FROM semantic.eligible_sales_lines WHERE product_name = 'tra vai'"),
            final({"type": "unanswerable", "reason": "no data"}),
            sql("SELECT SUM(line_amount) FROM semantic.eligible_sales_lines WHERE name_like(product_name, 'tra vai')"),
            final({"type": "number", "answer": 170}))
        self.assertEqual(answer, {"type": "number", "answer": 170})

    def test_unanswerable_is_kept_for_a_period_outside_the_data(self):
        answer, _ = self.ask(final({"type": "unanswerable", "reason": "before the data", "period": ["2025-03-01", "2025-03-31"]}))
        self.assertEqual(answer["type"], "unanswerable")
        answer, _ = self.ask(
            sql("SELECT SUM(line_amount) FROM semantic.eligible_sales_lines WHERE order_date BETWEEN DATE '2026-01-01' AND DATE '2026-01-31'"),
            final({"type": "unanswerable", "reason": "after the data"}))
        self.assertEqual(answer["type"], "unanswerable")

    def test_period_overlapping_the_data_is_not_unanswerable(self):
        answer, _ = self.ask(final({"type": "unanswerable", "reason": "x", "period": ["2025-03-01", "2025-04-01"]}),
                             final({"type": "unanswerable", "reason": "x", "period": ["2025-03-01", "2025-04-01"]}))
        self.assertEqual(answer["type"], "abstain")

    def test_system_date_and_private_tables_are_refused(self):
        answer, model = self.ask(
            sql("SELECT SUM(line_amount) FROM semantic.eligible_sales_lines WHERE order_date = CURRENT_DATE"),
            sql("SELECT * FROM private.audit"),
            final({"type": "abstain", "reason": "blocked"}))
        self.assertEqual(answer["type"], "abstain")
        self.assertIn("system date", model.feedback[1])
        self.assertIn("not in the schema", model.feedback[2])

    def test_step_limit_ends_in_an_abstention(self):
        answer, _ = self.ask(*["no block here"] * 6)
        self.assertEqual(answer["type"], "abstain")


if __name__ == "__main__":
    unittest.main()
