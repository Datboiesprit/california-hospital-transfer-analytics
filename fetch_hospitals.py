"""
fetch_hospitals.py
------------------
Pulls the CMS "Hospital General Information" dataset from the public
CMS Provider Data API and saves:

  data/raw/hospitals_raw_<date>.json   -> untouched API response (raw layer)
  data/clean/hospitals.csv             -> cleaned table for dim_hospital

No API key needed. Usage:
    python fetch_hospitals.py              # California only (default)
    python fetch_hospitals.py --state TX   # another state
    python fetch_hospitals.py --all        # every state
"""
# ^ A "docstring": text at the top of a file (or function) that documents
#   what it does. Python ignores it when running; it's for humans.

# ===========================================================================
# IMPORTS: bringing in code other people wrote so we don't rewrite it
# ===========================================================================
import argparse          # reads options typed after the command, like --state TX
import json              # converts between Python data and JSON text
import logging           # prints timestamped status messages (the INFO lines you saw)
import time              # lets us pause between requests
from datetime import date     # gives us today's date for the raw file name
from pathlib import Path      # handles file/folder paths on any OS (Windows uses \, Mac uses /)

import pandas as pd      # table library: think Excel in code. "pd" is the standard nickname
import requests          # makes HTTP requests, i.e. calls APIs
from requests.adapters import HTTPAdapter   # lets us attach retry rules to requests
from urllib3.util.retry import Retry        # defines the retry rules themselves

# ===========================================================================
# CONFIG: settings in one place. ALL_CAPS names signal "constant, don't change at runtime"
# ===========================================================================
DATASET_ID = "xubh-q36u"  # CMS's ID for the Hospital General Information dataset

# The API endpoint (URL) we call. The f"..." is an f-string: {DATASET_ID} gets
# replaced by the variable's value, so the final URL ends in .../xubh-q36u/0
BASE_URL = f"https://data.cms.gov/provider-data/api/1/datastore/query/{DATASET_ID}/0"

PAGE_SIZE = 500            # the API returns at most 500 rows per request
PAUSE_SECONDS = 0.5        # wait half a second between requests so we don't hammer the server

RAW_DIR = Path("data/raw")       # folder for the untouched API response
CLEAN_DIR = Path("data/clean")   # folder for the cleaned CSV

# A dictionary: pairs of key -> value. Here, key = the column name CMS uses,
# value = the name we want in our database. Renaming gives us clean, consistent names.
COLUMN_MAP = {
    "facility_id": "hospital_id",
    "facility_name": "hospital_name",
    "address": "address",
    "citytown": "city",
    "state": "state",
    "zip_code": "zip_code",
    "countyparish": "county",
    "hospital_type": "hospital_type",
    "hospital_ownership": "ownership",
    "emergency_services": "has_emergency_services",
    "hospital_overall_rating": "overall_rating",
}

# Set up logging: show INFO-level messages and above, formatted as "time LEVEL message"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)   # create the logger we'll call as log.info(...)


# ===========================================================================
# EXTRACT: get the data out of the API (the "E" in ETL)
# ===========================================================================
def build_session() -> requests.Session:
    # "def" defines a function. "-> requests.Session" is a type hint: a note
    # saying what the function returns. It doesn't change how the code runs.
    """Session that automatically retries on rate limits (429) and server errors."""

    retry = Retry(
        total=5,               # try up to 5 more times before giving up
        backoff_factor=1,      # wait 1s, 2s, 4s, 8s... between retries ("exponential backoff")
        status_forcelist=[429, 500, 502, 503, 504],  # retry on these HTTP error codes:
                               # 429 = too many requests (rate limited)
                               # 5xx = the server had a problem, not us
        allowed_methods=["GET"],  # only retry GET requests (reading data is safe to repeat)
    )
    session = requests.Session()   # a Session reuses one connection for all requests (faster)
    session.mount("https://", HTTPAdapter(max_retries=retry))  # apply retry rules to all https:// calls
    return session                 # hand the configured session back to whoever called this


def fetch_all_pages(session: requests.Session) -> list[dict]:
    """Loop through the API page by page until every row is collected."""
    rows: list[dict] = []   # empty list that will hold every hospital (each one is a dict)
    offset = 0              # where to start reading; 0 = the first row
    total = None            # total row count; unknown until the first response tells us

    while True:             # keep looping until we hit a "break" statement
        # Query parameters get added to the URL, e.g. ...?limit=500&offset=1000
        # meaning "give me 500 rows, starting at row 1000"
        params = {"limit": PAGE_SIZE, "offset": offset}

        # Send the GET request. timeout=30 means give up if no answer in 30 seconds
        resp = session.get(BASE_URL, params=params, timeout=30)

        # If the response is an error (like 404), stop and show the error
        # instead of silently continuing with bad data
        resp.raise_for_status()

        payload = resp.json()   # convert the JSON text into a Python dictionary

        # .get("results", []) means: take the "results" field, or an empty list if it's missing
        page = payload.get("results", [])
        total = payload.get("count", total)   # CMS reports the total rows (5,419 in your run)
        rows.extend(page)       # add this page's rows onto our running list

        # Print progress. Each %d or %s is filled in by the values after the text, in order
        log.info("Fetched %d rows (offset %d, total so far %d of %s)",
                 len(page), offset, len(rows), total)

        offset += PAGE_SIZE     # move to the next page: 0 -> 500 -> 1000 ...

        # Stop when a page comes back empty OR we've passed the total row count
        if not page or (total is not None and offset >= int(total)):
            break               # exits the while loop

        time.sleep(PAUSE_SECONDS)   # polite pause before the next request

    return rows                 # all 5,419 hospitals as a list of dicts


