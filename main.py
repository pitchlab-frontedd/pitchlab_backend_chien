import sqlite3
import pandas as pd
import os
import requests
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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
DATA_DIR = os.path.join(BASE_DIR, "data")
PUBLIC_DB_FILENAME = os.getenv("BASEBALL_DB_FILENAME", "baseball_data_2024_2025.db")
DB_PATH = (
    PUBLIC_DB_FILENAME
    if os.path.isabs(PUBLIC_DB_FILENAME)
    else os.path.join(DATA_DIR, PUBLIC_DB_FILENAME)
)
ROOT_PUBLIC_DB_PATH = os.path.join(BASE_DIR, PUBLIC_DB_FILENAME)
FULL_DB_PATH = os.path.join(DATA_DIR, "baseball_data.db")
ROOT_FULL_DB_PATH = os.path.join(BASE_DIR, "baseball_data.db")
LOCAL_DESKTOP_DB_PATH = os.path.abspath(os.path.join(BASE_DIR, "..", "baseball_data.db"))
DATABASE_URL = os.getenv("DATABASE_URL")

batter_name_map = {}
pitcher_name_map = {}
TABLE_NAME = "pitches"
PITCH_COLUMNS = set()
TABLE_NAMES = set()

OUT_EVENTS = {
    "field_out", "strikeout", "force_out", "grounded_into_double_play",
    "fielders_choice_out", "double_play",
    "sac_fly", "sac_bunt", "strikeout_double_play"
}

RESULT_ORDER = ["ball", "called_strike", "swinging_strike", "foul", "in_play_out", "in_play_hit"]
DISPLAY_ZONES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14]
LOCATION_X_RANGE = (-2.6, 2.6)
LOCATION_Z_RANGE = (0.4, 5.2)
LOCATION_GRID_SIZE = 36
OUTCOME_ORDER = [
    "BB", "HBP", "1B", "2B", "3B", "HR", "K", "Out", "DP", "FC", "ROE",
    "Ball", "Called Strike", "Swinging Strike", "Foul", "In Play", "Other",
]

HIT_EVENTS = {"single", "double", "triple", "home_run"}
# AT_BAT_EVENTS kept for reference; use AB_EVENTS for official AB counting
AT_BAT_EVENTS = HIT_EVENTS | OUT_EVENTS | {"strikeout", "field_error"}
# AB_EVENTS: official at-bat events (excludes sac_fly, sac_bunt per baseball rules)
AB_EVENTS = HIT_EVENTS | {
    "field_out", "strikeout", "force_out", "grounded_into_double_play",
    "fielders_choice", "fielders_choice_out", "double_play",
    "strikeout_double_play", "field_error"
}
ON_BASE_EVENTS = HIT_EVENTS | {"walk", "intent_walk", "hit_by_pitch"}
# wOBA denominator: AB + uBB + HBP + SF (excludes IBB, sac_bunt)
WOBA_DENOM_EVENTS = AB_EVENTS | {"walk", "hit_by_pitch", "sac_fly"}
TOTAL_BASES_BY_EVENT = {
    "single": 1,
    "double": 2,
    "triple": 3,
    "home_run": 4,
}
WOBA_WEIGHTS = {
    "walk": 0.69,
    "hit_by_pitch": 0.72,
    "single": 0.88,
    "double": 1.25,
    "triple": 1.58,
    "home_run": 2.03,
}

def active_db_path():
    if os.path.exists(DB_PATH):
        return DB_PATH
    if os.path.exists(ROOT_PUBLIC_DB_PATH):
        return ROOT_PUBLIC_DB_PATH
    if os.path.exists(FULL_DB_PATH):
        return FULL_DB_PATH
    if os.path.exists(ROOT_FULL_DB_PATH):
        return ROOT_FULL_DB_PATH
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

def fetch_one_dict(query, params=None):
    conn = connect_db(dict_rows=True)
    cursor = conn.cursor()
    cursor.execute(query, params or [])
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return dict(row) if row else None

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

def table_count(conn, table_name):
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
    count = cursor.fetchone()[0]
    cursor.close()
    return count

def table_date_range(conn, table_name):
    cursor = conn.cursor()
    cursor.execute(f"SELECT MIN(game_date), MAX(game_date) FROM {table_name}")
    min_date, max_date = cursor.fetchone()
    cursor.close()
    return min_date, max_date

def fetch_player_name_options(player_column, fallback_label, source_name_column=None):
    conn = connect_db()
    try:
        tables = table_names(conn)
        has_player_names = "player_names" in tables
        name_expr = f"'{fallback_label} ' || CAST(p.{player_column} AS TEXT)"
        if has_player_names and source_name_column:
            name_expr = f"COALESCE(n.name, p.{source_name_column}, {name_expr})"
        elif has_player_names:
            name_expr = f"COALESCE(n.name, {name_expr})"
        elif source_name_column:
            name_expr = f"COALESCE(p.{source_name_column}, {name_expr})"

        join_sql = (
            f"LEFT JOIN player_names n ON n.player_id = p.{player_column}"
            if has_player_names
            else ""
        )
        query = f"""
            SELECT DISTINCT p.{player_column} AS id, {name_expr} AS name
            FROM {TABLE_NAME} p
            {join_sql}
            WHERE p.{player_column} IS NOT NULL
            ORDER BY name
        """
        df = pd.read_sql(query, conn)
        return {
            str(int(row["id"])): row["name"]
            for _, row in df.iterrows()
            if pd.notna(row["id"])
        }
    finally:
        conn.close()

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

