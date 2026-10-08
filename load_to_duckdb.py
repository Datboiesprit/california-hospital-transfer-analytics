"""
load_to_duckdb.py
-----------------
Builds the local DuckDB database from scratch:
  1. Deletes the old database file (so every run starts clean)
  2. Creates the tables from schema.sql
  3. Loads data/clean/hospitals.csv into dim_hospital
  4. Runs a few checks to confirm the load worked

Usage:
    python load_to_duckdb.py
"""

from pathlib import Path   # handles file paths on any OS
import duckdb              # the database engine; the whole database lives in one file

DB_PATH = Path("hospital_transfers.duckdb")      # the database file this script creates
SCHEMA_PATH = Path("schema.sql")                 # table definitions
HOSPITALS_CSV = Path("data/clean/hospitals_geo.csv") # output of add_geography.py


def main() -> None:
    # --- 1. Start fresh ----------------------------------------------------
    # Deleting and rebuilding makes the script "idempotent": running it twice
    # gives the same result as running it once, instead of erroring because
    # the tables already exist or loading the same hospitals twice.
    if DB_PATH.exists():
        DB_PATH.unlink()   # unlink = delete the file

    # Fail early with a clear message if the previous step hasn't been run
    if not HOSPITALS_CSV.exists():
        raise FileNotFoundError(f"{HOSPITALS_CSV} not found. Run fetch_hospitals.py and add_geography.py first.")

    # connect() opens the database file, creating it if it doesn't exist
    con = duckdb.connect(str(DB_PATH))

    # --- 2. Create the tables -------------------------------------------------
    # Read schema.sql as text and run it. DuckDB can run several
    # CREATE TABLE statements in one call
    con.execute(SCHEMA_PATH.read_text())
    print("Created tables from schema.sql")

    # --- 3. Load hospitals ----------------------------------------------------
    # read_csv reads the file directly, no pandas needed.
    # types={...} forces hospital_id and zip_code to stay text. Without it,
    # DuckDB would guess they're numbers and turn an ID like "050001" into
    # 50001, silently breaking joins later. This is a very common real-world bug.
    #
    # "BY NAME" matches CSV columns to table columns by name instead of by
    # position, so reordering columns in one place won't scramble the data.
    con.execute(f"""
        INSERT INTO dim_hospital BY NAME
        SELECT * FROM read_csv(
            '{HOSPITALS_CSV.as_posix()}',
            header = true,
            types = {{'hospital_id': 'VARCHAR', 'zip_code': 'VARCHAR'}}
        )
    """)
    # (The doubled {{ }} are needed because this is an f-string: they produce
    #  single { } in the actual SQL.)

    # --- 4. Verify the load ---------------------------------------------------
    # Always check a load instead of assuming it worked.
    # fetchone() returns the first result row as a tuple; [0] takes its first value
    count = con.execute("SELECT COUNT(*) FROM dim_hospital").fetchone()[0]
    print(f"Loaded {count} hospitals into dim_hospital")

    # Check for leading-zero IDs being intact (CMS IDs are 6 characters)
    bad_ids = con.execute(
        "SELECT COUNT(*) FROM dim_hospital WHERE LENGTH(hospital_id) <> 6"
    ).fetchone()[0]
    print(f"Hospital IDs not 6 characters long: {bad_ids} (should be 0)")

    # A real analysis query: hospitals and average CMS rating by type.
    # .df() returns the result as a pandas table, which prints neatly
    print("\nHospitals by type:")
    print(con.execute("""
        SELECT
            hospital_type,
            COUNT(*)                       AS hospitals,
            ROUND(AVG(overall_rating), 2)  AS avg_rating,
            SUM(CASE WHEN has_emergency_services THEN 1 ELSE 0 END) AS with_er
        FROM dim_hospital
        GROUP BY hospital_type
        ORDER BY hospitals DESC
    """).df().to_string(index=False))

    # Close the connection so the file isn't locked for other tools (like DBeaver)
    con.close()
    print(f"\nDatabase saved to {DB_PATH}")


if __name__ == "__main__":
    main()
