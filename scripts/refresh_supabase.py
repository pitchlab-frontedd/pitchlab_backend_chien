import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

STEPS = [
    ["python3", os.path.join(SCRIPT_DIR, "sqlite_to_postgres.py")],
    ["python3", os.path.join(SCRIPT_DIR, "import_pitcher_standard_stats.py")],
    ["python3", os.path.join(SCRIPT_DIR, "build_player_names.py")],
    ["python3", os.path.join(SCRIPT_DIR, "check_supabase.py")],
]


def main():
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError("Set DATABASE_URL before running this script.")

    env = {
        **os.environ,
        "POSTGRES_IMPORT_BATCH_SIZE": os.getenv("POSTGRES_IMPORT_BATCH_SIZE", "50000"),
        "POSTGRES_INSERT_PAGE_SIZE": os.getenv("POSTGRES_INSERT_PAGE_SIZE", "5000"),
        "POSTGRES_IMPORT_RESUME": "0",
    }

    for step in STEPS:
        print(f"\n==> {' '.join(step)}")
        subprocess.run(step, check=True, env=env)

    print("\nSupabase refresh complete.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
