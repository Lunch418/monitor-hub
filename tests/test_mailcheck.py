import logging
from datetime import date, datetime
from email.message import EmailMessage

from monitor import mailcheck as mc

LOG = logging.getLogger('test')


def letter(subject, body='', date_header='Mon, 10 Nov 2026 03:21:00 +0000'):
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = 'backup@example.org'
    msg['Date'] = date_header
    msg.set_content(body or 'text')
    return msg.as_bytes()


class FakeImap:
    """Минимальный IMAP: одна папка, письма по номерам."""
    def __init__(self, letters, folders=('INBOX',)):
        self.letters = letters
        self.folders = folders
        self.selected = []
        self.criteria = []
        self.closed = False

    def login(self, user, password):
        self.user = user

    def select(self, folder, readonly=True):
        self.selected.append(folder)
        return ('OK' if folder in self.folders else 'NO'), [b'']

    def search(self, charset, criteria):
        self.criteria.append(criteria)
        return 'OK', [b' '.join(str(i + 1).encode() for i in range(len(self.letters)))]

    def fetch(self, num, spec):
        return 'OK', [(b'1 (BODY[] {1}', self.letters[int(num) - 1]), b')']

    def logout(self):
        self.closed = True


def query(**kw):
    return mc.MailQuery(host='mail.example.org', port=993, user='u', password='p', **kw)


def run(letters, expected, **kw):
    fake = FakeImap(letters, kw.pop('folders', ('INBOX',)))
    start, end = datetime(2026, 11, 9, 18), datetime(2026, 11, 10, 12)
    res = mc.fetch_results(query(**kw), expected, start, end, LOG, connect=lambda h, p: fake)
    return res, fake


def test_rules_valid_and_invalid():
    assert mc.check_rule('mon,tue') is None
    assert mc.check_rule('daily') is None
    assert mc.check_rule('sun#1, sun#3') is None
    assert mc.check_rule('') is not None
    assert 'xyz' in mc.check_rule('xyz')
    assert mc.check_rule('sun#9') is not None
    assert mc.check_rule('sun#x') is not None


def test_expected_names_weekday_refers_to_start_day_and_hash_to_check_day():
    expect = {'a': 'mon', 'b': 'daily', 'c': 'sun#1', 'd': 'tue'}
    # 10.11.2026 - вторник: бэкап понедельника (a) завершился этой ночью
    assert mc.expected_names(expect, date(2026, 11, 10)) == ['a', 'b']
    # 01.11.2026 - первое воскресенье ноября
    assert mc.expected_names(expect, date(2026, 11, 1)) == ['b', 'c']


def test_night_window_and_date_arg():
    start, end = mc.night_window(date(2026, 11, 10), 18, 12)
    assert (start, end) == (datetime(2026, 11, 9, 18), datetime(2026, 11, 10, 12))
    today = date(2026, 11, 10)
    assert mc.parse_date_arg('', today) == today
    assert mc.parse_date_arg('вчера', today) == date(2026, 11, 9)
    assert mc.parse_date_arg('26.07', today) == date(2026, 7, 26)
    assert mc.parse_date_arg('26.07.2025', today) == date(2025, 7, 26)
    assert mc.parse_date_arg('32.13', today) == today
    assert mc.parse_date_arg('мусор', today) == today


def test_status_and_device():
    assert mc.parse_status('Backup completed successfully: app-01 (x)') == 'ok'
    assert mc.parse_status('Ошибка резервного копирования: app-01') == 'fail'
    assert mc.parse_status('Успешно, но с предупреждением') == 'fail'   # ошибка сильнее
    assert mc.parse_status('Привет') is None
    assert mc.find_device('Device: app-01 done') == 'app-01'
    assert mc.find_device('Backup: db-02 (daily)') == 'db-02'
    import re
    assert mc.find_device('job=web-3', re.compile(r'job=(\S+)')) == 'web-3'
    assert mc.find_device('nothing here') is None


def test_finish_duration_and_header_fallback():
    body = 'Время завершения Mon Nov 10 03:21:05 2026 Продолжительность 01:12:30'
    assert mc.parse_finish(body) == datetime(2026, 11, 10, 3, 21, 5)
    assert mc.parse_duration(body) == '1ч12м'
    assert mc.parse_duration('Продолжительность 00:05:09') == '5:09'
    assert mc.parse_duration('нет') is None
    assert mc.parse_finish('нет', 'Mon, 10 Nov 2026 03:21:00 +0000') is not None
    assert mc.parse_finish('нет', 'не дата') is None
    assert mc.parse_finish('нет') is None


