"""
run_analysis.py
---------------
Runs every query in analysis.sql against the DuckDB database and prints
the results. Also saves each result as a CSV in results/ (handy for
charts or Power BI).

Usage:
    python run_analysis.py
"""

import re                 # "regular expressions": pattern matching in text
from pathlib import Path
import duckdb

DB_PATH = Path("hospital_transfers.duckdb")
SQL_PATH = Path("analysis.sql")
RESULTS_DIR = Path("results")


def split_queries(sql_text: str) -> list[tuple[str, str]]:
    """Split analysis.sql into (name, query) pairs using the '-- name:' markers."""
    # re.split on the marker; the (.+) captures the name after "-- name:"
    parts = re.split(r"^-- name:\s*(.+)$", sql_text, flags=re.MULTILINE)
    # parts looks like [header, name1, query1, name2, query2, ...]
    # parts[1::2] = every name, parts[2::2] = every query; zip pairs them up
    return [(name.strip(), query.strip()) for name, query in zip(parts[1::2], parts[2::2])]


def main() -> None:
    con = duckdb.connect(str(DB_PATH), read_only=True)  # read_only: analysis can't change data
    RESULTS_DIR.mkdir(exist_ok=True)

    for name, query in split_queries(SQL_PATH.read_text()):
        print(f"\n{'=' * 70}\n{name}\n{'=' * 70}")   # section header in the terminal
        df = con.execute(query).df()
        print(df.to_string(index=False))

        # File name from the query name: "2. Top 10 receiving..." -> "2_top_10_receiving..."
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        df.to_csv(RESULTS_DIR / f"{slug}.csv", index=False)

    con.close()
    print(f"\nResults saved to {RESULTS_DIR}/")


if __name__ == "__main__":
    main()
