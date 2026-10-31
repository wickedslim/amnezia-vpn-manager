#!/usr/bin/env bash
# Install the manager only. Existing VPN containers are never restarted.
set -Eeuo pipefail
SOURCE="$(cd "$(dirname "$0")" && pwd)"
MODE=fresh
CHECK_ONLY=false
DOMAIN=""
PUBLIC_IP=""
INTERFACE=preserve
SERVICE="${AWG_SERVICE_NAME:-awg_bot}"
TASK_PYTHON="${TASK_PYTHON:-python3}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --upgrade) MODE=upgrade; shift ;;
        --check) CHECK_ONLY=true; shift ;;
        --bot-only|--no-miniapp) INTERFACE=bot; shift ;;
        --domain) DOMAIN="${2:?Укажите домен}"; shift 2 ;;
        --ip) PUBLIC_IP="${2:?Укажите публичный IP}"; shift 2 ;;
        --help|-h)
            echo 'sudo bash install.sh [--bot-only | --domain vpn.example.com --ip PUBLIC_IP]'
            echo 'sudo bash update.sh'
            echo 'AWG_INSTALL_DIR and AWG_SERVICE_NAME override installation defaults.'
            exit 0 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 1 ;;
    esac
done
if [[ -n "$DOMAIN" && "$INTERFACE" == bot ]]; then
    echo '--bot-only нельзя совмещать с --domain.' >&2; exit 1
fi
if [[ "$MODE" == fresh && -z "$DOMAIN" && "$INTERFACE" == preserve ]]; then
    if [[ -t 0 ]]; then
        read -r -p 'Настроить Mini App вместе с ботом? [y/N] ' answer
        if [[ "$answer" =~ ^[yYдД]$ ]]; then
            read -r -p 'Домен Mini App: ' DOMAIN
            [[ -n "$DOMAIN" ]] || { echo 'Домен не задан.' >&2; exit 1; }
            read -r -p 'Публичный IP (Enter для адресов сетевых интерфейсов): ' PUBLIC_IP
        else INTERFACE=bot; fi
    else INTERFACE=bot; fi
fi
[[ -z "$DOMAIN" ]] || INTERFACE=miniapp
[[ "$(uname -s)" == Linux && ( "$EUID" -eq 0 || "$CHECK_ONLY" == true ) ]] || { echo 'Нужны Linux, systemd и root (sudo).' >&2; exit 1; }
[[ "$SERVICE" =~ ^[a-zA-Z0-9_.@-]+$ ]] || exit 1
command -v systemctl >/dev/null
LOAD="$(systemctl show "$SERVICE" -p LoadState --value 2>/dev/null || true)"
EXISTS=false
[[ -n "$LOAD" && "$LOAD" != not-found ]] && EXISTS=true
if [[ "$MODE" == fresh && "$EXISTS" == true ]]; then
    echo 'Служба уже существует. Для обновления используйте update.sh.' >&2; exit 1
fi
if [[ "$MODE" == upgrade && "$EXISTS" != true ]]; then
    echo 'Служба не найдена. Проверьте AWG_SERVICE_NAME или используйте install.sh.' >&2; exit 1
fi
WORKDIR="$(systemctl show "$SERVICE" -p WorkingDirectory --value 2>/dev/null || true)"
if [[ "$MODE" == upgrade && -z "${AWG_INSTALL_DIR:-}" && "$WORKDIR" != */awg ]]; then
    echo 'Не удалось определить каталог. Задайте AWG_INSTALL_DIR явно.' >&2; exit 1
fi
TARGET="${AWG_INSTALL_DIR:-${WORKDIR%/awg}}"
TARGET="${TARGET:-/opt/amnezia-manager}"
[[ "$TARGET" =~ ^/[a-zA-Z0-9_./-]+$ && "$TARGET" != / && "$TARGET" != /root && "$TARGET" != /home ]] || { echo 'Небезопасный или неподдерживаемый путь.' >&2; exit 1; }
if [[ "$MODE" == fresh && -d "$TARGET" && -n "$(ls -A "$TARGET")" ]]; then
    echo 'Для новой установки нужен пустой каталог.' >&2; exit 1
fi
[[ "$SOURCE" != "$TARGET" ]] || { echo 'Запустите установщик из отдельного checkout/архива, не из рабочей папки.' >&2; exit 1; }
for name in awg/manager awg/bot_manager.py miniapp requirements.txt LICENSE VERSION; do
    [[ -e "$SOURCE/$name" ]] || { echo "Неполный релиз: нет $name" >&2; exit 1; }
done
if [[ "$CHECK_ONLY" == true ]]; then
    "$TASK_PYTHON" -c 'import sys; assert sys.version_info >= (3,11), "Нужен Python 3.11+"'
    echo "Проверка без изменений: режим=$MODE каталог=$TARGET интерфейс=$INTERFACE служба=$SERVICE"
    exit 0
