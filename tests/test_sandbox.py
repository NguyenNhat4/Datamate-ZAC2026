import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tabular_agent.sandbox import Sandbox
from tabular_agent.cli import main


@unittest.skipUnless(os.environ.get('RUN_SANDBOX_TESTS') == '1', 'opt-in real Linux sandbox tests')
class SandboxIntegration(unittest.TestCase):
    def setUp(self):
        import openpyxl
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.business = self.root / 'data' / 'shop'
        self.business.mkdir(parents=True)
        (self.business / 'sales.csv').write_text('item,amount\nA,10\nB,20\n')
        (self.business / 'README.md').write_text('amount is net revenue in VND')
        (self.root / 'calendar.csv').write_text('date\n2026-01-01\n')
        (self.root / 'rulebook.md').write_text('Sum amount for revenue.')
        (self.root / 'questions.jsonl').write_text(json.dumps({'id': 'q1', 'business_id': 'shop', 'question': 'Tổng doanh thu?', 'as_of_date': '2026-01-01'}) + '\n')
        (self.root / 'answers.jsonl').write_text('{"id":"q1","type":"number","answer":30}\n')
        book = openpyxl.Workbook()
        book.active.title = 'Month'
        book.active.append(['item', 'amount'])
        book.active.append(['A', 10])
        book.save(self.business / 'sales.xlsx')
        with sqlite3.connect(self.business / 'sales.sqlite') as db:
            db.execute('CREATE TABLE sales(item TEXT, amount INTEGER)')
            db.execute("INSERT INTO sales VALUES ('A',10)")
        self.sandbox = Sandbox()

    def tearDown(self):
        self.temp.cleanup()

    def call(self, action, **kwargs):
        return self.sandbox.run(action, self.business, self.root / 'calendar.csv', **kwargs)

    def test_real_csv_xlsx_sqlite_inspection(self):
        for action in [{'action': 'inspect', 'file': 'sales.csv'},
                       {'action': 'inspect', 'file': 'sales.xlsx'},
                       {'action': 'inspect', 'file': 'sales.xlsx', 'sheet': 'Month'},
                       {'action': 'inspect', 'file': 'sales.sqlite'},
                       {'action': 'inspect', 'file': 'sales.sqlite', 'table': 'sales'}]:
            result = self.call(action)
            self.assertTrue(result['ok'], result)
        result = self.call({'action': 'inspect', 'file': '../answers.jsonl'})
        self.assertFalse(result['ok'])

    def test_real_compute(self):
        result = self.call({'action': 'python', 'code': "import pandas as pd\nresult={'type':'number','answer':int(pd.read_csv(DATA_DIR/'sales.csv').amount.sum())}"})
        self.assertEqual(result['result'], {'type': 'number', 'answer': 30})
        relative = self.call({'action': 'python', 'code': "import pandas as pd\nresult=int(pd.read_csv('sales.csv').amount.sum())"})
        self.assertEqual(relative['result'], 30)

    def test_csv_bom_inspection_matches_actual_readers(self):
        (self.business / 'bom.csv').write_text('Giá bán,Số lượng\n100,2\n', encoding='utf-8-sig')
        inspected = self.call({'action': 'inspect', 'file': 'bom.csv'})
        profiled = self.call({'action': 'profile'})
        actual = self.call({'action': 'python', 'code': "import pandas as pd\na=read_table('bom.csv', header_row=0)\nb=pd.read_csv('bom.csv')\nresult={'helper':list(a.columns),'pandas':list(b.columns)}"})
        self.assertEqual(inspected['data']['columns'], ['Giá bán', 'Số lượng'])
        self.assertEqual(profiled['data']['files']['bom.csv']['columns'], inspected['data']['columns'])
        self.assertEqual(actual['result']['helper'], inspected['data']['columns'])
        self.assertEqual(actual['result']['pandas'], inspected['data']['columns'])

    def test_execution_reports_filtered_row_counts_without_row_values(self):
        code = "import pandas as pd\ndf=pd.read_csv('sales.csv')\nfiltered=df[df.amount > 10]"
        result = self.call({'action': 'python', 'code': code})
        self.assertTrue(result['ok'])
        self.assertEqual(result['variables']['df']['rows'], 2)
        self.assertEqual(result['variables']['filtered']['rows'], 1)
        self.assertNotIn('B', json.dumps(result['variables']))

    def test_alias_reads_actual_unicode_filename(self):
        name = 'Danh s├ích h├áng.csv'
        (self.business / name).write_text('item,amount\nA,10\nB,20\n')
        code = """import pandas as pd
first=read_table('F1',header_row=0)
second=pd.read_csv(FILES['F1'])
third=pd.read_csv('F1')
result={'a':len(first),'b':len(second),'c':len(third)}
"""
        result = self.call({'action': 'python', 'file_aliases': {'F1': name}, 'code': code})
        self.assertEqual(result['result'], {'a': 2, 'b': 2, 'c': 2})

        excel = self.call({'action': 'python', 'file_aliases': {'F1': 'sales.xlsx'},
                           'code': "import pandas as pd; result=len(pd.read_excel('F1',sheet_name='Month'))"})
        self.assertEqual(excel['result'], 1)
        original = self.call({'action': 'python', 'file_aliases': {'F1': 'sales.csv'},
                              'code': "import pandas as pd; result=len(pd.read_csv('sales.csv'))"})
        self.assertEqual(original['result'], 2)
        readonly = self.call({'action': 'python', 'file_aliases': {'F1': 'sales.csv'},
                              'code': "from pathlib import Path; Path('F1').write_text('changed')"})
        self.assertFalse(readonly['ok'])
        self.assertIn('A,10', (self.business / 'sales.csv').read_text())

    def test_numpy_results_and_invalid_serialization(self):
        result = self.call({'action': 'python', 'code': "import numpy as np\nresult={'type':'list','answer':[np.int64(7),np.float64(2.5)]}"})
        self.assertEqual(result['result'], {'type': 'list', 'answer': [7, 2.5]})
        for code in ['result=float("nan")', 'result=object()']:
            self.assertFalse(self.call({'action': 'python', 'code': code})['ok'])

    def test_profile_and_unknown_sheet(self):
        result = self.call({'action': 'profile'})
        self.assertTrue(result['ok'], result)
        xlsx = result['data']['files']['sales.xlsx']
        self.assertEqual(xlsx['sheets'], ['Month'])
        self.assertEqual(xlsx['first_sheet_example']['sample_rows'][0], ['item', 'amount'])
        result = self.call({'action': 'inspect', 'file': 'sales.xlsx', 'sheet': 'invented'})
        self.assertEqual(result['data']['sheets'], ['Month'])

    def test_readers_handle_header_offset_dates_and_numbers_without_guessing(self):
        import openpyxl
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.append(['Sales extract'])
        sheet.append([])
        sheet.append(['date', 'amount', 'item'])
        sheet.append(['01/06/2025', '51,000', 'A'])
        sheet.append(['2025-06-02T13:23:12', '1200', 'B'])
        book.save(self.business / 'messy.xlsx')
        code = """df=read_table('messy.xlsx', header_row=2)
dates=parse_dates(df['date'], dayfirst=True)
amounts=parse_numbers(df['amount'], decimal_separator='.', thousands_separator=',')
result={'dates':[str(x.date()) for x in dates], 'sum':sum(amounts)}
"""
        result = self.call({'action': 'python', 'code': code})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result'], {'dates': ['2025-06-01', '2025-06-02'], 'sum': 52200})
        mixed_numeric = self.call({'action': 'python', 'code': "result={'values':parse_numbers([1234.5,'1.234,50'],decimal_separator=',',thousands_separator='.').tolist()}"})
        self.assertEqual(mixed_numeric['result'], {'values': [1234.5, 1234.5]})
        (self.business / 'offset.csv').write_text('export\n\nitem,amount\nA,10\nB,20\n')
        csv_result = self.call({'action': 'python', 'code': "df=read_table('offset.csv',delimiter=','); result={'columns':list(df.columns),'rows':len(df)}"})
        self.assertEqual(csv_result['result'], {'columns': ['item', 'amount'], 'rows': 2})
        bad = self.call({'action': 'python', 'code': "parse_numbers(['12,34'], decimal_separator='.', thousands_separator=',')"})
        self.assertFalse(bad['ok'])
        bad_date = self.call({'action': 'python', 'code': "parse_dates(['01/02'], dayfirst=True)"})
        self.assertFalse(bad_date['ok'])
        escaped = self.call({'action': 'python', 'code': "read_table('../answers.jsonl')"})
        self.assertFalse(escaped['ok'])

    def test_join_cardinality_unmatched_keys_and_units(self):
        code = """import pandas as pd
left=pd.DataFrame({'id':[1,2]})
right=pd.DataFrame({'id':[1,2], 'amount':[10,20]})
result={'rows':len(checked_join(left,right,on='id')), 'units':split_units(80,6)}
"""
        result = self.call({'action': 'python', 'code': code})
        self.assertEqual(result['result'], {'rows': 2, 'units': [13, 2]})
        duplicate = """import pandas as pd
checked_join(pd.DataFrame({'id':[1]}),pd.DataFrame({'id':[1,1]}),on='id')
"""
        self.assertFalse(self.call({'action': 'python', 'code': duplicate})['ok'])
        unmatched = """import pandas as pd
checked_join(pd.DataFrame({'id':[1,2]}),pd.DataFrame({'id':[1]}),on='id',how='inner')
"""
        self.assertFalse(self.call({'action': 'python', 'code': unmatched})['ok'])

    def test_read_only_and_no_answers(self):
        result = self.call({'action': 'python', 'code': "(DATA_DIR/'sales.csv').write_text('changed')"})
        self.assertFalse(result['ok'])
        self.assertIn('A,10', (self.business / 'sales.csv').read_text())
        result = self.call({'action': 'python', 'code': "from pathlib import Path\nresult={'home':Path('/home/nhatnm/.ssh').exists(),'answers':Path('/answers.jsonl').exists()}"})
        self.assertEqual(result['result'], {'home': False, 'answers': False})

    def test_no_network_and_timeout(self):
        result = self.call({'action': 'python', 'code': "import socket\nsocket.create_connection(('1.1.1.1',443),timeout=1)"})
        self.assertFalse(result['ok'])
        result = self.call({'action': 'python', 'code': 'while True: pass'}, timeout=.2)
        self.assertFalse(result['ok'])
        self.assertIn('timed out', result['error'])

    def test_output_symlink_does_not_read_host(self):
        secret = self.root / 'secret'
        secret.write_text('HOST_SECRET')
        code = f"import os\nos.unlink('/tmp/observation.json')\nos.symlink({str(secret)!r}, '/tmp/observation.json')"
        result = self.call({'action': 'python', 'code': code})
        self.assertFalse(result['ok'])
        self.assertNotIn('HOST_SECRET', json.dumps(result))

    def test_cli_bench_with_scripted_model(self):
        # Real CLI, real worker, synthetic data, scripted model: NOT a quality benchmark.
        class Model:
            timeout = 10
            model = 'scripted-test'
            def __init__(self, *a, **kw):
                self.n = 0
            def check(self):
                return {'model': self.model}
            def complete(self, messages, **kw):
                assert '"answer": 30' not in messages[1]['content']
                self.n += 1
                actions = [
                    {'action':'plan', 'plan': {'metric':'revenue', 'period':None, 'sources':['sales.csv'],
                     'grain':'item', 'filters':[], 'unit':'VND', 'risk':'normal'}},
                    {'action':'python', 'code':"import pandas as pd\ndf=pd.read_csv(DATA_DIR/'sales.csv')\nresult={'type':'number','answer':int(df.amount.sum())}\nevidence={'sources':['sales.csv'],'row_count':len(df),'checks':{'unit':'VND'}}"},
                    {'action':'verify', 'code':"import pandas as pd\nrows=pd.read_csv(DATA_DIR/'sales.csv')\nresult={'type':'number','answer':sum(int(x) for x in rows.amount)}\nevidence={'sources':['sales.csv'],'row_count':len(rows),'checks':{'unit':'VND'}}"},
                    {'action':'review', 'verdict':'accept', 'findings':'Checked sum and unit'}]
                return json.dumps(actions[self.n - 1]), {}
        output = self.root.parent / (self.root.name + '-run')
        try:
            with patch('tabular_agent.cli.LocalModel', Model), patch('sys.argv', ['cli', 'bench', '--split', str(self.root), '--output', str(output)]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 0)
            report = json.loads((output / 'report.json').read_text())
            self.assertEqual(report['pa'], 1)
            self.assertEqual(report['written'], 1)
            self.assertEqual(json.loads((output / 'logs/000000.json').read_text())['id'], 'q1')
        finally:
            import shutil
            if output.exists():
                shutil.rmtree(output)
