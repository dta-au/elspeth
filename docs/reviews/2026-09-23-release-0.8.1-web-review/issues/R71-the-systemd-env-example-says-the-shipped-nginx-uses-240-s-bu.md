# R71. The systemd env example says the shipped nginx uses 240 s, but it uses 360 s

| | |
|---|---|
| Severity | low |
| Status | confirmed |
| Area | Deploy configuration and docs |
| Review line | seams |
| Pre-existing (touches lines outside the window) | no |
| Pinned at | `74c0ce0db` (branch `review/web-0.8.1-20260923`), window `7c986dc97..74c0ce0db` |
| Source findings | seam-09-deploy-config#2 |

## Finding

- **Location:** `deploy/linux-systemd/elspeth-web.env.example:21-22` versus `deploy/compose/nginx.conf:33-34`.
- **Wrong:** fa26f34b8 raised nginx to 360 s without updating this comment. `test_linux_systemd_bundle.py:116-117` pins 180 and 240. Line 17 ("shared by the supported deployment profiles") is also stale now.
- **Fix:** Name the actual ingress, and say whether systemd deliberately keeps 180 and 240.
- **Sources:** seam-09-deploy-config#2.
- **Verifier notes:** The drift is in the safe direction.


## Source findings and verification

### seam-09-deploy-config#2: systemd env example says the shipped nginx uses 240s; it uses 360s

- **Reported at:** `deploy/linux-systemd/elspeth-web.env.example:21`; reviewer severity low; category stale-comment; diff-anchored True.
- **Summary:** fa26f34b8 raised the only shipped nginx example to 360s but did not update the systemd example's comment, which still states 240s.
- **Failure scenario:** An operator puts the shipped nginx.conf (360s) in front of a systemd host, reads that the template uses 240s, and gets a false fact about the ingress. The direction is safe (declared 240 < real 360), but the two artefacts disagree and test_linux_systemd_bundle.py:116-117 pins 180/240, so nothing catches the drift.
- **Evidence:** elspeth-web.env.example:21-22 vs nginx.conf:33-34. git show fa26f34b8 touches nginx.conf, web-postgres.yaml and docker.md but not the systemd file.
- **Suggested fix:** Reword the comment to name the actual ingress (or 360s for the shipped template) and state whether the systemd profile deliberately keeps 180/240.
- **Verifier (trace):** upheld, confidence high, severity low. I confirmed the finding at 74c0ce0db. Commit 6e377bdc4, inside the window, added the comment at deploy/linux-systemd/elspeth-web.env.example:21: "Match the actual minimum ingress timeout; the shipped nginx example uses 240s." Commit fa26f34b8, later in the same window, raised the only nginx config in the tree (deploy/compose/nginx.conf:33-34) to 360s and did not touch the systemd file. The comment is therefore now false. No guard or other file makes it true: there is no second nginx example, and docker.md:334 points at deploy/compose/nginx.conf. As the finding says, the direction is safe: the declared ceiling of 240 is below the real 360, so a lower ceiling only makes the app give up sooner, never later than the proxy. The defect is documentation drift, so low severity is right. A related drift the finding did not name: line 17 says "Production composer limits shared by the supported deployment profiles", but the compose/docker profile now uses 300/360 while systemd keeps 180/240, so that comment is stale too.
