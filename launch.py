import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parent

try:
    result = subprocess.run([sys.executable, str(root / "app.py")], cwd=root)
except KeyboardInterrupt:
    sys.exit(0)
except (OSError, UnicodeError) as exc:
    sys.exit(f"Launch failed: {exc}")

sys.exit(result.returncode)
