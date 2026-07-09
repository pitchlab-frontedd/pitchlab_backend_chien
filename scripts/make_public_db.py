import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
SOURCE_DB = os.getenv("SOURCE_DB_PATH", os.path.join(DATA_DIR, "baseball_data.db"))
OUTPUT_DB = os.getenv("OUTPUT_DB_PATH", os.path.join(DATA_DIR, "baseball_data_2024_2025.db"))
START_YEAR = "2024"
END_YEAR = "2025"

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_game_date ON pitches(game_date)",
    "CREATE INDEX IF NOT EXISTS idx_player_name ON pitches(player_name)",
    "CREATE INDEX IF NOT EXISTS idx_pitcher ON pitches(pitcher)",
    "CREATE INDEX IF NOT EXISTS idx_batter ON pitches(batter)",
    "CREATE INDEX IF NOT EXISTS idx_pitch_type ON pitches(pitch_type)",
    "CREATE INDEX IF NOT EXISTS idx_zone ON pitches(zone)",
    "CREATE INDEX IF NOT EXISTS idx_count ON pitches(balls, strikes)",
    "CREATE INDEX IF NOT EXISTS idx_outs ON pitches(outs_when_up)",
    "CREATE INDEX IF NOT EXISTS idx_public_filters ON pitches(pitcher, batter, game_date, pitcher_role)",
]


def main():
    if not os.path.exists(SOURCE_DB):
        raise FileNotFoundError(f"Missing source database: {SOURCE_DB}")

    os.makedirs(os.path.dirname(OUTPUT_DB), exist_ok=True)
    if os.path.exists(OUTPUT_DB):
        os.remove(OUTPUT_DB)

    source = sqlite3.connect(SOURCE_DB)
    source.execute("ATTACH DATABASE ? AS public_db", (OUTPUT_DB,))
    source.execute(
        """
        CREATE TABLE public_db.pitches AS
        SELECT *
        FROM main.pitches
        WHERE substr(game_date, 1, 4) BETWEEN ? AND ?
        """,
        (START_YEAR, END_YEAR),
    )
    source.commit()
    source.close()

    public = sqlite3.connect(OUTPUT_DB)
    for sql in INDEXES:
        public.execute(sql)
    public.execute("VACUUM")
    count = public.execute("SELECT COUNT(*) FROM pitches").fetchone()[0]
    public.close()

    size_mb = os.path.getsize(OUTPUT_DB) / (1024 * 1024)
    print(f"Created {OUTPUT_DB}")
    print(f"Rows: {count:,}")
    print(f"Size: {size_mb:.1f} MB")


if __name__ == "__main__":
    main()
