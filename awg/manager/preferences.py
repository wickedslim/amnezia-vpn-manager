"""Small, versioned visual preferences; no effect on VPN configuration."""
import re

DEFAULTS = dict(version=1, mode='auto', palette='brand', customAccent='#FF4302', density='comfortable', fontSize=14, motion=True)


def normalize_preferences(value):
    v = value if isinstance(value, dict) else {}
    color = v.get('customAccent')
    font = v.get('fontSize')
    return dict(version=1,
                mode=v.get('mode') if v.get('mode') in ('light', 'dark', 'auto') else DEFAULTS['mode'],
                palette=v.get('palette') if v.get('palette') in ('brand', 'lime', 'pink', 'custom') else 'brand',
                customAccent=color.upper() if isinstance(color, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', color) else '#FF4302',
                density='compact' if v.get('density') == 'compact' else 'comfortable',
                fontSize=font if type(font) is int and font in (12, 14, 16) else 14,
                motion=v.get('motion') is not False)
