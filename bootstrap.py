"""Isolated app environment; install pinned dependencies only when requirements change."""
import hashlib
from pathlib import Path
import subprocess
import sys
import venv


def main():
    root = Path(__file__).resolve().parent
    folder = root / '.venv'
    python = folder / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    required = root / 'requirements.txt'
    signature = hashlib.sha256(required.read_bytes()).hexdigest()
    marker = folder / 'requirements.sha256'
    if not python.exists():
        print('Preparing app environment…')
        venv.EnvBuilder(with_pip=True).create(folder)
    if not marker.exists() or marker.read_text() != signature:
        print('Installing hot-stream dependencies…')
        result = subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(required)], cwd=root)
        if result.returncode:
            return result.returncode
        marker.write_text(signature)
    if '--setup' in sys.argv:
        return 0
    return subprocess.call([str(python), str(root / 'run.py'), *sys.argv[1:]], cwd=root)


if __name__ == '__main__':
    raise SystemExit(main())
