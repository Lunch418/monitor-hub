"""Данные для тестов интерфейса."""

TOML = '''
[targets.crl]
title = "CRL"
pam_user = "monitor"
password_env = "CRL_PW"
target_host = "192.0.2.10"
paths = ["/var/crl"]
style = "listing"
schedule = "daily 09:00"
command = "/get"

[targets.pg]
title = "Postgres"
pam_user = "monitor"
password_env = "PG_PW"
target_host = "192.0.2.30"
paths = ["/mnt/a", "/mnt/b"]
sudo = true
command = "/pg"
'''

LINES = [
    '-rw-r--r-- 1 root root 2085 Nov  9 06:54 issuing-ca.crl',
    '-rwxr-xr-x 1 root root 3505 Oct 10 10:12 update.sh',
    'drwxr-xr-x 2 root root 4096 Oct  1 02:00 archive',
]
