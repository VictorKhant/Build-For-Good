"""Shared SQLite access for calculation, optimization, and API code."""

from pathlib import Path
import sqlite3
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "calc" / "sql" / "bellyup.db"

def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection

def read_table(name: str) -> pd.DataFrame:
    with connect() as connection:
        return pd.read_sql_query(f'SELECT * FROM "{name}"', connection)

def write_table(name: str, frame: pd.DataFrame) -> None:
    with connect() as connection:
        frame.to_sql(name, connection, if_exists="replace", index=False)

def table_names() -> list[str]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    return [row[0] for row in rows]
