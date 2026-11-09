#!/usr/bin/env python3
"""Скриншот интерфейса для README (SVG) на выдуманных данных, без сети.

    python tools/make_screenshot.py docs/screenshot.svg
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEMO = '''
[targets.crl]
title = "CRL"
pam_user = "monitor"
password_env = "CRL_PAM_PASSWORD"
target_host = "192.0.2.10"
paths = ["/var/lib/pki/crl"]
style = "listing"
schedule = "daily 09:00"
command = "/get"

[targets.backup]
title = "Бэкапы файлов"
pam_user = "monitor"
password_env = "BK_PAM_PASSWORD"
target_host = "192.0.2.20"
paths = ["/mnt/backup"]
schedule = "mon 09:00"
command = "/backup"

[targets.pgbackup]
title = "Postgres"
pam_user = "monitor"
password_env = "PG_PAM_PASSWORD"
target_host = "192.0.2.30"
paths = ["/mnt/bkp/backup_0", "/mnt/bkp"]
style = "backups-sizes"
sudo = true
schedule = "wed 10:00"
command = "/pgbackup"
'''

LISTING = [
    'drwxr-xr-x  2 root root 4096 Nov  9 06:00 .',
    'drwxr-xr-x 18 root root 4096 Sep  5  2025 ..',
    '-rw-r--r--  1 root root 2085 Nov  9 06:54 issuing-ca.crl',
    '-rw-r--r--  1 root root 1904 Nov  9 06:54 issuing-ca-2.crl',
    '-rw-r--r--  1 root root 9061 Nov  8 07:33 root-ca.crl',
    '-rw-r--r--  1 root root 7730 Nov  8 07:33 policy-ca.crl',
    '-rwxr-xr-x  1 root root 3505 Oct 10 10:12 update-crl.sh',
    'drwxr-xr-x  2 root root 4096 Oct  1 02:00 archive',
]


async def main(out_svg: str) -> None:
    tmp = Path(tempfile.mkdtemp())
    (tmp / 'targets.toml').write_text(DEMO, encoding='utf-8')
    (tmp / '.env').write_text('CRL_PAM_PASSWORD=x\nBK_PAM_PASSWORD=x\n', encoding='utf-8')
    os.environ.update(STATE_DIR=str(tmp / 'state'), SAVE_DIR=str(tmp / 'shots'), PAM_HOST='pam.example.org',
                      NO_OPEN_SCREENSHOT='1', HOME=str(tmp))
    from monitor import engine, runlog, tui
    runlog.record(tmp / 'state', 'backup', True, 'картинок: 1')
    runlog.record(tmp / 'state', 'pgbackup', False, 'Не удалось пройти этап на jump-хосте')
    engine.Engine.fetch_listings = lambda self, target: dict.fromkeys(target.paths, LISTING)

    app = tui.HubApp(tmp / 'targets.toml', tmp, env_path=tmp / '.env')
    async with app.run_test(size=(160, 44)) as pilot:
        await pilot.pause()
        app.action_check()
        await pilot.pause(1.5)
        Path(out_svg).write_text(app.export_screenshot(title='monitor-hub'), encoding='utf-8')


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else 'docs/screenshot.svg'))
