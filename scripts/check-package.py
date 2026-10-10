"""Build/install a wheel in temporary directories; leave the checkout untouched."""

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        source = work / 'source'
        source.mkdir()
        for name in ('hub', 'deploy'):
            shutil.copytree(root / name, source / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        for name in ('pyproject.toml', 'README.md', 'LICENSE'):
            shutil.copyfile(root / name, source / name)
        wheels = work / 'wheels'
        subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--wheel-dir', str(wheels), str(source)], check=True)
        wheel = next(wheels.glob('*.whl'))
        with zipfile.ZipFile(wheel) as package:
            files = package.namelist()
            assert 'hub/web/app.js' in files
            assert 'hub/web/assets/brand/favicon.svg' in files
            assert not any(name.endswith('nodes.json') or 'soak-results' in name for name in files)
            metadata = package.read(next(name for name in files if name.endswith('/METADATA'))).decode('utf-8')
            assert 'Requires-Dist:' not in metadata
        site = work / 'installed'
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--target', str(site), str(wheel)], check=True)
        env = dict(os.environ, PYTHONPATH=str(site))
        subprocess.run([sys.executable, '-m', 'hub', '--version'], cwd=str(work), env=env, check=True)
        subprocess.run([sys.executable, '-c', 'from hub.http import ASSETS, ASSET_ROOT; assert all((ASSET_ROOT / path).is_file() for path, mime in ASSETS.values())'], cwd=str(work), env=env, check=True)
        print('Wheel build, isolated install, version and packaged assets passed')


if __name__ == '__main__':
    main()
