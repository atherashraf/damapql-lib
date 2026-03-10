
import re
from pathlib import Path

# Configuration
PROJECT_ROOT = "."  # Change if running from elsewhere
DEPRECATED_METHOD = r"\.get_session\(\)"
IGNORE_DIRS = {".git", "__pycache__", "venv", "env", ".pytest_cache"}


def scan_files():
    print(f"{'FILE':<50} | {'LINE':<5} | {'CODE'}")
    print("-" * 100)

    found_count = 0
    # Walk through python files
    for path in Path(PROJECT_ROOT).rglob("*.py"):
        # Skip hidden/ignored directories
        if any(ignored in path.parts for ignored in IGNORE_DIRS):
            continue

        # Skip the Mixin definition file itself to avoid false positives
        if "mixin" in path.name.lower():
            continue

        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if re.search(DEPRECATED_METHOD, line):
                    found_count += 1
                    clean_line = line.strip()
                    print(f"{str(path):<50} | {i:<5} | {clean_line}")

    print("-" * 100)
    print(f"Total deprecated usages found: {found_count}")


if __name__ == "__main__":
    scan_files()