def has_fallback_names(name_map, fallback_label):
    if not name_map:
        return False
    prefix = f"{fallback_label} "
    return any(str(name).startswith(prefix) for name in name_map.values())

def pct(part, total):
    return round((part / total) * 100, 1) if total else 0

def avg(total, count, digits=1):
    return round(total / count, digits) if count else None

def sum_nullable(rows, key):
    values = [row.get(key) for row in rows if row.get(key) is not None]
    return sum(values) if values else None

def avg_nullable(rows, key, digits=3):
    values = [row.get(key) for row in rows if row.get(key) is not None]
    return round(sum(values) / len(values), digits) if values else None

def outs_on_play(events):
    if events in {"grounded_into_double_play", "double_play", "strikeout_double_play", "sac_fly_double_play", "sac_bunt_double_play"}:
        return 2
    if events in OUT_EVENTS or events == "strikeout":
        return 1
    return 0

def optional_pitch_columns(*columns):
    return [col for col in columns if col in PITCH_COLUMNS]

def fetch_pitcher_standard_stats(pitcher_id=None, year=None):
    if not has_filter_value(pitcher_id) or "pitcher_standard_stats" not in TABLE_NAMES:
        return None

    placeholder = db_placeholder()
    pitcher_ids = [int(x) for x in str(pitcher_id).split(",") if str(x).strip().isdigit()]
    if not pitcher_ids:
        return None

    conds = [f"pitcher IN ({','.join([placeholder] * len(pitcher_ids))})"]
    params = list(pitcher_ids)
    if has_filter_value(year):
        conds.append(f"season = {placeholder}")
        params.append(int(year))

    rows = fetch_all_dicts(
        f"""
        SELECT season, pitcher, player_name, team, league, bf, w, l, era, g, gs, sv,
               ip, h, r, er, hr, bb, so, whip
        FROM pitcher_standard_stats
        WHERE {" AND ".join(conds)}
        """,
        params,
    )
    if not rows:
        return None

    if len(rows) == 1:
        row = rows[0]
        return {
            "source": "mlb_stats_api",
            "season": row.get("season"),
            "team": row.get("team"),
            "league": row.get("league"),
            "bf": row.get("bf"),
            "w": row.get("w"),
            "l": row.get("l"),
            "era": row.get("era"),
            "g": row.get("g"),
            "gs": row.get("gs"),
            "sv": row.get("sv"),
            "ip": row.get("ip"),
            "h": row.get("h"),
            "r": row.get("r"),
            "er": row.get("er"),
            "hr": row.get("hr"),
            "bb": row.get("bb"),
            "so": row.get("so"),
            "whip": row.get("whip"),
        }

    return {
        "source": "mlb_stats_api",
        "season": "ALL",
        "team": "Multiple",
        "league": "",
        "bf": sum_nullable(rows, "bf"),
        "w": sum_nullable(rows, "w"),
        "l": sum_nullable(rows, "l"),
        "era": avg_nullable(rows, "era", 2),
        "g": sum_nullable(rows, "g"),
        "gs": sum_nullable(rows, "gs"),
        "sv": sum_nullable(rows, "sv"),
        "ip": None,
        "h": sum_nullable(rows, "h"),
        "r": sum_nullable(rows, "r"),
        "er": sum_nullable(rows, "er"),
        "hr": sum_nullable(rows, "hr"),
        "bb": sum_nullable(rows, "bb"),
        "so": sum_nullable(rows, "so"),
        "whip": avg_nullable(rows, "whip", 2),
    }

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
        return "in_play_out" if events in OUT_EVENTS else "in_play_hit"
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
    batterHand=None,
    outs=None,
    on1b=None,
    on2b=None,
    on3b=None,
    table_alias=None,
):
    placeholder = db_placeholder()
    def qcol(name):
        return f"{table_alias}.{name}" if table_alias else name

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
    batter_hand = str(batterHand).strip() if batterHand else ""
    out_count = str(outs).strip() if outs is not None else ""
    runner_filters = [
        ("on_1b", str(on1b).strip() if on1b is not None else ""),
        ("on_2b", str(on2b).strip() if on2b is not None else ""),
        ("on_3b", str(on3b).strip() if on3b is not None else ""),
    ]

    if y.upper() != "ALL":
        conds.append(f"substr({qcol('game_date')}, 1, 4) = {placeholder}")
        params.append(y)
    if p_id.lower() not in null_vals and p_id != "0":
        pitcher_ids = [x.strip() for x in p_id.split(",") if x.strip().isdigit()]
        if pitcher_ids:
            conds.append(f"{qcol('pitcher')} IN ({','.join([placeholder] * len(pitcher_ids))})")
            params.extend(pitcher_ids)
    if b_id.lower() not in null_vals and b_id != "0":
        conds.append(f"{qcol('batter')} = {placeholder}")
        params.append(b_id)
    if role.lower() not in null_vals:
        conds.append(f"{qcol('pitcher_role')} = {placeholder}")
        params.append(role)
    if hand.lower() not in null_vals:
        conds.append(f"{qcol('p_throws')} = {placeholder}")
        params.append(hand)
    if batter_hand.lower() not in null_vals:
        conds.append(f"{qcol('stand')} = {placeholder}")
        params.append(batter_hand)
    if out_count and out_count.lower() not in null_vals:
        conds.append(f"{qcol('outs_when_up')} = {placeholder}")
        params.append(out_count)
    if z and z.lower() not in null_vals:
        zones = [int(x) for x in z.split(",") if x.strip().isdigit()]
        if zones:
            conds.append(f"{qcol('zone')} IN ({','.join([placeholder] * len(zones))})")
            params.extend(zones)
    if pt and pt.lower() not in null_vals:
        pitch_types = [x.strip() for x in pt.split(",") if x.strip()]
        if pitch_types:
            conds.append(f"{qcol('pitch_type')} IN ({','.join([placeholder] * len(pitch_types))})")
            params.extend(pitch_types)
    if b and b.lower() not in null_vals:
        conds.append(f"{qcol('balls')} = {placeholder}")
        params.append(b)
    if s and s.lower() not in null_vals:
        conds.append(f"{qcol('strikes')} = {placeholder}")
        params.append(s)
    for runner_col, val in runner_filters:
        if val and val.lower() not in null_vals:
            conds.append(f"COALESCE({qcol(runner_col)}, 0) = {placeholder}")
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
    if has_filter_value(batter_id):
        return "batter"
    if has_filter_value(pitcher_id):
        return "pitcher"
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
            "avgRunValue": expected_runs,
            "winProbChange": win_prob_change,
            "outRate": pct(
                data["outcomes"]["K"] + data["outcomes"]["Out"] + data["outcomes"]["DP"],
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
            "standardStats": None,
            "resultData": [],
            "pitchTypeData": [],
            "zoneData": empty_zones,
            "pitchZoneData": {
                "total": 0,
                "zones": {},
                "topCombos": [],
            },
            "pitchLocationData": {
                "total": 0,
                "xRange": LOCATION_X_RANGE,
                "zRange": LOCATION_Z_RANGE,
                "cells": [],
            },
        }

    result_counts = {key: 0 for key in RESULT_ORDER}
    hr_in_play_count = 0
    pitch_types = {}
    pitcher_velocity = {}  # { pitcher_id_str: { pitch_type: [speeds] } }
    zones = {
        z: {
            "total": 0, "ball": 0, "called_strike": 0, "swinging_strike": 0,
            "foul": 0, "in_play_out": 0, "in_play_hit": 0
        }
        for z in DISPLAY_ZONES
    }
    pitch_zone_counts = {
        z: {
            "total": 0,
            "pitchTypes": {},
        }
        for z in DISPLAY_ZONES
    }
    location_counts = {}
    location_points = []
    location_pitch_type_counts = {}
    location_total = 0
    game_dates = set()

    for row in rows:
        row_dict = dict(row)
        result = result_type(row_dict)
        if result not in result_counts:
            continue

        result_counts[result] += 1
        game_date = row_dict.get("game_date")
        if game_date:
            game_dates.add(game_date)

        pitch_type = row_dict.get("pitch_type") or "Unknown"
        if pitch_type not in pitch_types:
            pitch_types[pitch_type] = {
                "total": 0, "ball": 0, "called_strike": 0, "swinging_strike": 0,
                "foul": 0, "in_play_out": 0, "in_play_hit": 0,
                "rhb": 0, "lhb": 0, "speedSum": 0, "speedCount": 0,
                "pa": 0, "ab": 0, "h": 0, "single": 0, "double": 0,
                "triple": 0, "hr": 0, "so": 0, "bb": 0, "hbp": 0, "outs": 0, "runs": 0,
                "bbe": 0, "totalBases": 0,
                "wobaNumerator": 0, "wobaDenominator": 0,
                "twoStrikePitches": 0, "putAway": 0,
                "speeds": [],
            }
        pitch_types[pitch_type]["total"] += 1
        pitch_types[pitch_type][result] += 1

        stand = row_dict.get("stand")
        if stand == "R":
            pitch_types[pitch_type]["rhb"] += 1
        elif stand == "L":
            pitch_types[pitch_type]["lhb"] += 1

        speed = row_dict.get("release_speed")
        if speed is not None:
            try:
                v = float(speed)
                pitch_types[pitch_type]["speedSum"] += v
                pitch_types[pitch_type]["speedCount"] += 1
                if len(pitch_types[pitch_type]["speeds"]) < 600:
                    pitch_types[pitch_type]["speeds"].append(round(v, 1))
                pid = str(row_dict.get("pitcher") or "unknown")
                if pid not in pitcher_velocity:
                    pitcher_velocity[pid] = {}
                if pitch_type not in pitcher_velocity[pid]:
                    pitcher_velocity[pid][pitch_type] = []
                if len(pitcher_velocity[pid][pitch_type]) < 600:
                    pitcher_velocity[pid][pitch_type].append(round(v, 1))
            except (TypeError, ValueError):
                pass

        events = row_dict.get("events") or ""
        if events:
            pitch_types[pitch_type]["pa"] += 1
            if events == "home_run":
                hr_in_play_count += 1
            pitch_types[pitch_type]["outs"] += outs_on_play(events)
            try:
                pitch_types[pitch_type]["runs"] += int(row_dict.get("runs_on_pa") or 0)
            except (TypeError, ValueError):
                pass
            if events in AB_EVENTS:
                pitch_types[pitch_type]["ab"] += 1
            if events in HIT_EVENTS:
                pitch_types[pitch_type]["h"] += 1
            if events == "single":
                pitch_types[pitch_type]["single"] += 1
            elif events == "double":
                pitch_types[pitch_type]["double"] += 1
            elif events == "triple":
                pitch_types[pitch_type]["triple"] += 1
            elif events == "home_run":
                pitch_types[pitch_type]["hr"] += 1
            if events == "strikeout":
                pitch_types[pitch_type]["so"] += 1
            if events in {"walk", "intent_walk"}:
                pitch_types[pitch_type]["bb"] += 1
            if events == "hit_by_pitch":
                pitch_types[pitch_type]["hbp"] += 1
            if events in TOTAL_BASES_BY_EVENT:
                pitch_types[pitch_type]["totalBases"] += TOTAL_BASES_BY_EVENT[events]
            if events in WOBA_WEIGHTS:
                pitch_types[pitch_type]["wobaNumerator"] += WOBA_WEIGHTS[events]
            if events in WOBA_DENOM_EVENTS:
                pitch_types[pitch_type]["wobaDenominator"] += 1

        if result in {"in_play_out", "in_play_hit"}:
            pitch_types[pitch_type]["bbe"] += 1

        try:
            strikes_before_pitch = int(row_dict.get("strikes"))
        except (TypeError, ValueError):
            strikes_before_pitch = None
        if strikes_before_pitch == 2:
            pitch_types[pitch_type]["twoStrikePitches"] += 1
            if events == "strikeout":
                pitch_types[pitch_type]["putAway"] += 1

        try:
            zone_num = int(row_dict.get("zone"))
        except (TypeError, ValueError):
            zone_num = 0
        if zone_num in zones:
            zones[zone_num]["total"] += 1
            zones[zone_num][result] += 1
            pitch_zone_counts[zone_num]["total"] += 1
            pitch_zone_counts[zone_num]["pitchTypes"][pitch_type] = (
                pitch_zone_counts[zone_num]["pitchTypes"].get(pitch_type, 0) + 1
            )

        try:
            plate_x = float(row_dict.get("plate_x"))
            plate_z = float(row_dict.get("plate_z"))
        except (TypeError, ValueError):
            plate_x = None
            plate_z = None

        if plate_x is not None and plate_z is not None:
            min_x, max_x = LOCATION_X_RANGE
            min_z, max_z = LOCATION_Z_RANGE
            if min_x <= plate_x <= max_x and min_z <= plate_z <= max_z:
                x_idx = min(
                    LOCATION_GRID_SIZE - 1,
                    max(0, int(((plate_x - min_x) / (max_x - min_x)) * LOCATION_GRID_SIZE)),
                )
                z_idx = min(
                    LOCATION_GRID_SIZE - 1,
                    max(0, int(((plate_z - min_z) / (max_z - min_z)) * LOCATION_GRID_SIZE)),
                )
                location_counts[(x_idx, z_idx)] = location_counts.get((x_idx, z_idx), 0) + 1
                batter_id = row_dict.get("batter")
                pitcher_id = row_dict.get("pitcher")
                location_points.append({
                    "x": plate_x,
                    "z": plate_z,
                    "pitchType": pitch_type,
                    "batterId": str(batter_id) if batter_id is not None else None,
                    "pitcherId": str(pitcher_id) if pitcher_id is not None else None,
                    "batterName": batter_name_map.get(str(batter_id), str(batter_id)) if batter_id is not None else None,
                    "pitcherName": pitcher_name_map.get(str(pitcher_id), str(pitcher_id)) if pitcher_id is not None else None,
                    "gameDate": row_dict.get("game_date"),
                    "releaseSpeed": row_dict.get("release_speed"),
                    "launchSpeed": row_dict.get("launch_speed"),
                    "launchAngle": row_dict.get("launch_angle"),
                    "inning": row_dict.get("inning"),
                    "balls": row_dict.get("balls"),
                    "strikes": row_dict.get("strikes"),
                    "outs": row_dict.get("outs_when_up"),
                    "events": row_dict.get("events"),
                    "description": row_dict.get("description"),
                    "contactType": row_dict.get("bb_type"),
                })
                location_pitch_type_counts[pitch_type] = location_pitch_type_counts.get(pitch_type, 0) + 1
                location_total += 1

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
            "rhb": data["rhb"] if data["rhb"] else None,
            "lhb": data["lhb"] if data["lhb"] else None,
            "pct": pct(type_total, total),
            "mph": avg(data["speedSum"], data["speedCount"], 1),
            "pa": data["pa"],
            "ab": data["ab"],
            "h": data["h"],
            "singles": data["single"],
            "doubles": data["double"],
            "triples": data["triple"],
            "hr": data["hr"],
            "so": data["so"],
            "bb": data["bb"],
            "hbp": data["hbp"],
            "outs": data["outs"],
            "runs": data["runs"],
            "bbe": data["bbe"],
            "totalBases": data["totalBases"],
            "wobaNumerator": data["wobaNumerator"],
            "wobaDenominator": data["wobaDenominator"],
            "swingAttempts": type_swings,
            "swingingStrikes": data["swinging_strike"],
            "twoStrikePitches": data["twoStrikePitches"],
            "putAway": data["putAway"],
            "ba": avg(data["h"], data["ab"], 3),
            "slg": avg(data["totalBases"], data["ab"], 3),
            "woba": avg(data["wobaNumerator"], data["wobaDenominator"], 3),
            "ballPct": pct(data["ball"], type_total),
            "cswPct": pct(data["called_strike"] + data["swinging_strike"], type_total),
            "whiffPct": pct(data["swinging_strike"], type_swings),
            "putAwayPct": pct(data["putAway"], data["twoStrikePitches"]),
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

    pitch_zone_data = {}
    pitch_zone_combos = []
    for zone_num, data in pitch_zone_counts.items():
        zone_total = data["total"]
        pitch_types_in_zone = [
            {
                "pitchType": pitch_type,
                "count": count,
                "pct": pct(count, zone_total),
                "overallPct": pct(count, total),
            }
            for pitch_type, count in data["pitchTypes"].items()
        ]
        pitch_types_in_zone.sort(key=lambda item: item["count"], reverse=True)
        top_pitch = pitch_types_in_zone[0] if pitch_types_in_zone else None

        pitch_zone_data[zone_num] = {
            "zone": zone_num,
            "total": zone_total,
            "pct": pct(zone_total, total),
            "topPitchType": top_pitch["pitchType"] if top_pitch else None,
            "topPitchTypeCount": top_pitch["count"] if top_pitch else 0,
            "topPitchTypePct": top_pitch["pct"] if top_pitch else 0,
            "pitchTypes": pitch_types_in_zone,
        }

        for item in pitch_types_in_zone:
            pitch_zone_combos.append({
                "zone": zone_num,
                "pitchType": item["pitchType"],
                "count": item["count"],
                "zonePct": item["pct"],
                "overallPct": item["overallPct"],
            })

    pitch_zone_combos.sort(key=lambda item: item["count"], reverse=True)

    max_location_count = max(location_counts.values()) if location_counts else 0
    location_cells = []
    if max_location_count:
        min_x, max_x = LOCATION_X_RANGE
        min_z, max_z = LOCATION_Z_RANGE
        x_step = (max_x - min_x) / LOCATION_GRID_SIZE
        z_step = (max_z - min_z) / LOCATION_GRID_SIZE
        for (x_idx, z_idx), count in location_counts.items():
            intensity = count / max_location_count
            if intensity < 0.05 and count < 2:
                continue
            location_cells.append({
                "x": round(min_x + (x_idx + 0.5) * x_step, 3),
                "z": round(min_z + (z_idx + 0.5) * z_step, 3),
                "count": count,
                "pct": pct(count, location_total),
                "intensity": round(intensity, 3),
            })
        location_cells.sort(key=lambda item: item["count"], reverse=True)
    top_location_pitch_types = [
        pitch_type
        for pitch_type, _ in sorted(
            location_pitch_type_counts.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:6]
    ]
    filtered_location_points = [
        point for point in location_points
        if point["pitchType"] in top_location_pitch_types
    ]
    point_limit = 180
    if len(filtered_location_points) > point_limit:
        step = len(filtered_location_points) / point_limit
        sampled_location_points = [
            filtered_location_points[int(i * step)]
            for i in range(point_limit)
        ]
    else:
        sampled_location_points = filtered_location_points

    velocity_data = {}
    for pid, pt_map in pitcher_velocity.items():
        pitcher_pts = {pt: speeds for pt, speeds in pt_map.items() if len(speeds) >= 5}
        if pitcher_pts:
            velocity_data[pid] = {
                "name": pitcher_name_map.get(pid, pid),
                "pitchTypes": pitcher_pts,
            }

    return {
        "total": total,
        "summaryStats": {
            "total": total,
            "strikeRate": pct(total - result_counts["ball"], total),
            "swingRate": pct(swings, total),
            "whiffRate": pct(result_counts["swinging_strike"], swings),
            "cswRate": pct(result_counts["called_strike"] + result_counts["swinging_strike"], total),
            "babip": pct(result_counts["in_play_hit"] - hr_in_play_count, in_play - hr_in_play_count),
        },
        "standardStats": {
            "games": len(game_dates),
        },
        "resultData": result_data,
        "pitchTypeData": pitch_type_data,
        "zoneData": zone_data,
        "velocityData": velocity_data,
        "pitchZoneData": {
            "total": total,
            "zones": pitch_zone_data,
            "topCombos": pitch_zone_combos[:8],
        },
        "pitchLocationData": {
            "total": location_total,
            "xRange": LOCATION_X_RANGE,
            "zRange": LOCATION_Z_RANGE,
            "cells": location_cells[:140],
            "points": [
                {
                    "x": round(point["x"], 3),
                    "z": round(point["z"], 3),
                    "pitchType": point["pitchType"],
                    "batterId": point.get("batterId"),
                    "pitcherId": point.get("pitcherId"),
                    "batterName": point.get("batterName"),
                    "pitcherName": point.get("pitcherName"),
                    "gameDate": point.get("gameDate"),
                    "releaseSpeed": point.get("releaseSpeed"),
                    "launchSpeed": point.get("launchSpeed"),
                    "launchAngle": point.get("launchAngle"),
                    "inning": point.get("inning"),
                    "balls": point.get("balls"),
                    "strikes": point.get("strikes"),
                    "outs": point.get("outs"),
                    "events": point.get("events"),
                    "description": point.get("description"),
                    "contactType": point.get("contactType"),
                }
                for point in sampled_location_points
            ],
            "legendPitchTypes": top_location_pitch_types,
        },
    }

