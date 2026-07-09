import sqlite3
import os
import pandas as pd
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import train_test_split
from sklearn.linear_model import LogisticRegression
import joblib

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.getenv("BASEBALL_DB_FILENAME", os.path.join(DATA_DIR, "baseball_data.db"))
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.join(DATA_DIR, DB_PATH)

# 1. 撈取資料
print("Reading data from local database...")
conn = sqlite3.connect(DB_PATH)
# ⚠️ 注意：這步會吃掉很多記憶體，若有 700 萬筆，建議記憶體要夠。
df = pd.read_sql("SELECT * FROM pitches", conn)
print("Original shape:", df.shape)

# 2. 特徵工程 (處理投手角色)
if 'pitcher_role_y' in df.columns:
    df['pitcher_role'] = df['pitcher_role_y'].fillna('RP')
elif 'pitcher_role' in df.columns:
    df['pitcher_role'] = df['pitcher_role'].fillna('RP')
else:
    df['pitcher_role'] = 'RP'

# 3. 🎯 【關鍵修改】補齊人名與 ID 欄位
# 這裡一定要包含 player_name, pitcher, batter，否則前端選單會消失
keep_cols = [
    'player_name', 'pitcher', 'batter',        # ✨ 補回這三個！
    'pitch_type', 'balls', 'strikes', 'stand', 'p_throws', 
    'on_1b', 'on_2b', 'on_3b', 'pitcher_role', 'game_date',
    'release_speed', 'plate_x', 'plate_z', 'description', 'type', 'zone','events'
]

# 只選取存在的欄位
existing_cols = [c for c in keep_cols if c in df.columns]
df_final = df[existing_cols].copy()

# 🎯 【新增邏輯 1】計算並加入出局標記 (解決九宮格 Out% 為 0)
out_events = [
    'field_out', 'strikeout', 'force_out', 'grounded_into_double_play', 
    'fielders_choice', 'fielders_choice_out', 'double_play', 
    'sac_fly', 'sac_bunt', 'strikeout_double_play'
]
if 'events' in df_final.columns:
    df_final['is_out'] = df_final['events'].isin(out_events).astype(int)
else:
    df_final['is_out'] = 0

# 🎯 【新增邏輯 2】處理球數 (對應前端 COUNT 篩選器)
# 將 balls 和 strikes 強制轉為整數，避免空值造成前端篩選器錯誤
if 'balls' in df_final.columns and 'strikes' in df_final.columns:
    df_final['balls'] = pd.to_numeric(df_final['balls'], errors='coerce').fillna(0).astype(int)
    df_final['strikes'] = pd.to_numeric(df_final['strikes'], errors='coerce').fillna(0).astype(int)

# 4. 處理壘包狀態
for col in ['on_1b', 'on_2b', 'on_3b']:
    if col in df_final.columns:
        df_final[col] = pd.to_numeric(df_final[col], errors='coerce').fillna(0)
        df_final[col] = (df_final[col] > 0).astype(int)

# 準備訓練集 (過濾掉預測特徵為空的資料)
df_train = df_final.dropna(subset=['balls', 'strikes', 'stand', 'p_throws', 'pitch_type']).copy()

# 5. Label Encoding (維持原樣)
print("Encoding categorical features...")
le_stand = LabelEncoder()
df_train['stand'] = le_stand.fit_transform(df_train['stand'].astype(str))

le_throw = LabelEncoder()
df_train['p_throws'] = le_throw.fit_transform(df_train['p_throws'].astype(str))

le_pitch = LabelEncoder()
df_train['pitch_type'] = le_pitch.fit_transform(df_train['pitch_type'].astype(str))

le_role = LabelEncoder() 
df_train['pitcher_role'] = le_role.fit_transform(df_train['pitcher_role'].astype(str))

# 6. 建立 X 和 y
X = df_train[['balls', 'strikes', 'stand', 'p_throws', 'on_1b', 'on_2b', 'on_3b', 'pitcher_role']]
y = df_train['pitch_type']

# 7. 切割訓練集
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

# 8. 訓練模型
print(f"Training model with {len(X_train)} rows...")
model = LogisticRegression(max_iter=1000)
model.fit(X_train, y_train)

# 9. 儲存模型
joblib.dump(model, "baseball_model.pkl")
joblib.dump(le_pitch, "pitch_label_mapping.pkl")
print(f"✅ Model training complete. Accuracy = {model.score(X_test, y_test):.4f}")

# 10. 🎯 【最重要的一步】寫回資料庫
# 確保現在寫回去的 "pitches" 表格包含了 player_name 和各種座標，以及新增的 is_out
print("Saving complete data (with Names, IDs, and is_out) to database...")
# 分批寫入比較穩，避免大型資料庫操作超時
df_final.to_sql("pitches", conn, if_exists="replace", index=False, chunksize=100000)

# ⚡ 加上索引，讓你的 API 查詢「年份」和「人名」變飛快
print("Building Database Indexes...")
conn.execute("CREATE INDEX IF NOT EXISTS idx_game_date ON pitches(game_date)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_player_name ON pitches(player_name)")

# 🎯 【新增邏輯 3】幫篩選器加上索引，讓前端點擊 PITCH TYPE 跟 COUNT 時速度變快
conn.execute("CREATE INDEX IF NOT EXISTS idx_pitch_type ON pitches(pitch_type)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_count ON pitches(balls, strikes)")

conn.close()
print("🎉 全部修復完成！現在資料庫有名字、有ID、出局標記，且搜尋變快了。")
