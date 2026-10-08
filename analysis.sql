-- ============================================================================
-- Hospital Transfer Analytics: analysis queries
-- Each query starts with a "-- name:" line so run_analysis.py can run them
-- one by one and label the output.
-- ============================================================================


-- name: 1. Overall KPIs
-- The headline numbers a manager would ask for first.
SELECT
    COUNT(*)                                                     AS total_transfers,
    ROUND(100.0 * AVG(CASE WHEN status = 'Completed' THEN 1 ELSE 0 END), 1) AS pct_completed,
    ROUND(100.0 * AVG(CASE WHEN status = 'Cancelled' THEN 1 ELSE 0 END), 1) AS pct_cancelled,
    ROUND(100.0 * AVG(CASE WHEN status = 'Diverted'  THEN 1 ELSE 0 END), 1) AS pct_diverted,
    -- MEDIAN ignores nulls automatically, so cancelled transfers with no
    -- dispatch time don't distort it
    MEDIAN(date_diff('minute', requested_at, dispatched_at))     AS median_dispatch_min
FROM fact_transfer;


-- name: 2. Top 10 receiving hospitals (with cumulative share)
-- Recreates the "few hospitals handle most of the volume" finding.
-- A CTE (the WITH block) is a named temporary result you can build on.
WITH received AS (
    SELECT destination_hospital_id, COUNT(*) AS transfers
    FROM fact_transfer
    GROUP BY destination_hospital_id
)
SELECT
    h.hospital_name,
    h.city,
    r.transfers,
    ROUND(100.0 * r.transfers / SUM(r.transfers) OVER (), 1) AS pct_of_all,
    -- Window function: a running total down the sorted list
    ROUND(100.0 * SUM(r.transfers) OVER (ORDER BY r.transfers DESC)
          / SUM(r.transfers) OVER (), 1)                      AS cumulative_pct
FROM received r
JOIN dim_hospital h ON h.hospital_id = r.destination_hospital_id
ORDER BY r.transfers DESC
LIMIT 10;


-- name: 3. Busiest transfer routes
-- Joins dim_hospital twice (once as origin, once as destination), so each
-- copy needs its own alias. Groups by ID, not name, because several
-- California hospitals share a name (e.g. multiple Kaiser locations).
SELECT
    o.hospital_name || ' (' || o.city || ')' AS from_hospital,
    d.hospital_name || ' (' || d.city || ')' AS to_hospital,
    ROUND(MAX(f.distance_miles), 0)          AS miles,
    COUNT(*)                                 AS transfers
FROM fact_transfer f
JOIN dim_hospital o ON o.hospital_id = f.origin_hospital_id
JOIN dim_hospital d ON d.hospital_id = f.destination_hospital_id
GROUP BY o.hospital_id, d.hospital_id, from_hospital, to_hospital
ORDER BY transfers DESC
LIMIT 10;


-- name: 4. Dispatch time by acuity
-- Are critical patients (acuity 1) dispatched faster? They should be.
-- The 90th percentile shows the slow tail that averages hide.
SELECT
    acuity_level,
    COUNT(*)                                                                    AS transfers,
    MEDIAN(date_diff('minute', requested_at, dispatched_at))                    AS median_min,
    QUANTILE_CONT(date_diff('minute', requested_at, dispatched_at), 0.9)        AS p90_min
FROM fact_transfer
WHERE dispatched_at IS NOT NULL
GROUP BY acuity_level
ORDER BY acuity_level;


-- name: 5. Day vs night dispatch
-- Night = 10pm to 6am. Tests whether overnight staffing slows response.
SELECT
    CASE WHEN EXTRACT(HOUR FROM requested_at) >= 22
           OR EXTRACT(HOUR FROM requested_at) < 6
         THEN 'Night' ELSE 'Day' END                        AS shift,
    COUNT(*)                                                AS transfers,
    MEDIAN(date_diff('minute', requested_at, dispatched_at)) AS median_dispatch_min
FROM fact_transfer
WHERE dispatched_at IS NOT NULL
GROUP BY shift
ORDER BY shift;


-- name: 6. Distance and transport time by mode (completed transfers only)
-- Pickup to arrival. Only completed transfers have both timestamps.
-- Air covers longer distances, so compare miles alongside minutes.
SELECT
    m.mode_name,
    COUNT(*)                                                  AS transfers,
    ROUND(MEDIAN(f.distance_miles), 0)                        AS median_miles,
    MEDIAN(date_diff('minute', f.picked_up_at, f.arrived_at)) AS median_transport_min,
    ROUND(AVG(f.acuity_level), 2)                             AS avg_acuity
FROM fact_transfer f
JOIN dim_transport_mode m ON m.transport_mode_id = f.transport_mode_id
WHERE f.status = 'Completed'
GROUP BY m.mode_name
ORDER BY median_transport_min;


-- name: 7. Where each case type goes
-- Checks that specialty cases reach specialty hospitals.
SELECT
    f.transfer_reason,
    COUNT(*)                                                               AS transfers,
    ROUND(100.0 * AVG(CASE WHEN d.hospital_type = 'Childrens'   THEN 1 ELSE 0 END), 1) AS pct_to_childrens,
    ROUND(100.0 * AVG(CASE WHEN d.hospital_type = 'Psychiatric' THEN 1 ELSE 0 END), 1) AS pct_to_psychiatric
FROM fact_transfer f
JOIN dim_hospital d ON d.hospital_id = f.destination_hospital_id
GROUP BY f.transfer_reason
ORDER BY transfers DESC;


-- name: 8. Monthly volume
SELECT
    date_trunc('month', requested_at) AS month,
    COUNT(*)                          AS transfers
FROM fact_transfer
GROUP BY month
ORDER BY month;


-- name: 9. Data quality checks (every count should be 0)
-- Real pipelines test their own data. Later these become dbt tests.
SELECT 'Completed but missing arrival time' AS check_name, COUNT(*) AS failures
FROM fact_transfer WHERE status = 'Completed' AND arrived_at IS NULL
UNION ALL
SELECT 'Arrived before pickup', COUNT(*)
FROM fact_transfer WHERE arrived_at < picked_up_at
UNION ALL
SELECT 'Origin same as destination', COUNT(*)
FROM fact_transfer WHERE origin_hospital_id = destination_hospital_id
UNION ALL
SELECT 'Transfer to unknown hospital', COUNT(*)
FROM fact_transfer f LEFT JOIN dim_hospital h ON h.hospital_id = f.destination_hospital_id
WHERE h.hospital_id IS NULL;
