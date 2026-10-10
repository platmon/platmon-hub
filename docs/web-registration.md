# Local web device registration

The Dashboard's **Add device** dialog registers an explicit Agent address using
name, IP/hostname, port (default 9797), and enabled state. Existing CLI management
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
contents into the password field when saving a device. Never send it through chat.
The Hub must be able to write the registry directory and backup as well as read it;
read-only service configurations require an explicit ACL/permission update.

This option requires `--bind 127.0.0.1` and the normal registry watcher. Public
bindings and `--no-registry-reload` fail closed. Open the Hub directly at
`http://127.0.0.1:9787/` or `http://localhost:9787/` (use the configured Hub port).
The existing authenticated reverse proxy remains for viewers; web administration
through that proxy is deliberately unavailable. Do not change proxy authorization
stripping, TLS or firewall settings to bypass this boundary.

## Add a device

1. Click **Add device** in the Dashboard header.
2. Enter a display name, explicit Agent IP/hostname and Agent port. This is not
   the Hub port. Bare or bracketed IPv6 works. Do not enter a URL or credentials
   into the host field.
3. Leave **Start monitoring after registration** checked, or uncheck it to create
   a disabled entry. Paste the administrator token and click **Save device**.
4. The dialog reports **Saved** first, then confirms that the node is visible in
   Fleet after reload. Agent reachability is shown separately by its normal status.

Closing the dialog before saving performs no write. Concurrent submits are blocked
in the UI; repeated endpoints are rejected by registry validation. On an ambiguous
network timeout the file may already have been saved, so refresh Fleet before
retrying. A save can succeed while reload is delayed/invalidated by another local
edit; `/api/registry/status` diagnoses that separately. No automatic rollback is
implied. The token field clears after each attempt, closing the dialog or hiding
the tab. The page never stores it in cookies, localStorage or sessionStorage.

## Authorization and API boundary

`GET /api/admin/status` reports schema 1, `enabled`, and `local_port`. It contains
no token. `POST /api/admin/nodes` is the sole opt-in write route. Its JSON object
contains `name`, `host`, optional integer `port`, and optional boolean `enabled`.
No path, raw URL, node ID, history payload or arbitrary settings are accepted.
Success is 201 with schema 1, `saved: true` and the generated `node_id`.

Each write requires a constant-time checked `Authorization: Bearer TOKEN`, exact
local Host plus port, matching HTTP Origin, same-origin Fetch Metadata when present,
and JSON content type. Missing Origin fails closed; forwarded identity headers
are ignored. Cookies are not an authentication mechanism and no server session is
created. CORS is not enabled; all existing CSP/no-store/nosniff/frame-denial headers
remain unchanged. These controls follow the [OWASP CSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
for origin checks and rejecting simple cross-origin requests.

Bodies are capped at 4 KiB; ambiguous framing and duplicate JSON keys are rejected.
The shared HTTP request budget and 32-connection cap apply. Admin attempts also
have a global 0.2/s budget, burst 5, including failed authentication; 429 advises
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
