"""
add_geography.py
----------------
Adds latitude/longitude to each hospital using the Census Bureau's free
ZIP Code Tabulation Area (ZCTA) Gazetteer file, which lists the center
point of every ZIP code in the US.

Input:  data/clean/hospitals.csv          (from fetch_hospitals.py)
Output: data/clean/hospitals_geo.csv      (same table + latitude, longitude)

Run order:
    python fetch_hospitals.py
    python add_geography.py      <- this step
    python load_to_duckdb.py
    python generate_synthetic.py
    python run_analysis.py
"""

import zipfile
from pathlib import Path
import pandas as pd
import requests

HOSPITALS_CSV = Path("data/clean/hospitals.csv")
OUTPUT_CSV = Path("data/clean/hospitals_geo.csv")
REF_DIR = Path("data/reference")   # reference data: rarely changes, reused across runs

# The Census publishes a new file each year. Try the newest first and fall
# back to older years if a link doesn't work.
GAZETTEER_URLS = [
    f"https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{y}_Gazetteer/{y}_Gaz_zcta_national.zip"
    for y in (2025, 2024, 2023)
]
MANUAL_PAGE = "https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html"


def get_gazetteer_file() -> Path:
    """Use a copy already in data/reference/ if there is one; otherwise download it."""
    REF_DIR.mkdir(parents=True, exist_ok=True)

    # glob finds files matching a pattern (* = anything). If you downloaded
    # the file by hand and dropped it in data/reference/, this finds it
    existing = sorted(REF_DIR.glob("*Gaz_zcta_national*"))
    if existing:
        print(f"Using existing file: {existing[-1]}")
        return existing[-1]

    for url in GAZETTEER_URLS:
        print(f"Trying {url}")
        try:
            resp = requests.get(url, timeout=60)
        except requests.RequestException:
            continue   # network problem: move on to the next URL
        if resp.status_code == 200:
            path = REF_DIR / url.split("/")[-1]   # file name = last part of the URL
            path.write_bytes(resp.content)        # write_bytes because a .zip isn't text
            print(f"Downloaded -> {path}")
            return path

    # Every URL failed: explain how to fix it instead of crashing with a cryptic error
    raise RuntimeError(
        "Couldn't download the Census ZIP file automatically.\n"
        f"Download the 'ZIP Code Tabulation Areas' national file from:\n  {MANUAL_PAGE}\n"
        f"and put the .zip (or .txt) in {REF_DIR}/, then run this script again."
    )


def detect_delimiter(path: Path) -> str:
    """Read the file's first line and return whichever delimiter it contains."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:              # "with" closes the file automatically
            with zf.open(zf.namelist()[0]) as f:       # the one file inside the zip
                header = f.readline().decode("utf-8")  # zip contents are bytes; decode to text
    else:
        with open(path, encoding="utf-8") as f:
            header = f.readline()

    for sep in ["\t", "|", ","]:
        if sep in header:
            return sep
    raise ValueError(f"Couldn't detect the delimiter. Header line: {header!r}")


def load_zip_centroids(path: Path) -> pd.DataFrame:
    # A failed download can save an HTML error page with a .zip name.
    # is_zipfile checks the file really is a zip before we try to read it
    if path.suffix == ".zip" and not zipfile.is_zipfile(path):
        path.unlink()   # delete the bad file so the next run downloads again
        raise RuntimeError(f"{path.name} wasn't a valid zip and was deleted. Run again, "
                           f"or download it manually from:\n  {MANUAL_PAGE}")

    # Census files haven't always used the same delimiter (tabs in some
    # years, pipes "|" in others), so check the header line to see which
    # one this file uses. dtype=str on GEOID keeps ZIP codes as text so
    # leading zeros survive (same lesson as hospital IDs).
    # pandas can read a .zip directly as long as it holds a single file.
    sep = detect_delimiter(path)
    print(f"Detected delimiter: {sep!r}")   # !r shows it with quotes, so a tab prints as '\t'
    zips = pd.read_csv(path, sep=sep, dtype={"GEOID": str})

    # Census column names can have stray spaces ("INTPTLONG    "), so strip
    # whitespace and uppercase them to be safe
    zips.columns = zips.columns.str.strip().str.upper()

    # Check the columns we need exist, and show what's there if they don't
    needed = ["GEOID", "INTPTLAT", "INTPTLONG"]
    missing = [c for c in needed if c not in zips.columns]
    if missing:
        raise KeyError(f"Missing columns {missing}. The file has: {list(zips.columns)}")

    # INTPT = "internal point", the geographic center of the ZIP code
    zips = zips.rename(columns={"GEOID": "zip_code", "INTPTLAT": "latitude",
                                "INTPTLONG": "longitude"})
    return zips[["zip_code", "latitude", "longitude"]]


def main() -> None:
    if not HOSPITALS_CSV.exists():
        raise FileNotFoundError("Run fetch_hospitals.py first.")

    hospitals = pd.read_csv(HOSPITALS_CSV, dtype={"hospital_id": str, "zip_code": str})
    hospitals["zip_code"] = hospitals["zip_code"].str[:5]   # "92101-1234" -> "92101"

    zips = load_zip_centroids(get_gazetteer_file())

    # LEFT join: keep every hospital even if its ZIP isn't in the Census file
    # (some ZIPs are PO-box-only and have no geographic area)
    geo = hospitals.merge(zips, on="zip_code", how="left")

    # Fallback for unmatched hospitals: use the average location of the
    # other hospitals in the same county. groupby().transform("mean")
    # returns the county average on every row, lined up with the original rows
    missing = geo["latitude"].isna()
    print(f"Matched {(~missing).sum()} of {len(geo)} hospitals by ZIP code")
    if missing.any():
        for col in ["latitude", "longitude"]:
            geo[col] = geo[col].fillna(geo.groupby("county")[col].transform("mean"))
        still_missing = geo["latitude"].isna().sum()
        print(f"Filled {missing.sum() - still_missing} from county averages; "
              f"{still_missing} still missing (these are dropped)")
        geo = geo.dropna(subset=["latitude", "longitude"])

    geo.to_csv(OUTPUT_CSV, index=False)
    print(f"Saved {len(geo)} hospitals with coordinates -> {OUTPUT_CSV}")


if __name__ == "__main__":
    main()