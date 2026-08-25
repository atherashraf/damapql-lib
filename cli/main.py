import sys
import traceback

from app.damapql.cli import commands
from app.damapql.manage import parse_params

"""
  python -m app.damapql.cli.main ingest_raster_folder --folder "D:\DevProjects\idss\lbdc_be\media\ETpot" --use_minio true --object_prefix "pid-idss/Surfaces/ETpot"
  python -m app.damapql.cli.main ingest_raster_folder --folder "D:\DevProjects\idss\lbdc_be\media\ETact" --use_minio true --object_prefix "pid-idss/Surfaces/ETact"
"""

def help():
    print("\n📖 --- DCH Data Management CLI ---")
    print("Usage: python -m app.damapql.cli.main <command> [--key value]\n")
    print(f"{'Command':<25} {'Description'}")
    print("-" * 60)
    print(f"{'ingest_raster_folder':<25} Batch ingest rasters from a directory into DCH")
    print("\n")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ["help", "--help", "-h"]:
        help()
        return

    command = sys.argv[1]
    params = parse_params(sys.argv[2:])

    # Look up the function inside the commands package
    cmd_func = getattr(commands, command, None)

    if not cmd_func or not callable(cmd_func):
        print(f"❌ Unknown command: {command}")
        return

    try:
        cmd_func(**params)
    except Exception:
        print("💥 Execution failed:")
        traceback.print_exc()


if __name__ == "__main__":
    main()