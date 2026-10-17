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

MIGRATION_4 = """
CREATE TABLE alert_states (
    node_id TEXT NOT NULL, rule_id TEXT NOT NULL, received_at_ms INTEGER NOT NULL,
    config_json TEXT NOT NULL, state_json TEXT NOT NULL,
    PRIMARY KEY (node_id, rule_id)
);
PRAGMA user_version=4;
"""
VERSION = 4

MIGRATION_5 = """
ALTER TABLE core_samples ADD COLUMN npu_percent REAL CHECK (npu_percent BETWEEN 0 AND 100);
PRAGMA user_version=5;
"""
VERSION = 5

MIGRATION_6 = """
CREATE TABLE raw_publications (
    node_id TEXT NOT NULL, instance_id TEXT NOT NULL, stream TEXT NOT NULL,
    publication_id INTEGER NOT NULL CHECK (publication_id > 0),
    received_at_ms INTEGER NOT NULL, reported_started_at REAL NOT NULL,
    reported_completed_at REAL NOT NULL, gap_before INTEGER NOT NULL,
    data_json TEXT NOT NULL, envelope_id INTEGER NOT NULL, encoded_bytes INTEGER NOT NULL,
    PRIMARY KEY (node_id, instance_id, stream, publication_id)
);
CREATE TABLE raw_envelopes (
    envelope_id INTEGER PRIMARY KEY, digest TEXT NOT NULL UNIQUE,
    envelope_json TEXT NOT NULL, encoded_bytes INTEGER NOT NULL,
    refs INTEGER NOT NULL DEFAULT 0 CHECK (refs >= 0)
);
CREATE INDEX raw_publications_time ON raw_publications (received_at_ms);
CREATE INDEX raw_publications_node_time ON raw_publications (node_id, received_at_ms);
CREATE TABLE replay_cursors (
    node_id TEXT NOT NULL, instance_id TEXT NOT NULL, stream TEXT NOT NULL,
    after_id INTEGER NOT NULL CHECK (after_id >= 0), active INTEGER NOT NULL,
    received_at_ms INTEGER NOT NULL,
    PRIMARY KEY (node_id, instance_id, stream)
);
CREATE UNIQUE INDEX replay_active ON replay_cursors (node_id, stream) WHERE active=1;
CREATE TABLE replay_losses (
    node_id TEXT NOT NULL, instance_id TEXT NOT NULL, stream TEXT NOT NULL,
    from_id INTEGER NOT NULL, to_id INTEGER, reason TEXT NOT NULL,
    received_at_ms INTEGER NOT NULL,
    PRIMARY KEY (node_id, instance_id, stream, from_id, reason)
);
CREATE INDEX replay_losses_time ON replay_losses (received_at_ms);
CREATE TABLE raw_usage (
    singleton INTEGER PRIMARY KEY CHECK (singleton=1), encoded_bytes INTEGER NOT NULL
);
INSERT INTO raw_usage VALUES (1,0);
CREATE TRIGGER raw_usage_insert AFTER INSERT ON raw_publications BEGIN
    UPDATE raw_usage SET encoded_bytes=encoded_bytes+new.encoded_bytes WHERE singleton=1;
    UPDATE raw_envelopes SET refs=refs+1 WHERE envelope_id=new.envelope_id;
END;
CREATE TRIGGER raw_usage_delete AFTER DELETE ON raw_publications BEGIN
    UPDATE raw_usage SET encoded_bytes=encoded_bytes-old.encoded_bytes WHERE singleton=1;
    UPDATE raw_envelopes SET refs=refs-1 WHERE envelope_id=old.envelope_id;
    DELETE FROM raw_envelopes WHERE envelope_id=old.envelope_id AND refs=0;
END;
-- A page envelope is stored and charged once, however many publications share it.
CREATE TRIGGER raw_envelope_insert AFTER INSERT ON raw_envelopes BEGIN
    UPDATE raw_usage SET encoded_bytes=encoded_bytes+new.encoded_bytes WHERE singleton=1;
END;
CREATE TRIGGER raw_envelope_delete AFTER DELETE ON raw_envelopes BEGIN
    UPDATE raw_usage SET encoded_bytes=encoded_bytes-old.encoded_bytes WHERE singleton=1;
END;
PRAGMA user_version=6;
"""
VERSION = 6

# Pressure is a 0..100 share of time; rates are bytes per second, never negative.
MIGRATION_7 = """
ALTER TABLE core_samples ADD COLUMN psi_cpu_some_avg10 REAL CHECK (psi_cpu_some_avg10 BETWEEN 0 AND 100);
ALTER TABLE core_samples ADD COLUMN psi_memory_some_avg10 REAL CHECK (psi_memory_some_avg10 BETWEEN 0 AND 100);
ALTER TABLE core_samples ADD COLUMN psi_io_some_avg10 REAL CHECK (psi_io_some_avg10 BETWEEN 0 AND 100);
ALTER TABLE core_samples ADD COLUMN network_rx_busiest_bytes_per_s REAL CHECK (network_rx_busiest_bytes_per_s >= 0);
ALTER TABLE core_samples ADD COLUMN network_tx_busiest_bytes_per_s REAL CHECK (network_tx_busiest_bytes_per_s >= 0);
ALTER TABLE core_samples ADD COLUMN disk_read_bytes_per_s REAL CHECK (disk_read_bytes_per_s >= 0);
ALTER TABLE core_samples ADD COLUMN disk_write_bytes_per_s REAL CHECK (disk_write_bytes_per_s >= 0);
PRAGMA user_version=7;
"""
VERSION = 7