def summarize_next_pitch_rows(rows):
    total = len(rows)
    pitch_zone_counts = {
        z: {
            "total": 0,
            "pitchTypes": {},
        }
        for z in DISPLAY_ZONES
    }
    location_counts = {}
    location_points = []
    location_pitch_type_counts = {}

    for row in rows:
        row_dict = dict(row)
        pitch_type = row_dict.get("pitch_type") or "Unknown"
        try:
            zone_num = int(row_dict.get("zone"))
        except (TypeError, ValueError):
            zone_num = 0

        if zone_num in pitch_zone_counts:
            pitch_zone_counts[zone_num]["total"] += 1
            pitch_zone_counts[zone_num]["pitchTypes"][pitch_type] = (
                pitch_zone_counts[zone_num]["pitchTypes"].get(pitch_type, 0) + 1
            )

        try:
            plate_x = float(row_dict.get("plate_x"))
            plate_z = float(row_dict.get("plate_z"))
        except (TypeError, ValueError):
            plate_x = None
            plate_z = None

        if plate_x is None or plate_z is None:
            continue

        min_x, max_x = LOCATION_X_RANGE
        min_z, max_z = LOCATION_Z_RANGE
        if not (min_x <= plate_x <= max_x and min_z <= plate_z <= max_z):
            continue

        x_idx = min(
            LOCATION_GRID_SIZE - 1,
            max(0, int(((plate_x - min_x) / (max_x - min_x)) * LOCATION_GRID_SIZE)),
        )
        z_idx = min(
            LOCATION_GRID_SIZE - 1,
            max(0, int(((plate_z - min_z) / (max_z - min_z)) * LOCATION_GRID_SIZE)),
        )
        location_counts[(x_idx, z_idx)] = location_counts.get((x_idx, z_idx), 0) + 1
        batter_id = row_dict.get("batter")
        pitcher_id = row_dict.get("pitcher")
        location_points.append({
            "x": plate_x,
            "z": plate_z,
            "pitchType": pitch_type,
            "batterId": str(batter_id) if batter_id is not None else None,
            "pitcherId": str(pitcher_id) if pitcher_id is not None else None,
            "batterName": batter_name_map.get(str(batter_id), str(batter_id)) if batter_id is not None else None,
            "pitcherName": pitcher_name_map.get(str(pitcher_id), str(pitcher_id)) if pitcher_id is not None else None,
            "gameDate": row_dict.get("game_date"),
            "releaseSpeed": row_dict.get("release_speed"),
            "launchSpeed": row_dict.get("launch_speed"),
            "launchAngle": row_dict.get("launch_angle"),
            "inning": row_dict.get("inning"),
            "balls": row_dict.get("balls"),
            "strikes": row_dict.get("strikes"),
            "outs": row_dict.get("outs_when_up"),
            "events": row_dict.get("events"),
            "description": row_dict.get("description"),
            "contactType": row_dict.get("bb_type"),
        })
        location_pitch_type_counts[pitch_type] = location_pitch_type_counts.get(pitch_type, 0) + 1

    pitch_zone_data = {}
    pitch_zone_combos = []
    for zone_num, data in pitch_zone_counts.items():
        zone_total = data["total"]
        pitch_types_in_zone = [
            {
                "pitchType": pitch_type,
                "count": count,
                "pct": pct(count, zone_total),
                "overallPct": pct(count, total),
            }
            for pitch_type, count in data["pitchTypes"].items()
        ]
        pitch_types_in_zone.sort(key=lambda item: item["count"], reverse=True)
        top_pitch = pitch_types_in_zone[0] if pitch_types_in_zone else None

        pitch_zone_data[zone_num] = {
            "zone": zone_num,
            "total": zone_total,
            "pct": pct(zone_total, total),
            "topPitchType": top_pitch["pitchType"] if top_pitch else None,
            "topPitchTypeCount": top_pitch["count"] if top_pitch else 0,
            "topPitchTypePct": top_pitch["pct"] if top_pitch else 0,
            "pitchTypes": pitch_types_in_zone,
        }

        for item in pitch_types_in_zone:
            pitch_zone_combos.append({
                "zone": zone_num,
                "pitchType": item["pitchType"],
                "count": item["count"],
                "zonePct": item["pct"],
                "overallPct": item["overallPct"],
            })

    pitch_zone_combos.sort(key=lambda item: item["count"], reverse=True)

    max_location_count = max(location_counts.values()) if location_counts else 0
    location_cells = []
    if max_location_count:
        min_x, max_x = LOCATION_X_RANGE
        min_z, max_z = LOCATION_Z_RANGE
        x_step = (max_x - min_x) / LOCATION_GRID_SIZE
        z_step = (max_z - min_z) / LOCATION_GRID_SIZE
        for (x_idx, z_idx), count in location_counts.items():
            intensity = count / max_location_count
            if intensity < 0.05 and count < 2:
                continue
            location_cells.append({
                "x": round(min_x + (x_idx + 0.5) * x_step, 3),
                "z": round(min_z + (z_idx + 0.5) * z_step, 3),
                "count": count,
                "pct": pct(count, len(location_points)),
                "intensity": round(intensity, 3),
            })
        location_cells.sort(key=lambda item: item["count"], reverse=True)

    top_location_pitch_types = [
        pitch_type
        for pitch_type, _ in sorted(
            location_pitch_type_counts.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:6]
    ]
    filtered_location_points = [
        point for point in location_points
        if point["pitchType"] in top_location_pitch_types
    ]
    point_limit = 180
    if len(filtered_location_points) > point_limit:
        step = len(filtered_location_points) / point_limit
        sampled_location_points = [
            filtered_location_points[int(i * step)]
            for i in range(point_limit)
        ]
    else:
        sampled_location_points = filtered_location_points

    return {
        "pitchZoneData": {
            "total": total,
            "zones": pitch_zone_data,
            "topCombos": pitch_zone_combos[:8],
            "source": "next_pitch",
        },
        "pitchLocationData": {
            "total": len(location_points),
            "xRange": LOCATION_X_RANGE,
            "zRange": LOCATION_Z_RANGE,
            "cells": location_cells[:140],
            "points": [
                {
                    "x": round(point["x"], 3),
                    "z": round(point["z"], 3),
                    "pitchType": point["pitchType"],
                    "batterId": point.get("batterId"),
                    "pitcherId": point.get("pitcherId"),
                    "batterName": point.get("batterName"),
                    "pitcherName": point.get("pitcherName"),
                    "gameDate": point.get("gameDate"),
                    "releaseSpeed": point.get("releaseSpeed"),
                    "launchSpeed": point.get("launchSpeed"),
                    "launchAngle": point.get("launchAngle"),
                    "inning": point.get("inning"),
                    "balls": point.get("balls"),
                    "strikes": point.get("strikes"),
                    "outs": point.get("outs"),
                    "events": point.get("events"),
                    "description": point.get("description"),
                    "contactType": point.get("contactType"),
                }
                for point in sampled_location_points
            ],
            "legendPitchTypes": top_location_pitch_types,
            "source": "next_pitch",
        },
    }

