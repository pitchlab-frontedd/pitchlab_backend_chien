import os

import psycopg2
import requests
from psycopg2.extras import Json, execute_values


TARGET_DATABASE_URL = os.getenv("DATABASE_URL")
SOURCE_API_BASE_URL = os.getenv(
    "SOURCE_API_BASE_URL",
    "https://pitchlab-backend-chien-7f7b.onrender.com",
).rstrip("/")

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pitcher_similarities (
    id BIGSERIAL PRIMARY KEY,
    target_pitcher VARCHAR(100) NOT NULL,
    similar_pitcher VARCHAR(100) NOT NULL,
    rank INTEGER NOT NULL,
    distance_score NUMERIC,
    target_stats JSONB,
    similar_stats JSONB,
    UNIQUE (target_pitcher, rank)
)
"""

UPSERT_SQL = """
INSERT INTO pitcher_similarities (
    target_pitcher,
    similar_pitcher,
    rank,
    distance_score,
    target_stats,
    similar_stats
) VALUES %s
ON CONFLICT (target_pitcher, rank) DO UPDATE SET
    similar_pitcher = EXCLUDED.similar_pitcher,
    distance_score = EXCLUDED.distance_score,
    target_stats = EXCLUDED.target_stats,
    similar_stats = EXCLUDED.similar_stats
"""


def normalize_database_url(database_url):
    if database_url and database_url.startswith("DATABASE_URL="):
        database_url = database_url.split("=", 1)[1].strip().strip("'\"")
    if database_url and "sslmode=" not in database_url:
        separator = "&" if "?" in database_url else "?"
        return f"{database_url}{separator}sslmode=require"
    return database_url


def require_database_url(name, value):
    if not value:
        raise RuntimeError(f"Set {name} before running this migration.")
    return normalize_database_url(value)


def main():
    target_url = require_database_url("DATABASE_URL", TARGET_DATABASE_URL)

    print(f"Reading pitcher names from {SOURCE_API_BASE_URL}...")
    response = requests.get(f"{SOURCE_API_BASE_URL}/api/pitchers", timeout=90)
    response.raise_for_status()
    pitchers = response.json()
    if not isinstance(pitchers, list):
        raise RuntimeError("Source /api/pitchers did not return a list.")

    rows = []
    for index, pitcher in enumerate(pitchers, start=1):
        pitcher_name = pitcher.get("name")
        if not pitcher_name:
            continue

        response = requests.get(
            f"{SOURCE_API_BASE_URL}/api/pitcher-similarities",
            params={"pitcher_name": pitcher_name},
            timeout=90,
        )
        response.raise_for_status()
        similarities = response.json()
        if isinstance(similarities, list):
            for similarity in similarities:
                similar_pitcher = similarity.get("similar_pitcher")
                rank = similarity.get("rank")
                if not similar_pitcher or rank is None:
                    continue
                rows.append({
                    "target_pitcher": pitcher_name,
                    "similar_pitcher": similar_pitcher,
                    "rank": rank,
                    "distance_score": similarity.get("distance_score"),
                    "target_stats": None,
                    "similar_stats": similarity.get("similar_stats"),
                })

        if index % 100 == 0:
            print(f"Checked {index:,}/{len(pitchers):,} pitchers; collected {len(rows):,} rows.")

    if not rows:
        raise RuntimeError("The source similarity API returned no rows; target was not changed.")

    unique_rows = {}
    for row in rows:
        key = (row["target_pitcher"], int(row["rank"]))
        unique_rows[key] = row

    duplicate_count = len(rows) - len(unique_rows)
    rows = list(unique_rows.values())
    if duplicate_count:
        print(f"Removed {duplicate_count:,} duplicate target/rank rows before upsert.")

    values = [
        (
            row["target_pitcher"],
            row["similar_pitcher"],
            row["rank"],
            row["distance_score"],
            Json(row["target_stats"]),
            Json(row["similar_stats"]),
        )
        for row in rows
    ]

    target_conn = psycopg2.connect(target_url)
    try:
        with target_conn:
            with target_conn.cursor() as cursor:
                cursor.execute(CREATE_TABLE_SQL)
                execute_values(cursor, UPSERT_SQL, values, page_size=1000)
                cursor.execute("SELECT COUNT(*) FROM pitcher_similarities")
                target_count = cursor.fetchone()[0]
    finally:
        target_conn.close()

    print(f"Copied/upserted {len(values):,} similarity rows.")
    print(f"Target pitcher_similarities now contains {target_count:,} rows.")


if __name__ == "__main__":
    main()
