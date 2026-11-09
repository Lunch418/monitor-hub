#!/usr/bin/env bash
# Установка monitor-hub как службы systemd (запуск от root):
#
#   sudo bash deploy/install.sh 2>&1 | tee /tmp/monitor-hub-install.log
#
# Что делает: проверяет конфигурацию (monitor-hub validate), останавливает демон,
# запущенный вручную, ставит юнит с путём этого каталога и владельцем файлов,
# включает и запускает службу. Старый юнит сохраняется рядом (.bak-<дата>).
#
# Для проверок: DRY_RUN=1 UNIT_DIR=<каталог> SYSTEMCTL=<команда> (root не нужен).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${RUN_USER:-$(stat -c %U "$DIR")}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
DRY_RUN="${DRY_RUN:-}"
PY="$DIR/venv/bin/python"

die() { echo "ОШИБКА: $*" >&2; exit 1; }

[ -n "$DRY_RUN" ] || [ "$(id -u)" -eq 0 ] || die "запустите от root: sudo bash deploy/install.sh"
case "$DIR" in *" "*) die "в пути к каталогу есть пробел: $DIR" ;; esac
[ "$RUN_USER" != "root" ] || die "каталог принадлежит root; задайте RUN_USER=<пользователь>"
[ -x "$PY" ] || die "нет $DIR/venv (создайте: python3 -m venv venv && venv/bin/pip install -r requirements.txt)"
[ -f "$DIR/.env" ] || die "нет файла $DIR/.env (образец: .env.example)"
[ -f "$DIR/targets.toml" ] || die "нет файла $DIR/targets.toml (образец: targets.example.toml)"

echo "Каталог: $DIR"
echo "Пользователь: $RUN_USER"

# 1. Конфигурация должна быть в порядке до установки.
if [ -z "$DRY_RUN" ]; then
    sudo -u "$RUN_USER" "$PY" "$DIR/monitor-hub" validate || die "конфигурация неверна, служба не ставится"
    # 2. Остановить демон, запущенный вручную, чтобы не было двух копий.
    sudo -u "$RUN_USER" "$PY" "$DIR/monitor-hub" stop || true
fi

# 3. Юнит (старый - в бэкап).
UNIT="$UNIT_DIR/monitor-hub.service"
if [ -f "$UNIT" ]; then
    cp -p "$UNIT" "$UNIT.bak-$(date +%Y%m%d-%H%M%S)"
fi
sed -e "s|^User=.*|User=$RUN_USER|" -e "s|/opt/monitor-hub|$DIR|g" \
    "$DIR/deploy/monitor-hub.service" > "$UNIT"
chmod 644 "$UNIT"
echo "Юнит записан: $UNIT"

# 4. Включить и запустить.
$SYSTEMCTL daemon-reload
$SYSTEMCTL enable --now monitor-hub
sleep "${START_WAIT:-3}"
printf 'monitor-hub: %s\n' "$($SYSTEMCTL is-active monitor-hub || true)"

echo
echo "Журнал:  journalctl -u monitor-hub -n 30 --no-pager"
echo "Откат:   sudo systemctl disable --now monitor-hub"