fi
if command -v apt-get >/dev/null; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv ca-certificates curl
fi
"$TASK_PYTHON" -c 'import sys; assert sys.version_info >= (3,11), "Нужен Python 3.11+"'
SERVICE_USER="$(systemctl show "$SERVICE" -p User --value 2>/dev/null || true)"
SERVICE_USER="${SERVICE_USER:-root}"
id "$SERVICE_USER" >/dev/null
mkdir -p "$TARGET"
TARGET="$(cd "$TARGET" && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
# A separate environment keeps the previous dependencies available for rollback.
VENV="$TARGET/.venvs/$STAMP"
"$TASK_PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install -r "$SOURCE/requirements.txt"
BACKUP="/var/backups/amnezia-manager/$STAMP"
install -d -m 700 "$BACKUP"
UNIT="/etc/systemd/system/$SERVICE.service"
UNIT_SOURCE="$(systemctl show "$SERVICE" -p FragmentPath --value 2>/dev/null || true)"
HAD_UNIT=false
if [[ -f "$UNIT_SOURCE" ]]; then cp "$UNIT_SOURCE" "$BACKUP/previous.service"; HAD_UNIT=true; fi
WAS_ACTIVE=false
systemctl is-active --quiet "$SERVICE" && WAS_ACTIVE=true
READY=false
MODE_DROPIN="/etc/systemd/system/$SERVICE.service.d/zz-interface.conf"
HAD_MODE=false
if [[ -f "$MODE_DROPIN" ]]; then cp "$MODE_DROPIN" "$BACKUP/interface.conf"; HAD_MODE=true; fi
rollback() {
    status=$?; trap - ERR
    systemctl stop "$SERVICE" 2>/dev/null || true
    if [[ "$READY" == true ]]; then tar -xzf "$BACKUP/installation.tar.gz" -C "$TARGET"; fi
    if [[ "$HAD_UNIT" == true ]]; then cp "$BACKUP/previous.service" "$UNIT"; else rm -f "$UNIT"; fi
    if [[ "$INTERFACE" != preserve ]]; then
        if [[ "$HAD_MODE" == true ]]; then cp "$BACKUP/interface.conf" "$MODE_DROPIN"; else rm -f "$MODE_DROPIN"; fi
    fi
    systemctl daemon-reload
    if [[ "$WAS_ACTIVE" == true ]]; then systemctl start "$SERVICE" || true; fi
    echo "Не удалось установить. Предыдущая версия восстановлена; backup: $BACKUP" >&2
    exit "$status"
}
trap rollback ERR
if [[ "$WAS_ACTIVE" == true ]]; then systemctl stop "$SERVICE"; fi
tar --exclude='./.venvs' --exclude='./.venv-v2' --exclude='./myenv' --exclude='./.git' --exclude='*/__pycache__' -czf "$BACKUP/installation.tar.gz" -C "$TARGET" .
chmod 600 "$BACKUP/installation.tar.gz"
READY=true
# Runtime data in awg/files and awg/users are deliberately excluded from the release.
tar --exclude='*/__pycache__' -C "$SOURCE" -cf - awg/manager awg/bot_manager.py miniapp requirements.txt LICENSE VERSION | tar -C "$TARGET" -xf -
install -d -m 700 -o "$SERVICE_USER" "$TARGET/awg/files"
if [[ ! -f "$TARGET/awg/files/setting.ini" ]]; then
    "$VENV/bin/python" "$TARGET/awg/bot_manager.py" --init
fi
chown -R "$SERVICE_USER" "$TARGET/awg/files"
runuser -u "$SERVICE_USER" --preserve-environment -- "$VENV/bin/python" "$TARGET/awg/bot_manager.py" --check
cat > "$UNIT" <<UNITFILE
[Unit]
Description=Amnezia VPN Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$TARGET/awg
ExecStart=$VENV/bin/python $TARGET/awg/bot_manager.py
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
Restart=on-failure
RestartSec=5
RestartPreventExitStatus=78
TimeoutStopSec=150

[Install]
WantedBy=multi-user.target
UNITFILE
if [[ "$INTERFACE" != preserve ]]; then
    install -d -m 755 "/etc/systemd/system/$SERVICE.service.d"
    DISABLED=0
    [[ "$INTERFACE" == bot ]] && DISABLED=1
    printf '[Service]\nEnvironment=AWG_WEB_DISABLED=%s\n' "$DISABLED" > "$MODE_DROPIN"
fi
systemd-analyze verify "$UNIT"
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl start "$SERVICE"
sleep 3
systemctl is-active --quiet "$SERVICE"
trap - ERR
echo "Бот установлен. Backup: $BACKUP"
echo "Откройте Telegram и отправьте /servers. Логи: journalctl -u $SERVICE -n 100 --no-pager"
if [[ -n "$DOMAIN" ]]; then
    AWG_INSTALL_DIR="$TARGET" AWG_SERVICE_NAME="$SERVICE" AWG_PYTHON="$VENV/bin/python" bash "$SOURCE/scripts/install-miniapp.sh" "$DOMAIN" "$PUBLIC_IP"
fi