def fetch_next_pitch_summary(where, params):
    sequence_columns = {"game_pk", "at_bat_number", "pitch_number"}
    if not sequence_columns.issubset(PITCH_COLUMNS):
        return None

    next_columns = [
        "pitch_type", "zone", "plate_x", "plate_z",
        *optional_pitch_columns(
            "batter", "pitcher", "game_date", "release_speed", "launch_speed",
            "launch_angle", "inning", "balls", "strikes", "outs_when_up",
            "events", "description", "bb_type",
        ),
    ]
    select_columns = [f"n.{column} AS {column}" for column in next_columns]
    query = f"""
        SELECT {", ".join(select_columns)}
        FROM {TABLE_NAME} p
        JOIN {TABLE_NAME} n
          ON n.game_pk = p.game_pk
         AND n.at_bat_number = p.at_bat_number
         AND n.pitch_number = p.pitch_number + 1
        {where}
    """
    rows = fetch_all_dicts(query, params)
    return summarize_next_pitch_rows(rows)

@app.on_event("startup")
async def startup_event():
    global TABLE_NAME, PITCH_COLUMNS, TABLE_NAMES, DATABASE_URL

    if using_postgres():
        try:
            conn = connect_db()
            conn.close()
        except Exception as e:
            print(f"⚠️ PostgreSQL 連線失敗，改用 SQLite fallback: {e}")
            DATABASE_URL = None

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
        TABLE_NAMES = set(tables)
        if "pitches" in tables: 
            TABLE_NAME = "pitches"
        elif tables: 
            TABLE_NAME = tables[0]
        else:
            print("警告: 資料庫中找不到任何資料表")
            conn.close()
            return
        PITCH_COLUMNS = set(table_columns(conn, TABLE_NAME))

        conn.close()
        
        backend = "PostgreSQL" if using_postgres() else "SQLite"
        print(f"✅ 後端初始化完成，使用 {backend} 資料庫！")

    except Exception as e:
        print(f"❌ 啟動出錯: {e}")
