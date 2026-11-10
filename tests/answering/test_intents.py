"""Deterministic question kinds: text repair, periods and fixed queries, on invented data only."""
from datetime import date
import json
import tempfile
import unittest
from pathlib import Path

import duckdb

from tabular_agent.intents import Context, analyze, answer
from tabular_agent.intents import periods as P
from tabular_agent.intents.text import canonical, collapse, variants


def make_bundle(root, channel=False):
    """A bundle with rulebook-ready orders and lines, in the shape preparation publishes."""
    con = duckdb.connect(str(Path(root) / 'data.duckdb'))
    con.execute('CREATE SCHEMA semantic')
    con.execute('CREATE SCHEMA clean')
    con.execute("""CREATE TABLE semantic.orders AS SELECT * FROM (VALUES
        ('o1', DATE '2026-03-02', TIMESTAMP '2026-03-02 08:30:00', 'completed', 100000, 'c1', 'An', FALSE, FALSE, TRUE),
        ('o2', DATE '2026-03-02', TIMESTAMP '2026-03-02 15:10:00', 'completed', 50000, 'c1', 'An', FALSE, FALSE, TRUE),
        ('o3', DATE '2026-03-05', TIMESTAMP '2026-03-05 09:00:00', 'cancelled', 70000, 'c2', 'Bình', TRUE, FALSE, FALSE),
        ('o4', DATE '2026-03-10', TIMESTAMP '2026-03-10 08:45:00', 'completed', 200000, 'c2', 'Bình', FALSE, FALSE, TRUE),
        ('o5', DATE '2026-03-19', TIMESTAMP '2026-03-19 09:05:00', 'completed', 30000, NULL, NULL, FALSE, FALSE, TRUE),
        ('o6', DATE '2026-02-10', TIMESTAMP '2026-02-10 10:00:00', 'completed', 100000, 'c1', 'An', FALSE, FALSE, TRUE),
        ('o7', DATE '2026-02-11', TIMESTAMP '2026-02-11 10:00:00', 'returned', 60000, 'c2', 'Bình', FALSE, TRUE, FALSE))
        t(order_id, order_date, created_at, order_state, revenue, customer_key, customer_name, cancelled, returned,
          counts_as_sale)""")
    if channel:
        con.execute("ALTER TABLE semantic.orders ADD COLUMN channel VARCHAR")
        con.execute("UPDATE semantic.orders SET channel = CASE WHEN order_id IN ('o1', 'o4') THEN 'phone' ELSE 'store' END")
    con.execute('CREATE VIEW semantic.eligible_orders AS SELECT * FROM semantic.orders WHERE counts_as_sale')
    # Tết 2026 in this invented calendar: lunar 23/12/2025 is 2026-02-10 and lunar 10/1/2026 is 2026-02-26.
    con.execute("CREATE TABLE clean.calendar AS SELECT * FROM (VALUES (DATE '2026-02-10', 23, 12, 2025, FALSE, ''), "
                "(DATE '2026-02-26', 10, 1, 2026, FALSE, '')) t(date, lunar_day, lunar_month, lunar_year, lunar_leap, holiday)")
    con.execute("""CREATE TABLE semantic.eligible_sales_lines AS SELECT * FROM (VALUES
        ('o1', DATE '2026-03-02', 'Cà phê sữa (M)', 2, 2, 'ly', 60000),
        ('o1', DATE '2026-03-02', 'Trà đào (L)', 1, 1, 'ly', 40000),
        ('o2', DATE '2026-03-02', 'Cà phê sữa (M)', 2, 2, 'ly', 50000),
        ('o4', DATE '2026-03-10', 'Trà đào (L)', 4, 4, 'ly', 200000),
        ('o5', DATE '2026-03-19', 'Cà phê sữa (M)', 1, 1, 'ly', 30000),
        ('o6', DATE '2026-02-10', 'Cà phê sữa (M)', 4, 4, 'ly', 100000))
        t(order_id, order_date, product_name, quantity, base_quantity, base_unit, line_amount)""")
    con.execute('CREATE VIEW semantic.product_quantities AS SELECT * FROM semantic.eligible_sales_lines')
    con.close()

    def relation(name, columns, **extra):
        return {'name': name, 'grain': 'order', 'columns': [{'name': c, 'type': 'VARCHAR'} for c in columns], **extra}

    order_columns = ['order_id', 'order_date', 'created_at', 'order_state', 'revenue', 'customer_key', 'customer_name',
                     'cancelled', 'returned', 'counts_as_sale'] + (['channel'] if channel else [])
    schema = {'relations': [
        relation('semantic.orders', order_columns),
        relation('semantic.eligible_orders', order_columns),
        relation('clean.calendar', ['date', 'lunar_day', 'lunar_month', 'lunar_year', 'lunar_leap', 'holiday']),
        relation('semantic.eligible_sales_lines', ['order_id', 'order_date', 'product_name', 'quantity', 'base_quantity',
                                                   'base_unit', 'line_amount']),
        relation('semantic.product_quantities', ['order_id', 'order_date', 'product_name', 'quantity', 'base_quantity',
                                                  'base_unit', 'line_amount'])]}
    (Path(root) / 'schema.json').write_text(json.dumps(schema), encoding='utf-8')


