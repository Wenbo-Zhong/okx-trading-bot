"""Run the plain-script tests with isolated storage and process state."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    data_dir = tempfile.mkdtemp(prefix="okxbot-tests-")
    os.environ["OKXBOT_DATA_DIR"] = data_dir
    root = Path(__file__).resolve().parent.parent
    tests = sorted((root / "tests").glob("test_*.py"))
    failed = []
    try:
        for test in tests:
            print(f"\n=== {test.name} ===", flush=True)
            result = subprocess.run([sys.executable, str(test)], cwd=root,
                                    env=os.environ.copy())
            if result.returncode:
                failed.append(test.name)
    finally:
        shutil.rmtree(data_dir)
    print(f"\n{len(tests) - len(failed)}/{len(tests)} test modules passed", flush=True)
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed or not tests else 0


if __name__ == "__main__":
    sys.exit(main())
