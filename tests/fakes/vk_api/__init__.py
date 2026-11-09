"""
Фальшивый vk_api для сквозного теста демона: ничего не отправляет в сеть, а пишет
каждое действие строкой JSON в файл из переменной E2E_VK_LOG.
"""
import json
import os


def _log(**event):
    with open(os.environ['E2E_VK_LOG'], 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + '\n')


class _Http:
    def request(self, method, url, **kwargs):
        raise RuntimeError('сеть в тесте запрещена')


class ApiError(Exception):
    """Ошибка API ВК: у настоящей есть .code, по нему monitor-hub решает, повторять ли."""

    def __init__(self, code, text):
        super().__init__(f'[{code}] {text}')
        self.code = code


def _next_failure():
    """E2E_VK_FAILS="100,100,5" - коды ошибок для первых вызовов messages.send."""
    plan = [int(x) for x in os.environ.get('E2E_VK_FAILS', '').split(',') if x]
    state = os.environ['E2E_VK_LOG'] + '.fails'
    done = int(open(state).read()) if os.path.exists(state) else 0
    if done >= len(plan):
        return None
    with open(state, 'w') as fh:
        fh.write(str(done + 1))
    return plan[done]


class _Messages:
    def send(self, **kwargs):
        code = _next_failure()
        if code is not None:
            _log(kind='send_failed', code=code)
            raise ApiError(code, 'One of the parameters specified was missing or invalid: photo is undefined'
                           if code == 100 else 'simulated VK failure')
        _log(kind='send', **kwargs)


class _Api:
    messages = _Messages()


class VkApi:
    def __init__(self, token=None, **kwargs):
        self.http = _Http()

    def get_api(self):
        return _Api()


class VkUpload:
    counter = 0

    def __init__(self, session):
        self.http = _Http()

    def photo_messages(self, photos, peer_id=None):
        paths = [photos] if isinstance(photos, str) else list(photos)
        result = []
        for path in paths:
            with open(path, 'rb') as fh:
                head = fh.read(8)
            _log(kind='upload', path=os.path.basename(path), png=head.startswith(b'\x89PNG'),
                 size=os.path.getsize(path))
            VkUpload.counter += 1
            result.append({'owner_id': 1, 'id': VkUpload.counter})
        return result
