"""Validate a client export before attaching its secret to an existing peer."""
from __future__ import annotations

import base64
import configparser
import ipaddress

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .domain import ManagerError, fields, validate_key


def validate_client_export(text: str, peer, server_public: str) -> str:
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 12000:
        raise ManagerError('Выберите клиентский .conf размером до 12 Кб.')
    text = text.lstrip('\ufeff').replace('\r\n', '\n')
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(text)
        if set(parser.sections()) != {'Interface', 'Peer'} or parser.defaults():
            raise ValueError
        interface, remote = parser['Interface'], parser['Peer']
        if any(k in interface for k in ('preup', 'postup', 'predown', 'postdown')):
            raise ValueError
        private = validate_key(interface['privatekey'].strip())
        key = X25519PrivateKey.from_private_bytes(base64.b64decode(private, validate=True))
        public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
        if public != peer.public_key:
            raise ManagerError('Этот .conf принадлежит другому клиенту. Ключи и подключение не изменены.')
        if validate_key(remote['publickey'].strip()) != server_public:
            raise ManagerError('Этот .conf относится к другому серверу.')
        addresses = {ipaddress.ip_interface(a.strip()).ip for a in interface['address'].split(',')}
        expected = {ipaddress.ip_network(a.strip(), strict=False).network_address for a in peer.allowed_ips.split(',')}
        if not expected.issubset(addresses):
            raise ManagerError('VPN-адрес в .conf не совпадает с адресом клиента.')
        psk = fields(peer.block).get('PresharedKey', '').strip()
        if remote.get('presharedkey', '').strip() != psk:
            raise ManagerError('Общий ключ в .conf не совпадает с настройками сервера.')
        if not remote.get('endpoint') or not remote.get('allowedips'):
            raise ValueError
    except (configparser.Error, ValueError, KeyError, TypeError):
        raise ManagerError('Нужен клиентский .conf с секциями Interface и Peer, без команд запуска. Экспортируйте его из Amnezia.') from None
    return text.strip() + '\n'
