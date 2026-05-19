import sqlite3
import pandas as pd
import os
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pybaseball import playerid_reverse_lookup
import uvicorn

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None

app = FastAPI()

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DB_FILENAME = os.getenv("BASEBALL_DB_FILENAME", "baseball_data_2023_2025.db")
DB_PATH = os.path.join(BASE_DIR, PUBLIC_DB_FILENAME)
FULL_DB_PATH = os.path.join(BASE_DIR, "baseball_data.db")
LOCAL_DESKTOP_DB_PATH = os.path.abspath(os.path.join(BASE_DIR, "..", "baseball_data.db"))
DATABASE_URL = os.getenv("DATABASE_URL")

batter_name_map = {}
TABLE_NAME = "pitches"
PITCH_COLUMNS = set()

OUT_EVENTS = {
    "field_out", "strikeout", "force_out", "grounded_into_double_play",
    "fielders_choice", "fielders_choice_out", "double_play",
    "sac_fly", "sac_bunt", "strikeout_double_play"
}

RESULT_ORDER = ["ball", "called_strike", "swinging_strike", "foul", "in_play_out", "in_play_hit"]
DISPLAY_ZONES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14]
OUTCOME_ORDER = [
    "BB", "HBP", "1B", "2B", "3B", "HR", "K", "Out", "DP", "FC", "ROE",
    "Ball", "Called Strike", "Swinging Strike", "Foul", "In Play", "Other",
]

def active_db_path():
    if os.path.exists(DB_PATH):
        return DB_PATH
    if os.path.exists(FULL_DB_PATH):
        return FULL_DB_PATH
    if os.path.exists(LOCAL_DESKTOP_DB_PATH):
        return LOCAL_DESKTOP_DB_PATH
    return DB_PATH

def using_postgres():
    return bool(DATABASE_URL)

def db_placeholder():
    return "%s" if using_postgres() else "?"

def connect_db(dict_rows=False):
    if using_postgres():
        if psycopg2 is None:
            raise RuntimeError("DATABASE_URL is set, but psycopg2-binary is not installed")
        cursor_factory = RealDictCursor if dict_rows else None
        return psycopg2.connect(DATABASE_URL, cursor_factory=cursor_factory)

    conn = sqlite3.connect(active_db_path())
    if dict_rows:
        conn.row_factory = sqlite3.Row
    return conn

def fetch_all_dicts(query, params=None):
    conn = connect_db(dict_rows=True)
    cursor = conn.cursor()
    cursor.execute(query, params or [])
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return [dict(row) for row in rows]

def table_names(conn):
    cursor = conn.cursor()
    if using_postgres():
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    else:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    cursor.close()
    return tables

