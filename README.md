# Baseball App Backend

FastAPI backend for MLB pitch tendency and summary endpoints.

## Main Files

- `main.py` - FastAPI app and API routes.
- `requirements.txt` - Python dependencies.
- `data/` - local SQLite databases. This folder is ignored by git.
- `scripts/build_db.py` - downloads Statcast data and builds a full SQLite database.
- `scripts/make_public_db.py` - creates a smaller public database from the full database.
- `scripts/sqlite_to_postgres.py` - imports SQLite data into PostgreSQL/Supabase.
- `scripts/refresh_supabase.py` - runs the Supabase refresh steps in order.
- `scripts/check_supabase.py` - checks whether the PostgreSQL database is ready.

## Local Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

By default the app looks for:

```text
data/baseball_data_2024_2025.db
```

You can override the database with:

```bash
BASEBALL_DB_FILENAME=/absolute/path/to/baseball_data.db uvicorn main:app --reload
```

## Notes

Generated folders such as `.venv/` and `__pycache__/` are not source files. Local database files can be large and should stay in `data/`.
