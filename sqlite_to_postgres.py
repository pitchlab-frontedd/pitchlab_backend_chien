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
INSERT_PAGE_SIZE = int(os.getenv("POSTGRES_INSERT_PAGE_SIZE", "5000"))
RESUME_IMPORT = os.getenv("POSTGRES_IMPORT_RESUME", "").lower() in {"1", "true", "yes"}


def normalize_database_url(database_url):
    if database_url and database_url.startswith("DATABASE_URL="):
        print("Detected DATABASE_URL= inside the connection string; using the value after '='.")
        database_url = database_url.split("=", 1)[1].strip().strip("'\"")
    if database_url and "sslmode=" not in database_url:
        separator = "&" if "?" in database_url else "?"
        return f"{database_url}{separator}sslmode=require"
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
    "launch_speed",
    "launch_angle",
    "bb_type",
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
    launch_speed DOUBLE PRECISION,
    launch_angle DOUBLE PRECISION,
    bb_type TEXT,
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

FLOAT_COLUMNS = {
    "delta_run_exp",
    "delta_home_win_exp",
    "pitcher_wpa",
    "release_speed",
    "plate_x",
    "plate_z",
    "launch_speed",
    "launch_angle",
}

INTEGER_COLUMNS = {
    "balls",
    "strikes",
    "on_1b",
    "on_2b",
    "on_3b",
    "outs_when_up",
    "runs_on_pa",
    "zone",
    "pitcher",
    "batter",
    "is_out",
}


def decode_numeric_blob(value):
    if value is None or not isinstance(value, (bytes, bytearray, memoryview)):
        return value

    raw = bytes(value)
    try:
        return float(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        pass

    if len(raw) in {1, 2, 4, 8}:
        return float(int.from_bytes(raw, byteorder="little", signed=True))

    return None


def clean_value(column, value):
    value = decode_numeric_blob(value)
    if value is None:
        return None

    if column in FLOAT_COLUMNS:
        return float(value)
    if column in INTEGER_COLUMNS:
        return int(value)
    return value


def chunked(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


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
        if RESUME_IMPORT:
            cursor.execute(CREATE_TABLE_SQL)
            target.commit()
            cursor.execute("SELECT COUNT(*) FROM pitches")
            imported = cursor.fetchone()[0]
            print(f"Resuming from existing PostgreSQL rows: {imported:,}/{total:,}")
        else:
            cursor.execute("DROP TABLE IF EXISTS player_names")
            cursor.execute("DROP TABLE IF EXISTS pitcher_standard_stats")
            cursor.execute("DROP TABLE IF EXISTS pitches")
            cursor.execute(CREATE_TABLE_SQL)
            target.commit()
            imported = 0

    source_columns = {
        row[1]
        for row in source.execute("PRAGMA table_info(pitches)").fetchall()
    }
    select_columns = [
        column if column in source_columns else f"NULL AS {column}"
        for column in COLUMNS
    ]
    select_sql = f"SELECT {', '.join(select_columns)} FROM pitches ORDER BY rowid"
    if imported:
        select_sql = f"{select_sql} LIMIT -1 OFFSET {imported}"
    insert_sql = f"INSERT INTO pitches ({', '.join(COLUMNS)}) VALUES %s"
    source_cursor = source.execute(select_sql)

    with target.cursor() as cursor:
        while True:
            rows = source_cursor.fetchmany(BATCH_SIZE)
            if not rows:
                break

            values = [
                tuple(clean_value(column, row[column]) for column in COLUMNS)
                for row in rows
            ]

            for value_page in chunked(values, INSERT_PAGE_SIZE):
                execute_values(cursor, insert_sql, value_page, page_size=len(value_page))
                target.commit()
                imported += len(value_page)
                print(f"Imported {imported:,}/{total:,} rows")

        for sql in INDEXES:
            print(sql)
            cursor.execute("SET statement_timeout = 0")
            cursor.execute(sql)
            target.commit()

        cursor.execute("SET statement_timeout = 0")
        cursor.execute("ANALYZE pitches")
        target.commit()

    source.close()
    target.close()
    print("PostgreSQL import complete.")


if __name__ == "__main__":
    main()
