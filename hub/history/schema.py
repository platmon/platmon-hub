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
