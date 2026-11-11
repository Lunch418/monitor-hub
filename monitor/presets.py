"""
presets.py - готовые команды для просмотра состояния машины (живой просмотр и команды беседы).

Настоящий htop рисует экран терминала, поэтому берутся пакетные аналоги (top -b и т.п.).
Модуль без зависимостей: его читают и config, и engine.
"""

from __future__ import annotations

LIVE_PRESETS = {
    'top': ('top -b -n 1 -w 160 | head -n 30', 'нагрузка и самые прожорливые процессы'),
    'cpu': ('ps -eo pid,user,pcpu,pmem,etime,comm --sort=-pcpu | head -n 15', 'процессы по CPU'),
    'mem': ('free -h; echo; ps -eo pid,user,pmem,rss,comm --sort=-pmem | head -n 12', 'память и процессы по ней'),
    'df': ('df -hT -x tmpfs -x devtmpfs -x squashfs', 'свободное место на дисках'),
    'net': ('ss -s; echo; ss -tn state established | head -n 15', 'сетевые соединения'),
    'uptime': ('uptime; echo; w -h | head -n 15', 'аптайм и кто вошёл'),
}
LIVE_MAX_COMMAND = 300

# Слово в беседе (/disk) -> набор. Только эти слова и свои команды из конфигурации;
# произвольный текст из беседы на машине не выполняется.
CHAT_WORDS = {
    'uptime': 'uptime', 'top': 'top', 'cpu': 'cpu', 'mem': 'mem', 'disk': 'df', 'df': 'df', 'net': 'net',
}

# Слова, которые нельзя использовать как имя своей команды в targets.toml
BUILTIN_WORDS = ('targets', 'run', 'help', *CHAT_WORDS)
