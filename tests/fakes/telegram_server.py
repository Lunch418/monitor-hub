"""Поддельный Telegram Bot API: принимает вызовы, отдаёт заготовленные обновления и ошибки."""
import json
import threading
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


class FakeTelegram:
    def __init__(self, token='123:abc'):
        self.token = token
        self.calls: list[dict] = []          # {'method', 'fields', 'files'}
        self.updates: list[dict] = []        # {'update_id', 'message'}
        self.errors: dict[str, list] = {}    # method -> [(http_code, body_dict), ...] по одному на вызов
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length)
                prefix = f'/bot{outer.token}/'
                if not self.path.startswith(prefix):
                    return self._reply(404, {'ok': False, 'error_code': 404, 'description': 'Not Found'})
                method = self.path[len(prefix):]
                fields, files = {}, []
                ctype = self.headers.get('Content-Type', '')
                if ctype.startswith('multipart/'):
                    msg = BytesParser(policy=default).parsebytes(
                        b'Content-Type: ' + ctype.encode() + b'\r\n\r\n' + body)
                    for part in msg.iter_parts():
                        name = part.get_param('name', header='content-disposition')
                        if part.get_filename():
                            files.append((name, len(part.get_payload(decode=True) or b'')))
                        else:
                            fields[name] = (part.get_payload(decode=True) or b'').decode('utf-8')
                else:
                    fields = {k: v[0] for k, v in parse_qs(body.decode()).items()}
                with outer.lock:
                    outer.calls.append({'method': method, 'fields': fields, 'files': files})
                    queued = outer.errors.get(method)
                    if queued:
                        code, payload = queued.pop(0)
                        return self._reply(code, payload)
                    if method == 'getUpdates':
                        return self._reply(200, {'ok': True, 'result': outer._updates(fields)})
                self._reply(200, {'ok': True, 'result': {'message_id': 1}})

            def _reply(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _updates(self, fields):
        offset = int(fields.get('offset', 0))
        if offset < 0:
            return self.updates[-1:]
        out = [u for u in self.updates if u['update_id'] >= offset]
        if not out:
            return []
        return out

    def push(self, chat_id, from_id, text):
        with self.lock:
            number = len(self.updates) + 100
            self.updates.append({'update_id': number, 'message': {
                'message_id': number, 'chat': {'id': chat_id}, 'from': {'id': from_id}, 'text': text}})

    def methods(self, name):
        return [c for c in self.calls if c['method'] == name]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