@app.get("/api/batters")
async def get_batters():
    global batter_name_map
    if not batter_name_map or has_fallback_names(batter_name_map, "Batter"):
        try:
            batter_name_map = fetch_player_name_options("batter", "Batter")
        except Exception as e:
            print(f"❌ Batters API 錯誤: {e}")
            return []
    return sorted([{"id": k, "name": v} for k, v in batter_name_map.items()], key=lambda x: x['name'])

@app.get("/api/health")
async def get_health():
    health = {
        "ok": False,
        "backend": "PostgreSQL" if using_postgres() else "SQLite",
        "databaseUrlSet": using_postgres(),
        "tableName": TABLE_NAME,
        "batterNameCount": len(batter_name_map),
    }

    try:
        conn = connect_db()
        tables = table_names(conn)
        health["tables"] = tables

        if TABLE_NAME in tables:
            health["pitchesRows"] = table_count(conn, TABLE_NAME)
            min_date, max_date = table_date_range(conn, TABLE_NAME)
            health["gameDateRange"] = {"min": min_date, "max": max_date}
            health["columns"] = sorted(table_columns(conn, TABLE_NAME))

        if "player_names" in tables:
            health["playerNamesRows"] = table_count(conn, "player_names")

        conn.close()
        health["ok"] = bool(health.get("pitchesRows"))
        return health
    except Exception as e:
        health["error"] = str(e)
        return health

