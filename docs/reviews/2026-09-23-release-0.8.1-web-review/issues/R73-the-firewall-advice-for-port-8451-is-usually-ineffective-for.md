# R73. The firewall advice for port 8451 is usually ineffective for a Docker-published port

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Deploy configuration and docs |
| Review line | seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-09-deploy-config#5 |

## Finding

- **Location:** `docs/guides/docker.md:337-339` (new in the window) and `deploy/compose/web-postgres.yaml:67` (`"8451:8451"`).
- **Wrong:** Published-port traffic is DNAT'd and never reaches INPUT, so `ufw` and `firewalld` rules do not restrict it (the Docker documentation says so). The guide does not mention `DOCKER-USER`. **Scenario:** an operator who picks the firewall option leaves plaintext HTTP, carrying passwords and tokens, reachable from the network.
- **Fix:** Ship `127.0.0.1:8451:8451` now that the bundle includes a loopback proxy, or drop the firewall option.
- **Sources:** seam-09-deploy-config#5.
- **Verifier notes:** The guide's second option, the loopback bind, is correct and appears in the same sentence. `ELSPETH_WEB__HOST: 0.0.0.0` inside the container is required and is not part of the defect. The Docker bypass behaviour was not measured in this session.


## Source findings and verification

### seam-09-deploy-config#5: Firewall advice for port 8451 is usually ineffective for a Docker-published port

- **Reported at:** `docs/guides/docker.md:337`; reviewer severity low; category security (prose-only guard); diff-anchored True.
- **Summary:** The guide and nginx.conf:3 tell operators to restrict direct access to port 8451 with the host firewall, but Docker's own DNAT rules for published ports usually bypass ufw/firewalld INPUT rules. The shipped compose file still publishes '8451:8451' on 0.0.0.0 rather than 127.0.0.1.
- **Failure scenario:** An operator follows the first option (ufw deny 8451). The plaintext HTTP backend stays reachable from the network, bypassing TLS and nginx log redaction, so login passwords and bearer tokens can cross the network in cleartext.
- **Evidence:** docker.md:337-339 (new in window). nginx.conf:3. web-postgres.yaml:4 (HOST 0.0.0.0), :67 ('8451:8451'). The Docker/ufw bypass is documented Docker networking behaviour and was not measured in this read-only session.
- **Suggested fix:** Ship the compose file with '127.0.0.1:8451:8451' now that the bundle includes a proxy pointing at loopback, or drop the host-firewall option from the guide.
- **Verifier (trace):** upheld, confidence medium, severity low. I could not refute the core claim. The guide text added in this window (docs/guides/docker.md:337-339, from commits fa26f34b8 and 6e377bdc4) gives operators two ways to protect the plaintext backend: "Restrict direct access to port 8451 with the host firewall or change the Compose port binding to `127.0.0.1:8451:8451`." The shipped overlay still publishes the port on every interface: deploy/compose/web-postgres.yaml:67 has `- "8451:8451"`, and that line did not change in the window. Docker's own packet-filtering documentation says published-port traffic is DNAT'd in nat PREROUTING and passes through the FORWARD/DOCKER chains, so it never reaches the INPUT chain where a plain `ufw deny 8451` rule sits. firewalld zone rules are bypassed the same way. The only filter point that works is the DOCKER-USER chain, and the guide never mentions it. I searched docs/ and deploy/ for ufw, firewalld, DOCKER-USER and iptables and found none of them, so no caveat exists anywhere. No ruling in docs/agents/recent-code-hints.md covers this.

The finding does overstate its case in three places:
(1) nginx.conf:3 says only "restrict direct access to backend port 8451". It does not recommend a firewall.
(2) The guide's second option, binding to 127.0.0.1, is correct and sits in the same sentence, so the defect is incomplete prose, not a missing safeguard.
(3) `ELSPETH_WEB__HOST: 0.0.0.0` (web-postgres.yaml:4) and `--host 0.0.0.0` (:64) set the bind address inside the container. They are needed for any published port to work (docker.md:87-88 says so), so they are not part of the defect.

The failure path is real only for an operator who picks the firewall option and uses ufw INPUT rules. It is a documentation-only defect, and low severity is right.
