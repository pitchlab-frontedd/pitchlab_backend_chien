import os

import psycopg2


DATABASE_URL = os.getenv("DATABASE_URL")

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_pitcher ON pitches(pitcher)",
    "CREATE INDEX IF NOT EXISTS idx_batter ON pitches(batter)",
    "CREATE INDEX IF NOT EXISTS idx_public_filters ON pitches(game_date, pitcher_role, pitch_type, balls, strikes)",
    "CREATE INDEX IF NOT EXISTS idx_stand ON pitches(stand)",
]


def normalize_database_url(database_url):
    if database_url and database_url.startswith("DATABASE_URL="):
        database_url = database_url.split("=", 1)[1].strip().strip("'\"")
    if database_url and "sslmode=" not in database_url:
        separator = "&" if "?" in database_url else "?"
        return f"{database_url}{separator}sslmode=require"
    return database_url


def main():
    database_url = normalize_database_url(DATABASE_URL)
    if not database_url:
        raise RuntimeError("Set DATABASE_URL to your PostgreSQL connection string first.")

    conn = psycopg2.connect(database_url)
    conn.autocommit = True

    with conn.cursor() as cursor:
        cursor.execute("SET statement_timeout = 0")
        for sql in INDEXES:
            print(sql)
            cursor.execute(sql)

        print("ANALYZE pitches")
        cursor.execute("ANALYZE pitches")

    conn.close()
    print("PostgreSQL import finishing steps complete.")


if __name__ == "__main__":
    main()
