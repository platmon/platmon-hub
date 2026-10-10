# Troubleshooting

Start with `/healthz`, `/api/history/status`, `/api/registry/status` and local logs.
A responsive process can still have unavailable devices or storage. Keep registry,
logs, DB backups and screenshots private; redact endpoints and credentials before
sharing evidence.

| Symptom | Check / action |
| --- | --- |
| Bind fails | Check port ownership and permissions; choose an explicit free port. Hub never silently changes ports. |
| 403 from Hub | Use localhost/loopback Host; configure the proxy's upstream Host as documented. |
| Proxy 401 | Verify the authorized proxy account and TLS configuration; Hub has no built-in login. |
| 429 / overload 503 | Back off client requests; inspect connection load and resource budget. |
| Device Offline | Two consecutive core transport failures are required; check the approved endpoint without changing the Agent. |
| Last known / N/A | Inspect `data_current` and age. Unsupported or stale values are not current zero readings. Optional failures do not make core Offline. |
| History disabled | Supply an explicit `--history-db` with a writable parent directory. |
| Writer errors / drops | Check free space, ownership, competing writer and SQLite lock contention. Live polling can continue while history is incomplete. |
| History 422 | Narrow the time range; discontinuities exceed the point budget. |
| History 503 | Check writer initialization/schema and query load. Retry with a shorter interval after resolving the cause. |
| Registry changes ignored | Validate the file with the CLI, inspect reload diagnostics and `--no-registry-reload`. Invalid updates leave the last good configuration active. |
| CLI lock conflict | Wait for the other administrator command; do not delete a lock to bypass ownership. |
| Alert does not activate | Check rule enablement, freshness, supported metric and continuous pending duration. Unknown data does not prove recovery. |
| Retention suspended | Correct the wall clock and restart Hub; inspect diagnostics before expecting cleanup. |
| Future schema rejected | Use a compatible application or restore a verified compatible backup to a clean path. Never edit schema version manually. |
| Service works only interactively | Check the service account's executable/config/state permissions and machine-accessible Python path. Test actual reboot startup. |

Stop all Hub users of a database before offline recovery. Preserve the failed DB
and WAL/SHM together. Do not erase files to make an error disappear. Use the
[backup and rollback procedure](operations.md#backup-upgrade-and-rollback).
