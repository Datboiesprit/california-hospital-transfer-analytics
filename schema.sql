-- Hospital Transfer Analytics: star schema
-- Real data:      dim_hospital (CMS Provider Data API)
-- Synthetic data: everything else (no real patient information)

-- Hospitals (from fetch_hospitals.py)
CREATE TABLE dim_hospital (
    hospital_id             VARCHAR PRIMARY KEY,  -- CMS facility ID
    hospital_name           VARCHAR NOT NULL,
    address                 VARCHAR,
    city                    VARCHAR,
    state                   VARCHAR(2),
    zip_code                VARCHAR(10),
    county                  VARCHAR,
    hospital_type           VARCHAR,              -- e.g. Acute Care, Critical Access
    ownership               VARCHAR,
    has_emergency_services  BOOLEAN,
    overall_rating          INTEGER,              -- CMS 1-5 stars, null if not rated
    latitude                DOUBLE,               -- ZIP code center (Census Gazetteer)
    longitude               DOUBLE
);

-- Synthetic patients (demographic bands only, no identifiers)
CREATE TABLE dim_patient (
    patient_id      VARCHAR PRIMARY KEY,
    age_band        VARCHAR,      -- 0-17, 18-44, 45-64, 65+
    sex             VARCHAR(1),
    insurance_type  VARCHAR       -- Medicare, Medicaid, Commercial, Uninsured
);

-- How patients are moved
CREATE TABLE dim_transport_mode (
    transport_mode_id  INTEGER PRIMARY KEY,
    mode_name          VARCHAR,   -- Ground BLS, Ground ALS, Critical Care, Rotor Air, Fixed-Wing Air
    avg_speed_mph      INTEGER
);

-- One row per transfer request (the core fact table)
CREATE TABLE fact_transfer (
    transfer_id                VARCHAR PRIMARY KEY,
    patient_id                 VARCHAR REFERENCES dim_patient(patient_id),
    origin_hospital_id         VARCHAR REFERENCES dim_hospital(hospital_id),
    destination_hospital_id    VARCHAR REFERENCES dim_hospital(hospital_id),
    transport_mode_id          INTEGER REFERENCES dim_transport_mode(transport_mode_id),
    distance_miles             DOUBLE,      -- straight-line distance between the hospitals
    transfer_reason            VARCHAR,     -- Trauma, Cardiac, Stroke, Neonatal, Burn, Psychiatric, Specialty
    acuity_level               INTEGER,     -- 1 (most critical) to 5
    status                     VARCHAR,     -- Completed, Cancelled, Diverted
    requested_at               TIMESTAMP,
    dispatched_at              TIMESTAMP,
    picked_up_at               TIMESTAMP,
    arrived_at                 TIMESTAMP
);
