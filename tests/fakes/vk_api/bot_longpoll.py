"""
Фальшивый long poll: события берутся из JSON-файла E2E_EVENTS (список
{"text", "peer_id", "from_id"}), после последнего поток засыпает, как настоящий
long poll без новых сообщений.
"""
import json
import os
import time
import types


class VkBotEventType:
    MESSAGE_NEW = 'message_new'


class VkBotLongPoll:
    def __init__(self, session, group_id):
        self.group_id = group_id

    def listen(self):
        path = os.environ.get('E2E_EVENTS')
        events = json.load(open(path, encoding='utf-8')) if path and os.path.exists(path) else []
        for item in events:
            yield types.SimpleNamespace(
                type=VkBotEventType.MESSAGE_NEW,
                obj=types.SimpleNamespace(message={
                    'peer_id': item['peer_id'], 'from_id': item['from_id'], 'text': item['text']}))
        while True:
            time.sleep(3600)
