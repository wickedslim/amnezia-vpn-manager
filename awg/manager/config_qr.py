"""Render a private client configuration as PNG without network calls."""
import struct
import zlib

from ._qrcodegen import DataTooLongError, QrCode
from .domain import ManagerError


def config_qr_png(config: str) -> bytes:
    payload = config.encode('utf-8')
    if len(payload) > 2953:  # QR Model 2, version 40, byte mode, level L.
        raise ManagerError('Конфиг слишком длинный для QR-кода. Скачайте файл.')
    try:
        code = QrCode.encode_binary(payload, QrCode.Ecc.LOW)
    except DataTooLongError:
        raise ManagerError('Конфиг слишком длинный для QR-кода. Скачайте файл.') from None

    scale, border = 5, 4
    size = (code.get_size() + 2 * border) * scale
    rows = []
    for y in range(-border, code.get_size() + border):
        row = b''.join((b'\0' if code.get_module(x, y) else b'\xff') * scale
                       for x in range(-border, code.get_size() + border))
        rows.append((b'\0' + row) * scale)  # PNG filter byte at the start of each row.

    def chunk(kind, data):
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data)))

    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 0, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b''))
