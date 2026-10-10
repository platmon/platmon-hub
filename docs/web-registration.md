# Local web device management

The Dashboard has separate **Monitoring** and **Devices** tabs. Devices lists the
registry and supports add, edit, enable/disable and remove. The forms use name,
IP/hostname, port (default 9797), and enabled state. Existing CLI management
remains available. Registration generates a UUID, validates the complete registry,
takes the same nonblocking process lock, saves the previous bytes as `.bak`, and
atomically replaces the file. History is never deleted. The existing watcher
applies the saved file without restarting Hub or Agent.

## Enable on the Hub host

Web registration is disabled by default. Create a protected token file in an
existing private configuration directory, using the same OS account that runs Hub:

```sh
python -c "import os,secrets; fd=os.open('admin-token.txt', os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600); os.write(fd,(secrets.token_hex(32)+'\n').encode('ascii')); os.close(fd)"
python -m hub --nodes nodes.json --admin-token-file admin-token.txt
```

The creation command refuses to overwrite an existing file and does not print
the token. On POSIX, group/other permissions must be absent. On Windows, restrict
the token file and its parent directory with OS ACLs to the operator/Hub account;
POSIX mode bits are not a Windows access-control mechanism. Keep the token out of
source control, URLs, logs and browser storage. Read the file locally and paste its
contents into the Devices password field to unlock management. Never send it through chat.
The Hub must be able to write the registry directory and backup as well as read it;
read-only service configurations require an explicit ACL/permission update.

This option requires `--bind 127.0.0.1` and the normal registry watcher. Public
bindings and `--no-registry-reload` fail closed. Open the Hub directly at
`http://127.0.0.1:9787/` or `http://localhost:9787/` (use the configured Hub port).
The existing authenticated reverse proxy remains for viewers; web administration
through that proxy is deliberately unavailable. Do not change proxy authorization
stripping, TLS or firewall settings to bypass this boundary.

## Manage devices

1. Open the **Devices** tab and enter the administrator token, then click
   **Unlock management**. Registered names, endpoints, UUIDs and enabled states
   appear. These are registry settings; Monitoring shows live Agent status.
2. **Add device** creates a UUID. Enter name, explicit Agent IP/hostname and
   Agent port (default 9797, not Hub port), then **Save device**. Bare or bracketed
   IPv6 works. Do not enter a URL or credentials into the host field.
3. **Edit** changes name, endpoint and enabled state in one atomic operation and
   keeps the UUID and History. **Disable / Enable** opens the same form with the
   desired enabled state selected; save to apply it. Disable retains registration
   and History while stopping future polling.
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

The administrator token is held only in page memory for at most five minutes.
The password field clears when unlocking; **Lock management**, switching back to
Monitoring, hiding the browser tab/window or expiry clears access, closes forms
and removes the authenticated list. It is never put in cookies, localStorage or
sessionStorage; this is not a server session. Each request independently checks
its bearer token. After an ambiguous timeout/lock while saving, refresh the
registry before retrying: an already committed write cannot be undone by aborting
the browser request. A confirmed save with a failed list refresh remains confirmed
and cannot be resubmitted from the same dialog. Lock when finished on shared hosts.

## Preview before merge

An older checkout will not recognize `--admin-token-file`. Until the Draft PR is
merged, fetch `feat/device-registration` and use an isolated worktree so existing
code and private files stay intact:

```sh
git fetch origin feat/device-registration
git worktree add --detach ../platmon-hub-web-preview FETCH_HEAD
```

Run from that preview folder with an absolute path to the existing registry and
protected token file. Do not merge the PR just to preview it. Updating preview
code requires stopping its Hub first, then fetching and fast-forwarding/checking
out the reviewed commit there before restarting; do not replace static assets of
an old running backend. `--history-db` is independently needed to enable History;
without it the History query API returns 503 `history_disabled` intentionally.

## Authorization and API boundary

`GET /api/admin/status` reports schema 1, `enabled`, and `local_port`. It contains
no token. Authenticated `GET /api/admin/nodes` lists bounded registry records.
`POST /api/admin/nodes` adds a device. Its JSON object
contains `name`, `host`, optional integer `port`, and optional boolean `enabled`.
No path, raw URL, history payload or arbitrary settings are accepted for additions.
Success is 201 with schema 1, `saved: true` and the generated `node_id`.

Edits use `POST /api/admin/nodes/{node_id}/update` with name, host, port, enabled
and `expected` (the complete record from the authenticated list). Removal uses
`POST /api/admin/nodes/{node_id}/remove` with `expected` and `confirm_node_id`
matching the route's UUID. Edit/removal return 200 with schema 1, `saved: true`,
`node_id`, and `action`. Missing devices return 404; record conflicts return 409.
No generic PATCH/DELETE or browser-selected file path is accepted.

Each write requires a constant-time checked `Authorization: Bearer TOKEN`, exact
local Host plus port, matching HTTP Origin, same-origin Fetch Metadata when present,
and JSON content type. Missing Origin on writes fails closed; authenticated
GET allows an absent Origin but rejects a supplied mismatched one. Forwarded identity headers
are ignored. Cookies are not an authentication mechanism and no server session is
created. CORS is not enabled; all existing CSP/no-store/nosniff/frame-denial headers
remain unchanged. These controls follow the [OWASP CSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
for origin checks and rejecting simple cross-origin requests.

Bodies are capped at 4 KiB; ambiguous framing and duplicate JSON keys are rejected.
The shared HTTP request budget and 32-connection cap apply. Writes and failed authentication attempts also
have a global 0.2/s budget, burst 5; authorized list reads use the shared budget. 429 advises
waiting. Tokenless/bad-token requests return 401, origin violations 403, invalid
input 400, duplicates 409 and registry I/O/lock failures 503. Disabled mode and all
other write routes/methods retain 405. Registration does not immediately fetch an
address; only the established fixed-path scheduler polls enabled registered Agents.

Use one random 256-bit token per Hub host. Rotate by replacing the protected file
and restarting Hub; startup loads the token once. Removing the startup option
disables web writes. This limited shared secret identifies a local administrator,
not an individual user. Status-only HTTP logs and normal Registry change events
are available; per-user attribution, MFA, remote administration and tamper-proof
logging are not provided. A compromised host or browser extension remains inside
the local trust boundary. No custom password database/session framework or runtime
dependency was introduced; organizational viewer authentication stays in the TLS
proxy.
