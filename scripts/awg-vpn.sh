#!/usr/bin/env bash
# AmneziaWG server without the AmneziaVPN app: the same image, files and container
# options the app creates, so the manager and existing clients work unchanged.
set -Eeuo pipefail
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
TOOL="$HERE/awgtool.py"
NAME=amnezia-awg
IMAGE=amnezia-awg
PORT=""
SUBNET=10.8.1.0/24
EXPECT=""

usage() {
    cat <<'EOF'
Новый сервер AmneziaWG без приложения AmneziaVPN:
  sudo awg-vpn install [--port 42080] [--subnet 10.8.1.0/24] [--name amnezia-awg]
Восстановить VPN из резервной копии бота (ZIP из Telegram):
  sudo awg-vpn restore amnezia-1-....zip [--name amnezia-awg]
Проверить контейнер:
  sudo awg-vpn status [--name amnezia-awg]
EOF
}
die() { echo "Ошибка: $*" >&2; exit 1; }
say() { echo "==> $*"; }

ensure_docker() {
    if ! command -v docker >/dev/null; then
        command -v apt-get >/dev/null || die "Docker не установлен, а автоустановка поддерживает только Ubuntu/Debian."
        say "Устанавливаю Docker"
        apt-get update -q
        DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker.io
    fi
    systemctl enable --now docker >/dev/null 2>&1 || true
    docker info >/dev/null 2>&1 || die "Docker не запущен."
}

# The Dockerfile the AmneziaVPN app puts in /opt/amnezia/amnezia-awg on the server.
build_image() {
    if docker image inspect "$IMAGE" >/dev/null 2>&1; then return; fi
    say "Собираю образ $IMAGE (как приложение AmneziaVPN)"
    docker build -q -t "$IMAGE" - >/dev/null <<'DOCKERFILE'
FROM amneziavpn/amnezia-wg:latest

LABEL maintainer="AmneziaVPN"

#Install required packages
RUN apk add --no-cache bash curl dumb-init
RUN apk --update upgrade --no-cache

RUN mkdir -p /opt/amnezia
RUN echo -e "#!/bin/bash\ntail -f /dev/null" > /opt/amnezia/start.sh
RUN chmod a+x /opt/amnezia/start.sh

# Tune network
RUN echo -e " \n\
  fs.file-max = 51200 \n\
  \n\
  net.core.rmem_max = 67108864 \n\
  net.core.wmem_max = 67108864 \n\
  net.core.netdev_max_backlog = 250000 \n\
  net.core.somaxconn = 4096 \n\
  \n\
  net.ipv4.tcp_syncookies = 1 \n\
  net.ipv4.tcp_tw_reuse = 1 \n\
  net.ipv4.tcp_tw_recycle = 0 \n\
  net.ipv4.tcp_fin_timeout = 30 \n\
  net.ipv4.tcp_keepalive_time = 1200 \n\
  net.ipv4.ip_local_port_range = 10000 65000 \n\
  net.ipv4.tcp_max_syn_backlog = 8192 \n\
  net.ipv4.tcp_max_tw_buckets = 5000 \n\
  net.ipv4.tcp_fastopen = 3 \n\
  net.ipv4.tcp_mem = 25600 51200 102400 \n\
  net.ipv4.tcp_rmem = 4096 87380 67108864 \n\
  net.ipv4.tcp_wmem = 4096 65536 67108864 \n\
  net.ipv4.tcp_mtu_probing = 1 \n\
  net.ipv4.tcp_congestion_control = hybla \n\
  # for low-latency network, use cubic instead \n\
  # net.ipv4.tcp_congestion_control = cubic \n\
  " | sed -e 's/^\s\+//g' | tee -a /etc/sysctl.conf && \
  mkdir -p /etc/security && \
  echo -e " \n\
  * soft nofile 51200 \n\
  * hard nofile 51200 \n\
  " | sed -e 's/^\s\+//g' | tee -a /etc/security/limits.conf

ENTRYPOINT [ "dumb-init", "/opt/amnezia/start.sh" ]
CMD [ "" ]
DOCKERFILE
}

port_free() { [[ -z "$(ss -Hlun "sport = :$1" 2>/dev/null)" ]]; }

open_firewall() {
    if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
        ufw allow "$1/udp" >/dev/null && say "Открыт порт $1/udp в ufw"
    fi
}

# Copy a prepared /opt/amnezia tree into a new container and start it.
launch() {
    local tree="$1" port="$2"
    docker create --log-driver none --restart always --privileged --cap-add=NET_ADMIN --cap-add=SYS_MODULE \
        -p "$port:$port/udp" -v /lib/modules:/lib/modules --sysctl net.ipv4.conf.all.src_valid_mark=1 \
        --name "$NAME" "$IMAGE" >/dev/null
    tar --owner=0 --group=0 -C "$tree" -cf - opt | docker cp - "$NAME:/"
    docker start "$NAME" >/dev/null
    open_firewall "$port"
}

