import os
import sqlite3

import psycopg2
from psycopg2.extras import execute_values

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
SQLITE_DB = os.getenv(
    "SQLITE_DB_PATH",
    os.path.join(DATA_DIR, "baseball_data_2024_2025.db"),
)

BATCH_SIZE = int(os.getenv("POSTGRES_IMPORT_BATCH_SIZE", "10000"))
INSERT_PAGE_SIZE = int(os.getenv("POSTGRES_INSERT_PAGE_SIZE", "5000"))
# ✨ 修改 2：設為 False，讓 Supabase 重新建立包含新欄位的資料表
RESUME_IMPORT = True

# ✨ 修改 3：把 6 個新欄位加入欄位清單
COLUMNS = [
    "game_pk",
    "at_bat_number",
    "pitch_number",
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
    "pfx_x",
    "pfx_z",
    "release_spin_rate",
    "release_pos_x",
    "release_pos_z",
    "release_extension",
]

# ✨ 修改 4：在 SQL 建表語法中，宣告這 6 個新欄位的資料型態 (DOUBLE PRECISION)
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pitches (
    game_pk INTEGER,
    at_bat_number INTEGER,
    pitch_number INTEGER,
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
    is_out INTEGER,
    pfx_x DOUBLE PRECISION,
    pfx_z DOUBLE PRECISION,
    release_spin_rate DOUBLE PRECISION,
    release_pos_x DOUBLE PRECISION,
    release_pos_z DOUBLE PRECISION,
    release_extension DOUBLE PRECISION
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_pitcher ON pitches(pitcher)",
    "CREATE INDEX IF NOT EXISTS idx_batter ON pitches(batter)",
    "CREATE INDEX IF NOT EXISTS idx_public_filters ON pitches(game_date, pitcher_role, pitch_type, balls, strikes)",
    "CREATE INDEX IF NOT EXISTS idx_stand ON pitches(stand)",
    "CREATE INDEX IF NOT EXISTS idx_pitch_sequence ON pitches(game_pk, at_bat_number, pitch_number)",
]

# ✨ 修改 5：把新欄位加入 FLOAT 判定，確保傳上雲端時都是數字
FLOAT_COLUMNS = {
    "delta_run_exp",
    "delta_home_win_exp",
    "pitcher_wpa",
    "release_speed",
    "plate_x",
    "plate_z",
    "launch_speed",
    "launch_angle",
    "pfx_x",
    "pfx_z",
    "release_spin_rate",
    "release_pos_x",
    "release_pos_z",
    "release_extension",
}

INTEGER_COLUMNS = {
    "balls",
    "strikes",
    "game_pk",
    "at_bat_number",
    "pitch_number",
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
    if not os.path.exists(SQLITE_DB):
        raise FileNotFoundError(f"Missing SQLite database: {SQLITE_DB}")

    source = sqlite3.connect(SQLITE_DB)
    source.row_factory = sqlite3.Row
    total = source.execute("SELECT COUNT(*) FROM pitches").fetchone()[0]

    target = psycopg2.connect(
        host="aws-1-ap-south-1.pooler.supabase.com",
        port=5432,
        user="postgres.huemnymfnigovthslkbz",
        password="guanipese911003",
        database="postgres"
    )
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