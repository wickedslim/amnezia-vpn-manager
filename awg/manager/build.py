"""Non-sensitive installation identity, cached until the service restarts."""
import hashlib,json
from functools import lru_cache
from pathlib import Path

@lru_cache(maxsize=1)
def build_info():
    root=Path(__file__).resolve().parents[2]
    try:
        value=json.loads((root/'BUILD.json').read_text())
        if all(isinstance(value.get(k),str) for k in ['version','id','source_sha256']):return {k:value[k] for k in ['version','id','source_sha256']}
    except (OSError,ValueError):pass
    digest=hashlib.sha256()
    for folder,pattern in [('awg/manager','*.py'),('miniapp','*.js'),('miniapp','*.css')]:
        for p in sorted((root/folder).glob(pattern)):
            digest.update(p.relative_to(root).as_posix().encode());digest.update(p.read_bytes())
    version=(root/'VERSION').read_text().strip() if (root/'VERSION').exists() else 'development'
    return {'version':version,'id':digest.hexdigest()[:16],'source_sha256':digest.hexdigest()}
