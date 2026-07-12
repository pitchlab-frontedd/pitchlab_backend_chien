import os
import sys

try:
    import psycopg2
except ImportError:
    psycopg2 = None


REQUIRED_PITCH_COLUMNS = {
    "game_date",
    "pitch_type",
    "balls",
    "strikes",
    "stand",
    "p_throws",
    "pitcher_role",
    "outs_when_up",
    "on_1b",
    "on_2b",
    "on_3b",
    "description",
    "type",
    "zone",
    "player_name",
    "pitcher",
    "batter",
    "events",
    "launch_speed",
    "launch_angle",
    "bb_type",
    # ✨ 幫你補上 6 個新增的進階物理特徵欄位，讓健康檢查能驗證它們的存在
    "pfx_x",
    "pfx_z",
    "release_spin_rate",
    "release_pos_x",
    "release_pos_z",
    "release_extension",
}


def main():
    if psycopg2 is None:
        print("psycopg2-binary is not installed, so PostgreSQL cannot be used.")
        return 1

    print("Checking connection to aws-1-ap-south-1.pooler.supabase.com...")

    # ====================================================================================
    # 🎯 使用你指定的拆解參數邏輯，直接走 Pooler 安全通道進行檢查
    # ====================================================================================
    try:
        conn = psycopg2.connect(
            host="aws-1-ap-south-1.pooler.supabase.com",
            port=5432,
            user="postgres.huemnymfnigovthslkbz",
            password="guanipese911003",
            database="postgres",
            connect_timeout=10
        )
    except Exception as exc:
        print(f"Connection failed: {exc}")
        return 1

    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            db_name, user = cursor.fetchone()
            print(f"Connected as {user} to {db_name}")
            cursor.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
            db_size = cursor.fetchone()[0]
            print(f"Database size: {db_size}")

            cursor.execute(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
                ORDER BY tablename
                """
            )
            tables = [row[0] for row in cursor.fetchall()]
            print(f"Public tables: {', '.join(tables) if tables else '(none)'}")

            if "pitches" not in tables:
                print("Problem: missing required table 'pitches'. Run sqlite_to_postgres.py first.")
                return 1

            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'pitches'
                """
            )
            pitch_columns = {row[0] for row in cursor.fetchall()}
            missing_columns = sorted(REQUIRED_PITCH_COLUMNS - pitch_columns)
            if missing_columns:
                print(f"Problem: pitches is missing columns: {', '.join(missing_columns)}")
            else:
                print("pitches schema has the required API columns.")

            cursor.execute("SELECT COUNT(*) FROM pitches")
            pitch_count = cursor.fetchone()[0]
            print(f"pitches rows: {pitch_count:,}")
            if pitch_count == 0:
                print("Problem: pitches exists but has no rows.")

            cursor.execute("SELECT MIN(game_date), MAX(game_date) FROM pitches")
            min_date, max_date = cursor.fetchone()
            print(f"game_date range: {min_date} to {max_date}")

            if "player_names" not in tables:
                print("Warning: missing player_names. Run build_player_names.py to avoid slow startup/name fallback.")
            else:
                cursor.execute("SELECT COUNT(*) FROM player_names")
                name_count = cursor.fetchone()[0]
                print(f"player_names rows: {name_count:,}")
                if name_count == 0:
                    print("Warning: player_names is empty. Run build_player_names.py.")

            if missing_columns or pitch_count == 0:
                return 1

            print("Supabase/PostgreSQL check passed.")
            return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())