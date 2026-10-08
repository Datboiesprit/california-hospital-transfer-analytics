"""
generate_synthetic.py
---------------------
Creates realistic FAKE data for the three tables that aren't from CMS:
  dim_transport_mode, dim_patient, fact_transfer

Transfers happen between the REAL California hospitals already in
dim_hospital, with realistic patterns built in:
  - Transfers go to nearby hubs (real distances from hospital coordinates)
  - Hubs are general acute hospitals in dense metro areas
  - Neonatal cases go to children's hospitals, psychiatric cases to psychiatric hospitals
  - Critical patients (acuity 1-2) are more likely to fly
  - Night requests take longer to dispatch
  - Some transfers are cancelled or diverted, so they have missing timestamps

Writes to the DuckDB database AND saves CSVs to data/synthetic/ (for Snowflake later).

Run order:
    python fetch_hospitals.py
    python add_geography.py
    python load_to_duckdb.py
    python generate_synthetic.py
"""

from pathlib import Path
import duckdb
import numpy as np     # fast math and random-number tools
import pandas as pd

DB_PATH = Path("hospital_transfers.duckdb")
OUT_DIR = Path("data/synthetic")

N_PATIENTS = 6000
N_TRANSFERS = 9000
YEAR = 2025

# A "seed" makes the random numbers repeatable: the same seed always
# produces the same data. Anyone who runs this gets identical results,
# which matters when someone wants to check your analysis.
rng = np.random.default_rng(seed=42)


# ===========================================================================
# 1. TRANSPORT MODES (a small fixed lookup table)
# ===========================================================================
def build_transport_modes() -> pd.DataFrame:
    return pd.DataFrame({
        "transport_mode_id": [1, 2, 3, 4, 5],
        "mode_name": ["Ground BLS", "Ground ALS", "Critical Care Ground", "Rotor Air", "Fixed-Wing Air"],
        # BLS = basic life support, ALS = advanced life support
        "avg_speed_mph": [40, 45, 45, 130, 250],
    })


# ===========================================================================
# 2. PATIENTS (demographic bands only, no names or real identifiers)
# ===========================================================================
def build_patients() -> pd.DataFrame:
    # rng.choice picks from a list using the probabilities in p (must add up to 1)
    age_band = rng.choice(["0-17", "18-44", "45-64", "65+"], size=N_PATIENTS,
                          p=[0.10, 0.25, 0.30, 0.35])
    sex = rng.choice(["F", "M"], size=N_PATIENTS)

    # Insurance depends on age: most 65+ patients are on Medicare
    insurance = np.where(
        age_band == "65+",
        rng.choice(["Medicare", "Commercial", "Medicaid"], size=N_PATIENTS, p=[0.85, 0.10, 0.05]),
        rng.choice(["Commercial", "Medicaid", "Uninsured", "Medicare"], size=N_PATIENTS,
                   p=[0.50, 0.35, 0.12, 0.03]),
    )  # np.where(condition, value_if_true, value_if_false), applied row by row

    return pd.DataFrame({
        # f"P{i:05d}" makes IDs like P00001, P00002 (zero-padded to 5 digits)
        "patient_id": [f"P{i:05d}" for i in range(1, N_PATIENTS + 1)],
        "age_band": age_band,
        "sex": sex,
        "insurance_type": insurance,
    })


# ===========================================================================
# 3. TRANSFERS (the fact table)
# ===========================================================================
REASONS = ["Cardiac", "Stroke", "Trauma", "Specialty", "Psychiatric", "Neonatal", "Burn"]
REASON_WEIGHTS = [0.24, 0.16, 0.18, 0.20, 0.12, 0.06, 0.04]

# Typical acuity range per reason (1 = most critical, 5 = least)
ACUITY_BY_REASON = {
    "Trauma": [0.35, 0.35, 0.20, 0.07, 0.03],
    "Burn": [0.40, 0.35, 0.15, 0.07, 0.03],
    "Stroke": [0.30, 0.40, 0.20, 0.07, 0.03],
    "Cardiac": [0.20, 0.35, 0.30, 0.10, 0.05],
    "Neonatal": [0.25, 0.35, 0.25, 0.10, 0.05],
    "Specialty": [0.05, 0.15, 0.35, 0.30, 0.15],
    "Psychiatric": [0.00, 0.05, 0.25, 0.40, 0.30],
}

# How fast the chance of choosing a hospital drops with distance (miles).
# With 40, a hub 40 miles away is ~37% as likely as an identical hub next
# door, and one 120 miles away is ~5% as likely. Specialty hospitals are
# rarer, so patients travel farther to reach them.
DISTANCE_DECAY_MILES = 40
SPECIALTY_DECAY_MILES = 100

SPEED_MPH = {1: 40, 2: 45, 3: 45, 4: 130, 5: 250}
ROAD_FACTOR = 1.3   # roads are ~30% longer than a straight line; aircraft fly straight


