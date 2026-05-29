import sqlite3
import pandas as pd
from pybaseball import statcast
import pybaseball
import datetime
import time
import os

# 開啟緩存，減少重複抓取負擔
pybaseball.cache.enable()

def create_database():
    db_file = os.getenv("BASEBALL_DB_FILENAME", "baseball_data.db")
    
    # 💡 專業建議：如果你發現舊資料沒名字或沒出局標記，建議刪除原有的 baseball_data.db 重新執行
    conn = sqlite3.connect(db_file)
    start_year = int(os.getenv("START_YEAR", "2024"))
    current_year = int(os.getenv("END_YEAR", "2025"))
    only_start_date = os.getenv("START_DATE")
    only_end_date = os.getenv("END_DATE")
    
    print(f"🚀 開始建立 {start_year}-{current_year} 棒球資料庫 (含姓名、出局標記與篩選器索引)...")

    if only_start_date and only_end_date:
        periods = [(int(only_start_date[:4]), [(only_start_date, only_end_date)])]
    else:
        periods = []
        for year in range(start_year, current_year + 1):
            year_periods = []
            for month in range(3, 12):
                if month == 3:
                    year_periods.append((f"{year}-03-20", f"{year}-03-31"))
                else:
                    year_periods.extend([
                        (f"{year}-{month:02d}-01", f"{year}-{month:02d}-15"),
                        (f"{year}-{month:02d}-16", f"{year}-{month:02d}-28")
                    ])
            periods.append((year, year_periods))

    for year, sub_periods in periods:
            
        for start_d, end_d in sub_periods:
                if start_d > datetime.date.today().strftime('%Y-%m-%d'): continue

                try:
                    # 檢查機制
                    check_query = f"SELECT count(*) FROM pitches WHERE game_date BETWEEN '{start_d}' AND '{end_d}'"
                    try:
                        count = pd.read_sql(check_query, conn).iloc[0, 0]
                        if count > 0:
                            print(f"⏩ 跳過 {start_d} (資料庫已有 {count} 筆)")
                            continue
                    except:
                        pass

                    print(f"📡 正在抓取 {start_d} ~ {end_d}...")
                    df = statcast(start_d, end_d)
                    
                    if df is None or df.empty:
                        continue

                    # 投手角色判斷
                    starters = df[df['inning'] == 1][['game_pk', 'pitcher']].drop_duplicates()
                    starters['pitcher_role_new'] = 'SP'
                    df = df.merge(starters, on=['game_pk', 'pitcher'], how='left')
                    df['pitcher_role'] = df['pitcher_role_new'].fillna('RP')

                    # 壘包處理 (轉為 0/1)
                    for col in ['on_1b', 'on_2b', 'on_3b']:
                        df[col] = df[col].notna().astype(int)

                    # 🎯 【核心欄位】：包含視覺化、統計、與搜尋所需的 ID/姓名
                    # balls, strikes (對應 COUNT), pitch_type (對應 PITCH TYPE) 已經在裡面了！
                    keep_cols = [
                        'game_pk', 'at_bat_number', 'pitch_number',
                        'game_date', 'pitch_type', 'balls', 'strikes', 'stand', 'p_throws', 
                        'on_1b', 'on_2b', 'on_3b', 'pitcher_role', 'inning', 'outs_when_up',
                        'bat_score', 'fld_score', 'post_bat_score', 'post_fld_score',
                        'home_score', 'away_score', 'post_home_score', 'post_away_score',
                        'inning_topbot', 'delta_run_exp', 'delta_home_win_exp',
                        'home_win_exp', 'bat_win_exp',
                        'release_speed', 'plate_x', 'plate_z', 'description', 'type', 
                        'zone', 'player_name', 'pitcher', 'batter', 'events'
                    ]
                    
                    # 只選取存在的欄位
                    df_to_save = df[[c for c in keep_cols if c in df.columns]].copy()
                    
                    # 確保 player_name 沒名字時給 Unknown，避免 API 報錯
                    if 'player_name' in df_to_save.columns:
                        df_to_save['player_name'] = df_to_save['player_name'].fillna('Unknown')

                    # ✨ 【新增 1】加入 is_out 出局標記 (解決九宮格 Out% = 0 的問題)
                    out_events = [
                        'field_out', 'strikeout', 'force_out', 'grounded_into_double_play', 
                        'fielders_choice', 'fielders_choice_out', 'double_play', 
                        'sac_fly', 'sac_bunt', 'strikeout_double_play'
                    ]
                    if 'events' in df_to_save.columns:
                        df_to_save['is_out'] = df_to_save['events'].isin(out_events).astype(int)
                    else:
                        df_to_save['is_out'] = 0

                    # ✨ 【新增 2】把對應 COUNT 篩選器的 balls 和 strikes 加入空值排除名單
                    # 避免前端傳入 0-0 卻因為資料庫有空值而報錯
                    df_to_save = df_to_save.dropna(subset=['pitch_type', 'plate_x', 'plate_z', 'balls', 'strikes'])

                    # 強制將球數轉為整數
                    if 'game_date' in df_to_save.columns:
                        df_to_save['game_date'] = pd.to_datetime(df_to_save['game_date']).dt.strftime('%Y-%m-%d')
                    df_to_save['balls'] = df_to_save['balls'].astype(int)
                    df_to_save['strikes'] = df_to_save['strikes'].astype(int)
                    if 'outs_when_up' in df_to_save.columns:
                        df_to_save['outs_when_up'] = df_to_save['outs_when_up'].fillna(0).astype(int)
                    if {'bat_score', 'post_bat_score'}.issubset(df_to_save.columns):
                        df_to_save['runs_on_pa'] = (
                            pd.to_numeric(df_to_save['post_bat_score'], errors='coerce')
                            - pd.to_numeric(df_to_save['bat_score'], errors='coerce')
                        ).fillna(0).astype(int)
                    else:
                        df_to_save['runs_on_pa'] = 0

                    if 'delta_run_exp' in df_to_save.columns:
                        df_to_save['delta_run_exp'] = pd.to_numeric(
                            df_to_save['delta_run_exp'],
                            errors='coerce',
                        ).fillna(0)
                    else:
                        df_to_save['delta_run_exp'] = 0.0

                    if 'home_win_exp' in df_to_save.columns:
                        df_to_save['home_win_exp'] = pd.to_numeric(
                            df_to_save['home_win_exp'],
                            errors='coerce',
                        )
                    else:
                        df_to_save['home_win_exp'] = None

                    if 'delta_home_win_exp' in df_to_save.columns:
                        df_to_save['delta_home_win_exp'] = pd.to_numeric(
                            df_to_save['delta_home_win_exp'],
                            errors='coerce',
                        ).fillna(0)
                    elif {'game_pk', 'at_bat_number', 'pitch_number', 'home_win_exp'}.issubset(df_to_save.columns):
                        order_cols = ['game_pk', 'at_bat_number', 'pitch_number']
                        df_to_save = df_to_save.sort_values(order_cols)
                        df_to_save['delta_home_win_exp'] = (
                            df_to_save.groupby('game_pk')['home_win_exp'].diff().fillna(0)
                        )
                    else:
                        df_to_save['delta_home_win_exp'] = 0.0

                    if 'inning_topbot' in df_to_save.columns:
                        is_bottom = df_to_save['inning_topbot'].astype(str).str.lower().str.startswith('bot')
                        df_to_save['pitcher_wpa'] = df_to_save['delta_home_win_exp'].where(
                            ~is_bottom,
                            -df_to_save['delta_home_win_exp'],
                        )
                    else:
                        df_to_save['pitcher_wpa'] = 0.0

                    # 存入資料庫
                    df_to_save.to_sql("pitches", conn, if_exists="append", index=False)
                    print(f"✅ 成功儲存 {len(df_to_save)} 筆 (年份: {year})")
                    
                    # ⚡ 每一批寫入後建立/更新索引
                    # ✨ 【新增 3】幫篩選器的變數加上索引，點擊篩選時載入會變極快
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_pname ON pitches(player_name)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_gdate ON pitches(game_date)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_ptype ON pitches(pitch_type)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_count ON pitches(balls, strikes)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_outs ON pitches(outs_when_up)")
                    
                    time.sleep(1)

                except KeyboardInterrupt:
                    print("\n🛑 手動停止。正在關閉資料庫...")
                    conn.close()
                    return
                except Exception as e:
                    print(f"⚠️ {start_d} 失敗: {e}")
                    time.sleep(5)

    conn.close()
    print("🎉 資料庫補完完畢！所有球員名字、出局標記與篩選器資料已就緒。")

if __name__ == "__main__":
    create_database()
