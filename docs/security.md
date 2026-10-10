# Access boundaries

The Hub has no public authentication endpoint and no HTTP administration role.
Keep its default `127.0.0.1:9787` listener. Agent endpoints remain private, explicit
GET-only origins. Do not publish either Hub or Agent ports directly to the internet.
A local process with access to loopback can read fleet data; the host is a trust
boundary. Loopback binding is not user authentication on a shared host.

| Identity | Permitted operations | Enforcement |
| --- | --- | --- |
| Authenticated proxy viewer | Dashboard and read-only APIs | TLS proxy authentication |
| OS administrator | Local registry CLI, rules, service configuration | OS login, filesystem ACLs, approved sudo/task permissions |
| Hub service account | Read registry/rules; write its DB/log directories | Dedicated account and directory permissions |
| Anonymous remote client | No fleet access | Authenticated proxy and firewall |

`POST`, `PUT`, `PATCH`, `DELETE` remain 405, including requests with forged admin
headers. The Hub ignores forwarded user identities. There is no URL-fetch proxy,
network discovery, web device editor or agent control path. A future web management
API requires a separate authorization/CSRF design and review.

## Optional TLS proxy

`deploy/nginx.conf.example` is an opt-in nginx http-context include. It is never
installed automatically. Compared with custom sessions, this keeps password checking
and TLS in a maintained external component and leaves Hub runtime dependencies
unchanged. It adds a separately patched/configured service and must be included in
backup, audit and certificate rotation procedures. Windows deployments may use an
organization-managed TLS/authentication gateway with the same constraints.

The example listens on loopback 8443 until an operator deliberately configures its
approved interface, DNS, certificate and firewall. Use a trusted certificate chain,
restricted private-key/password-file permissions and a strong supported password
hash. Never store credentials in source control, URLs or Hub config. Protect all
paths, including assets, health and history. Rewrite upstream Host to the loopback
origin; the Hub rejects arbitrary Host names on loopback to reduce DNS rebinding.
Strip upstream Authorization/Cookie; do not trust client-provided identity headers.
Use `nginx -t` before reload. Proxy and Hub ports must not provide an authentication
bypass through another interface. Review both IPv4 and IPv6 firewall rules.

Basic authentication checks each request; nginx does not create a Hub session.
Browsers may cache credentials, so there is no reliable application logout or idle
session timeout in this example. Removing a viewer's password entry revokes future
requests. If timed sessions, MFA or centrally managed logout are required, use the
organization's identity-aware gateway before deployment. Do not claim those features
for this Basic-auth example. No Hub cookies or access tokens are persisted.

Official configuration references: [nginx Basic authentication](https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html),
[HTTPS configuration](https://nginx.org/en/docs/http/configuring_https_servers.html),
and [request limiting](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html).

## Input, resource and audit controls

Hub HTTP admission is capped at 32 concurrent connections, with a 5-second socket
inactivity timeout. GET requests share a 100 requests/second token budget with a
200-request burst; exhausted budget returns 429. Exhausted connection slots return
503 for a complete bounded request; clients that do not finish headers within
100ms may instead be disconnected. The proxy example applies an additional per-IP 10 requests/second, burst 20.
These are initial protection budgets, not measured supported viewer capacity. Requests
have a 4 KiB target and 16 KiB parsed-header limit; Python's parser has its own limits
before that check. An inactivity timeout is not a total request deadline. The Hub is
not hardened as an internet-facing HTTP server; the proxy supplies the outer boundary.

SQL inputs are parameterized, metric names allowlisted, responses bounded and missing
metrics preserved as null. HTTP APIs remain same-origin with no permissive CORS.
All application responses carry no-store/nosniff, restrictive CSP and frame denial;
static files retain their exact allowlist. Read-only operations have no CSRF-sensitive
mutation. The proxy must not add permissive CORS or relax these headers.

Hub access logs record method/status only, excluding URLs, query strings, headers,
credentials and arbitrary strings. Use `--log-file` for rotation. The proxy's
status-only audit records timestamps, client address, method and status, including
401 authentication failures; error logging is suppressed in the example because nginx
error messages can include raw request URIs. Configure protected log rotation and
retention externally. Registry events record changes/reasons, not a verified human
identity. Use OS audit facilities for attribution of local CLI/configuration changes;
no claim of tamper-proof auditing is made. DB/logs/backups contain device information
and must have restricted permissions. SQLite is not encrypted by this application.

## Validation and limits

`python scripts/check-proxy.py` is an opt-in nginx/openssl test using
a temporary unprivileged process, credentials/certificate, and loopback ports.
Use `--nginx PATH` for an extracted nginx binary without system installation.
It verifies certificate trust, anonymous/bad-password rejection, authenticated reads,
write rejection and status-only audit output, then stops its test process. This is
not deployment and does not establish production DNS, firewall, Windows gateway,
account revocation or organization session-policy compliance. Unit tests independently
exercise Host checks, request limits, redaction, headers and forged-admin rejection.
