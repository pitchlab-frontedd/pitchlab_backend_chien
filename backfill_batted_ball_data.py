import datetime
import os
import sqlite3
import time

import pandas as pd
import pybaseball
from pybaseball import statcast

pybaseball.cache.enable()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv(
    "BASEBALL_DB_FILENAME",
    os.path.join(BASE_DIR, "baseball_data_2024_2025.db"),
)
BATCH_DAYS = int(os.getenv("BACKFILL_BATCH_DAYS", "14"))

TARGET_COLUMNS = {
    "launch_speed": "REAL",
    "launch_angle": "REAL",
    "bb_type": "TEXT",
}


def ensure_columns(conn):
    existing = {row[1] for row in conn.execute("PRAGMA table_info(pitches)").fetchall()}
    for column, column_type in TARGET_COLUMNS.items():
        if column not in existing:
            print(f"Adding column {column}")
            conn.execute(f"ALTER TABLE pitches ADD COLUMN {column} {column_type}")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pitch_identity
        ON pitches(game_pk, at_bat_number, pitch_number)
        """
    )
    conn.commit()


def date_ranges(start_date, end_date, batch_days):
    current = start_date
    while current <= end_date:
        batch_end = min(current + datetime.timedelta(days=batch_days - 1), end_date)
        yield current.strftime("%Y-%m-%d"), batch_end.strftime("%Y-%m-%d")
        current = batch_end + datetime.timedelta(days=1)


def clean_value(value):
    return None if pd.isna(value) else value


def main():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"Missing SQLite database: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    ensure_columns(conn)

    min_date, max_date = conn.execute("SELECT MIN(game_date), MAX(game_date) FROM pitches").fetchone()
    if not min_date or not max_date:
      raise RuntimeError("pitches table has no game_date range")

    start_date = datetime.date.fromisoformat(min_date)
    end_date = datetime.date.fromisoformat(max_date)
    print(f"Backfilling batted-ball data from {min_date} to {max_date}")

    update_sql = """
        UPDATE pitches
        SET launch_speed = ?,
            launch_angle = ?,
            bb_type = ?
        WHERE game_pk = ?
          AND at_bat_number = ?
          AND pitch_number = ?
    """

    for start_d, end_d in date_ranges(start_date, end_date, BATCH_DAYS):
        needs_update = conn.execute(
            """
            SELECT COUNT(*)
            FROM pitches
            WHERE game_date BETWEEN ? AND ?
              AND (launch_speed IS NULL OR launch_angle IS NULL OR bb_type IS NULL)
            """,
            (start_d, end_d),
        ).fetchone()[0]

        if needs_update == 0:
            print(f"Skipping {start_d} ~ {end_d}: already filled")
            continue

        print(f"Fetching {start_d} ~ {end_d}")
        df = statcast(start_d, end_d)
        if df is None or df.empty:
            continue

        required = {"game_pk", "at_bat_number", "pitch_number"}
        if not required.issubset(df.columns):
            print(f"Missing key columns for {start_d} ~ {end_d}; skipping")
            continue

        for column in TARGET_COLUMNS:
            if column not in df.columns:
                df[column] = None

        rows = [
            (
                clean_value(row.launch_speed),
                clean_value(row.launch_angle),
                clean_value(row.bb_type),
                int(row.game_pk),
                int(row.at_bat_number),
                int(row.pitch_number),
            )
            for row in df[
                ["game_pk", "at_bat_number", "pitch_number", "launch_speed", "launch_angle", "bb_type"]
            ].itertuples(index=False)
        ]

        conn.executemany(update_sql, rows)
        conn.commit()
        print(f"Updated {conn.total_changes:,} total row changes so far")
        time.sleep(1)

    conn.execute("VACUUM")
    conn.close()
    print("Batted-ball backfill complete.")


if __name__ == "__main__":
    main()