def save_raw(rows: list[dict]) -> Path:
    """Keep the untouched API response so cleaning can be rerun without refetching."""
    # Create the folder. parents=True also creates "data" if needed;
    # exist_ok=True means don't error if it already exists
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    # Build the file name with today's date, e.g. hospitals_raw_20260926.json.
    # The "/" here joins path pieces (pathlib handles Windows backslashes for you)
    path = RAW_DIR / f"hospitals_raw_{date.today():%Y%m%d}.json"

    # Convert the list to JSON text (indent=2 makes it readable) and write it to the file
    path.write_text(json.dumps(rows, indent=2))
    log.info("Saved raw data -> %s", path)
    return path


# ===========================================================================
# TRANSFORM: clean and reshape the data (the "T" in ETL)
# ===========================================================================
def clean(rows: list[dict], state: str | None) -> pd.DataFrame:
    # "str | None" means state can be text like "CA", or None (meaning keep all states)

    df = pd.DataFrame(rows)   # turn the list of dicts into a table; each dict becomes a row

    # List comprehension: a one-line loop. Reads as "for each column name c in
    # COLUMN_MAP, keep c if it's NOT in the table's columns"
    missing = [c for c in COLUMN_MAP if c not in df.columns]
    if missing:   # an empty list counts as False, so this only runs if something is missing
        log.warning("API columns not found (CMS may have renamed them): %s", missing)
        log.warning("Available columns: %s", list(df.columns))

    # Keep only the columns we mapped (dropping the other ~30 CMS columns),
    # then rename them to our names
    df = df[[c for c in COLUMN_MAP if c in df.columns]].rename(columns=COLUMN_MAP)

    if state:   # only filter if a state was given (not None)
        # .str.upper() makes the comparison case-insensitive ("ca" matches "CA").
        # df[condition] keeps only the rows where the condition is True
        df = df[df["state"].str.upper() == state.upper()]

    # Ratings arrive as text ("4" or "Not Available"). to_numeric converts to
    # numbers; errors="coerce" turns anything unconvertible into null (NaN).
    # "Int64" (capital I) is pandas' integer type that allows nulls
    if "overall_rating" in df.columns:
        df["overall_rating"] = pd.to_numeric(df["overall_rating"], errors="coerce").astype("Int64")

    # "Yes"/"No" text -> True/False. Strip spaces, lowercase, then check if it equals "yes"
    if "has_emergency_services" in df.columns:
        df["has_emergency_services"] = df["has_emergency_services"].str.strip().str.lower().eq("yes")

    # CMS stores names in ALL CAPS. .str.title() makes "SCRIPPS MERCY" -> "Scripps Mercy"
    for col in ["hospital_name", "city", "county"]:
        if col in df.columns:
            df[col] = df[col].str.strip().str.title()

    # Remove any duplicate hospitals (same ID twice), then renumber rows 0, 1, 2...
    # drop=True throws away the old row numbers instead of keeping them as a column
    df = df.drop_duplicates(subset="hospital_id").reset_index(drop=True)
    return df


# ===========================================================================
# LOAD: save the result (the "L" in ETL; later this will load into DuckDB/Snowflake)
# ===========================================================================
def save_clean(df: pd.DataFrame) -> Path:
    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    path = CLEAN_DIR / "hospitals.csv"
    df.to_csv(path, index=False)   # index=False: don't write pandas' row numbers as a column
    log.info("Saved %d hospitals -> %s", len(df), path)
    return path


# ===========================================================================
# MAIN: runs the steps above in order
# ===========================================================================
def main() -> None:   # "-> None" means this function doesn't return anything
    # Define the command-line options this script accepts
    parser = argparse.ArgumentParser(description="Fetch CMS hospital data")
    parser.add_argument("--state", default="CA", help="Two-letter state code (default CA)")
    # action="store_true": if you type --all, args.all becomes True; otherwise False
    parser.add_argument("--all", action="store_true", help="Keep every state")
    args = parser.parse_args()   # read what the user actually typed

    session = build_session()        # 1. set up the connection with retry rules
    rows = fetch_all_pages(session)  # 2. EXTRACT all pages from the API
    save_raw(rows)                   # 3. save the raw copy

    # 4. TRANSFORM. This is a one-line if/else: pass None (all states)
    #    if --all was typed, otherwise pass the chosen state
    df = clean(rows, state=None if args.all else args.state)
    save_clean(df)                   # 5. LOAD to CSV

    # Quick sanity check printed to the terminal
    print("\nHospitals by type:")   # \n = a blank line
    print(df["hospital_type"].value_counts().to_string())  # count rows per hospital type
    if "has_emergency_services" in df.columns:
        # .sum() on True/False values counts the Trues
        print(f"\nWith emergency services: {df['has_emergency_services'].sum()} of {len(df)}")


# This line means: only run main() when this file is run directly
# (python fetch_hospitals.py), NOT when another script imports it.
# That's how the test earlier could import these functions without calling the API.
if __name__ == "__main__":
    main()