@app.get("/api/pitchers")
async def get_pitchers():
    global pitcher_name_map
    if pitcher_name_map and not has_fallback_names(pitcher_name_map, "Pitcher"):
        return sorted([{"id": k, "name": v} for k, v in pitcher_name_map.items()], key=lambda x: x["name"])

    try:
        pitcher_name_map = fetch_player_name_options("pitcher", "Pitcher", "player_name")
        return sorted([{"id": k, "name": v} for k, v in pitcher_name_map.items()], key=lambda x: x["name"])
    except Exception as e:
        print(f"❌ Pitchers API 錯誤: {e}")
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
    pitcherHand: str = None,
    batterHand: str = None,
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
            batterHand=batterHand,
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
    batterHand: str = None,
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
            batterHand=batterHand,
            outs=outs,
            on1b=on1b,
            on2b=on2b,
            on3b=on3b,
        )
        next_where, next_params = build_pitch_filters(
            year=year,
            pitcherId=pitcherId,
            batterId=batterId,
            pitcherRole=pitcherRole,
            zone=zone,
            pitchType=pitchType,
            balls=balls,
            strikes=strikes,
            pitcherHand=pitcherHand,
            batterHand=batterHand,
            outs=outs,
            on1b=on1b,
            on2b=on2b,
            on3b=on3b,
            table_alias="p",
        )

        if where is None:
            return summarize_rows([])

        summary_columns = [
            "pitch_type", "zone", "description", "type", "events",
            *optional_pitch_columns(
                "stand", "release_speed", "strikes", "plate_x", "plate_z",
                "pitcher", "batter", "game_date", "launch_speed", "launch_angle",
                "inning", "balls", "outs_when_up", "bb_type", "runs_on_pa",
            ),
        ]

        query = f"""
            SELECT {", ".join(summary_columns)}
            FROM {TABLE_NAME}
            {where}
        """

        rows = fetch_all_dicts(query, params)

        summary = summarize_rows(rows)
        next_pitch_summary = fetch_next_pitch_summary(next_where, next_params)
        if next_pitch_summary:
            summary["pitchZoneData"] = next_pitch_summary["pitchZoneData"]
            summary["pitchLocationData"] = next_pitch_summary["pitchLocationData"]

        pitcher_standard = fetch_pitcher_standard_stats(pitcherId, year)
        if pitcher_standard:
            summary["standardStats"] = pitcher_standard
        return summary

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
    batterHand: str = None,
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
            batterHand=batterHand,
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

@app.get("/api/pitches/empirical")
@app.get("/api/pitches/predict")
async def get_pitch_empirical(
    year: str = None,
    pitcherId: str = None,
    batterId: str = None,
    pitcherRole: str = "All",
    balls: str = None,
    strikes: str = None,
    pitcherHand: str = None,
    batterHand: str = None,
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
            batterHand=batterHand,
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
            "source": "empirical_statcast_history",
            "model": None,
        }

    except Exception as e:
        print(f"❌ Empirical API 錯誤: {e}")
        return {"total": 0, "recommendations": []}

@app.get("/api/pitcher-similarities")  # 💡 確定移除網址後面的 {pitcher_name}
async def get_pitcher_similarities(pitcher_name: str = None):  # 💡 用 Query 參數接收
    try:
        if not pitcher_name:
            return []
            
        query = """
            SELECT similar_pitcher, rank, distance_score, similar_stats 
            FROM pitcher_similarities 
            WHERE target_pitcher = %s
            ORDER BY rank ASC;
        """
        
        rows = fetch_all_dicts(query, [pitcher_name])
        return rows

    except Exception as e:
        print(f"❌ Similarities API 錯誤: {e}")
        return []
    
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
