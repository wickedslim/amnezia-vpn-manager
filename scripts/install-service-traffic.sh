#!/usr/bin/env bash
set -euo pipefail

container=${1:-amnezia-awg}
interface=${2:-wg0}
pattern='^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'
if [[ $EUID != 0 || ! $container =~ $pattern || ! $interface =~ $pattern ]]; then
    echo "Запуск: sudo bash scripts/install-service-traffic.sh [контейнер] [интерфейс]" >&2
    exit 1
fi

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
target=/opt/amnezia-service-traffic
unit=amnezia-service-traffic.service

exec 9>/run/lock/amnezia-service-traffic-install.lock
flock -n 9 || { echo "Установка сборщика уже выполняется." >&2; exit 1; }
if [[ -e /etc/systemd/system/$unit || -L /etc/systemd/system/$unit ]] ||
    [[ $(systemctl show -p LoadState --value "$unit") != not-found ]]; then
    echo "Сборщик уже установлен. Проверьте настройки через systemctl cat $unit." >&2
    exit 1
fi

docker exec "$container" sh -c 'if command -v awg >/dev/null; then exec awg show "$1" allowed-ips; fi; exec wg show "$1" allowed-ips' sh "$interface" >/dev/null
command -v nsenter >/dev/null
python=
for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9; do
    if "$candidate" -c 'import ensurepip, sys, venv; sys.exit(not ((3, 9) <= sys.version_info[:2] <= (3, 14)))' 2>/dev/null; then
        python=$candidate
        break
    fi
done
if [[ -z $python ]]; then
    echo "На VPS нужен Python с venv и ensurepip." >&2
    exit 1
fi
install -d -m 0755 "$target"
# An interrupted attempt may have left an environment with a different Python.
"$python" -m venv --clear "$target/venv"
"$target/venv/bin/pip" install --disable-pip-version-check --timeout 15 --retries 2 --only-binary=:all: 'nfstream==6.6.0'
"$target/venv/bin/python" -c 'from nfstream import NFStreamer'
install -m 0644 "$source_dir/service_traffic.py" "$target/collector.py"
cat > "/etc/systemd/system/$unit" <<EOF
[Unit]
Description=Amnezia VPN service traffic collector
After=docker.service
Requisite=docker.service

[Service]
Type=simple
ExecStart=$target/venv/bin/python $target/collector.py collect $container $interface
Restart=always
RestartSec=10
TimeoutStopSec=10s
UMask=0077
CPUQuota=50%
MemoryMax=384M
Nice=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
echo "Сборщик установлен. Включите его в настройках Mini App или через systemctl enable --now $unit."