def table_columns(conn, table_name):
    cursor = conn.cursor()
    if using_postgres():
        cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s
            """,
            (table_name,),
        )
    else:
        cursor.execute(f"PRAGMA table_info({table_name})")
        cols = [row[1] for row in cursor.fetchall()]
        cursor.close()
        return cols
    cols = [row[0] for row in cursor.fetchall()]
    cursor.close()
    return cols

def load_player_names(conn, player_ids):
    if not player_ids:
        return {}

    tables = table_names(conn)
    if "player_names" not in tables:
        return {}

    placeholder = db_placeholder()
    ids = [str(int(player_id)) for player_id in player_ids if pd.notna(player_id)]
    names = {}

    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        query = f"""
            SELECT player_id, name
            FROM player_names
            WHERE player_id IN ({','.join([placeholder] * len(chunk))})
        """
        df = pd.read_sql(query, conn, params=chunk)
        for _, row in df.iterrows():
            names[str(int(row["player_id"]))] = row["name"]

    return names

def pct(part, total):
    return round((part / total) * 100, 1) if total else 0

def result_type(row):
    pitch_type = row.get("type")
    description = row.get("description") or ""
    events = row.get("events") or ""

    if pitch_type == "B":
        return "ball"
    if description == "called_strike":
        return "called_strike"
    if "swinging_strike" in description:
        return "swinging_strike"
    if description == "foul":
        return "foul"
    if pitch_type == "X":
        return "in_play_out" if events in OUT_EVENTS or "out" in events else "in_play_hit"
    return "other"

def build_pitch_filters(
    year=None,
    pitcherId=None,
    batterId=None,
    pitcherRole="All",
    zone=None,
    pitchType=None,
    balls=None,
    strikes=None,
    pitcherHand=None,
    outs=None,
    on1b=None,
    on2b=None,
    on3b=None,
):
    placeholder = db_placeholder()
    null_vals = {"", "none", "null", "undefined", "all"}
    conds = []
    params = []

    y = str(year).strip() if year else "ALL"
    p_id = str(pitcherId).strip() if pitcherId else ""
    b_id = str(batterId).strip() if batterId else ""
    role = str(pitcherRole).strip() if pitcherRole else "All"
    z = str(zone).strip() if zone else ""
    pt = str(pitchType).strip() if pitchType else ""
    b = str(balls).strip() if balls is not None else ""
    s = str(strikes).strip() if strikes is not None else ""
    hand = str(pitcherHand).strip() if pitcherHand else ""
    out_count = str(outs).strip() if outs is not None else ""
    runner_filters = [
        ("on_1b", str(on1b).strip() if on1b is not None else ""),
        ("on_2b", str(on2b).strip() if on2b is not None else ""),
        ("on_3b", str(on3b).strip() if on3b is not None else ""),
    ]

    if y.upper() != "ALL":
        conds.append(f"substr(game_date, 1, 4) = {placeholder}")
        params.append(y)
    if p_id.lower() not in null_vals and p_id != "0":
        pitcher_ids = [x.strip() for x in p_id.split(",") if x.strip().isdigit()]
        if pitcher_ids:
            conds.append(f"pitcher IN ({','.join([placeholder] * len(pitcher_ids))})")
            params.extend(pitcher_ids)
    if b_id.lower() not in null_vals and b_id != "0":
        conds.append(f"batter = {placeholder}")
        params.append(b_id)
    if role.lower() not in null_vals:
        conds.append(f"pitcher_role = {placeholder}")
        params.append(role)
    if hand.lower() not in null_vals:
        conds.append(f"p_throws = {placeholder}")
        params.append(hand)
    if out_count and out_count.lower() not in null_vals:
        conds.append(f"outs_when_up = {placeholder}")
        params.append(out_count)
    if z and z.lower() not in null_vals:
        zones = [int(x) for x in z.split(",") if x.strip().isdigit()]
        if zones:
            conds.append(f"zone IN ({','.join([placeholder] * len(zones))})")
            params.extend(zones)
    if pt and pt.lower() not in null_vals:
        pitch_types = [x.strip() for x in pt.split(",") if x.strip()]
        if pitch_types:
            conds.append(f"pitch_type IN ({','.join([placeholder] * len(pitch_types))})")
            params.extend(pitch_types)
    if b and b.lower() not in null_vals:
        conds.append(f"balls = {placeholder}")
        params.append(b)
    if s and s.lower() not in null_vals:
        conds.append(f"strikes = {placeholder}")
        params.append(s)
    for col, val in runner_filters:
        if val and val.lower() not in null_vals:
            conds.append(f"COALESCE({col}, 0) = {placeholder}")
            params.append(1 if val == "1" else 0)

    if not conds:
        return None, None

    where = " WHERE " + " AND ".join(conds) if conds else ""
    return where, params

def plate_outcome(row):
    description = row.get("description") or ""
    events = row.get("events") or ""
    pitch_result = row.get("type") or ""

    if events in {"walk", "intent_walk"}:
        return "BB"
    if events == "hit_by_pitch":
        return "HBP"
    if events == "single":
        return "1B"
    if events == "double":
        return "2B"
    if events == "triple":
        return "3B"
    if events == "home_run":
        return "HR"
    if events == "strikeout":
        return "K"
    if events in {"grounded_into_double_play", "double_play", "strikeout_double_play"}:
        return "DP"
    if events in {"fielders_choice", "fielders_choice_out"}:
        return "FC"
    if events == "field_error":
        return "ROE"
    if events in OUT_EVENTS or "out" in events:
        return "Out"
    if pitch_result == "B":
        return "Ball"
    if description == "called_strike":
        return "Called Strike"
    if "swinging_strike" in description:
        return "Swinging Strike"
    if description == "foul":
        return "Foul"
    if pitch_result == "X":
        return "In Play"
    return "Other"

def empirical_run_value(row, outcome):
    delta_run_exp = row.get("delta_run_exp")
    if delta_run_exp is not None:
        try:
            return float(delta_run_exp)
        except (TypeError, ValueError):
            pass
    return 0.0

def pitcher_wpa_value(row):
    pitcher_wpa = row.get("pitcher_wpa")
    if pitcher_wpa is not None:
        try:
            return float(pitcher_wpa)
        except (TypeError, ValueError):
            pass

    delta_home_win_exp = row.get("delta_home_win_exp")
    inning_topbot = str(row.get("inning_topbot") or "").lower()
    if delta_home_win_exp is not None:
        try:
            delta = float(delta_home_win_exp)
            return -delta if inning_topbot.startswith("bot") else delta
        except (TypeError, ValueError):
            pass

    return None

def batting_wpa_value(row):
    pitcher_wpa = pitcher_wpa_value(row)
    if pitcher_wpa is not None:
        return -pitcher_wpa

    delta_home_win_exp = row.get("delta_home_win_exp")
    inning_topbot = str(row.get("inning_topbot") or "").lower()
    if delta_home_win_exp is not None:
        try:
            delta = float(delta_home_win_exp)
            return delta if inning_topbot.startswith("bot") else -delta
        except (TypeError, ValueError):
            pass

    return None

def has_filter_value(value):
    null_vals = {"", "none", "null", "undefined", "all", "0"}
    return str(value).strip().lower() not in null_vals if value is not None else False

def wpa_perspective_for_filters(pitcher_id=None, batter_id=None):
    if has_filter_value(pitcher_id):
        return "pitcher"
    if has_filter_value(batter_id):
        return "batter"
    return "batter"

def wpa_value_for_perspective(row, perspective):
    if perspective == "pitcher":
        return pitcher_wpa_value(row)
    return batting_wpa_value(row)

def summarize_outcomes(rows, wpa_perspective="batter"):
    total = len(rows)
    counts = {key: 0 for key in OUTCOME_ORDER}
    pitch_types = {}

    for row in rows:
        row_dict = dict(row)
        outcome = plate_outcome(row_dict)
        if outcome not in counts:
            outcome = "Other"
        counts[outcome] += 1

        pitch_type = row_dict.get("pitch_type") or "Unknown"
        if pitch_type not in pitch_types:
            pitch_types[pitch_type] = {
                "total": 0,
                "runValue": 0.0,
                "wpa": 0.0,
                "wpaCount": 0,
                "outcomes": {key: 0 for key in OUTCOME_ORDER},
            }

        rv = empirical_run_value(row_dict, outcome)
        wpa = wpa_value_for_perspective(row_dict, wpa_perspective)
        pitch_types[pitch_type]["total"] += 1
        pitch_types[pitch_type]["runValue"] += rv
        if wpa is not None:
            pitch_types[pitch_type]["wpa"] += wpa
            pitch_types[pitch_type]["wpaCount"] += 1
        pitch_types[pitch_type]["outcomes"][outcome] += 1

    outcomes = [
        {"outcome": key, "count": count, "pct": pct(count, total)}
        for key, count in counts.items()
        if count > 0
    ]

    pitch_type_outcomes = []
    for pitch_type, data in pitch_types.items():
        type_total = data["total"]
        type_outcomes = [
            {"outcome": key, "count": count, "pct": pct(count, type_total)}
            for key, count in data["outcomes"].items()
            if count > 0
        ]
        expected_runs = round(data["runValue"] / type_total, 3) if type_total else 0
        wpa_count = data.get("wpaCount", 0)
        win_prob_change = (
            round((data["wpa"] / wpa_count) * 100, 2)
            if wpa_count
            else round(expected_runs * (1 if wpa_perspective == "batter" else -1) * 9.0, 2)
        )
        pitch_type_outcomes.append({
            "pitchType": pitch_type,
            "count": type_total,
            "expectedRuns": expected_runs,
            "winProbChange": win_prob_change,
            "outRate": pct(
                data["outcomes"]["K"] + data["outcomes"]["Out"] + data["outcomes"]["DP"] + data["outcomes"]["FC"],
                type_total,
            ),
            "outcomes": type_outcomes,
        })

    pitch_type_outcomes.sort(key=lambda item: (item["expectedRuns"], -item["count"]))

    return {
        "total": total,
        "wpaPerspective": wpa_perspective,
        "outcomes": outcomes,
        "pitchTypeOutcomes": pitch_type_outcomes,
    }

def outcome_select_columns():
    cols = ["pitch_type", "balls", "strikes", "description", "type", "events"]
    if "delta_run_exp" in PITCH_COLUMNS:
        cols.append("delta_run_exp")
    if "pitcher_wpa" in PITCH_COLUMNS:
        cols.append("pitcher_wpa")
    elif "delta_home_win_exp" in PITCH_COLUMNS and "inning_topbot" in PITCH_COLUMNS:
        cols.extend(["delta_home_win_exp", "inning_topbot"])
    return ", ".join(cols)

def summarize_rows(rows):
    total = len(rows)
    if total == 0:
        empty_zones = {
            z: {
                "total": 0, "ball": 0, "called_strike": 0, "swinging_strike": 0,
                "foul": 0, "in_play_out": 0, "in_play_hit": 0,
                "whiffRate": 0, "outRate": 0, "foulRate": 0
            }
            for z in DISPLAY_ZONES
        }
        return {
            "total": 0,
            "summaryStats": None,
            "resultData": [],
            "pitchTypeData": [],
            "zoneData": empty_zones,
        }

    result_counts = {key: 0 for key in RESULT_ORDER}
    pitch_types = {}
    zones = {
        z: {
            "total": 0, "ball": 0, "called_strike": 0, "swinging_strike": 0,
            "foul": 0, "in_play_out": 0, "in_play_hit": 0
        }
        for z in DISPLAY_ZONES
    }

    for row in rows:
        row_dict = dict(row)
        result = result_type(row_dict)
        if result not in result_counts:
            continue

        result_counts[result] += 1

        pitch_type = row_dict.get("pitch_type") or "Unknown"
        if pitch_type not in pitch_types:
            pitch_types[pitch_type] = {
                "total": 0, "ball": 0, "called_strike": 0, "swinging_strike": 0,
                "foul": 0, "in_play_out": 0, "in_play_hit": 0
            }
        pitch_types[pitch_type]["total"] += 1
        pitch_types[pitch_type][result] += 1

        try:
            zone_num = int(row_dict.get("zone"))
        except (TypeError, ValueError):
            zone_num = 0
        if zone_num in zones:
            zones[zone_num]["total"] += 1
            zones[zone_num][result] += 1

    swings = result_counts["swinging_strike"] + result_counts["foul"] + result_counts["in_play_out"] + result_counts["in_play_hit"]
    in_play = result_counts["in_play_out"] + result_counts["in_play_hit"]

    result_data = [
        {"result": key, "count": count, "pct": pct(count, total)}
        for key, count in result_counts.items()
        if count > 0
    ]

    pitch_type_data = []
    for pitch_type, data in pitch_types.items():
        type_total = data["total"]
        type_swings = data["swinging_strike"] + data["foul"] + data["in_play_out"] + data["in_play_hit"]
        pitch_type_data.append({
            "pitchType": pitch_type,
            "count": type_total,
            "pct": pct(type_total, total),
            "ballPct": pct(data["ball"], type_total),
            "cswPct": pct(data["called_strike"] + data["swinging_strike"], type_total),
            "whiffPct": pct(data["swinging_strike"], type_swings),
            "inPlayPct": pct(data["in_play_out"] + data["in_play_hit"], type_total),
            "hitPct": pct(data["in_play_hit"], type_total),
        })
    pitch_type_data.sort(key=lambda x: x["count"], reverse=True)

    zone_data = {}
    for zone_num, data in zones.items():
        zone_swings = data["swinging_strike"] + data["foul"] + data["in_play_out"] + data["in_play_hit"]
        zone_total = data["total"]
        zone_data[zone_num] = {
            **data,
            "whiffRate": data["swinging_strike"] / zone_swings if zone_swings else 0,
            "outRate": data["in_play_out"] / zone_total if zone_total else 0,
            "foulRate": data["foul"] / zone_total if zone_total else 0,
        }

    return {
        "total": total,
        "summaryStats": {
            "total": total,
            "strikeRate": pct(total - result_counts["ball"], total),
            "swingRate": pct(swings, total),
            "whiffRate": pct(result_counts["swinging_strike"], swings),
            "cswRate": pct(result_counts["called_strike"] + result_counts["swinging_strike"], total),
            "babip": pct(result_counts["in_play_hit"], in_play),
        },
        "resultData": result_data,
        "pitchTypeData": pitch_type_data,
        "zoneData": zone_data,
    }

@app.on_event("startup")
async def startup_event():
    global batter_name_map, TABLE_NAME, PITCH_COLUMNS

    if not using_postgres():
        # --- 1. 下載邏輯 (使用 Dropbox 直連) ---
        DB_URL = os.getenv(
            "BASEBALL_DB_URL",
            "https://www.dropbox.com/scl/fi/vvytrbedvwfamdx3uqhjv/baseball_data.db?rlkey=jx3t30rwcrxu8sqjqlkwz2xgz&st=rqm3pqhf&dl=1"
        )

        db_path = active_db_path()

        if os.path.exists(db_path):
            if os.path.getsize(db_path) < 100 * 1024 * 1024:
                print("偵測到上次下載不完整的殘留檔案，正在清理並重新下載...")
                os.remove(db_path)
                db_path = DB_PATH

        if not os.path.exists(db_path):
            print("正在從 Dropbox 下載大型資料庫，這可能需要幾分鐘，請保持耐心...")
            try:
                response = requests.get(DB_URL, stream=True)
                response.raise_for_status()

                with open(DB_PATH, "wb") as f:
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)

                actual_size = os.path.getsize(DB_PATH) / (1024 * 1024)
                print(f"下載完成！實際檔案大小: {actual_size:.2f} MB")

            except Exception as e:
                print(f"下載失敗: {e}")
                if os.path.exists(DB_PATH):
                    os.remove(DB_PATH)
                return

    # --- 2. 資料庫讀取邏輯 ---
    try:
        conn = connect_db()
        tables = table_names(conn)
        if "pitches" in tables: 
            TABLE_NAME = "pitches"
        elif tables: 
            TABLE_NAME = tables[0]
        else:
            print("警告: 資料庫中找不到任何資料表")
            conn.close()
            return
        PITCH_COLUMNS = set(table_columns(conn, TABLE_NAME))

        print("正在讀取打者清單並進行名稱轉換 (這一步連線較久)...")
        u_ids = pd.read_sql(f"SELECT DISTINCT batter FROM {TABLE_NAME} WHERE batter IS NOT NULL", conn)["batter"].tolist()
        stored_names = load_player_names(conn, u_ids)
        conn.close()

        batter_name_map = {
            str(int(batter_id)): stored_names.get(str(int(batter_id)), f"Batter {int(batter_id)}")
            for batter_id in u_ids
            if pd.notna(batter_id)
        }

        missing_ids = [int(batter_id) for batter_id in u_ids if pd.notna(batter_id) and str(int(batter_id)) not in stored_names]
        if missing_ids:
            try:
                # 這裡只補查 player_names 尚未涵蓋的球員。
                lookup_df = playerid_reverse_lookup(missing_ids, key_type='mlbam')
                for _, row in lookup_df.iterrows():
                    batter_name_map[str(row['key_mlbam'])] = f"{row['name_last'].title()}, {row['name_first'].title()}"
            except Exception as e:
                print(f"⚠️ 打者姓名轉換失敗，改用 batter ID 顯示: {e}")
        
        backend = "PostgreSQL" if using_postgres() else "SQLite"
        print(f"✅ 後端初始化完成，使用 {backend} 資料庫！")

    except Exception as e:
        print(f"❌ 啟動出錯: {e}")
@app.get("/api/batters")
async def get_batters():
    return sorted([{"id": k, "name": v} for k, v in batter_name_map.items()], key=lambda x: x['name'])

@app.get("/api/pitchers")
async def get_pitchers():
    try:
        conn = connect_db()
        df = pd.read_sql(f"SELECT DISTINCT pitcher, player_name FROM {TABLE_NAME} WHERE player_name IS NOT NULL", conn)
        conn.close()
        return [{"id": str(int(row['pitcher'])), "name": row['player_name']} for _, row in df.iterrows()]
    except:
        return []

@app.get("/api/pitches")
async def get_pitches(
    year: str = None, 
    pitcherId: str = None, 
    batterId: str = None, 
    pitcherRole: str = "All",
    zone: str = None,
    pitchType: str = None,  # ⚾ 新增：接收球種
    balls: str = None,      # ⚾ 新增：接收壞球數
    strikes: str = None,    # ⚾ 新增：接收好球數
    on1b: str = None,
    on2b: str = None,
    on3b: str = None,
    outs: str = None,
):
    try:
        where, params = build_pitch_filters(
            year=year,
            pitcherId=pitcherId,
            batterId=batterId,
            pitcherRole=pitcherRole,
            zone=zone,
            pitchType=pitchType,
            balls=balls,
            strikes=strikes,
            outs=outs,
            on1b=on1b,
            on2b=on2b,
            on3b=on3b,
        )

        if where is None:
            return []

        query = f"SELECT * FROM {TABLE_NAME}{where} ORDER BY game_date DESC LIMIT 50000"

        conn = connect_db()
        df = pd.read_sql(query, conn, params=params)
        conn.close()

        if df.empty:
            return []

        col_map = {
            'pitch_type': 'pitchType', 
            'release_speed': 'speed', 
            'plate_x': 'plateX', 
            'plate_z': 'plateZ',
            'is_out': 'isOut'
        }
        for old, new in col_map.items():
            if old in df.columns:
                df[new] = df[old]
            
        # 清洗 NaN
        records = df.to_dict(orient='records')
        return [{k: (None if pd.isna(v) else v) for k, v in row.items()} for row in records]

    except Exception as e:
        print(f"❌ API 錯誤: {e}")
        return []

@app.get("/api/pitches/summary")
async def get_pitch_summary(
    year: str = None,
    pitcherId: str = None,
    batterId: str = None,
    pitcherRole: str = "All",
    zone: str = None,
    pitchType: str = None,
    balls: str = None,
    strikes: str = None,
    pitcherHand: str = None,
    on1b: str = None,
    on2b: str = None,
    on3b: str = None,
    outs: str = None,
):
    try:
        where, params = build_pitch_filters(
            year=year,
            pitcherId=pitcherId,
            batterId=batterId,
            pitcherRole=pitcherRole,
            zone=zone,
            pitchType=pitchType,
            balls=balls,
            strikes=strikes,
            pitcherHand=pitcherHand,
            outs=outs,
            on1b=on1b,
            on2b=on2b,
            on3b=on3b,
        )

        if where is None:
            return summarize_rows([])

        query = f"""
            SELECT pitch_type, zone, description, type, events
            FROM {TABLE_NAME}
            {where}
        """

        rows = fetch_all_dicts(query, params)

        return summarize_rows(rows)

    except Exception as e:
        print(f"❌ Summary API 錯誤: {e}")
        return summarize_rows([])

@app.get("/api/pitches/outcomes")
async def get_pitch_outcomes(
    year: str = None,
    pitcherId: str = None,
    batterId: str = None,
    pitcherRole: str = "All",
    zone: str = None,
    pitchType: str = None,
    balls: str = None,
    strikes: str = None,
    pitcherHand: str = None,
    on1b: str = None,
    on2b: str = None,
    on3b: str = None,
    outs: str = None,
):
    try:
        where, params = build_pitch_filters(
            year=year,
            pitcherId=pitcherId,
            batterId=batterId,
            pitcherRole=pitcherRole,
            zone=zone,
            pitchType=pitchType,
            balls=balls,
            strikes=strikes,
            pitcherHand=pitcherHand,
            outs=outs,
            on1b=on1b,
            on2b=on2b,
            on3b=on3b,
        )

        wpa_perspective = wpa_perspective_for_filters(pitcherId, batterId)

        if where is None:
            return summarize_outcomes([], wpa_perspective)

        query = f"""
            SELECT {outcome_select_columns()}
            FROM {TABLE_NAME}
            {where}
        """
        rows = fetch_all_dicts(query, params)
        return summarize_outcomes(rows, wpa_perspective)

    except Exception as e:
        print(f"❌ Outcomes API 錯誤: {e}")
        return summarize_outcomes([], wpa_perspective_for_filters(pitcherId, batterId))

@app.get("/api/pitches/predict")
async def get_pitch_prediction(
    year: str = None,
    pitcherId: str = None,
    batterId: str = None,
    pitcherRole: str = "All",
    balls: str = None,
    strikes: str = None,
    pitcherHand: str = None,
    on1b: str = None,
    on2b: str = None,
    on3b: str = None,
    outs: str = None,
):
    try:
        where, params = build_pitch_filters(
            year=year or "ALL",
            pitcherId=pitcherId,
            batterId=batterId,
            pitcherRole=pitcherRole,
            balls=balls,
            strikes=strikes,
            pitcherHand=pitcherHand,
            outs=outs,
            on1b=on1b,
            on2b=on2b,
            on3b=on3b,
        )

        wpa_perspective = wpa_perspective_for_filters(pitcherId, batterId)

        if where is None:
            return {"total": 0, "recommendations": []}

        query = f"""
            SELECT {outcome_select_columns()}
            FROM {TABLE_NAME}
            {where}
        """
        rows = fetch_all_dicts(query, params)
        summary = summarize_outcomes(rows, wpa_perspective)
        return {
            "total": summary["total"],
            "wpaPerspective": summary["wpaPerspective"],
            "recommendations": summary["pitchTypeOutcomes"][:6],
            "model": "empirical_outcome_distribution_v1",
        }

    except Exception as e:
        print(f"❌ Prediction API 錯誤: {e}")
        return {"total": 0, "recommendations": []}
    
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