def distance_matrix(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Miles between every pair of hospitals (haversine formula: distance on a sphere)."""
    lat, lon = np.radians(lat), np.radians(lon)   # trig functions need radians
    # [:, None] turns a row into a column, so subtracting a row from a column
    # gives a full grid: every hospital vs every other hospital at once
    dlat = lat[:, None] - lat[None, :]
    dlon = lon[:, None] - lon[None, :]
    a = np.sin(dlat / 2) ** 2 + np.cos(lat[:, None]) * np.cos(lat[None, :]) * np.sin(dlon / 2) ** 2
    return 3959 * 2 * np.arcsin(np.sqrt(a))       # 3959 = Earth's radius in miles


def hub_popularity(hospitals: pd.DataFrame, dist: np.ndarray) -> np.ndarray:
    """How attractive each hospital is as a transfer destination (0 = never a hub)."""
    # Only general acute hospitals act as hubs. VA and military hospitals
    # serve restricted populations, so they're excluded
    is_hub = (hospitals["hospital_type"] == "Acute Care Hospitals").to_numpy()

    # Big regional centers sit in dense metro areas. With no bed counts in
    # the CMS data, use "how many hospitals are within 15 miles" as a proxy
    metro_density = (dist < 15).sum(axis=1)

    rating = hospitals["overall_rating"].fillna(3).astype(float).to_numpy()

    # Random boost so some hospitals become major centers and most stay small
    # (a power-law pattern, like real transfer networks)
    boost = 1 + rng.pareto(a=1.5, size=len(hospitals))

    return np.where(is_hub, rating * metro_density * boost, 0.0)


def pick_destination(origin_idx: int, candidates: np.ndarray, weights: np.ndarray,
                     dist: np.ndarray, decay: float) -> int | None:
    """Pick a destination, favoring popular hospitals that are close to the origin."""
    # Weight = popularity x distance penalty. exp(-d / decay) is 1 next door
    # and shrinks toward 0 as distance grows
    w = weights * np.exp(-dist[origin_idx, candidates] / decay)
    w[candidates == origin_idx] = 0          # can't transfer to yourself
    if w.sum() == 0:
        return None
    return int(rng.choice(candidates, p=w / w.sum()))


def pick_mode(reason: str, acuity: int, miles: float) -> int:
    """Transport mode from how sick the patient is and how far they're going."""
    if reason == "Psychiatric":
        return 1 if acuity >= 4 else 2
    if acuity <= 2:   # critical patients
        if miles > 150:
            return int(rng.choice([5, 4, 3], p=[0.50, 0.30, 0.20]))
        if miles > 40:
            return int(rng.choice([4, 3, 2], p=[0.55, 0.35, 0.10]))
        return int(rng.choice([3, 2, 4], p=[0.50, 0.40, 0.10]))
    if miles > 150:   # stable patients, long trip
        return int(rng.choice([2, 1, 5], p=[0.60, 0.30, 0.10]))
    return int(rng.choice([1, 2, 3], p=[0.50, 0.40, 0.10]))


def build_transfers(hospitals: pd.DataFrame, patients: pd.DataFrame) -> pd.DataFrame:
    hospitals = hospitals.reset_index(drop=True)   # row positions 0..n-1 line up with the matrix
    ids = hospitals["hospital_id"].to_numpy()
    dist = distance_matrix(hospitals["latitude"].to_numpy(), hospitals["longitude"].to_numpy())

    all_idx = np.arange(len(hospitals))            # [0, 1, 2, ..., n-1]
    popularity = hub_popularity(hospitals, dist)
    htype = hospitals["hospital_type"].to_numpy()
    childrens = all_idx[htype == "Childrens"]
    psych = all_idx[htype == "Psychiatric"]

    # Senders must have an ER. Small rural (critical access) hospitals send
    # more often because they lack specialists
    senders = all_idx[hospitals["has_emergency_services"].to_numpy()]
    send_w = np.where(htype[senders] == "Critical Access Hospitals", 3.0, 1.0)
    send_w = send_w / send_w.sum()

    patient_ids = patients["patient_id"].to_numpy()
    rows = []
    while len(rows) < N_TRANSFERS:
        reason = rng.choice(REASONS, p=REASON_WEIGHTS)
        acuity = int(rng.choice([1, 2, 3, 4, 5], p=ACUITY_BY_REASON[reason]))
        origin = int(rng.choice(senders, p=send_w))

        # --- Destination: specialty hospital for neonatal/psych, otherwise a nearby hub ---
        dest = None
        if reason == "Neonatal" and len(childrens) and rng.random() < 0.8:
            dest = pick_destination(origin, childrens, np.ones(len(childrens)), dist, SPECIALTY_DECAY_MILES)
        elif reason == "Psychiatric" and len(psych) and rng.random() < 0.85:
            dest = pick_destination(origin, psych, np.ones(len(psych)), dist, SPECIALTY_DECAY_MILES)
        if dest is None:
            dest = pick_destination(origin, all_idx, popularity, dist, DISTANCE_DECAY_MILES)
        if dest is None:
            continue   # no valid destination (very rare); try another transfer

        miles = float(dist[origin, dest])
        mode = pick_mode(reason, acuity, miles)

        # --- Timestamps ---
        requested = pd.Timestamp(f"{YEAR}-01-01") + pd.Timedelta(
            minutes=int(rng.integers(0, 365 * 24 * 60)))
        night = requested.hour >= 22 or requested.hour < 6

        # Lognormal = mostly short waits with an occasional long one.
        # Critical patients are dispatched faster; nights are ~40% slower
        dispatch_min = rng.lognormal(mean=np.log(10 + acuity * 8), sigma=0.5)
        dispatch_min *= 1.4 if night else 1.0
        dispatched = requested + pd.Timedelta(minutes=round(dispatch_min))
        picked_up = dispatched + pd.Timedelta(minutes=round(rng.uniform(15, 45)))

        # Travel time from real distance: ground follows roads, aircraft fly straight.
        # Fixed-wing planes also need ground ambulances at both airports (+40 min)
        route_miles = miles if mode in (4, 5) else miles * ROAD_FACTOR
        travel_min = route_miles / SPEED_MPH[mode] * 60 + rng.uniform(5, 15)
        if mode == 5:
            travel_min += 40
        arrived = picked_up + pd.Timedelta(minutes=round(travel_min))

        # --- Outcome: most complete, some don't ---
        status = rng.choice(["Completed", "Cancelled", "Diverted"], p=[0.90, 0.06, 0.04])
        if status == "Cancelled":
            picked_up = arrived = pd.NaT       # these steps never happened
            if rng.random() < 0.5:
                dispatched = pd.NaT            # cancelled before a crew was sent

        rows.append({
            "transfer_id": f"T{len(rows) + 1:06d}",
            "patient_id": rng.choice(patient_ids),
            "origin_hospital_id": ids[origin],
            "destination_hospital_id": ids[dest],
            "transport_mode_id": mode,
            "distance_miles": round(miles, 1),
            "transfer_reason": reason,
            "acuity_level": acuity,
            "status": status,
            "requested_at": requested,
            "dispatched_at": dispatched,
            "picked_up_at": picked_up,
            "arrived_at": arrived,
        })

    return pd.DataFrame(rows)


# ===========================================================================
# MAIN
# ===========================================================================
def main() -> None:
    if not DB_PATH.exists():
        raise FileNotFoundError("Database not found. Run load_to_duckdb.py first.")

    con = duckdb.connect(str(DB_PATH))
    hospitals = con.execute("SELECT * FROM dim_hospital").df()
    if hospitals.empty:
        raise ValueError("dim_hospital is empty. Run load_to_duckdb.py first.")

    modes = build_transport_modes()
    patients = build_patients()
    transfers = build_transfers(hospitals, patients)

    # Clear old synthetic rows so reruns don't duplicate data.
    # The fact table goes first because it references the other two
    # (you can't delete a patient that a transfer still points to)
    for table in ["fact_transfer", "dim_patient", "dim_transport_mode"]:
        con.execute(f"DELETE FROM {table}")

    # DuckDB can read a pandas DataFrame directly by its variable name
    con.execute("INSERT INTO dim_transport_mode BY NAME SELECT * FROM modes")
    con.execute("INSERT INTO dim_patient BY NAME SELECT * FROM patients")
    con.execute("INSERT INTO fact_transfer BY NAME SELECT * FROM transfers")

    # Save CSV copies for loading into Snowflake later
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    modes.to_csv(OUT_DIR / "dim_transport_mode.csv", index=False)
    patients.to_csv(OUT_DIR / "dim_patient.csv", index=False)
    transfers.to_csv(OUT_DIR / "fact_transfer.csv", index=False)

    # --- Sanity checks ---
    for table in ["dim_transport_mode", "dim_patient", "fact_transfer"]:
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}: {n:,} rows")   # {n:,} adds thousands separators

    # Does the "few hubs get most of the volume" pattern show up?
    share = con.execute("""
        WITH counts AS (
            SELECT destination_hospital_id, COUNT(*) AS n
            FROM fact_transfer
            GROUP BY 1
        ), ranked AS (
            SELECT n, NTILE(5) OVER (ORDER BY n DESC) AS quintile  -- split into 5 equal groups
            FROM counts
        )
        SELECT ROUND(100.0 * SUM(CASE WHEN quintile = 1 THEN n END) / SUM(n), 1)
        FROM ranked
    """).fetchone()[0]
    print(f"\nShare of transfers received by the top 20% of destinations: {share}%")

    con.close()
    print(f"CSV copies saved to {OUT_DIR}/")


if __name__ == "__main__":
    main()