def test_imap_utf7():
    assert mc.imap_utf7_encode('INBOX') == 'INBOX'
    assert mc.imap_utf7_encode('A&B') == 'A&-B'
    encoded = mc.imap_utf7_encode('Входящие')
    assert encoded.startswith('&') and encoded.endswith('-')
    import base64
    payload = encoded[1:-1].replace(',', '/')
    assert base64.b64decode(payload + '=' * (-len(payload) % 4)).decode('utf-16-be') == 'Входящие'
    assert mc.imap_utf7_encode('a Входящие b').startswith('a &')


def test_fetch_results_picks_failure_then_latest(tmp_path):
    letters = [
        letter('Backup completed successfully: app-01 (x)', 'Время завершения Mon Nov 10 03:10:00 2026'),
        letter('Backup completed successfully: app-01 (x)', 'Время завершения Mon Nov 10 04:10:00 2026'),
        letter('Backup failed: db-01 (x)', 'Время завершения Mon Nov 10 03:30:00 2026'),
        letter('Backup completed successfully: db-01 (x)', 'Время завершения Mon Nov 10 05:30:00 2026'),
        letter('Backup completed successfully: other (x)', 'Время завершения Mon Nov 10 03:00:00 2026'),
        letter('Backup completed successfully: app-02 (x)', 'Время завершения Sun Nov 08 03:00:00 2026'),
    ]
    res, fake = run(letters, ['app-01', 'db-01', 'app-02'], mail_from='backup')
    assert res['app-01']['finish'] == '04:10'        # позднее из двух успешных
    assert res['db-01']['status'] == 'fail'           # ошибка сильнее позднего успеха
    assert 'app-02' not in res and 'other' not in res  # вне окна и не ожидается
    assert 'FROM "backup"' in fake.criteria[0]
    assert fake.closed


def test_fetch_results_saves_eml_and_skips_missing_folder(tmp_path):
    letters = [letter('Backup completed successfully: app-01 (x)', 'Время завершения Mon Nov 10 03:10:00 2026')]
    res, _ = run(letters, ["app-01"], folders=("INBOX",))
    assert 'app-01' in res
    fake2 = FakeImap(letters)
    start, end = datetime(2026, 11, 9, 18), datetime(2026, 11, 10, 12)
    res = mc.fetch_results(query(folders=('Нет', 'INBOX')), ['app-01'], start, end, LOG,
                           save_dir=str(tmp_path), connect=lambda h, p: fake2)
    assert 'app-01' in res
    assert len(fake2.selected) == 2
    assert list(tmp_path.glob('*/app-01_ok.eml'))


def test_report_and_split():
    results = {'a': {'status': 'ok', 'finish': '03:10', 'dur': '5:09'}, 'b': {'status': 'fail'}}
    text = mc.build_report('Бэкапы', date(2026, 11, 10), ['a', 'b', 'c'], results)
    assert '[ok] a (в 03:10, за 5:09)' in text and '[!] b - ошибка' in text and '[?] c - письма нет' in text
    assert 'Итог: 1 из 3 успешно' in text
    assert 'не запланированы' in mc.build_report('Бэкапы', date(2026, 11, 10), [], {})
    assert mc.split_message('abc') == ['abc']
    long = '\n'.join(['x' * 30] * 10)
    parts = mc.split_message(long, 100)
    assert all(len(p) <= 100 for p in parts) and '\n'.join(parts).replace('\n', '') == long.replace('\n', '')
    assert mc.split_message('y' * 250, 100) == ['y' * 100, 'y' * 100, 'y' * 50]


def test_body_text_prefers_plain_and_strips_html():
    both = EmailMessage()
    both.set_content('plain')
    both.add_alternative('<p>html</p>', subtype='html')
    assert mc.body_text(both) == 'plain'
    only_html = EmailMessage()
    only_html.set_content('<p>hello&nbsp;<b>1</b></p>', subtype='html')
    assert mc.body_text(only_html) == 'hello 1'


def test_body_text_nested_multipart_and_base64():
    import base64
    import email

    inner = ('Content-Type: multipart/alternative; boundary="in"\r\n\r\n'
             '--in\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n'
             + base64.b64encode('Device: web-01\nвсё хорошо'.encode()).decode() + '\r\n'
             '--in\r\nContent-Type: text/html\r\n\r\n<b>html</b>\r\n--in--\r\n')
    raw = ('Subject: nested\r\nContent-Type: multipart/mixed; boundary="out"\r\n\r\n'
           '--out\r\n' + inner + '--out\r\nContent-Type: application/octet-stream\r\n\r\nxx\r\n--out--\r\n')
    text = mc.body_text(email.message_from_string(raw))
    assert 'Device: web-01' in text and 'всё хорошо' in text and 'html' not in text
