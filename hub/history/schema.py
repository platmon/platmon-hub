"""Hub history schema, independent of HTTP schema versions."""

VERSION = 1
SQL = """
CREATE TABLE core_samples (
    node_id TEXT NOT NULL CHECK (length(node_id) = 36),
    instance_id TEXT NOT NULL
        CHECK (length(CAST(instance_id AS BLOB)) BETWEEN 1 AND 128),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    received_at_ms INTEGER NOT NULL CHECK (received_at_ms >= 0),
    data_age_ms REAL NOT NULL CHECK (data_age_ms >= 0),
    stale_after_ms REAL NOT NULL CHECK (stale_after_ms > 0),
    core_state TEXT NOT NULL CHECK (core_state IN ('ready', 'degraded')),
    cpu_percent REAL CHECK (cpu_percent BETWEEN 0 AND 100),
    gpu_percent REAL CHECK (gpu_percent BETWEEN 0 AND 100),
    memory_percent REAL CHECK (memory_percent BETWEEN 0 AND 100),
    disk_percent REAL CHECK (disk_percent BETWEEN 0 AND 100),
    hottest_c REAL,
    PRIMARY KEY (node_id, instance_id, sequence),
    CHECK (data_age_ms <= stale_after_ms)
);
CREATE INDEX core_samples_node_time
    ON core_samples (node_id, received_at_ms, instance_id, sequence);
CREATE INDEX core_samples_retention ON core_samples (received_at_ms);
PRAGMA user_version = 1;
"""

MIGRATION_2 = """
CREATE TABLE observations (
    node_id TEXT NOT NULL, instance_id TEXT NOT NULL, group_name TEXT NOT NULL,
    observation_id INTEGER NOT NULL, received_at_ms INTEGER NOT NULL,
    data_age_ms REAL NOT NULL, stale_after_ms REAL NOT NULL,
    state TEXT NOT NULL, data_json TEXT NOT NULL,
    PRIMARY KEY (node_id, instance_id, group_name, observation_id)
);
CREATE INDEX observations_time ON observations (node_id, received_at_ms);
CREATE INDEX observations_retention ON observations (received_at_ms);
CREATE TABLE gaps (
    id INTEGER PRIMARY KEY, node_id TEXT NOT NULL, scope TEXT NOT NULL,
    instance_id TEXT, start_ms INTEGER NOT NULL, end_ms INTEGER,
    reason TEXT NOT NULL, approximate INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX gaps_time ON gaps (node_id, scope, start_ms);
CREATE TABLE continuity (
    node_id TEXT NOT NULL, scope TEXT NOT NULL, instance_id TEXT,
    received_at_ms INTEGER NOT NULL, reason TEXT, gap_id INTEGER,
    PRIMARY KEY (node_id, scope)
);
CREATE TABLE hub_sessions (
    session_id TEXT PRIMARY KEY, started_at_ms INTEGER NOT NULL,
    last_seen_ms INTEGER NOT NULL, closed_at_ms INTEGER
);
PRAGMA user_version=2;
"""
VERSION = 2

MIGRATION_3 = """
CREATE TABLE events (
    node_id TEXT, event_type TEXT NOT NULL, received_at_ms INTEGER NOT NULL,
    previous_state TEXT, current_state TEXT, reason_code TEXT NOT NULL,
    event_id TEXT PRIMARY KEY
);
CREATE INDEX events_time ON events (received_at_ms, event_id);
CREATE INDEX events_node_time ON events (node_id, received_at_ms);
ALTER TABLE continuity ADD COLUMN headline TEXT;
PRAGMA user_version=3;
"""
VERSION = 3
