import os
import sqlite3
from datetime import date

import psycopg2
import requests
from psycopg2.extras import execute_values


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
SQLITE_DB = os.getenv(
    "SQLITE_DB_PATH",
    os.path.join(DATA_DIR, "baseball_data_2024_2025.db"),
)
DATABASE_URL = os.getenv("DATABASE_URL")
CURRENT_YEAR = date.today().year
START_YEAR = int(os.getenv("PITCHER_STATS_START_YEAR", str(CURRENT_YEAR)))
END_YEAR = int(os.getenv("PITCHER_STATS_END_YEAR", str(CURRENT_YEAR)))

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pitcher_standard_stats (
    season INTEGER,
    pitcher INTEGER,
    player_name TEXT,
    team TEXT,
    league TEXT,
    bf INTEGER,
    w INTEGER,
    l INTEGER,
    era DOUBLE PRECISION,
    g INTEGER,
    gs INTEGER,
    sv INTEGER,
    ip TEXT,
    h INTEGER,
    r INTEGER,
    er INTEGER,
    hr INTEGER,
    bb INTEGER,
    so INTEGER,
    whip DOUBLE PRECISION,
    PRIMARY KEY (season, pitcher)
)
"""

COLUMNS = [
    "season",
    "pitcher",
    "player_name",
    "team",
    "league",
    "bf",
    "w",
    "l",
    "era",
    "g",
    "gs",
    "sv",
    "ip",
    "h",
    "r",
    "er",
    "hr",
    "bb",
    "so",
    "whip",
]

CONFLICT_COLUMNS = ["season", "pitcher"]
UPDATE_COLUMNS = [column for column in COLUMNS if column not in CONFLICT_COLUMNS]
POSTGRES_UPSERT_SQL = f"""
INSERT INTO pitcher_standard_stats ({', '.join(COLUMNS)}) VALUES %s
ON CONFLICT (season, pitcher) DO UPDATE SET
{', '.join(f'{column} = EXCLUDED.{column}' for column in UPDATE_COLUMNS)}
"""


def normalize_database_url(database_url):
    if database_url and database_url.startswith("DATABASE_URL="):
        database_url = database_url.split("=", 1)[1].strip().strip("'\"")
    if database_url and "sslmode=" not in database_url:
        separator = "&" if "?" in database_url else "?"
        return f"{database_url}{separator}sslmode=require"
    return database_url


def to_int(value):
    if value in {None, "", ".---"}:
        return None
    return int(value)


def to_float(value):
    if value in {None, "", ".---", "-.--"}:
        return None
    return float(value)


def fetch_year(year):
    response = requests.get(
        "https://statsapi.mlb.com/api/v1/stats",
        params={
            "stats": "season",
            "group": "pitching",
            "season": year,
            "playerPool": "all",
            "limit": 10000,
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    splits = data.get("stats", [{}])[0].get("splits", [])
    rows = []

    for split in splits:
        stat = split.get("stat") or {}
        player = split.get("player") or {}
        team = split.get("team") or {}
        league = split.get("league") or {}
        pitcher_id = player.get("id")
        if pitcher_id is None:
            continue

        rows.append((
            int(split.get("season") or year),
            int(pitcher_id),
            player.get("fullName"),
            team.get("abbreviation") or team.get("name"),
            league.get("name"),
            to_int(stat.get("battersFaced")),
            to_int(stat.get("wins")),
            to_int(stat.get("losses")),
            to_float(stat.get("era")),
            to_int(stat.get("gamesPitched") or stat.get("gamesPlayed")),
            to_int(stat.get("gamesStarted")),
            to_int(stat.get("saves")),
            stat.get("inningsPitched"),
            to_int(stat.get("hits")),
            to_int(stat.get("runs")),
            to_int(stat.get("earnedRuns")),
            to_int(stat.get("homeRuns")),
            to_int(stat.get("baseOnBalls")),
            to_int(stat.get("strikeOuts")),
            to_float(stat.get("whip")),
        ))

    return rows


def connect_target():
    database_url = normalize_database_url(DATABASE_URL)
    if database_url:
        return psycopg2.connect(database_url), "postgres"
    if not os.path.exists(SQLITE_DB):
        raise FileNotFoundError(f"Missing SQLite database: {SQLITE_DB}")
    return sqlite3.connect(SQLITE_DB), "sqlite"


def main():
    if START_YEAR > END_YEAR:
        raise ValueError("PITCHER_STATS_START_YEAR cannot be greater than PITCHER_STATS_END_YEAR")

    all_rows = []
    for year in range(START_YEAR, END_YEAR + 1):
        print(f"Fetching pitcher standard stats {year}")
        rows = fetch_year(year)
        print(f"Fetched {len(rows):,} rows for {year}")
        all_rows.extend(rows)

    if not all_rows:
        print("No pitcher standard stats returned; database was not changed.")
        return

    conn, kind = connect_target()
    placeholders = ", ".join(["%s"] * len(COLUMNS)) if kind == "postgres" else ", ".join(["?"] * len(COLUMNS))

    with conn:
        cursor = conn.cursor()
        cursor.execute(CREATE_TABLE_SQL)

        if kind == "postgres":
            print("Upserting into PostgreSQL pitcher_standard_stats...")
            execute_values(cursor, POSTGRES_UPSERT_SQL, all_rows, page_size=1000)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitcher_standard_pitcher ON pitcher_standard_stats(pitcher)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitcher_standard_season ON pitcher_standard_stats(season)")
        else:
            print(f"Upserting into local SQLite database: {SQLITE_DB}")
            update_sql = ", ".join(f"{column} = excluded.{column}" for column in UPDATE_COLUMNS)
            insert_sql = f"""
                INSERT INTO pitcher_standard_stats ({', '.join(COLUMNS)})
                VALUES ({placeholders})
                ON CONFLICT (season, pitcher) DO UPDATE SET {update_sql}
            """
            cursor.executemany(insert_sql, all_rows)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitcher_standard_pitcher ON pitcher_standard_stats(pitcher)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_pitcher_standard_season ON pitcher_standard_stats(season)")

    conn.close()
    print(f"Upserted {len(all_rows):,} pitcher standard stat rows for {START_YEAR}-{END_YEAR}.")


if __name__ == "__main__":
    main()
