"""Run every tests/test_*.py and report. Exit code 1 if any check fails.

    python tests/run_all.py
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
failed = []
for path in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
    name = os.path.basename(path)
    r = subprocess.run([sys.executable, "-X", "utf8", path], cwd=HERE,
                       capture_output=True, text=True, encoding="utf-8")
    lines = r.stdout.splitlines()
    fails = [l for l in lines if l.startswith("FAIL ")]
    n = sum(1 for l in lines if l.startswith(("PASS ", "FAIL ")))
    if r.returncode or fails:
        failed.append(name)
        print(f"FAIL {name}: {len(fails)}/{n} checks failed")
        for l in fails:
            print("     " + l)
        if r.returncode and not fails:
            print("\n".join("     " + l for l in r.stderr.strip().splitlines()[-8:]))
    else:
        print(f"ok   {name}: {n} checks")
print("\nALL PASS" if not failed else f"\nFAILED: {', '.join(failed)}")
sys.exit(1 if failed else 0)
