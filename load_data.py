"""
Shared helper: load a table from home_data.db into a pandas DataFrame.

Every numbered script in this folder imports load_table() from here instead
of repeating the SQLite/pandas boilerplate six times.
"""
import os
import sqlite3

import pandas as pd

# Edit this to wherever home_data.db actually lives on your machine.
# (You can also override it without editing this file by setting an
# environment variable, e.g.  set HOME_DATA_DB=D:\backup\home_data.db)
DB_PATH = os.environ.get(
    "HOME_DATA_DB",
    r"C:\AI\Projects\off_grid_house_control\home_data.db",
)


def load_table(table_name: str, db_path: str = DB_PATH, since_days: float | None = None) -> pd.DataFrame:
    """
    Load an entire table into a DataFrame, with `timestamp` parsed as a
    real (UTC) datetime column instead of plain text.

    since_days: if given, keep only rows from the last N days, measured
    from the newest timestamp actually in the table (not "now") — so this
    still works if the collector has been off for a while.
    """
    con = sqlite3.connect(db_path)
    try:
        df = pd.read_sql(f"SELECT * FROM {table_name}", con)
    finally:
        con.close()

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    if since_days is not None:
        cutoff = df["timestamp"].max() - pd.Timedelta(days=since_days)
        df = df[df["timestamp"] >= cutoff]

    return df.sort_values("timestamp").reset_index(drop=True)
