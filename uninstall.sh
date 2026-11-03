#!/usr/bin/env bash
# Remove only the manager. Preserve its installation in a private recovery folder.
set -Eeuo pipefail
SERVICE="${AWG_SERVICE_NAME:-awg_bot}"
CHECK_ONLY=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) CHECK_ONLY=true; shift ;;
        --help|-h) echo 'sudo bash uninstall.sh [--check]'; echo 'AWG_SERVICE_NAME / AWG_INSTALL_DIR override service and installation.'; exit 0 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 1 ;;
    esac
done
[[ "$(uname -s)" == Linux ]] || { echo 'Нужны Linux и systemd.' >&2; exit 1; }
[[ "$SERVICE" =~ ^[a-zA-Z0-9_.@-]+$ ]] || { echo 'Некорректная служба.' >&2; exit 1; }
command -v systemctl >/dev/null
LOAD="$(systemctl show "$SERVICE" -p LoadState --value 2>/dev/null || true)"
[[ "$LOAD" == loaded ]] || { echo 'Служба менеджера не найдена. Изменений нет.' >&2; exit 1; }
WORKDIR="$(systemctl show "$SERVICE" -p WorkingDirectory --value)"
[[ "$WORKDIR" == /*/awg ]] || { echo 'Не удалось определить установку менеджера.' >&2; exit 1; }
TARGET="${AWG_INSTALL_DIR:-${WORKDIR%/awg}}"
[[ "$TARGET" =~ ^/[a-zA-Z0-9_./-]+$ && -d "$TARGET" && ! -L "$TARGET" ]] || { echo 'Небезопасный путь установки.' >&2; exit 1; }
TARGET="$(cd "$TARGET" && pwd -P)"
case "$TARGET" in /|/root|/home|/opt|/usr|/etc|/var|/var/lib|/var/backups|/tmp|/run) echo 'Слишком широкий путь. Изменений нет.' >&2; exit 1 ;; esac
[[ "$WORKDIR" == "$TARGET/awg" && -f "$TARGET/awg/bot_manager.py" && -d "$TARGET/awg/manager" ]] || { echo 'Каталог не соответствует установке менеджера.' >&2; exit 1; }
EXEC="$(systemctl show "$SERVICE" -p ExecStart --value)"
[[ "$EXEC" == *"$TARGET/awg/bot_manager.py"* ]] || { echo 'Служба запускает другой код. Изменений нет.' >&2; exit 1; }
UNIT="$(systemctl show "$SERVICE" -p FragmentPath --value)"
[[ "$UNIT" == "/etc/systemd/system/$SERVICE.service" ]] || { echo 'Нестандартный systemd unit: удалите его вручную после проверки.' >&2; exit 1; }
if systemctl is-active --quiet awg-manager-update; then echo 'Сначала дождитесь завершения обновления менеджера.' >&2; exit 1; fi
# Never move a directory used as a Docker bind mount (including stopped containers).
if command -v docker >/dev/null; then
    IDS="$(docker ps -aq)" || { echo 'Не удалось проверить Docker mounts.' >&2; exit 1; }
    for CONTAINER in $IDS; do
        MOUNTS="$(docker inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' "$CONTAINER")"
        while IFS= read -r MOUNT; do
            [[ -n "$MOUNT" ]] || continue
            if [[ "$MOUNT" == "$TARGET" || "$MOUNT" == "$TARGET/"* || "$TARGET/" == "$MOUNT/"* || "$MOUNT" == / ]]; then
                echo 'Каталог пересекается с Docker mount. Автоматическое удаление отменено для защиты VPN.' >&2; exit 1
            fi
        done <<< "$MOUNTS"
    done
fi
DROPINS="/etc/systemd/system/$SERVICE.service.d"
# Only the exact dedicated Caddyfile produced by our HTTPS installer is ours.
OWN_CADDY=false
if [[ -f /etc/caddy/Caddyfile && ! -L /etc/caddy/Caddyfile ]]; then
    CADDY_TEXT="$(cat /etc/caddy/Caddyfile)"
    CADDY_RE=$'^# AMNEZIA_MANAGER\n[a-zA-Z0-9.-]+ \\{\n    encode zstd gzip\n    reverse_proxy 127\\.0\\.0\\.1:[0-9]+\n\\}'
    [[ "$CADDY_TEXT" =~ $CADDY_RE && "${BASH_REMATCH[0]}" == "$CADDY_TEXT" ]] && OWN_CADDY=true
fi
printf 'Будет удалён менеджер: %s\nКаталог: %s\n' "$SERVICE" "$TARGET"
echo 'Каталог, настройки, база и systemd unit будут перенесены в закрытую резервную папку.'
if [[ "$OWN_CADDY" == true ]]; then echo 'Будет отключён выделенный Caddy и сохранён его Caddyfile.'
else echo 'Общий/пользовательский HTTPS-прокси останется без изменений.'; fi
echo 'VPN-контейнеры, конфиги на VPN-серверах, пакеты системы и прежние резервные копии не изменяются.'
if [[ "$CHECK_ONLY" == true ]]; then echo 'Проверка без изменений завершена.'; exit 0; fi
[[ "$EUID" -eq 0 ]] || { echo 'Запустите с sudo.' >&2; exit 1; }
[[ -t 0 ]] || { echo 'Нужен интерактивный SSH-терминал для подтверждения.' >&2; exit 1; }
read -r -p 'Удалить менеджер с сохранением резервной копии? [y/N] ' ANSWER
case "$ANSWER" in y|Y|yes|YES|да|Да|ДА) ;; *) echo 'Отменено.'; exit 0 ;; esac
BACKUP="/var/backups/amnezia-manager/uninstall-$(date -u +%Y%m%dT%H%M%SZ)-$$"
[[ "$BACKUP/" != "$TARGET/"* ]] || { echo 'Резервная копия оказалась внутри установки. Отменено.' >&2; exit 1; }
install -d -m 700 "$BACKUP"
ACTIVE=false; ENABLED=false; CADDY_ACTIVE=false; CADDY_ENABLED=false
systemctl is-active --quiet "$SERVICE" && ACTIVE=true
systemctl is-enabled --quiet "$SERVICE" && ENABLED=true
if [[ "$OWN_CADDY" == true ]]; then
    systemctl is-active --quiet caddy && CADDY_ACTIVE=true
    systemctl is-enabled --quiet caddy && CADDY_ENABLED=true
fi
rollback() {
    status=${1:-$?}; trap - ERR INT TERM; set +e
    [[ ! -d "$BACKUP/installation" || -e "$TARGET" ]] || mv -- "$BACKUP/installation" "$TARGET"
    [[ ! -e "$BACKUP/manager.service" ]] || mv -- "$BACKUP/manager.service" "$UNIT"
    [[ ! -d "$BACKUP/drop-ins" ]] || mv -- "$BACKUP/drop-ins" "$DROPINS"
    [[ ! -e "$BACKUP/Caddyfile" ]] || mv -- "$BACKUP/Caddyfile" /etc/caddy/Caddyfile
    systemctl daemon-reload || true
    if [[ "$ENABLED" == true ]]; then systemctl enable "$SERVICE" || true; fi
    if [[ "$ACTIVE" == true ]]; then systemctl start "$SERVICE" || true; fi
    if [[ "$CADDY_ENABLED" == true ]]; then systemctl enable caddy || true; fi
    if [[ "$CADDY_ACTIVE" == true ]]; then systemctl start caddy || true; fi
    echo "Удаление прервано; выполнена попытка восстановления из $BACKUP. Проверьте службу $SERVICE." >&2
    exit "$status"
}
trap rollback ERR
trap 'rollback 130' INT
trap 'rollback 143' TERM
printf '%s\n' "$TARGET" > "$BACKUP/original-path.txt"
printf '%s\n' "$SERVICE" > "$BACKUP/service-name.txt"
systemctl stop "$SERVICE"
UPDATE_STATE="$(systemctl show awg-manager-update -p ActiveState --value 2>/dev/null || true)"
case "$UPDATE_STATE" in active|activating|reloading) echo 'Обновление началось во время подтверждения. Дождитесь его завершения.' >&2; false ;; esac
systemctl disable "$SERVICE"
if [[ "$OWN_CADDY" == true ]]; then
    systemctl stop caddy; systemctl disable caddy
    mv -- /etc/caddy/Caddyfile "$BACKUP/Caddyfile"
fi
mv -- "$TARGET" "$BACKUP/installation"
mv -- "$UNIT" "$BACKUP/manager.service"
if [[ -d "$DROPINS" ]]; then mv -- "$DROPINS" "$BACKUP/drop-ins"; fi
systemctl daemon-reload
trap - ERR INT TERM
echo "Менеджер удалён. Резервная копия: $BACKUP"
echo 'VPN продолжает работать. Инструкция восстановления: UNINSTALL.ru.md в репозитории.'
