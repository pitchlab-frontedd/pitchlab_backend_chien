import os
import psycopg2

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_pitcher ON pitches(pitcher)",
    "CREATE INDEX IF NOT EXISTS idx_batter ON pitches(batter)",
    "CREATE INDEX IF NOT EXISTS idx_public_filters ON pitches(game_date, pitcher_role, pitch_type, balls, strikes)",
    "CREATE INDEX IF NOT EXISTS idx_stand ON pitches(stand)",
    # ✨ 幫你補上主程式裡有的這一個索引，確保資料庫效能最佳化
    "CREATE INDEX IF NOT EXISTS idx_pitch_sequence ON pitches(game_pk, at_bat_number, pitch_number)",
]

def main():
    # ====================================================================================
    # 🎯 使用你指定的連線邏輯，直接連上 Pooler 通道
    # ====================================================================================
    conn = psycopg2.connect(
        host="aws-1-ap-south-1.pooler.supabase.com",
        port=5432,
        user="postgres.huemnymfnigovthslkbz",
        password="guanipese911003",
        database="postgres"
    )
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