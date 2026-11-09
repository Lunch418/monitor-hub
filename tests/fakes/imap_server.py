"""Минимальный IMAP-сервер (без TLS) для проверок: LOGIN, SELECT, SEARCH, FETCH, LOGOUT."""
import socketserver
import threading


class FakeImap:
    def __init__(self, letters, user='mailuser', password='mailpass', folders=('INBOX',)):
        self.letters = letters          # список bytes: готовые письма
        self.user, self.password, self.folders = user, password, tuple(folders)
        self.log: list[str] = []
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b'* OK fake imap ready\r\n')
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode('utf-8', 'replace').rstrip('\r\n')
                    outer.log.append(line)
                    tag, _, rest = line.partition(' ')
                    cmd, _, args = rest.partition(' ')
                    cmd = cmd.upper()
                    if cmd == 'CAPABILITY':
                        self._say('* CAPABILITY IMAP4rev1', f'{tag} OK CAPABILITY done')
                    elif cmd == 'LOGIN':
                        parts = args.replace('"', '').split(' ')
                        ok = parts[:2] == [outer.user, outer.password]
                        self._say(f'{tag} OK LOGIN done' if ok else f'{tag} NO [AUTHENTICATIONFAILED] bad login')
                    elif cmd in ('SELECT', 'EXAMINE'):
                        name = args.strip('"')
                        if name in outer.folders:
                            self._say(f'* {len(outer.letters)} EXISTS', f'{tag} OK [READ-ONLY] SELECT done')
                        else:
                            self._say(f'{tag} NO no such folder')
                    elif cmd == 'SEARCH':
                        numbers = ' '.join(str(i + 1) for i in range(len(outer.letters)))
                        self._say(f'* SEARCH {numbers}', f'{tag} OK SEARCH done')
                    elif cmd == 'FETCH':
                        number = int(args.split(' ')[0])
                        body = outer.letters[number - 1]
                        self.wfile.write(f'* {number} FETCH (BODY[] {{{len(body)}}}\r\n'.encode() + body + b')\r\n')
                        self._say(f'{tag} OK FETCH done')
                    elif cmd == 'LOGOUT':
                        self._say('* BYE bye', f'{tag} OK LOGOUT done')
                        return
                    else:
                        self._say(f'{tag} BAD unknown command')

            def _say(self, *lines):
                for text in lines:
                    self.wfile.write(text.encode() + b'\r\n')

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
