"""Run every offline test script: python tests/run_all.py   (exit status 1 if any fails)."""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
failed = []
for script in sorted(HERE.glob("test_*.py")):
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    print("{} {}".format("ok  " if result.returncode == 0 else "FAIL", script.name))
    if result.returncode:
        failed.append(script.name)
        print(result.stdout[-1500:] + result.stderr[-1500:])
print("\n{} of {} test scripts failed".format(len(failed), len(list(HERE.glob("test_*.py")))) if failed
      else "\nall test scripts passed")
sys.exit(1 if failed else 0)
