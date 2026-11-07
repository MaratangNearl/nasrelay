"""Restore normal creation permissions on explicitly selected old downloads."""
import argparse
import os
from pathlib import Path
import posixpath
import stat
import sys

from .storage import NAS, clean_path


def main():
    parser = argparse.ArgumentParser(description='지정한 NAS 파일의 0600 권한을 부모 폴더의 일반 생성 권한으로 복구합니다.')
    parser.add_argument('paths', nargs='+', help='nasrelay 탐색기에 표시되는 파일 경로 (/nas 마운트 접두사 제외)')
    args = parser.parse_args()
    nas = NAS(Path(os.environ.get('NAS_ROOT', '/nas')))
    failed = False
    for value in args.paths:
        try:
            path = clean_path(value)
            local = nas.local(path)
            info = local.stat()
            if not stat.S_ISREG(info.st_mode) or local.name.startswith('.ftn-'):
                raise ValueError('완료된 일반 파일만 복구할 수 있습니다.')
            before = stat.S_IMODE(info.st_mode)
            if before != 0o600:
                print(f'{path}: {before:04o}, 0600 파일이 아니므로 건너뜁니다.')
                continue
            mode = nas.default_file_mode(posixpath.dirname(path))
            nas.finish_download_permissions(path, mode)
            print(f'{path}: {before:04o} → {mode:04o}')
        except (OSError, ValueError) as exc:
            failed = True
            print(f'{value}: 복구 실패: {exc}', file=sys.stderr)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
