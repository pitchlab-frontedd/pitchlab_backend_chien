import os
import subprocess
import sys


STEPS = [
    ["python3", "sqlite_to_postgres.py"],
    ["python3", "import_pitcher_standard_stats.py"],
    ["python3", "build_player_names.py"],
    ["python3", "check_supabase.py"],
]


def main():
    if not os.getenv("DATABASE_URL"):
        raise RuntimeError("Set DATABASE_URL before running this script.")

    env = {
        **os.environ,
        "POSTGRES_IMPORT_BATCH_SIZE": os.getenv("POSTGRES_IMPORT_BATCH_SIZE", "50000"),
        "POSTGRES_INSERT_PAGE_SIZE": os.getenv("POSTGRES_INSERT_PAGE_SIZE", "5000"),
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
