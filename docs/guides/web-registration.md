# Local web device management

[Documentation index](../README.md) · [User guide](README.md)

The Dashboard has separate **Monitoring** and **Devices** tabs. Devices lists the
registry and supports add, edit, enable/disable and remove. The forms use name,
IP/hostname, port (default 9797), and enabled state. Existing CLI management
remains available. Registration generates a UUID, validates the complete registry,
takes the same nonblocking process lock, saves the previous bytes as `.bak`, and
atomically replaces the file. History is never deleted. The existing watcher
applies the saved file without restarting Hub or Agent.

## Enable on the Hub host

Web management is disabled by default. Enable it explicitly on a trusted Hub host:

```sh
python -m hub --nodes nodes.json --web-admin
```

No administrator token or login is needed. Any local process/user able to reach
this listener can manage the registry when enabled. Use this mode only on a host
whose local users you trust; it does not provide individual user authorization.
The Hub must be able to write the registry directory and backup as well as read it;
read-only service configurations require an explicit ACL/permission update.

This option requires `--bind 127.0.0.1` and the normal registry watcher. Public
bindings and `--no-registry-reload` fail closed. Open the Hub directly at
`http://127.0.0.1:9787/` or `http://localhost:9787/` (use the configured Hub port).
The existing authenticated reverse proxy remains for viewers; web administration
through that proxy is deliberately unavailable. The proxy must reject `/api/admin/` routes and strip `X-Platmon-Admin` from upstream requests, as in the supplied example. Do not relax these controls.

## Manage devices

1. Open the **Devices** tab. Registered names, endpoints, UUIDs and enabled states
   appear immediately. These are registry settings; Monitoring shows live Agent status.
2. **Add device** creates a UUID. Enter name, explicit Agent IP/hostname and
   Agent port (default 9797, not Hub port), then **Save device**. Bare or bracketed
   IPv6 works. Do not enter a URL or credentials into the host field.
3. **Edit** changes name, endpoint and enabled state in one atomic operation and
   keeps the UUID and History. **Disable / Enable** applies immediately without a
   form; the status line confirms the result. Disable retains registration and
   History while stopping future polling.
4. **Remove** opens a separate confirmation showing name, endpoint and UUID.
   Cancel keeps the device. **Remove device** deletes only that registration;
   stored History and events remain. Re-adding creates a new UUID; use Disable
   for temporary removal from polling instead.

Saving updates the registry file immediately; the Hub watcher applies it after
its normal debounce. Monitoring may briefly show the previous state. Removal
stops new polling once applied; an in-flight request finishes under existing
limits and its obsolete response is discarded. No Agent is stopped or controlled.

Edits/removal include the loaded record. The writer compares it to the current
record inside its process lock before making any change. If a CLI/another tab
changed the same device, the API returns 409 and asks for a fresh list. Other
nodes' changes do not invalidate that record. Reload Devices and reopen the form
before retrying; stale input never silently overwrites newer settings.

After an ambiguous timeout or leaving the tab while saving, refresh the registry
before retrying: an already committed write cannot be undone by aborting the
browser request. A confirmed save with a failed list refresh remains confirmed
and cannot be resubmitted from the same dialog. Leaving Devices or hiding the
page aborts pending browser requests and closes forms. No credentials or sessions
are created or stored.

## Preview before merge

An older checkout will not recognize `--web-admin`. Until the Draft PR is
merged, fetch `feat/device-registration` and use an isolated worktree so existing
code and private files stay intact:

```sh
git fetch origin feat/device-registration
git worktree add --detach ../platmon-hub-web-preview FETCH_HEAD
```

Run from that preview folder with an absolute path to the existing registry. Do not merge the PR just to preview it. Updating preview
code requires stopping its Hub first, then fetching and fast-forwarding/checking
out the reviewed commit there before restarting; do not replace static assets of
an old running backend. `--history-db` is independently needed to enable History;
without it the History query API returns 503 `history_disabled` intentionally.

## Authorization and API boundary

`GET /api/admin/status` reports schema 1, `enabled`, and `local_port`. It contains
no token. Local `GET /api/admin/nodes` lists bounded registry records.
`POST /api/admin/nodes` adds a device. Its JSON object
contains `name`, `host`, optional integer `port`, and optional boolean `enabled`.
No path, raw URL, history payload or arbitrary settings are accepted for additions.
Success is 201 with schema 1, `saved: true` and the generated `node_id`.

Edits use `POST /api/admin/nodes/{node_id}/update` with name, host, port, enabled
and `expected` (the complete record from the local list). Removal uses
`POST /api/admin/nodes/{node_id}/remove` with `expected` and `confirm_node_id`
matching the route's UUID. Edit/removal return 200 with schema 1, `saved: true`,
`node_id`, and `action`. Missing devices return 404; record conflicts return 409.
No generic PATCH/DELETE or browser-selected file path is accepted.

Each management request requires the non-simple `X-Platmon-Admin: 1` header,
exact local Host plus port, and same-origin Fetch Metadata when present. Writes
also require matching HTTP Origin and JSON content type. Missing Origin on writes
fails closed; GET allows an absent Origin but rejects a supplied mismatched one.
The header is a CSRF request marker, not a secret or authentication credential.
Cross-origin preflight and other OPTIONS requests remain 405; CORS is not enabled.
Forwarded identity headers are ignored. No cookies or server sessions are used.
All existing CSP/no-store/nosniff/frame-denial headers remain unchanged.

Bodies are capped at 4 KiB; ambiguous framing and duplicate JSON keys are rejected.
The shared HTTP request budget and 32-connection cap apply. Writes also
have a global 0.2/s budget, burst 5; list reads use the shared budget. 429 advises
waiting. Missing/invalid request markers or origin violations return 403, invalid
input 400, duplicates 409 and registry I/O/lock failures 503. Disabled mode and all
other write routes/methods retain 405. Registration does not immediately fetch an
address; only the established fixed-path scheduler polls enabled registered Agents.

Removing `--web-admin` disables management. This local-only feature has no
individual user identity, login, MFA or per-user audit attribution. Status-only
HTTP logs and Registry change events remain available. Use OS access controls
for shared hosts; remote users continue to use the separately authenticated
read-only viewer proxy.
