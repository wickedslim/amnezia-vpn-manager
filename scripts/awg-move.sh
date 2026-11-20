#!/usr/bin/env bash
# Move the manager and its local AmneziaWG containers to a new VPS with one command.
# Run it on the OLD VPS, e.g. from the hosting provider's web console:  awg-move 203.0.113.10
# Keys, obfuscation parameters, clients, history and settings move unchanged, so clients
# that reach the server by a domain name keep working after the DNS record is changed.
set -Eeuo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
TOOL="$HERE/awgtool.py"
SERVICE="${AWG_SERVICE_NAME:-awg_bot}"
TRAFFIC=amnezia-service-traffic.service
YES=false
CHECK=false
BATCH=false
TARGET_ARG=""

usage() {
    cat <<'EOF'
Переезд бота и VPN на новый VPS. Запускайте на СТАРОМ сервере:
  sudo awg-move НОВЫЙ_IP            (SSH нового VPS на порту 22)
  sudo awg-move НОВЫЙ_IP:2222       (другой порт SSH)
  sudo awg-move НОВЫЙ_IP --check    (только проверить, ничего не меняя)
Команда спросит root-пароль нового VPS (или использует SSH-ключ).
EOF
}
die() { echo; echo "Ошибка: $*" >&2; exit 1; }
say() { echo "==> $*"; }
warn() { echo "Внимание: $*" >&2; WARNINGS+=("$*"); }
WARNINGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y) YES=true; shift ;;
        --check) CHECK=true; shift ;;
        --batch) BATCH=true; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) usage; die "неизвестный параметр $1" ;;
        *) [[ -z "$TARGET_ARG" ]] || die "укажите один новый сервер"; TARGET_ARG="$1"; shift ;;
    esac
done
[[ -n "$TARGET_ARG" ]] || { usage; exit 1; }
[[ "$EUID" -eq 0 ]] || die "нужен root (sudo)."
command -v python3 >/dev/null && command -v docker >/dev/null || die "на этом сервере нужны python3 и docker."
if [[ "$TARGET_ARG" =~ ^\[([0-9a-fA-F:]+)\](:([0-9]+))?$ ]]; then
    HOST="${BASH_REMATCH[1]}"; SSH_PORT="${BASH_REMATCH[3]:-22}"
elif [[ "$TARGET_ARG" =~ ^([A-Za-z0-9.-]+)(:([0-9]+))?$ ]]; then
    HOST="${BASH_REMATCH[1]}"; SSH_PORT="${BASH_REMATCH[3]:-22}"
else
    die "адрес нового VPS должен быть вида 203.0.113.10 или 203.0.113.10:2222"
fi

WORK="$(mktemp -d)"
SOCK="$WORK/ssh.sock"
REMOTE_TMP=""
SSH_POLICY=(-o StrictHostKeyChecking=accept-new -o ControlPersist=yes)
if [[ "$BATCH" == true ]]; then
    SSH_POLICY=(-o StrictHostKeyChecking=yes -o BatchMode=yes -o IdentityAgent=none -o ControlPersist=30)
