"""Check systemd unit syntax with a local executable substitution; do not install."""
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory() as directory:
    unit = Path(directory) / 'platmon-hub.service'
    text = (root / 'deploy/platmon-hub.service').read_text(encoding='utf-8')
    unit.write_text(text.replace('/opt/platmon-hub/venv/bin/platmon-hub', sys.executable), encoding='utf-8')
    subprocess.run(['systemd-analyze', 'verify', str(unit)], check=True)
