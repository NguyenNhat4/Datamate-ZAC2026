"""Publication safety checks using small invented sources."""
import csv
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

import duckdb

from tabular_agent.preparation import prepare_business, load_agent_schema


class PublicationSafety(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.business=self.root/'split'/'data'/'shop'
        self.business.mkdir(parents=True)
        self.rules=self.root/'split'/'rulebook.md'
        self.rules.write_text('Current rules have no verified sales policy.\n')
        self.calendar=self.root/'split'/'calendar.csv'
        self.calendar.write_text('date,lunar_day,lunar_month,lunar_year\n2025-01-01,2,12,2024\n')
        (self.business/'README.md').write_text('Amounts are in VND.\n')

    def write(self,name,rows):
        with (self.business/name).open('w',newline='') as stream:
            csv.writer(stream).writerows(rows)

    def prepare(self):
        return prepare_business(self.business,self.rules,self.calendar,output=self.root/'bundles')

    def test_blocked_partition_withholds_incomplete_family(self):
        self.write('orders_1.csv',[['order_id','order_date','status'],['1','2025-01-01','completed']])
        self.write('orders_2.csv',[['order_id','order_date','status'],['2','2025-01-02','completed','unexpected']])
        bundle=self.prepare()
        schema=load_agent_schema(bundle)
        self.assertNotIn('clean.orders',{r['name'] for r in schema.relations})
        profile=json.loads(bundle.profile_path.read_text())
        self.assertTrue(any(i['code']=='INCOMPLETE_PARTITIONS' for i in profile['issues']))

    def prepare_partitions(self):
        headers=['order_id','line_number','quantity','line_amount','customer_name']
        self.write('order_items_1.csv',[headers,['1','1','1','0.5','']])
        self.write('order_items_2.csv',[headers,['2','1','1','0.005','Sample']])
        return self.prepare()

    def test_compatible_partitions_combine_with_their_scales(self):
        bundle=self.prepare_partitions()
        schema=load_agent_schema(bundle)
        sources=[r for r in schema.relations if r['name'].startswith('clean.order_items')]
        self.assertEqual(len(sources),1)
        with duckdb.connect(str(bundle.database),read_only=True) as db:
            self.assertEqual(db.execute('SELECT SUM(line_amount),COUNT(*) FROM clean.order_items').fetchone(),(Decimal('0.505'),2))

    # Known gap, backlog B17 in docs/fix/README.md: a blank text cell is published as '' (parse_value keeps text as
    # written), so COUNT and GROUP BY on clean text columns count it. Fixing it changes the bundles, so it needs a
    # new bundle generation and a new measurement. This test has never passed (since d8d0352).
    @unittest.expectedFailure
    def test_blank_optional_text_is_missing(self):
        bundle=self.prepare_partitions()
        with duckdb.connect(str(bundle.database),read_only=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(customer_name) FROM clean.order_items').fetchone()[0],1)

    def test_schema_and_database_tampering_are_detected(self):
        self.write('products.csv',[['product_id','product_name'],['P1','Example']])
        bundle=self.prepare()
        bundle.schema_path.write_text('{}')
        with self.assertRaisesRegex(ValueError,'integrity'):
            load_agent_schema(bundle)
        with self.assertRaisesRegex(ValueError,'modified'):
            self.prepare()

    def test_calendar_is_queryable_and_preparation_keeps_future_events(self):
        self.write('orders.csv',[['order_id','order_date','status'],['1','2020-01-01','completed'],['2','2030-01-01','completed']])
        bundle=self.prepare()
        with duckdb.connect(str(bundle.database),read_only=True) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM clean.orders').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT lunar_year FROM clean.calendar').fetchone()[0],2024)


if __name__=='__main__':unittest.main()
