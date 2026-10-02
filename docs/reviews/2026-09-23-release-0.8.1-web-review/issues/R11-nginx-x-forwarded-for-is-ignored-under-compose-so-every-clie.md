# R11. nginx `X-Forwarded-For` is ignored under Compose, so every client shares the bridge IP

| | |
|---|---|
| Severity | medium |
| Status | confirmed |
| Area | — |
| Review line | seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-09-deploy-config#1 |

## Finding

- **Status and severity:** confirmed, **medium**.
- **Location:**
  - `deploy/compose/nginx.conf:26,30`: a new file in the window (6e377bdc4, fa26f34b8).
  - `src/elspeth/cli.py:4791-4799`: `uvicorn.run` is called without proxy settings.
  - `src/elspeth/web/rate_limit.py:191-203`.
- **What is wrong:** nginx proxies to the Docker-published `127.0.0.1:8451`. Inside the container the peer is the network gateway, not 127.0.0.1. uvicorn 0.46.0 defaults `forwarded_allow_ips` to `FORWARDED_ALLOW_IPS` or `127.0.0.1`, and neither the Dockerfile, `web-postgres.yaml`, `deploy/` nor the docs set it. So the header is ignored and `request.client.host` is the same for every user.
- **Failure scenario:** One user mistypes a password 20 times in a minute, or a few tabs hold expired tokens (`auth/middleware.py:61` calls the limiter on failure). The shared auth bucket (default 20/min, `config.py:452`) fills, and `/api/auth/login` returns 429 for everyone. An attacker can sustain the lockout at 20 requests/min. Auth audit `client_host` (`auth/audit.py:593-597`) and the `ip_address` fields at `messages.py:1239` and `inspect.py:60` all record the gateway IP.
- **Evidence:** `docker.md:331-363` documents the nginx template but not the IP collapse. The SSO spec (`:956-958`) defers the same limitation only for ECS. The `rate_limit.py:197-199` docstring assumes `--proxy-headers`.
- **Suggested fix:** Set `FORWARDED_ALLOW_IPS` to the bridge gateway or subnet in `web-postgres.yaml`, or expose a forwarded-allow-ips setting through `elspeth web`. Failing that, document the collapse. nginx overwrites `X-Forwarded-For` with `$remote_addr`, so trusting it does not open a spoofing hole.
- **Sources:** seam-09-deploy-config#1.
- **Verifier notes:** None beyond the finding.


## Source findings and verification

### seam-09-deploy-config#1: nginx X-Forwarded-For is dropped under Compose, so every client gets the bridge IP

- **Reported at:** `deploy/compose/nginx.conf:30`; reviewer severity medium; category trust-boundary / half-wired; diff-anchored True.
- **Summary:** nginx sets X-Forwarded-For and proxies to the Docker-published 127.0.0.1:8451. uvicorn (launched by cli.py:4791 without forwarded_allow_ips) trusts the header only from 127.0.0.1, but inside the container the peer is the Docker bridge gateway. The header is ignored and request.client.host is the same IP for every user.
- **Failure scenario:** Compose plus the shipped nginx.conf: one user mistypes a password 20 times in a minute, or a few tabs hold expired tokens (get_current_user's failure path calls check_auth_rate_limit). The shared per-IP auth bucket (auth_rate_limit_per_minute=20) fills and /api/auth/login returns 429 for all users. An attacker can sustain this lockout with 20 req/min. Auth-failure audit rows and the ip_address fields in messages.py:1239 and inspect.py:60 all record the bridge IP.
- **Evidence:** nginx.conf:26,30. cli.py:4791 uvicorn.run passes no proxy settings. uvicorn/config.py:342 defaults forwarded_allow_ips to FORWARDED_ALLOW_IPS or '127.0.0.1'. No FORWARDED_ALLOW_IPS anywhere in deploy/ or the docs. rate_limit.py:202 keys on request.client.host. auth/middleware.py:61. auth/audit.py:594-598. The SSO spec (2026-09-02-pluggable-sso-design.md:956-958) records the same limitation as deferred work only for ECS.
- **Suggested fix:** Set FORWARDED_ALLOW_IPS to the bridge gateway/subnet in web-postgres.yaml (or expose a forwarded-allow-ips setting via `elspeth web`), or document that Compose client IPs collapse.
- **Verifier (trace):** upheld, confidence high, severity medium. I traced the path at 74c0ce0db and it holds. Nothing earlier in the path guards against it or normalises the address. The window added deploy/compose/nginx.conf (a new file, commit 6e377bdc4/fa26f34b8). It sets X-Forwarded-For and proxies to the Docker-published port. Nothing in the launch path makes uvicorn trust that header from a Docker peer.

- `elspeth web` (cli.py:4792-4799) calls uvicorn.run with no proxy_headers or forwarded_allow_ips arguments.
- In the pinned uvicorn 0.46.0 (uv.lock:4534-4535), config.py:341-342 defaults the trusted list to FORWARDED_ALLOW_IPS or "127.0.0.1".
- ProxyHeadersMiddleware only rewrites scope["client"] when the connecting peer is in the trusted list.
- FORWARDED_ALLOW_IPS is not set anywhere: not in the Dockerfile ENV lines (176-179), not in web-postgres.yaml, not in deploy/ or docs.
- Host nginx connects to 127.0.0.1:8451, the published port (web-postgres.yaml:66-67). Docker then delivers the connection to the container from the network gateway address. That is true for docker-proxy and for DNAT with hairpin MASQUERADE. So the peer uvicorn sees is never 127.0.0.1, the header is ignored, and request.client.host is the gateway IP for every user.

The consequence is also confirmed:
- check_auth_rate_limit (rate_limit.py:191-203) keys on request.client.host. It runs on the auth routes (auth/routes.py:306 and 359) and in the get_current_user failure path (auth/middleware.py:61).
- The limit defaults to 20 per minute (config.py:452).
- So every client behind the shipped nginx shares one auth bucket. One user or attacker making about 20 failed auth requests a minute gets 429s for everyone.
- Auth audit client_host (auth/audit.py:593-597) and the ip_address fields at messages.py:1239 and inspect.py:60 all record the gateway IP.

Neither ruling nor docs cover this:
- recent-code-hints.md has no ruling on proxy headers or client IP.
- docker.md:331-363 documents the nginx template but says nothing about client IPs collapsing.
- The SSO spec (lines 956-958) records the limitation as deferred work for ECS only.

The rate_limit.py:197-199 docstring assumes "--proxy-headers enabled". That is exactly the setting the Compose launch leaves effectively off for non-loopback peers.

Nginx sets X-Forwarded-For to $remote_addr, overwriting the header rather than appending to it. So the fix would not open a spoofing hole. Medium severity fits: an unauthenticated attacker can deny logins to all users at a low request rate, and the audit trail loses client attribution.
