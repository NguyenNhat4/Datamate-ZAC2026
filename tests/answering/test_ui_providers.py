"""Exercise provider selection with an invented local model server."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import unittest

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[2] / 'streamlit_app.py'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == '/v1/models' else 404)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'data': [{'id': 'fixture-model'}]}).encode())

    def log_message(self, *args):
        pass


class ProviderUITests(unittest.TestCase):
    def test_switch_and_check_local_model_server(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            app = AppTest.from_file(str(APP)).run()
            self.assertFalse(app.exception)
            self.assertEqual([widget.label for widget in app.radio], ['Model provider'])
            app.text_input[1].set_value('fixture-model')
            app.text_input[2].set_value(f'http://127.0.0.1:{server.server_port}/v1')
            app.button[0].click().run()
            self.assertFalse(app.exception)
            self.assertIn('model name is available', app.success[0].value)
            app.text_input[1].set_value('missing-model')
            app.button[0].click().run()
            self.assertIn('not in its model list', app.warning[0].value)
            app.radio[0].set_value('OpenAI API').run()
            self.assertFalse(app.exception)
            self.assertTrue(any(widget.label == 'OpenAI model name' for widget in app.text_input))
            app.radio[0].set_value('Local server').run()
            self.assertFalse(app.exception)
            self.assertTrue(any(widget.label == 'Local model endpoint' for widget in app.text_input))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
