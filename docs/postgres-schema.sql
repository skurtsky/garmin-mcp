-- Garmin MCP PostgreSQL schema.
-- Safe to run multiple times against the garmin database.

-- Daily health metrics, one row per date.
CREATE TABLE IF NOT EXISTS daily_metrics (
    metric_date          DATE PRIMARY KEY,
    resting_hr           SMALLINT,
    hrv                  SMALLINT,
    sleep_score          SMALLINT,
    stress               SMALLINT,
    steps                INTEGER,
    training_load        REAL,
    body_battery_wake    SMALLINT,
    body_battery_drain   SMALLINT,
    sleep_data           JSONB,
    health_data          JSONB,
    readiness_data       JSONB,
    training_data        JSONB,
    training_status_data JSONB,
    synced_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Activities, one row per Garmin activity.
CREATE TABLE IF NOT EXISTS activities (
    garmin_id       BIGINT PRIMARY KEY,
    activity_date   TIMESTAMPTZ NOT NULL,
    activity_type   TEXT NOT NULL,
    name            TEXT,
    distance_km     REAL,
    duration_min    REAL,
    avg_hr          SMALLINT,
    training_load   REAL,
    summary         JSONB,
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_activities_date
    ON activities (activity_date DESC);

-- Activity details are synced separately because each row costs extra Garmin calls.
CREATE TABLE IF NOT EXISTS activity_details (
    garmin_id   BIGINT PRIMARY KEY,
    detail      JSONB,
    route       JSONB,
    synced_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Personal records by sport and record type.
CREATE TABLE IF NOT EXISTS personal_records (
    sport            TEXT NOT NULL,
    record_type      TEXT NOT NULL,
    value_raw        REAL,
    value_formatted  TEXT,
    record_date      DATE,
    activity_id      BIGINT,
    synced_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (sport, record_type)
);

-- Athlete profile, single current snapshot.
CREATE TABLE IF NOT EXISTS athlete_profile (
    id           INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    profile_data JSONB,
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Active goals snapshots.
CREATE TABLE IF NOT EXISTS active_goals (
    id         SERIAL PRIMARY KEY,
    goals_data JSONB,
    synced_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Garmin gear catalog. Every Garmin-synced gear item lands here: bikes,
-- shoes, and separately tracked components such as chains, tires, cassettes,
-- or other custom gear. Manual component placeholders can also be inserted
-- with source = 'manual' when a component is not tracked separately in Garmin.
CREATE TABLE IF NOT EXISTS gear (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    type            TEXT,
    status          TEXT,
    usage_meters    BIGINT,
    lifespan_meters BIGINT,
    created_date    DATE,
    retired_date    DATE,
    source          TEXT NOT NULL DEFAULT 'garmin',
    raw_data        JSONB,
    last_synced     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- User-managed relationship between a bike and a serviceable component.
-- gear_id points to the component gear row; parent_gear_id points to the bike
-- gear row when the component is linked/mounted to a bike.
CREATE TABLE IF NOT EXISTS bike_components (
    id                      TEXT PRIMARY KEY,
    gear_id                 TEXT NOT NULL REFERENCES gear(id) ON DELETE CASCADE,
    parent_gear_id          TEXT REFERENCES gear(id) ON DELETE SET NULL,
    component_type          TEXT NOT NULL,
    service_interval_meters BIGINT,
    notify                  BOOLEAN NOT NULL DEFAULT TRUE,
    install_date            DATE NOT NULL,
    install_usage_meters    BIGINT NOT NULL DEFAULT 0,
    notes                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bike_components_parent_gear
    ON bike_components (parent_gear_id, gear_id);

-- Service schedules for bike components only. Examples: Lube, Inspect,
-- Replace, Check wear, Adjust. Each row has its own interval and notify flag.
CREATE TABLE IF NOT EXISTS bike_component_services (
    id                      TEXT PRIMARY KEY,
    bike_component_id       TEXT NOT NULL REFERENCES bike_components(id) ON DELETE CASCADE,
    service_type            TEXT NOT NULL,
    service_interval_meters BIGINT,
    notify                  BOOLEAN NOT NULL DEFAULT TRUE,
    notes                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bike_component_services_type
    ON bike_component_services (bike_component_id, lower(service_type));

-- Event history for each bike-component service schedule.
CREATE TABLE IF NOT EXISTS bike_component_service_logs (
    id                         TEXT PRIMARY KEY,
    bike_component_service_id  TEXT NOT NULL REFERENCES bike_component_services(id) ON DELETE CASCADE,
    service_date               DATE NOT NULL,
    service_datetime           TIMESTAMPTZ,
    service_type               TEXT NOT NULL,
    usage_meters               BIGINT NOT NULL,
    notes                      TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_bike_component_service_logs_date
    ON bike_component_service_logs (service_date DESC, created_at DESC);

-- Sync state tracks what the scheduled job last refreshed.
CREATE TABLE IF NOT EXISTS sync_state (
    data_type         TEXT PRIMARY KEY,
    last_synced_date  DATE,
    last_sync_time    TIMESTAMPTZ,
    status            TEXT DEFAULT 'ok',
    error_message     TEXT
);

INSERT INTO sync_state (data_type) VALUES
    ('daily_metrics'),
    ('activities'),
    ('activity_details'),
    ('personal_records'),
    ('athlete_profile'),
    ('active_goals'),
    ('gear')
ON CONFLICT DO NOTHING;
