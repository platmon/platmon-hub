"""Check systemd unit syntax with a local executable substitution; do not install."""
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
units = {
    'platmon-hub.service': '/opt/platmon-hub/venv/bin/platmon-hub',
    'platmon-hub-user.service': '%h/.local/share/platmon-hub/venv/bin/platmon-hub',
}
with tempfile.TemporaryDirectory() as directory:
    for name, executable in units.items():
        unit = Path(directory) / name
        text = (root / 'deploy' / name).read_text(encoding='utf-8')
        assert executable in text, name + ' no longer starts ' + executable
        unit.write_text(text.replace(executable, sys.executable), encoding='utf-8')
        subprocess.run(['systemd-analyze', 'verify', str(unit)], check=True)
