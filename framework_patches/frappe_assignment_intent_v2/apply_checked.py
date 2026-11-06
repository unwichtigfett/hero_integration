"""Apply the assignment boundary only to the verified Frappe image source."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
FRAPPE_ROOT = Path('/home/frappe/frappe-bench/apps/frappe')
TARGET = 'frappe/desk/form/assign_to.py'
BASE_SHA256 = 'e317417e9f7ad0a6c2a60c5238f5c393fe9302b0209c2c9fab11014d624143f1'
PATCHED_SHA256 = 'd226b9de31c9f5edf1f207382534926547021ce14bfdaac1d62a334e59921b64'
PATCH_SHA256 = '1273a4d1ab9a803de4a1394c6366bc490f4dabd64f94bae392573806cf976969'


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def main():
    target = FRAPPE_ROOT / TARGET
    if sha256((HERE / 'assignment-intent.patch').read_bytes()) != PATCH_SHA256:
        raise SystemExit('Unverified Frappe assignment patch.')
    digest = sha256(target.read_bytes())
    if digest not in {BASE_SHA256, PATCHED_SHA256}:
        raise SystemExit('Unverified Frappe assignment module; refusing framework patch.')
    for line in (HERE / 'frappe-2fcb-python.manifest').read_text().splitlines():
        expected, relative = line.split('  ', 1)
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts:
            raise SystemExit('Invalid framework manifest path.')
        data = (FRAPPE_ROOT / path).read_bytes()
        if relative == TARGET and digest == PATCHED_SHA256:
            continue
        actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if actual != expected:
            raise SystemExit(f'Unverified Frappe source file: {relative}')
    if digest == BASE_SHA256:
        patch = str(HERE / 'assignment-intent.patch')
        subprocess.run(['git', 'apply', '--check', patch], cwd=FRAPPE_ROOT, check=True)
        subprocess.run(['git', 'apply', patch], cwd=FRAPPE_ROOT, check=True)
    if sha256(target.read_bytes()) != PATCHED_SHA256:
        raise SystemExit('Frappe assignment patch verification failed.')
    print('Verified Frappe assignment boundary v2.')


if __name__ == '__main__':
    sys.exit(main())