fi
remote() { ssh "${SSH_POLICY[@]}" -o ControlPath="$SOCK" -p "$SSH_PORT" "root@$HOST" "$@"; }
cleanup() {
    [[ -z "$REMOTE_TMP" ]] || remote "rm -rf $(q "$REMOTE_TMP")" >/dev/null 2>&1 || true
    ssh -o ControlPath="$SOCK" -O exit -p "$SSH_PORT" "root@$HOST" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT
q() { printf '%q ' "$@"; }

# ---------- What lives on this (old) server ----------
BOT=false
LOAD="$(systemctl show "$SERVICE" -p LoadState --value 2>/dev/null || true)"
[[ -n "$LOAD" && "$LOAD" != not-found ]] && BOT=true
CONTAINERS=()
ENDPOINT_IPS=()
DOMAINS=()
if [[ "$BOT" == true ]]; then
    WORKDIR="$(systemctl show "$SERVICE" -p WorkingDirectory --value)"
    TARGET="${WORKDIR%/awg}"
    [[ -d "$TARGET/awg/manager" && "$WORKDIR" == */awg ]] || die "не найден каталог бота ($WORKDIR)."
    EXEC="$(systemctl show "$SERVICE" -p ExecStart --value)"
    BOT_PYTHON="$(python3 -c 'import re,sys; m=re.search(r"path=([^ ;]+)",sys.argv[1]); print(m[1] if m else "")' "$EXEC")"
    [[ "$BOT_PYTHON" == "$TARGET"/* && -x "$BOT_PYTHON" ]] || die "не найден Python бота в каталоге $TARGET."
    VENV="$(dirname "$(dirname "$BOT_PYTHON")")"
    UNIT_FILE="$(systemctl show "$SERVICE" -p FragmentPath --value)"
    [[ "$UNIT_FILE" == /etc/systemd/system/* ]] || die "служба $SERVICE описана не в /etc/systemd/system."
    DROPINS="/etc/systemd/system/$SERVICE.service.d"
    ENVIRONMENT="$(systemctl show "$SERVICE" -p Environment --value)"
    [[ "$ENVIRONMENT" != *AWG_DATA_DIR=* ]] || die "данные бота вынесены через AWG_DATA_DIR — такой переезд не поддерживается."
    SERVICE_USER="$(systemctl show "$SERVICE" -p User --value)"; SERVICE_USER="${SERVICE_USER:-root}"
    DB="$TARGET/awg/files/v2/manager.sqlite3"
    [[ -f "$DB" && -f "$TARGET/awg/files/setting.ini" ]] || die "не найдены настройки или база бота в $TARGET/awg/files."
    SERVERS="$(python3 "$TOOL" servers "$DB")"
    mapfile -t CONTAINERS < <(python3 -c 'import json,sys; sys.stdout.write("\n".join(sorted({s["container"] for s in json.loads(sys.argv[1]) if s["local"]})))' "$SERVERS")
    mapfile -t ENDPOINT_IPS < <(python3 -c 'import ipaddress,json,sys
out=set()
for s in json.loads(sys.argv[1]):
    if s["local"]:
        for address in (s["endpoint"], s.get("endpoint_ip", "")):
            try: ipaddress.ip_address(address); out.add(address)
            except ValueError: pass
sys.stdout.write("\n".join(sorted(out)))' "$SERVERS")
    mapfile -t DOMAINS < <(python3 -c 'import ipaddress,json,sys
out=set()
for s in json.loads(sys.argv[1]):
    if not s["local"]: continue
    for address in (s["endpoint"], *s.get("endpoints", [])):
        if not address: continue
        try: ipaddress.ip_address(address)
        except ValueError: out.add(address)
sys.stdout.write("\n".join(sorted(out)))' "$SERVERS")
    WEB_DOMAIN=""
    if [[ -f "$DROPINS/miniapp.conf" ]]; then
        WEB_DOMAIN="$(sed -n 's|^Environment=AWG_WEB_URL=https://||p' "$DROPINS/miniapp.conf" | head -1)"
    fi
    CADDY=false
    if [[ -n "$WEB_DOMAIN" ]]; then
        if grep -q '^# AMNEZIA_MANAGER' /etc/caddy/Caddyfile 2>/dev/null; then CADDY=true
        else warn "Mini App работает через ваш собственный веб-сервер — его нужно будет настроить на новом VPS вручную."; fi
    fi
    HAS_TRAFFIC=false; TRAFFIC_ENABLED=false
    if [[ "$(systemctl show "$TRAFFIC" -p LoadState --value 2>/dev/null)" == loaded ]]; then
        HAS_TRAFFIC=true
        systemctl is-enabled --quiet "$TRAFFIC" && TRAFFIC_ENABLED=true
        read -r TRAFFIC_CONTAINER TRAFFIC_IFACE < <(systemctl show "$TRAFFIC" -p ExecStart --value | sed -n 's/.*collector\.py collect \([^ ;]*\) \([^ ;]*\).*/\1 \2/p') || true
        [[ -n "${TRAFFIC_CONTAINER:-}" ]] || { HAS_TRAFFIC=false; warn "не удалось прочитать настройки сборщика «Трафик по сервисам» — его нужно будет включить заново в Mini App."; }
    fi
else
    docker container inspect amnezia-awg >/dev/null 2>&1 && CONTAINERS=(amnezia-awg)
fi
[[ "$BOT" == true || ${#CONTAINERS[@]} -gt 0 ]] || die "на этом сервере нет ни бота ($SERVICE), ни контейнера amnezia-awg."
UDP_PORTS=()
IMAGES=()
for c in "${CONTAINERS[@]}"; do
    docker container inspect "$c" >"$WORK/$c.json" 2>/dev/null || die "контейнер $c из настроек бота не найден на этом сервере."
    mapfile -t ports < <(python3 "$TOOL" udp-ports "$WORK/$c.json")
    UDP_PORTS+=("${ports[@]}")
    python3 "$TOOL" create-args "$WORK/$c.json" >"$WORK/$c.args" || die "контейнер $c нельзя перенести автоматически."
    IMAGES+=("$(tail -1 "$WORK/$c.args")")
    docker exec "$c" wg show wg0 >/dev/null 2>&1 || die "в контейнере $c не работает wg0 — сначала запустите VPN на старом сервере."
done
mapfile -t IMAGES < <(printf '%s\n' "${IMAGES[@]}" | sort -u | grep . || true)

# ---------- Connect to the new server ----------
say "Подключаюсь к новому VPS $HOST (порт SSH $SSH_PORT)"
[[ "$BATCH" == true ]] || echo "    Если спросит пароль — введите root-пароль НОВОГО сервера (символы не отображаются)."
ssh "${SSH_POLICY[@]}" -o ControlMaster=yes -o ControlPath="$SOCK" \
    -o ServerAliveInterval=15 -o ConnectTimeout=20 -fN -p "$SSH_PORT" "root@$HOST" || die "не удалось подключиться к $HOST по SSH."
[[ "$(remote 'id -u')" == 0 ]] || die "на новом VPS нужен вход под root."
remote 'command -v systemctl >/dev/null && command -v apt-get >/dev/null' || die "новый VPS должен быть на Ubuntu или Debian с systemd."
NEW_OS="$(remote '. /etc/os-release; echo "$PRETTY_NAME"')"
SAME_ARCH=false; [[ "$(remote 'uname -m')" == "$(uname -m)" ]] && SAME_ARCH=true
if [[ "$BOT" == true ]]; then
    [[ "$(remote "systemctl show $(q "$SERVICE") -p LoadState --value")" == not-found ]] || die "на новом VPS уже есть служба $SERVICE. Нужен чистый сервер."
    remote "test ! -e $(q "$TARGET")" || die "на новом VPS уже есть каталог $TARGET. Нужен чистый сервер."
    [[ "$SERVICE_USER" == root ]] || remote "id $(q "$SERVICE_USER")" >/dev/null 2>&1 || die "на новом VPS нет пользователя $SERVICE_USER, от которого работает бот."
fi
for c in "${CONTAINERS[@]}"; do
    remote "command -v docker >/dev/null && docker container inspect $(q "$c") >/dev/null 2>&1" && die "на новом VPS уже есть контейнер $c."
done
for p in "${UDP_PORTS[@]}"; do
    [[ -z "$(remote "ss -Hlun 'sport = :$p'")" ]] || die "на новом VPS занят UDP-порт $p, нужный VPN."
done
if [[ "${CADDY:-false}" == true ]]; then
    [[ -z "$(remote "ss -Hltn '( sport = :80 or sport = :443 )'")" ]] || die "на новом VPS заняты порты 80/443, нужные Mini App."
fi

NEW_IP=""; python3 "$TOOL" is-ip "$HOST" && NEW_IP="$HOST"
echo
echo "Что будет перенесено на $HOST ($NEW_OS):"
[[ ${#CONTAINERS[@]} -gt 0 ]] && echo "  • VPN: ${CONTAINERS[*]} (UDP ${UDP_PORTS[*]:-—}) — ключи, параметры обфускации и все клиенты без изменений"
if [[ "$BOT" == true ]]; then
    echo "  • бот $SERVICE из $TARGET: настройки, база, история и пользователи"
    [[ "$CADDY" == true ]] && echo "  • Mini App $WEB_DOMAIN с текущим сертификатом"
    [[ "$HAS_TRAFFIC" == true ]] && echo "  • «Трафик по сервисам» со статистикой"
    echo "  • бот на этом сервере будет остановлен примерно на минуту и потом отключён; VPN здесь продолжит работать"
fi
[[ "$SAME_ARCH" == true ]] || echo "  • архитектура процессора отличается — образ VPN будет собран заново"
if [[ "$CHECK" == true ]]; then echo; echo "Проверка завершена, ничего не изменено."; exit 0; fi
if [[ "$YES" != true ]]; then
    read -r -p "Начать переезд? Введите yes: " answer </dev/tty
    [[ "$answer" == yes ]] || die "переезд отменён, ничего не изменено."
fi

# ---------- Prepare the new server; the old one keeps working ----------
fail_prepare() {
    echo >&2
    echo "Переезд остановлен до переключения: этот сервер, бот и VPN работают как раньше." >&2
    echo "На новом VPS могли остаться Docker и часть файлов — перед повтором проще переустановить на нём ОС." >&2
}
trap fail_prepare ERR
say "Устанавливаю пакеты на новом VPS"
PACKAGES="docker.io python3 python3-venv ca-certificates curl"
[[ "${CADDY:-false}" == true ]] && PACKAGES="$PACKAGES caddy"
install_packages() {
    remote "set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get -o DPkg::Lock::Timeout=120 update -q >/dev/null
apt-get -o DPkg::Lock::Timeout=120 install -y -q $PACKAGES >/dev/null
systemctl enable --now docker >/dev/null 2>&1
docker info >/dev/null"
}
install_packages
REMOTE_TMP="$(remote 'umask 077; mktemp -d /root/.awg-move.XXXXXXXX')"
tar -C "$HERE" -cf - awgtool.py awg-vpn.sh | remote "tar -C $REMOTE_TMP -xf -"

for image in "${IMAGES[@]}"; do
    if [[ "$SAME_ARCH" == true ]]; then
        say "Переношу образ $image"
        docker save "$image" | gzip -1 | remote 'gunzip | docker load >/dev/null'
    elif [[ "$image" == amnezia-awg || "$image" == amnezia-awg:latest ]]; then
        say "Собираю образ $image на новом VPS"
        remote "bash $REMOTE_TMP/awg-vpn.sh image"
    else
        die "образ $image нельзя собрать на другой архитектуре."
    fi
done

copy_container() {  # current /opt/amnezia of the old container into the new one
    docker exec "$1" tar -C / -cf - opt/amnezia | remote "docker cp - $(q "$1:/")"
}
peers() { docker exec "$1" wg show wg0 peers | grep -c . || true; }
for c in "${CONTAINERS[@]}"; do
    say "Создаю VPN-контейнер $c"
    mapfile -t args <"$WORK/$c.args"
    remote "docker create $(q "${args[@]}") >/dev/null"
    copy_container "$c"
    remote "docker start $(q "$c") >/dev/null"
    remote "bash $REMOTE_TMP/awg-vpn.sh status --name $(q "$c") --expect $(peers "$c")"
    remote "if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then for p in ${UDP_PORTS[*]}; do ufw allow \$p/udp >/dev/null; done; fi"
done

if [[ "$BOT" == true ]]; then
    say "Копирую бота и создаю его окружение Python"
    remote 'python3 -c "import sys; assert sys.version_info >= (3, 11)"' || die "на новом VPS нужен Python 3.11 или новее (Ubuntu 24.04, Debian 12)."
    tar -C "$TARGET" --exclude=./.venvs --exclude=./.venv-v2 --exclude=./myenv --exclude=./.git --exclude='*/__pycache__' \
        --exclude=./awg/files -czf - . | remote "install -d -m 755 $(q "$TARGET") && tar -C $(q "$TARGET") -xzf -"
    remote "python3 -m venv $(q "$VENV") && $(q "$VENV/bin/python") -m pip install -q --disable-pip-version-check -r $(q "$TARGET/requirements.txt")"
    remote "ln -sfn $(q "$TARGET/scripts/awg-move.sh") /usr/local/bin/awg-move && ln -sfn $(q "$TARGET/scripts/awg-vpn.sh") /usr/local/bin/awg-vpn"
    UNIT_PARTS=("${UNIT_FILE#/}")
    [[ -d "$DROPINS" ]] && UNIT_PARTS+=("${DROPINS#/}")
    tar -C / -cf - "${UNIT_PARTS[@]}" | remote 'tar -C / -xf - && systemctl daemon-reload'
    if [[ "$CADDY" == true ]]; then
        say "Переношу Mini App ($WEB_DOMAIN) и сертификат"
        remote 'systemctl stop caddy'
        CADDY_PARTS=(etc/caddy/Caddyfile)
        [[ -d /var/lib/caddy/.local/share/caddy ]] && CADDY_PARTS+=(var/lib/caddy/.local/share/caddy)
        tar -C / -cf - "${CADDY_PARTS[@]}" | remote 'tar -C / -xf - && chown -R caddy:caddy /var/lib/caddy && caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 && systemctl enable --now caddy >/dev/null 2>&1'
    fi
    if [[ "$HAS_TRAFFIC" == true ]]; then
        say "Устанавливаю сборщик «Трафик по сервисам» (это может занять пару минут)"
        if ! remote "bash $(q "$TARGET/scripts/install-service-traffic.sh") $(q "$TRAFFIC_CONTAINER") $(q "$TRAFFIC_IFACE") >/dev/null"; then
            HAS_TRAFFIC=false
            warn "сборщик «Трафик по сервисам» не установился на новом VPS — включите его потом в Mini App."
        fi
    fi
fi
trap - ERR

# ---------- Switch the bot over ----------
if [[ "$BOT" == true ]]; then
    TRAFFIC_ACTIVE=false; systemctl is-active --quiet "$TRAFFIC" && TRAFFIC_ACTIVE=true
    rollback() {
        trap - ERR
        echo >&2
        echo "Переключение не удалось, возвращаю бота на этом сервере." >&2
        remote "systemctl disable --now $(q "$SERVICE") $TRAFFIC" >/dev/null 2>&1 || true
        systemctl start "$SERVICE" || true
        [[ "$TRAFFIC_ACTIVE" == true ]] && systemctl start "$TRAFFIC" || true
        echo "Бот снова работает на этом сервере. Подробности выше; журнал нового бота: journalctl -u $SERVICE на $HOST." >&2
        exit 1
    }
    trap rollback ERR
    say "Останавливаю бота на этом сервере"
    systemctl stop "$SERVICE"
    [[ "$TRAFFIC_ACTIVE" == true ]] && systemctl stop "$TRAFFIC"
fi
for c in "${CONTAINERS[@]}"; do
    say "Повторно копирую клиентов $c"
    copy_container "$c"
    remote "docker restart $(q "$c") >/dev/null"
    remote "bash $REMOTE_TMP/awg-vpn.sh status --name $(q "$c") --expect $(peers "$c")"
done
if [[ "$BOT" == true ]]; then
    say "Копирую настройки и базу бота"
    tar -C "$TARGET/awg" -cf - files | remote "tar -C $(q "$TARGET/awg") -xf -"
    if [[ "$HAS_TRAFFIC" == true && -d /var/lib/amnezia-service-traffic ]]; then
        tar -C / -cf - var/lib/amnezia-service-traffic | remote 'tar -C / -xf -'
    fi
    if [[ -n "$NEW_IP" ]]; then
        for ip in "${ENDPOINT_IPS[@]}"; do
            [[ "$ip" == "$NEW_IP" ]] && continue
            remote "python3 $REMOTE_TMP/awgtool.py set-endpoint $(q "$TARGET/awg/files/v2/manager.sqlite3") $(q "$ip") $(q "$NEW_IP") >/dev/null"
        done
    fi
    say "Запускаю бота на новом VPS"
    remote "systemctl enable --now $(q "$SERVICE") >/dev/null 2>&1"
    for _ in 1 2 3 4; do sleep 5; remote "systemctl is-active --quiet $(q "$SERVICE")"; done
    if [[ "$HAS_TRAFFIC" == true && "$TRAFFIC_ENABLED" == true ]]; then
        remote "systemctl enable --now $TRAFFIC >/dev/null 2>&1" || warn "сборщик «Трафик по сервисам» не запустился на новом VPS."
    fi
    trap - ERR
    systemctl disable "$SERVICE" >/dev/null 2>&1 || true
    [[ "$HAS_TRAFFIC" == true ]] && systemctl disable "$TRAFFIC" >/dev/null 2>&1 || true
    remote "python3 $REMOTE_TMP/awgtool.py notify $(q "$TARGET/awg/files/setting.ini") $(q "Бот переехал на новый сервер ${HOST}. VPN и все клиенты перенесены. Поменяйте IP в DNS на ${HOST}.")" >/dev/null 2>&1 \
        || warn "не удалось отправить сообщение в Telegram с нового VPS."
fi

# ---------- Result ----------
echo
echo "Готово. Бот и VPN работают на $HOST."
if [[ ${#DOMAINS[@]} -gt 0 || -n "${WEB_DOMAIN:-}" ]]; then
    echo "Поменяйте в DNS IP этих записей на $HOST:"
    for d in "${DOMAINS[@]}" ${WEB_DOMAIN:+"$WEB_DOMAIN"}; do echo "  • $d"; done | sort -u
fi
if [[ ${#ENDPOINT_IPS[@]} -gt 0 ]]; then
    echo "Клиенты, у которых в конфиге адрес ${ENDPOINT_IPS[*]}, нужно перенастроить на новый адрес вручную."
    echo "Выбранный режим выдачи сохранён. Для новых конфигов с доменом используйте «Выдача конфигов · IP / DNS»."
fi
[[ ${#CONTAINERS[@]} -gt 0 ]] && echo "VPN на этом сервере продолжает работать, пока клиенты не перейдут. Потом его можно удалить."
for w in "${WARNINGS[@]}"; do echo "Внимание: $w"; done
