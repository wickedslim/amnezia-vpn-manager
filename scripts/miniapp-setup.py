#!/usr/bin/env python3
"""Interactive Mini App setup planning; no system changes or DuckDNS credentials."""
import ipaddress
import json
import re
import socket
import subprocess
import sys


def configured(environment):
    import shlex
    values = dict(item.split('=', 1) for item in shlex.split(environment) if '=' in item)
    return bool(values.get('AWG_WEB_URL')) and values.get('AWG_WEB_DISABLED') != '1'


def valid_domain(value, duck=False):
    labels = value.split('.')
    return (len(value) <= 253 and len(labels) >= 2
            and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)
            and (not duck or len(labels) == 3 and labels[1:] == ['duckdns', 'org']))


def local_addresses():
    data = json.loads(subprocess.check_output(['ip', '-j', 'address'], text=True))
    return {str(ipaddress.ip_address(a['local'])) for interface in data
            for a in interface.get('addr_info', [])}


def dns_matches(domain, expected):
    try:
        addresses = {str(ipaddress.ip_address(row[4][0]))
                     for row in socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)}
        return bool(addresses) and addresses.issubset(expected)
    except (OSError, ValueError):
        return False


def ask(prompt):
    print(prompt, file=sys.stderr, end=' ', flush=True)
    return input().strip()


def plan():
    print('Mini App: 1 — свой домен; 2 — бесплатный DuckDNS; 3 — только бот (Enter).', file=sys.stderr)
    choice = ask('Выберите 1 / 2 / 3:')
    if choice not in {'1', '2'}:
        return {}
    addresses = local_addresses()
    public = sorted(a for a in addresses if ipaddress.ip_address(a).is_global)
    print('Адреса сетевых интерфейсов: ' + (', '.join(public) or 'публичный адрес не обнаружен'), file=sys.stderr)
    while True:
        ip = ask('Публичный IP VPS (при NAT укажите внешний IP; Enter — адреса выше; 0 — пропустить Mini App):')
        if ip == '0':
            return {}
        try:
            expected = addresses | ({str(ipaddress.ip_address(ip))} if ip else set())
            break
        except ValueError:
            print('Некорректный IP. Введите IPv4 или IPv6 без порта.', file=sys.stderr)
    if choice == '2':
        print('Откройте https://www.duckdns.org/ и войдите в свой аккаунт.\n'
              'Создайте свободное имя, укажите IP этого VPS и сохраните.\n'
              'Пример адреса: my-vpn.duckdns.org. Токен DuckDNS сюда вводить не нужно.', file=sys.stderr)
    print('Для автоматического HTTPS нужны свободные TCP 80/443 и доступ к ним из интернета.\n'
          'Если уже есть Nginx/Caddy, используйте инструкцию MINIAPP.ru.md.', file=sys.stderr)
    while True:
        domain = ask('Адрес Mini App без https:// (0 — пропустить):').lower()
        if domain == '0':
            return {}
        if not valid_domain(domain, duck=choice == '2'):
            print('Укажите корректный ' + ('адрес вида name.duckdns.org.' if choice == '2' else 'домен.'), file=sys.stderr)
            continue
        while not dns_matches(domain, expected):
            print('DNS ещё не указывает на этот VPS либо есть посторонняя A/AAAA-запись.\n'
                  'Проверьте IP на сайте, отключите DNS-прокси и дождитесь обновления DNS.', file=sys.stderr)
            retry = ask('Enter — проверить снова; 1 — другой адрес; 0 — продолжить только с ботом:')
            if retry == '0':
                return {}
            if retry == '1':
                break
        else:
            return {'domain': domain, 'ip': ip}


if __name__ == '__main__':
    if len(sys.argv) > 1 and configured(sys.argv[1]):
        print('{}')
    else:
        try:
            print(json.dumps(plan()))
        except (EOFError, KeyboardInterrupt):
            print('\nНастройка Mini App пропущена; режим бота сохраняется.', file=sys.stderr)
            print('{}')
