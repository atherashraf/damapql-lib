import os
import re
from pathlib import Path

# Configuration
PROJECT_ROOT = "."
# Matches .save(...) calls and captures the contents of the parentheses
SAVE_PATTERN = r"\.save\((.*?)\)"
IGNORE_DIRS = {".git", "__pycache__", "venv", "env"}


def audit_save_methods():
    print(f"{'FILE':<40} | {'LINE':<5} | {'STATUS':<15} | {'CALL'}")
    print("-" * 110)

    audit_count = 0
    for path in Path(PROJECT_ROOT).rglob("*.py"):
        if any(ignored in path.parts for ignored in IGNORE_DIRS) or "mixin" in path.name.lower():
            continue

        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                match = re.search(SAVE_PATTERN, line)
                if match:
                    audit_count += 1
                    args_content = match.group(1)

                    # Logic check: is unique_fields present?
                    if "unique_fields" in args_content:
                        status = "✅ UPSERT"
                    elif "session=" in args_content and len(args_content.split(',')) == 1:
                        status = "ℹ️ SESSION ONLY"
                    elif not args_content.strip():
                        status = "⚠️ RAW SAVE"
                    else:
                        status = "❓ CHECK ARGS"

                    print(f"{str(path)[-40:]:<40} | {i:<5} | {status:<15} | {line.strip()}")

    print("-" * 110)
    print(f"Total .save() calls audited: {audit_count}")


if __name__ == "__main__":
    audit_save_methods()