"""Delivery addresses; independent of SSH and of client DNS resolvers."""
import ipaddress
import re
from dataclasses import replace
from .domain import ManagerError


def delivery(server):
    selected = server.endpoint or server.host
    try:
        ipaddress.ip_address(selected)
        mode = 'ip'
    except ValueError:
        mode = 'dns' if selected else 'ip'
    return {'mode': mode,
            'ip': selected if mode == 'ip' else server.endpoint_ip,
            'dns': selected if mode == 'dns' else server.endpoint_dns}


def configure(server, body):
    if not isinstance(body, dict) or body.get('mode') not in ('ip', 'dns'):
        raise ManagerError('Выберите выдачу по IP или через DNS.')
    mode = body['mode']
    values = {}
    for key in ('ip', 'dns'):
        value = body.get(key, '')
        if not isinstance(value, str) or len(value) > 253:
            raise ManagerError('Проверьте IP и домен VPN.')
        values[key] = value.strip()
    if values['ip']:
        try:
            if '%' in values['ip']:
                raise ValueError
            values['ip'] = str(ipaddress.ip_address(values['ip']))
        except ValueError:
            raise ManagerError('Укажите IP без протокола, скобок и порта.') from None
    if values['dns']:
        try:
            domain = values['dns'].rstrip('.').encode('idna').decode('ascii').lower()
            if len(domain) > 253 or '.' not in domain or re.fullmatch(r'[0-9.]+', domain) or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in domain.split('.')):
                raise ValueError
            values['dns'] = domain
        except (ValueError, UnicodeError):
            raise ManagerError('Укажите домен без https://, пути и порта, например vpn.example.com.') from None
    if not values[mode]:
        raise ManagerError('Заполните IP.' if mode == 'ip' else 'Заполните домен VPN.')
    return replace(server, endpoint=values[mode], endpoint_ip=values['ip'], endpoint_dns=values['dns'])


MAX_ENDPOINTS = 12
_ENDPOINT_LINE = re.compile(r'(?m)^([ \t]*Endpoint[ \t]*=[ \t]*)([^#;\r\n]*?)(?=[ \t]*(?:[#;]|\r?$))')


def endpoint_mode(address):
    try:
        ipaddress.ip_address(address)
        return 'ip'
    except ValueError:
        return 'dns'


def endpoint_choices(server):
    """Return the configured list, or the addresses stored by earlier versions."""
    if server.endpoints:
        return server.endpoints
    addresses = dict.fromkeys((server.endpoint, server.endpoint_ip, server.endpoint_dns))
    return [{'label': '', 'address': value} for value in addresses if value]


def configure_endpoints(server, rows, default):
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ENDPOINTS:
        raise ManagerError(f'Укажите от 1 до {MAX_ENDPOINTS} адресов подключения.')
    if not isinstance(default, str):
        raise ManagerError('Выберите адрес по умолчанию.')
    normalized = []
    seen = set()
    default_address = ''
    for row in rows:
        if not isinstance(row, dict):
            raise ManagerError('Проверьте список адресов.')
        label, address = row.get('label', ''), row.get('address')
        if not isinstance(label, str) or len(label) > 40 or any(ord(c) < 32 for c in label):
            raise ManagerError('Подпись адреса должна быть не длиннее 40 символов и без переносов.')
        if not isinstance(address, str) or not address.strip():
            raise ManagerError('Заполните адрес.')
        mode = endpoint_mode(address.strip())
        canonical = configure(server, {'mode': mode, mode: address}).endpoint
        if canonical in seen:
            raise ManagerError('Адреса не должны повторяться.')
        seen.add(canonical)
        normalized.append({'label': label.strip(), 'address': canonical})
        if address == default:
            default_address = canonical
    if not default_address:
        raise ManagerError('Выберите адрес по умолчанию из списка.')
    preferred = {
        mode: next((row['address'] for row in normalized if endpoint_mode(row['address']) == mode), '')
        for mode in ('ip', 'dns')
    }
    preferred[endpoint_mode(default_address)] = default_address
    return replace(server, endpoints=normalized, endpoint=default_address,
                   endpoint_ip=preferred['ip'], endpoint_dns=preferred['dns'])


def config_endpoint(config):
    matches = list(_ENDPOINT_LINE.finditer(config or ''))
    if len(matches) != 1:
        return ''
    value = matches[0][2]
    match = re.fullmatch(r'(?:\[([0-9a-fA-F:.]+)\]|([a-zA-Z0-9._-]+)):(\d{1,5})', value)
    if not match or not 1 <= int(match[3]) <= 65535:
        return ''
    if match[1]:
        try:
            ipaddress.IPv6Address(match[1])
        except ValueError:
            return ''
    return value


def config_host(config):
    value = config_endpoint(config)
    return value.rsplit(':', 1)[0].strip('[]') if value else ''


def choose_endpoint(server, requested=None, current=''):
    if requested is None or (current and requested == current):
        address = current or server.endpoint
        if address:
            return address
        raise ManagerError('У сервера не задан внешний адрес для клиентской конфигурации.')
    if not isinstance(requested, str) or not any(row['address'] == requested for row in endpoint_choices(server)):
        raise ManagerError('Этот адрес недоступен. Выберите его из настроек сервера.')
    return requested


def replace_config_endpoint(config, host):
    old = config_endpoint(config)
    if not old:
        raise ManagerError('В сохранённом конфиге нет однозначного адреса подключения.')
    port = old.rsplit(':', 1)[1]
    literal = f'[{host}]' if ':' in host else host
    return _ENDPOINT_LINE.sub(lambda match: match[1] + f'{literal}:{port}', config, count=1)
