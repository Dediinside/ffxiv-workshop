"""Run independent suites without clashing legacy test module names."""
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
for suite in ('tests', 'modules/leve/tests', 'modules/market/tests', 'modules/reseller/tests'):
    script = "import run,unittest; result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover(" + repr(suite) + ")); raise SystemExit(not result.wasSuccessful())"
    result = subprocess.run([sys.executable, '-c', script], cwd=root)
    if result.returncode:
        raise SystemExit(result.returncode)
