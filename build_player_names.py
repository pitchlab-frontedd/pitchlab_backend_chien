import os
import sqlite3
import time

import pandas as pd
import requests
from pybaseball import playerid_reverse_lookup

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    psycopg2 = None
    execute_values = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SQLITE_DB = os.getenv(
    "SQLITE_DB_PATH",
    os.path.join(BASE_DIR, "baseball_data_2024_2025.db"),
)
DATABASE_URL = os.getenv("DATABASE_URL")
BATCH_SIZE = int(os.getenv("PLAYER_LOOKUP_BATCH_SIZE", "500"))
MLB_API_BATCH_SIZE = int(os.getenv("MLB_API_LOOKUP_BATCH_SIZE", "100"))


def normalize_database_url(database_url):
    if database_url and database_url.startswith("DATABASE_URL="):
        print("Detected DATABASE_URL= inside the connection string; using the value after '='.")
        return database_url.split("=", 1)[1].strip().strip("'\"")
    return database_url


DATABASE_URL = normalize_database_url(DATABASE_URL)


def using_postgres():
    return bool(DATABASE_URL)


def connect_db():
    if using_postgres():
        if psycopg2 is None:
            raise RuntimeError("psycopg2-binary is required when DATABASE_URL is set")
        return psycopg2.connect(DATABASE_URL)

    if not os.path.exists(SQLITE_DB):
        raise FileNotFoundError(f"Missing SQLite database: {SQLITE_DB}")
    return sqlite3.connect(SQLITE_DB)


def fetch_player_ids(conn):
    query = """
        SELECT batter AS player_id FROM pitches WHERE batter IS NOT NULL
        UNION
        SELECT pitcher AS player_id FROM pitches WHERE pitcher IS NOT NULL
    """
    df = pd.read_sql(query, conn)
    return sorted({int(v) for v in df["player_id"].dropna().tolist()})


def create_table(conn):
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS player_names (
            player_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL
        )
        """
    )
    conn.commit()
    cursor.close()


def upsert_names(conn, rows):
    if not rows:
        return

    cursor = conn.cursor()
    if using_postgres():
        execute_values(
            cursor,
            """
            INSERT INTO player_names (player_id, name)
            VALUES %s
            ON CONFLICT (player_id) DO UPDATE SET name = EXCLUDED.name
            """,
            rows,
        )
    else:
        cursor.executemany(
            """
            INSERT INTO player_names (player_id, name)
            VALUES (?, ?)
            ON CONFLICT(player_id) DO UPDATE SET name = excluded.name
            """,
            rows,
        )
    conn.commit()
    cursor.close()


def format_name(last_name, first_name):
    return f"{str(last_name).title()}, {str(first_name).title()}"


def lookup_pybaseball_names(player_ids):
    lookup_df = playerid_reverse_lookup(player_ids, key_type="mlbam")
    return [
        (
            int(row["key_mlbam"]),
            format_name(row["name_last"], row["name_first"]),
        )
        for _, row in lookup_df.iterrows()
    ]


def lookup_mlb_api_names(player_ids):
    rows = []
    for i in range(0, len(player_ids), MLB_API_BATCH_SIZE):
        chunk = player_ids[i:i + MLB_API_BATCH_SIZE]
        response = requests.get(
            "https://statsapi.mlb.com/api/v1/people",
            params={"personIds": ",".join(str(player_id) for player_id in chunk)},
            timeout=20,
        )
        response.raise_for_status()
        for person in response.json().get("people", []):
            player_id = person.get("id")
            first_name = person.get("useName") or person.get("firstName")
            last_name = person.get("lastName")
            full_name = person.get("fullName")
            if player_id and first_name and last_name:
                rows.append((int(player_id), format_name(last_name, first_name)))
            elif player_id and full_name:
                parts = full_name.rsplit(" ", 1)
                name = f"{parts[1]}, {parts[0]}" if len(parts) == 2 else full_name
                rows.append((int(player_id), name))
        time.sleep(0.1)
    return rows


def main():
    conn = connect_db()
    create_table(conn)

    player_ids = fetch_player_ids(conn)
    print(f"Found {len(player_ids):,} unique MLBAM player ids")

    saved = 0
    for i in range(0, len(player_ids), BATCH_SIZE):
        chunk = player_ids[i:i + BATCH_SIZE]
        rows = lookup_pybaseball_names(chunk)
        found_ids = {player_id for player_id, _ in rows}
        missing_ids = [player_id for player_id in chunk if player_id not in found_ids]

        if missing_ids:
            try:
                fallback_rows = lookup_mlb_api_names(missing_ids)
                rows.extend(fallback_rows)
                found_ids.update(player_id for player_id, _ in fallback_rows)
                still_missing = len(missing_ids) - len(fallback_rows)
                if still_missing:
                    print(f"Missing names for {still_missing} ids in this batch")
            except Exception as exc:
                print(f"MLB Stats API fallback failed for {len(missing_ids)} ids: {exc}")

        upsert_names(conn, rows)
        saved += len(rows)
        print(f"Saved {saved:,}/{len(player_ids):,} names")
        time.sleep(0.2)

    conn.close()
    print("Player names table is ready.")


if __name__ == "__main__":
    main()
