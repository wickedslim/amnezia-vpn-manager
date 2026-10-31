#!/usr/bin/env bash
# Optional HTTPS setup for an already installed manager. Ubuntu/Debian + Caddy.
set -Eeuo pipefail
if [[ "${1:-}" == --help || $# -eq 0 ]]; then
    echo 'sudo AWG_INSTALL_DIR=/opt/amnezia-manager bash scripts/install-miniapp.sh vpn.example.com [PUBLIC_IP]'
    exit 0
fi
DOMAIN="$1"
EXPECTED_IP="${2:-}"
SERVICE="${AWG_SERVICE_NAME:-awg_bot}"
PORT="${AWG_WEB_PORT:-8088}"
WORKDIR="$(systemctl show "$SERVICE" -p WorkingDirectory --value 2>/dev/null || true)"
TARGET="${AWG_INSTALL_DIR:-${WORKDIR%/awg}}"
[[ "$EUID" -eq 0 && "$DOMAIN" =~ ^[a-zA-Z0-9.-]+$ && "$SERVICE" =~ ^[a-zA-Z0-9_.@-]+$ ]] || { echo 'Нужны root, домен и корректное имя службы.' >&2; exit 1; }
[[ "$PORT" =~ ^[0-9]+$ && "$PORT" -ge 1024 && "$PORT" -le 65535 ]] || exit 1
[[ -d "$TARGET/awg/manager" ]] || { echo 'Сначала установите бота.' >&2; exit 1; }
PYTHON="${AWG_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    EXEC="$(systemctl show "$SERVICE" -p ExecStart --value)"
    PYTHON="$(python3 -c 'import re,sys; m=re.search(r"path=([^ ;]+)",sys.argv[1]); print(m[1] if m else "")' "$EXEC")"
fi
[[ -x "$PYTHON" ]] || { echo 'Не найден Python службы. Задайте AWG_PYTHON.' >&2; exit 1; }
"$PYTHON" - "$DOMAIN" "$EXPECTED_IP" <<'PY'
import ipaddress,json,socket,subprocess,sys
name=sys.argv[1]
assert len(name)<=253 and '.' in name and not name.startswith('-') and not name.endswith('-'), 'Некорректный домен'
addresses={ipaddress.ip_address(r[4][0]) for r in socket.getaddrinfo(name,443,type=socket.SOCK_STREAM)}
local={ipaddress.ip_address(a['local']) for i in json.loads(subprocess.check_output(['ip','-j','address'],text=True)) for a in i.get('addr_info',[])}
if sys.argv[2]:local.add(ipaddress.ip_address(sys.argv[2]))
assert addresses and addresses.issubset(local), 'DNS A/AAAA не совпадают с адресами сервера. Для NAT задайте PUBLIC_IP вторым аргументом.'
PY
if [[ -n "$(ss -ltnH "( sport = :80 or sport = :443 or sport = :$PORT )")" ]]; then
    echo 'Веб-порт уже занят. Используйте существующий HTTPS-прокси по MINIAPP.ru.md.' >&2; exit 1
fi
[[ ! -e /etc/caddy/Caddyfile && ! -e "/etc/systemd/system/$SERVICE.service.d/miniapp.conf" ]] || { echo 'Настройки HTTPS уже существуют; автоматическая замена запрещена.' >&2; exit 1; }
command -v apt-get >/dev/null || { echo 'Автоустановка HTTPS поддерживает Ubuntu/Debian.' >&2; exit 1; }
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y caddy curl
BACKUP="/var/backups/amnezia-manager/https-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$BACKUP" "/etc/systemd/system/$SERVICE.service.d"
cp /etc/caddy/Caddyfile "$BACKUP/Caddyfile"
DROPIN="/etc/systemd/system/$SERVICE.service.d/miniapp.conf"
MODE_DROPIN="/etc/systemd/system/$SERVICE.service.d/zz-interface.conf"
HAD_MODE=false
if [[ -f "$MODE_DROPIN" ]]; then cp "$MODE_DROPIN" "$BACKUP/interface.conf"; HAD_MODE=true; fi
rollback() {
    status=$?; trap - ERR
    rm -f "$DROPIN"
    if [[ "$HAD_MODE" == true ]]; then cp "$BACKUP/interface.conf" "$MODE_DROPIN"; else rm -f "$MODE_DROPIN"; fi
    cp "$BACKUP/Caddyfile" /etc/caddy/Caddyfile
    systemctl stop caddy || true
    systemctl daemon-reload; systemctl restart "$SERVICE" || true
    echo "HTTPS не настроен; бот восстановлен. Backup: $BACKUP" >&2
    exit "$status"
}
trap rollback ERR
cat > "$DROPIN" <<CONF
[Service]
Environment=AWG_WEB_HOST=127.0.0.1
Environment=AWG_WEB_PORT=$PORT
Environment=AWG_WEB_URL=https://$DOMAIN
CONF
cat > /etc/caddy/Caddyfile <<CONF
# AMNEZIA_MANAGER
$DOMAIN {
    encode zstd gzip
    reverse_proxy 127.0.0.1:$PORT
}
CONF
printf '[Service]\nEnvironment=AWG_WEB_DISABLED=0\n' > "$MODE_DROPIN"
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl daemon-reload
systemctl restart "$SERVICE"
status=0
for attempt in {1..30}; do
    status="$(curl -s --max-time 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/overview" || true)"
    [[ "$status" == 401 ]] && break
    sleep 1
done
[[ "$status" == 401 ]]
systemctl enable caddy
systemctl restart caddy
for attempt in {1..30}; do
    if curl -fsS --max-time 3 "https://$DOMAIN/" -o /dev/null; then break; fi
    sleep 1
done
curl -fsS --max-time 5 "https://$DOMAIN/" -o /dev/null
[[ "$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "https://$DOMAIN/api/overview")" == 401 ]]
trap - ERR
echo "Mini App: https://$DOMAIN ; backup: $BACKUP. Откройте кнопку «Панель VPN» в боте."