class Scripted:
    """A model that replies with prepared texts."""

    def __init__(self, *replies):
        self.replies = list(replies)

    def generate(self, messages, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(content=self.replies.pop(0), token_usage=None)


class ChannelsAndAbsentInformation(unittest.TestCase):
    AS_OF = '2026-03-20'

    def test_revenue_by_channel_and_its_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_bundle(tmp, channel=True)
            ctx = Context(tmp)
            try:
                # Phone orders o1 and o4 count 100000 + 200000 of the 380000 in March.
                self.assertEqual(answer('doanh thu các đơn bán qua điện thoại tháng 3/2026', self.AS_OF, ctx)[0],
                                 {'type': 'number', 'answer': 300000})
                self.assertEqual(answer('doanh thu các đơn bán qua điện thoại chiếm bao nhiêu % tổng doanh thu tháng 3/2026',
                                        self.AS_OF, ctx)[0], {'type': 'percent', 'answer': 78.95})
                self.assertEqual(answer('doanh thu các đơn bán qua điện thoại và các đơn bán tại quầy tháng 3/2026 mỗi loại bao nhiêu',
                                        self.AS_OF, ctx)[0], {'type': 'list', 'answer': [300000, 80000]})
            finally:
                ctx.close()

    def test_product_questions_wait_where_names_join_a_dish_and_its_toppings(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_bundle(tmp)
            con = duckdb.connect(str(Path(tmp) / 'data.duckdb'))
            con.execute("UPDATE semantic.eligible_sales_lines SET product_name = product_name || ' + Trân châu đen (7.000đ)' "
                        "WHERE order_id = 'o5'")
            con.close()
            ctx = Context(tmp)
            try:
                # Totals do not depend on what the lines are called; a dish's own revenue does (rulebook: the topping is
                # a product of its own, and preparation does not split it yet).
                self.assertEqual(answer('doanh thu tháng 3/2026', self.AS_OF, ctx)[0]['answer'], 380000)
                self.assertIsNone(answer('doanh thu của Cà phê sữa (M) tháng 3/2026', self.AS_OF, ctx))
                self.assertIsNone(answer('top 3 mặt hàng bán chạy nhất tháng 3/2026', self.AS_OF, ctx))
            finally:
                ctx.close()

    def test_information_the_data_does_not_record_is_unanswerable_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_bundle(tmp)  # no channel column, and no source column that looks like one
            ctx = Context(tmp)
            try:
                self.assertEqual(answer('doanh thu các đơn bán qua điện thoại tháng 3/2026', self.AS_OF, ctx)[0]['type'], 'unanswerable')
            finally:
                ctx.close()


class WithTheModelPath(unittest.TestCase):
    def test_the_model_can_open_a_bundle_after_an_intent_attempt_on_it(self):
        """DuckDB shares one locked instance per file in a process: the attempt must leave the file closed."""
        from unittest.mock import patch
        from tabular_agent.cli import answer_by_intent
        from tabular_agent.sql_agent import answer_question_sql
        opened = []

        class Tracked(Context):
            def __init__(self, bundle):
                super().__init__(bundle)
                self.closed = False
                opened.append(self)

            def close(self):
                self.closed = True
                super().close()

        with tempfile.TemporaryDirectory() as tmp, patch('tabular_agent.cli.IntentContext', Tracked):
            bundle, business = Path(tmp) / 'bundle', Path(tmp) / 'business'
            bundle.mkdir()
            business.mkdir()
            make_bundle(bundle)
            catalogs, events = {}, []
            asked = {'id': 'q', 'business_id': 'shop', 'question': 'doanh thu tháng 3/2026', 'as_of_date': '2026-03-20'}
            for _ in range(2):  # the second round reuses the cached catalogue
                answer = answer_by_intent(catalogs, {'shop': bundle}, asked, events.append)
                self.assertEqual(answer, {'type': 'number', 'answer': 380000})
                open_ended = {**asked, 'question': 'doanh thu tháng 3/2026 của khách vip'}
                self.assertIsNone(answer_by_intent(catalogs, {'shop': bundle}, open_ended, events.append))
                reply = answer_question_sql(Scripted('```json\n{"type": "abstain"}\n```'), open_ended, business, bundle,
                                            'Rules.', max_steps=2)
                self.assertEqual(reply['type'], 'abstain')
            self.assertEqual([e['stage'] for e in events], ['intent', 'intent'])
            self.assertEqual(events[0]['intent'], 'revenue')
            self.assertEqual(events[0]['spec']['periods'], [['2026-03-01', '2026-03-31']])
            self.assertEqual(len(opened), 4)
            self.assertTrue(all(c.closed for c in opened), 'an intent attempt left the bundle open')


class TextRepair(unittest.TestCase):
    def test_telex_residue_is_removed(self):
        self.assertEqual(collapse('ddown'), 'don')
        self.assertEqual(collapse('toongr'), 'tongr')
        self.assertIn('tong', variants('toongr'))
        self.assertIn('duoc', variants('dduwowcj'))
        self.assertIn('thang', variants('thangs'))

    def test_abbreviations_and_phrases_become_canonical_words(self):
        known = {'thang', 'ban', 'duoc', 'don', 'tien'}
        self.assertEqual(canonical('thangs 3/2026 dthu hqua bnhiu ddown', known), 'thang 3/2026 doanhthu hom qua baonhieu don')
        self.assertEqual(canonical('Tỉ lệ đơn hàng bị huỷ là bao nhiêu %?', known),
                         'tile don bi huy la baonhieu phantram')


class Periods(unittest.TestCase):
    TODAY = '2026-03-20'  # a Friday

    def periods(self, text, today=TODAY):
        masked, found = P.extract(text, today)
        return masked, [(p.kind, p.first.isoformat(), p.last.isoformat()) for p in found]

    def test_named_and_relative_periods_follow_the_rulebook(self):
        self.assertEqual(self.periods('thang 2/2026')[1], [('month', '2026-02-01', '2026-02-28')])
        self.assertEqual(self.periods('thang nay')[1], [('this_month', '2026-03-01', '2026-03-20')])
        self.assertEqual(self.periods('tuan nay')[1], [('this_week', '2026-03-16', '2026-03-20')])
        self.assertEqual(self.periods('tuan truoc')[1], [('week', '2026-03-09', '2026-03-15')])
        self.assertEqual(self.periods('7 ngay gan nhat')[1], [('last_days', '2026-03-14', '2026-03-20')])
        self.assertEqual(self.periods('tu dau nam den het hom nay')[1], [('year_to_date', '2026-01-01', '2026-03-20')])
        self.assertEqual(self.periods('quy 1/2026')[1], [('quarter', '2026-01-01', '2026-03-31')])

    def test_a_month_without_a_year_is_this_year_unless_it_starts_after_today(self):
        self.assertEqual(self.periods('thang 2')[1][0][1:], ('2026-02-01', '2026-02-28'))
        self.assertEqual(self.periods('thang 11')[1][0][1:], ('2025-11-01', '2025-11-30'))

    def test_today_is_the_reference_date_not_the_system_date(self):
        self.assertEqual(self.periods('hom qua', '2024-03-01')[1], [('day', '2024-02-29', '2024-02-29')])

    def test_an_impossible_date_is_refused(self):
        self.assertIsNone(P.extract('ngay 30/02/2026', self.TODAY)[1])

    def test_the_comparison_base_depends_on_the_kind_of_period(self):
        today = date(2026, 3, 20)
        month = P.Period('month', *P.month_range(2026, 3))
        self.assertEqual(P.base_of(month, today).first, date(2026, 2, 1))
        self.assertEqual(P.base_of(month, today, year_ago=True).first, date(2025, 3, 1))
        to_date = P.Period('this_month', date(2026, 3, 1), today)
        base = P.base_of(to_date, today)
        self.assertEqual((base.first, base.last), (date(2026, 2, 1), date(2026, 2, 20)))
        self.assertEqual(P.base_of(P.Period('month_to_date', date(2026, 3, 1), date(2026, 3, 31)), today).last, date(2026, 2, 28))


class FixedQueries(unittest.TestCase):
    AS_OF = '2026-03-20'

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        make_bundle(cls.tmp.name)
        cls.ctx = Context(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.ctx.close()
        cls.tmp.cleanup()

    def ask(self, question):
        hit = answer(question, self.AS_OF, self.ctx)
        return None if hit is None else (hit[1], hit[0])

    def test_revenue_counts_only_orders_that_count_as_sales(self):
        self.assertEqual(self.ask('Doanh thu tháng 3/2026 là bao nhiêu?'), ('revenue', {'type': 'number', 'answer': 380000}))
        self.assertEqual(self.ask('thangs 3/2026 cua hang ban dc bnhiu tieenf')[1]['answer'], 380000)
        self.assertEqual(self.ask('doanh thu hôm qua')[1]['answer'], 30000)

    def test_orders_cancellations_and_average(self):
        self.assertEqual(self.ask('tháng 3/2026 có bao nhiêu đơn')[1]['answer'], 4)
        self.assertEqual(self.ask('đếm số đơn đã huỷ trong tháng 3/2026')[1]['answer'], 1)
        # One cancelled order among the five orders of March, whatever their state.
        self.assertEqual(self.ask('tỉ lệ đơn huỷ tháng 3/2026 là bao nhiêu %')[1], {'type': 'percent', 'answer': 20.0})
        self.assertEqual(self.ask('giá trị trung bình một đơn tháng 3/2026')[1]['answer'], 95000)

    def test_change_against_the_month_before_and_best_day(self):
        self.assertEqual(self.ask('so với tháng trước, doanh thu tháng 3/2026 thay đổi bao nhiêu %')[1],
                         {'type': 'percent', 'answer': 280.0})
        self.assertEqual(self.ask('tháng 3/2026 ngày nào bán được nhiều tiền nhất')[1], {'type': 'date', 'answer': '2026-03-10'})

    def test_thresholds_use_the_stated_comparison(self):
        self.assertEqual(self.ask('đếm số ngày trong tháng 3/2026 mà doanh thu cả ngày từ 150.000 đồng trở lên')[1]['answer'], 2)
        self.assertEqual(self.ask('đếm số ngày trong tháng 3/2026 mà doanh thu cả ngày trên 150.000 đồng')[1]['answer'], 1)
        self.assertEqual(self.ask('tháng 3/2026 bao nhiêu đơn có tiền hàng từ 100.000 đồng trở lên')[1]['answer'], 2)

    def test_named_products_and_units(self):
        self.assertEqual(self.ask('doanh thu của Cà phê sữa (M) tháng 3/2026')[1]['answer'], 140000)
        self.assertEqual(self.ask('tháng 3/2026 bán được bao nhiêu ly Cà phê sữa (M)')[1]['answer'], 5)
        self.assertEqual(self.ask('tháng 3/2026 quán bán được bao nhiêu ly size L, cộng mọi món')[1]['answer'], 5)

    def test_rankings_defined_by_name_in_the_rulebook(self):
        self.assertEqual(self.ask('ngày đắt khách nhất tháng 3/2026 là ngày nào')[1], {'type': 'date', 'answer': '2026-03-02'})
        self.assertEqual(self.ask('giờ cao điểm tháng 3/2026 là mấy giờ')[1], {'type': 'number', 'answer': 8})
        self.assertEqual(self.ask('khách hay đổi ý nhất tháng 3/2026 là ai')[1], {'type': 'string', 'answer': 'Bình'})
        self.assertEqual(self.ask('mặt hàng được ưa chuộng nhất tháng 3/2026')[1], {'type': 'string', 'answer': 'Cà phê sữa (M)'})
        # Customers tie on 1 day with a purchase (the cancelled order is not a purchase); the lower code wins.
        self.assertEqual(self.ask('khách trung thành nhất tháng 3/2026')[1], {'type': 'string', 'answer': 'An'})
        # Nobody has three orders in the month, so there is no big spender to name.
        self.assertIsNone(self.ask('khách sộp nhất tháng 3/2026'))

    def test_several_months_a_trend_and_the_best_month(self):
        self.assertEqual(self.ask('doanh thu mỗi tháng từ tháng 2/2026 đến tháng 3/2026')[1], {'type': 'list', 'answer': [100000, 380000]})
        self.assertEqual(self.ask('cho tôi doanh thu của tháng 2/2026 và tháng 3/2026, ghi lần lượt theo thứ tự đó')[1]['answer'], [100000, 380000])
        self.assertEqual(self.ask('từ tháng 2/2026 đến tháng 3/2026 tháng nào cửa hàng bán được nhiều tiền nhất')[1],
                         {'type': 'date', 'answer': '2026-03'})
        self.assertEqual(self.ask('doanh thu có tăng liên tục qua 2 tháng 2/2026, 3/2026 không')[1], {'type': 'boolean', 'answer': True})

    def test_tet_comes_from_the_lunar_calendar_of_the_bundle(self):
        # 2026-02-10 to 2026-02-26 holds only the order of the 10th that counts as sales.
        self.assertEqual(self.ask('doanh thu dịp Tết 2026 là bao nhiêu')[1], {'type': 'number', 'answer': 100000})
        self.assertIsNone(self.ask('doanh thu dịp Tết 2024 là bao nhiêu'))

    def test_a_period_outside_the_data_is_unanswerable_not_zero(self):
        self.assertEqual(self.ask('doanh thu tháng 1/2026 là bao nhiêu')[1]['type'], 'unanswerable')

    def test_an_unexplained_word_leaves_the_question_to_the_model(self):
        self.assertIsNone(self.ask('doanh thu tháng 3/2026 của khách vip là bao nhiêu'))
        self.assertIsNone(self.ask('doanh thu tháng 3/2026 trừ voucher là bao nhiêu'))
        self.assertIsNone(self.ask('doanh thu'))

    def test_two_periods_where_one_is_expected_are_not_guessed(self):
        self.assertIsNone(self.ask('doanh thu tháng 2/2026 và tháng 3/2026 là bao nhiêu'))

    def test_analysis_masks_dates_and_numbers(self):
        parsed = analyze('doanh thu ngày 05/03/2026 từ 1.500.000 đồng', self.AS_OF)
        self.assertEqual(parsed.words, ['doanhthu', '@t', 'tu', '@n', 'dong'])
        self.assertEqual(int(parsed.numbers[0]), 1500000)


if __name__ == '__main__':
    unittest.main()
