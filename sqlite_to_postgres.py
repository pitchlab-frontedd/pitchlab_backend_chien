import os
import sqlite3

import psycopg2
from psycopg2.extras import execute_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SQLITE_DB = os.getenv(
    "SQLITE_DB_PATH",
    os.path.join(BASE_DIR, "baseball_data_2024_2025.db"),
)
DATABASE_URL = os.getenv("DATABASE_URL")
BATCH_SIZE = int(os.getenv("POSTGRES_IMPORT_BATCH_SIZE", "10000"))


def normalize_database_url(database_url):
    if database_url and database_url.startswith("DATABASE_URL="):
        print("Detected DATABASE_URL= inside the connection string; using the value after '='.")
        return database_url.split("=", 1)[1].strip().strip("'\"")
    return database_url


DATABASE_URL = normalize_database_url(DATABASE_URL)

COLUMNS = [
    "game_date",
    "pitch_type",
    "balls",
    "strikes",
    "stand",
    "p_throws",
    "on_1b",
    "on_2b",
    "on_3b",
    "pitcher_role",
    "outs_when_up",
    "inning_topbot",
    "delta_run_exp",
    "delta_home_win_exp",
    "pitcher_wpa",
    "runs_on_pa",
    "release_speed",
    "plate_x",
    "plate_z",
    "description",
    "type",
    "zone",
    "player_name",
    "pitcher",
    "batter",
    "events",
    "is_out",
]

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pitches (
    game_date TEXT,
    pitch_type TEXT,
    balls INTEGER,
    strikes INTEGER,
    stand TEXT,
    p_throws TEXT,
    on_1b INTEGER,
    on_2b INTEGER,
    on_3b INTEGER,
    pitcher_role TEXT,
    outs_when_up INTEGER,
    inning_topbot TEXT,
    delta_run_exp DOUBLE PRECISION,
    delta_home_win_exp DOUBLE PRECISION,
    pitcher_wpa DOUBLE PRECISION,
    runs_on_pa INTEGER,
    release_speed DOUBLE PRECISION,
    plate_x DOUBLE PRECISION,
    plate_z DOUBLE PRECISION,
    description TEXT,
    type TEXT,
    zone INTEGER,
    player_name TEXT,
    pitcher INTEGER,
    batter INTEGER,
    events TEXT,
    is_out INTEGER
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_pitcher ON pitches(pitcher)",
    "CREATE INDEX IF NOT EXISTS idx_batter ON pitches(batter)",
    "CREATE INDEX IF NOT EXISTS idx_public_filters ON pitches(game_date, pitcher_role, pitch_type, balls, strikes)",
    "CREATE INDEX IF NOT EXISTS idx_stand ON pitches(stand)",
]


def main():
    if not DATABASE_URL:
        raise RuntimeError("Set DATABASE_URL to your PostgreSQL connection string first.")
    if not os.path.exists(SQLITE_DB):
        raise FileNotFoundError(f"Missing SQLite database: {SQLITE_DB}")

    source = sqlite3.connect(SQLITE_DB)
    source.row_factory = sqlite3.Row
    total = source.execute("SELECT COUNT(*) FROM pitches").fetchone()[0]

    target = psycopg2.connect(DATABASE_URL)
    target.autocommit = False

    with target.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS player_names")
        cursor.execute("DROP TABLE IF EXISTS pitches")
        cursor.execute(CREATE_TABLE_SQL)
        target.commit()

    source_columns = {
        row[1]
        for row in source.execute("PRAGMA table_info(pitches)").fetchall()
    }
    select_columns = [
        column if column in source_columns else f"NULL AS {column}"
        for column in COLUMNS
    ]
    select_sql = f"SELECT {', '.join(select_columns)} FROM pitches"
    insert_sql = f"INSERT INTO pitches ({', '.join(COLUMNS)}) VALUES %s"
    source_cursor = source.execute(select_sql)

    imported = 0
    with target.cursor() as cursor:
        while True:
            rows = source_cursor.fetchmany(BATCH_SIZE)
            if not rows:
                break

            values = [tuple(row[column] for column in COLUMNS) for row in rows]
            execute_values(cursor, insert_sql, values, page_size=BATCH_SIZE)
            target.commit()

            imported += len(rows)
            print(f"Imported {imported:,}/{total:,} rows")

        for sql in INDEXES:
            print(sql)
            cursor.execute(sql)
            target.commit()

        cursor.execute("ANALYZE pitches")
        target.commit()

    source.close()
    target.close()
    print("PostgreSQL import complete.")


if __name__ == "__main__":
    main()