# Wait until wg0 is up with the obfuscation parameters and the expected peers.
verify() {
    local expected="${1:-}" show peers
    for _ in $(seq 1 20); do
        show="$(docker exec "$NAME" wg show wg0 2>/dev/null || true)"
        if grep -q '^  jc:' <<<"$show"; then
            peers="$(docker exec "$NAME" wg show wg0 peers | grep -c . || true)"
            if [[ -z "$expected" || "$peers" == "$expected" ]]; then
                say "Контейнер $NAME работает: порт $(docker exec "$NAME" wg show wg0 listen-port), клиентов $peers, параметры обфускации на месте"
                return 0
            fi
        fi
        sleep 1
    done
    die "контейнер $NAME не поднял wg0 с параметрами обфускации${expected:+ и $expected клиентами}. Журнал: docker logs $NAME"
}

prepare_tree() {
    local tree="$1" subnet="$2"
    install -d -m 755 "$tree/opt/amnezia/awg"
    python3 "$TOOL" start-script --subnet "$subnet" >"$tree/opt/amnezia/start.sh"
    chmod 555 "$tree/opt/amnezia/start.sh"
}

cmd_install() {
    ensure_docker
    docker container inspect "$NAME" >/dev/null 2>&1 && die "контейнер $NAME уже существует."
    if [[ -z "$PORT" ]]; then
        for _ in $(seq 1 50); do PORT=$((30000 + RANDOM % 30000)); port_free "$PORT" && break; done
    fi
    [[ "$PORT" =~ ^[0-9]+$ ]] && port_free "$PORT" || die "порт $PORT/udp занят или некорректен."
    build_image
    local tree="$WORK"
    prepare_tree "$tree" "$SUBNET"
    local awg="$tree/opt/amnezia/awg" private public psk
    private="$(docker run --rm --entrypoint wg "$IMAGE" genkey)"
    public="$(printf '%s' "$private" | docker run --rm -i --entrypoint wg "$IMAGE" pubkey)"
    psk="$(docker run --rm --entrypoint wg "$IMAGE" genpsk)"
    (umask 077; python3 "$TOOL" interface --private "$private" --port "$PORT" --subnet "$SUBNET" >"$awg/wg0.conf"; echo '[]' >"$awg/clientsTable")
    printf '%s\n' "$private" >"$awg/wireguard_server_private_key.key"
    printf '%s\n' "$public" >"$awg/wireguard_server_public_key.key"
    printf '%s\n' "$psk" >"$awg/wireguard_psk.key"
    launch "$tree" "$PORT"
    verify 0
    echo
    echo "Сервер AmneziaWG готов. Порт: $PORT/udp, подсеть: $SUBNET."
    echo "Подключите его в боте: «Серверы» → «＋ Сервер» (контейнер $NAME, конфигурация /opt/amnezia/awg/wg0.conf)."
}

cmd_restore() {
    local archive="${1:-}"
    [[ -f "$archive" ]] || die "укажите ZIP резервной копии бота."
    ensure_docker
    docker container inspect "$NAME" >/dev/null 2>&1 && die "контейнер $NAME уже существует."
    local tree="$WORK"
    install -d -m 700 "$tree/opt/amnezia/awg"
    python3 - "$archive" "$tree/opt/amnezia/awg" <<'PY'
import os, sys, zipfile
archive, target = sys.argv[1:]
with zipfile.ZipFile(archive) as z:
    for name in ("server/wg0.conf", "server/clientsTable"):
        path = os.path.join(target, os.path.basename(name))
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
            out.write(z.read(name))
PY
    local info port subnet peers
    info="$(python3 "$TOOL" config-info "$tree/opt/amnezia/awg/wg0.conf")"
    read -r port subnet peers < <(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(d["port"], d["subnet"], d["peers"])' "$info")
    port_free "$port" || die "порт $port/udp из резервной копии занят."
    prepare_tree "$tree" "$subnet"
    build_image
    launch "$tree" "$port"
    verify "$peers"
    echo
    echo "VPN восстановлен из резервной копии: порт $port/udp, клиентов $peers. Ключи и параметры прежние —"
    echo "направьте адрес VPN (домен в DNS) на этот сервер, и клиенты подключатся без перенастройки."
}

cmd_status() {
    docker container inspect "$NAME" >/dev/null 2>&1 || die "контейнер $NAME не найден."
    verify "$EXPECT"
}

[[ "$EUID" -eq 0 ]] || { usage; die "нужен root (sudo)."; }
command -v python3 >/dev/null || die "нужен python3."
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
COMMAND="${1:-}"; shift || true
ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="${2:?}"; shift 2 ;;
        --subnet) SUBNET="${2:?}"; shift 2 ;;
        --name) NAME="${2:?}"; shift 2 ;;
        --expect) EXPECT="${2:?}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) ARGS+=("$1"); shift ;;
    esac
done
[[ "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] || die "недопустимое имя контейнера."
case "$COMMAND" in
    install) cmd_install ;;
    restore) cmd_restore "${ARGS[0]:-}" ;;
    status) cmd_status ;;
    image) ensure_docker; build_image ;;
    *) usage; exit 1 ;;
esac
