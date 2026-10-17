"""Rebuild/install a wheel in temporary directories without changing the checkout."""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', help='optional directory for validated wheel/report artifacts')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='platmon-package-') as directory:
        work=Path(directory)
        build_env=work/'build-env'
        venv.EnvBuilder(with_pip=True).create(build_env)
        builder=build_env/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        subprocess.run([str(builder),'-m','pip','install','setuptools>=61','wheel'],check=True)
        versions=json.loads(subprocess.check_output([str(builder),'-c',
            'import importlib.metadata,json;print(json.dumps({n:importlib.metadata.version(n) for n in ("setuptools","wheel")}))'],text=True))
        # Fixed ZIP timestamps make the same inputs/toolchain reproducible.
        env=dict(os.environ,SOURCE_DATE_EPOCH=os.environ.get('SOURCE_DATE_EPOCH','1704067200'))
        artifacts=[]
        for index in range(2):
            source=work/('source-'+str(index));source.mkdir()
            for name in ('hub','deploy','docs'):
                shutil.copytree(root/name,source/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            for name in ('pyproject.toml','README.md','backend.md','LICENSE','CHANGELOG.md','nodes.example.json','alerts.example.json'):
                shutil.copyfile(root/name,source/name)
            wheels=work/('wheels-'+str(index))
            subprocess.run([str(builder),'-m','pip','wheel','--no-deps','--no-build-isolation','--wheel-dir',str(wheels),str(source)],env=env,check=True)
            artifacts.append(next(wheels.glob('*.whl')))
        hashes=[hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts]
        assert hashes[0]==hashes[1], 'same-toolchain wheel builds differ'
        wheel=artifacts[0]
        with zipfile.ZipFile(wheel) as package:
            files=package.namelist()
            assert 'hub/web/app.js' in files and 'hub/web/assets/brand/favicon.svg' in files
            assert all(name in files for name in ('hub/replay.py', 'hub/raw_export.py', 'hub/history/raw.py'))
            assert any(name.endswith('/share/platmon-hub/nodes.example.json') for name in files)
            expected_docs={path.relative_to(root).as_posix()
                           for path in (root/'docs').rglob('*')
                           if path.is_file() and path.suffix in ('.md', '.json', '.png')}
            packaged_docs={name.split('/share/platmon-hub/',1)[1] for name in files
                           if '/share/platmon-hub/docs/' in name}
            assert packaged_docs==expected_docs, (
                f'packaged documentation mismatch: missing={sorted(expected_docs-packaged_docs)}, '
                f'unexpected={sorted(packaged_docs-expected_docs)}')
            assert all(any(name.endswith('/deploy/'+script) for name in files)
                       for script in ('nginx.conf.example', 'start-windows.ps1', 'install-windows-startup.ps1'))
            assert not any(name.endswith('nodes.json') or 'soak-results' in name for name in files)
            metadata=package.read(next(name for name in files if name.endswith('/METADATA'))).decode('utf-8')
            assert 'Requires-Dist:' not in metadata
        site=work/'installed'
        subprocess.run([str(builder),'-m','pip','install','--no-deps','--target',str(site),str(wheel)],check=True)
        installed_env=dict(os.environ,PYTHONPATH=str(site))
        version=subprocess.check_output([sys.executable,'-m','hub','--version'],cwd=str(work),env=installed_env,text=True).strip()
        subprocess.run([sys.executable,'-m','hub','raw-export','--help'],cwd=str(work),env=installed_env,check=True,stdout=subprocess.PIPE)
        subprocess.run([sys.executable,'-c','from hub.http import ASSETS,ASSET_ROOT; assert all((ASSET_ROOT/p).is_file() for p,m in ASSETS.values())'],cwd=str(work),env=installed_env,check=True)
        empty=site/'share'/'platmon-hub'/'nodes.example.json'
        subprocess.run([sys.executable,'-m','hub','nodes','--file',str(empty),'validate'],cwd=str(work),env=installed_env,check=True)
        report=dict(version=version,wheel=wheel.name,sha256=hashes[0],reproducible=True,
            source_date_epoch=env['SOURCE_DATE_EPOCH'],build_tools=versions,python=platform.python_version(),platform=platform.system())
        if args.output_dir:
            output=Path(args.output_dir);output.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(wheel,output/wheel.name)
            (output/'package-report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(report))
        print('Identical wheel rebuilds, isolated install, CLI, examples and assets passed')


if __name__=='__main__': main()
