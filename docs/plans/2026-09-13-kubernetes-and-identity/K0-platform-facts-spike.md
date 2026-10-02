### Task K0: Spike — measure the platform facts the manifests depend on

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: nothing (a workstream start). Runs before: K1. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

**Files:**
- Create: `docs/plans/2026-09-13-kubernetes-platform-facts.md` (`git check-ignore -v` prints nothing for this path, measured 2026-09-13: it is a tracked location)
- Scratch, never committed (`.gitignore:67` ignores `.claude/lanes/`): `.claude/lanes/k8s/bin/` (the pinned `kubectl` and `kind`), `.claude/lanes/k8s/spike/` (spike manifests, kubeconfig, logs)
- Read only, unchanged: `deploy/azure-container-apps/workload.bicep:430-452` (the root `provision-storage` Job this spike reproduces on kind), `Dockerfile:125-126` and `:185` (UID/GID 1654, `USER elspeth`), `.github/workflows/ci.yaml:738-752` (the checksum-pinned Bicep install step whose three-line shape the kubectl and kind installs copy), `tests/unit/deployment/test_azure_container_apps_bundle.py:33-34` and `:509-527` (the pin constants and the facts-document cross-check the K3 pin test mirrors), `docs/plans/2026-09-05-phase6b-azure-container-apps-platform-facts.md:1-24` (the provenance-tag convention this document reuses)

**Interfaces:**
- Consumes: nothing from another task; K0 is the root of workstream K. No kubectl, kind or Azure subscription exists on the development box (`which kubectl kind` empty, `which docker` → `/usr/bin/docker`, Docker 29.7.2, buildx v0.36.1, measured 2026-09-13); Step 1 installs the two binaries into the lane directory.
- Produces, all in `docs/plans/2026-09-13-kubernetes-platform-facts.md` and copied verbatim by later tasks:
  - §1.1 literals `KUBECTL_VERSION=1.37.0`, `KUBECTL_SHA256=6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f`, `KIND_VERSION=0.33.0`, `KIND_SHA256=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d` — the `test` job install step (K1) and the `kubernetes-render` job (K3) carry the two `KUBECTL_*` assignments (K3's pin test over `KUBECTL_PINNED_JOBS`); the kind lane's pins are bound through `scripts/cicd/kubernetes-kind-smoke.sh` (K4), whose prelude carries all four assignments and which the `kubernetes-kind` job runs without carrying any pin assignment of its own, so K4's pin test reads the four from the script text and leaves `KUBECTL_PINNED_JOBS` unextended; the K3/K4 pin test also asserts `KUBECTL_SHA256`, `KIND_SHA256`, `` `v1.37.0` `` and `` `v0.33.0` `` appear literally in this file.
  - §1.1 the tool-directory convention `${ELSPETH_K8S_TOOLS:-.claude/lanes/k8s/bin}` prepended to `PATH`, which K4's smoke prelude and every local run use.
  - §1.2 `KIND_NODE_IMAGE=kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5` for K4's `kind-config.yaml` (`image:` line) and K5/K7, which reuse K4's fixture.
  - §1.3 `PROVISION_STORAGE_IMAGE=busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0` for K1's `job-provision-storage.yaml` (K1 writes it fully qualified as `docker.io/library/busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0` and its test asserts exact equality, `container["image"] == f"docker.io/library/busybox@{PROVISION_STORAGE_DIGEST}"`, plus that `busybox@<digest>` appears literally in this facts document), and `HARNESS_POSTGRES_IMAGE=postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94` for K4's `postgresql.yaml`.
  - §2.1–§2.3 the measured kind facts K1's root-Job rationale and K4's `pv-rwx-hostpath.yaml` cite: a single-node `hostPath` PV satisfies `ReadWriteMany`; the default `standard` StorageClass (`rancher.io/local-path`) refuses `ReadWriteMany`; `fsGroup` is not applied to `hostPath`, so the share root arrives `0:0 0755` and only a root Job can create the 1654-owned subtree.
  - §2.4 the clusterless facts K1 and K3 depend on: `kubectl kustomize` renders offline (embedded kustomize `v5.8.1`, so kustomization `labels:` is supported); `kubectl apply --dry-run=client` needs an API server and appears in no unit test and no clusterless CI job.
  - §2.5 wall times: K0 records `kind create`, `docker build`, `kind load`, `kind delete` on this box; the `[K4]` row and §5.1 are written by Task K4 Step 6 from two consecutive development-box runs of `scripts/cicd/kubernetes-kind-smoke.sh` (cold, warm), checked against the `kubernetes-kind` job's `timeout-minutes: 60` budget.
  - §4 the AKS facts K6 binds: StorageClass parameters for Azure Files NFS (`provisioner: file.csi.azure.com`, `protocol: nfs`, `skuName: Premium_LRS`, `networkEndpointType: privateEndpoint`, `rootSquashType: NoRootSquash`), the `SecretProviderClass` `secretObjects` shape that syncs into `elspeth-web-secrets` / `elspeth-schema-owner-secrets`, the two cookie-affinity annotation families (ingress-nginx `nginx.ingress.kubernetes.io/affinity: "cookie"`; AGIC `appgw.ingress.kubernetes.io/cookie-based-affinity: "true"`), and (§4.5) the Azure Standard Load Balancer inbound TCP idle timeout — default and minimum 4 minutes, maximum 100, set by the Service annotation `service.beta.kubernetes.io/azure-load-balancer-tcp-idle-timeout` — from which K6 derives its `230` s transport ceiling (240 − 10); and (§4.6) the add-on's Kubernetes Secret sync: Microsoft Learn documents `secretObjects` sync for the add-on with no enablement step, the `az aks` add-on options are only `--enable-secret-rotation` and `--rotation-poll-interval`, and `secrets-store-csi-driver.syncSecret.enabled` (default `false`) is the upstream Helm chart's RBAC switch for self-managed installs, not an add-on setting.
  - §5.2 an empty `[K7]` section "Affinity residuals" that Task K7 writes in Step 5a (the pass entry, when the no-affinity run passes) or Step 5b (the failing surfaces, when it fails).

- [ ] **Step 1: Install the pinned `kubectl` and `kind` into the lane directory, verifying both against the upstream-published checksums.**

The July pins ([archived plan](https://github.com/dta-au/elspeth/blob/888bfab53f298648379a92bc06e7bd996cae2ed9/docs/plans/2026-07-26-finish-deferred-deployment-platforms.md), lines 65–67: kubectl `v1.36.3`, kind `v0.32.0`, node `v1.36.1`) have moved; the values below were fetched from `https://dl.k8s.io/release/stable.txt` and `https://api.github.com/repos/kubernetes-sigs/kind/releases/latest` on 2026-09-13. Re-run the two fetches first; if either has moved, the values they print replace these everywhere in this task and in the document (the document is the authority, this plan is the starting value).

```bash
cd "$(git rev-parse --show-toplevel)" && mkdir -p .claude/lanes/k8s/bin .claude/lanes/k8s/spike && cd .claude/lanes/k8s/bin
echo "stable.txt now: $(curl -fsSL https://dl.k8s.io/release/stable.txt)"
curl -fsSL https://api.github.com/repos/kubernetes-sigs/kind/releases/latest | python3 -c 'import json,sys; print("kind latest:", json.load(sys.stdin)["tag_name"])'

KUBECTL_VERSION=1.37.0
KUBECTL_SHA256=6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f
KIND_VERSION=0.33.0
KIND_SHA256=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d

# The published checksums, so the two literals above are upstream's, not this spike's.
echo "published kubectl sha: $(curl -fsSL "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256")"
echo "published kind sha:    $(curl -fsSL "https://github.com/kubernetes-sigs/kind/releases/download/v${KIND_VERSION}/kind-linux-amd64.sha256sum")"

curl -fsSLo kubectl.tmp "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
printf '%s  kubectl.tmp\n' "$KUBECTL_SHA256" | sha256sum -c -
install -m 0755 kubectl.tmp kubectl && rm kubectl.tmp

curl -fsSLo kind.tmp "https://github.com/kubernetes-sigs/kind/releases/download/v${KIND_VERSION}/kind-linux-amd64"
printf '%s  kind.tmp\n' "$KIND_SHA256" | sha256sum -c -
install -m 0755 kind.tmp kind && rm kind.tmp

./kubectl version --client -o json | python3 -c 'import json,sys; v=json.load(sys.stdin); print(v["clientVersion"]["gitVersion"], "kustomize", v["kustomizeVersion"], v["clientVersion"]["platform"])'
./kind version

# Negative control: the gate must go red on a wrong digest.
printf '%s  kubectl\n' 0000000000000000000000000000000000000000000000000000000000000000 | sha256sum -c -; echo "negative_control_exit=$?"
```

Expected (measured 2026-09-13 with these exact commands):

```text
stable.txt now: v1.37.0
kind latest: v0.33.0
published kubectl sha: 6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f
published kind sha:    aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d  kind-linux-amd64
kubectl.tmp: OK
kind.tmp: OK
v1.37.0 kustomize v5.8.1 linux/amd64
kind v0.33.0 go1.26.7 linux/amd64
kubectl: FAILED
sha256sum: WARNING: 1 computed checksum did NOT match
negative_control_exit=1
```

The three lines `curl -fsSLo`, `printf '%s  <file>\n' "$SHA" | sha256sum -c -`, `install -m 0755` are the shape of `ci.yaml:744-752` (Bicep); K1's `test`-job step, K3's job and K4's smoke prelude repeat them with the §1.1 literals.

- [ ] **Step 2: Resolve the node image and the two container-image digests the manifests pin.**

```bash
cd "$(git rev-parse --show-toplevel)"
# kind node image for this kind release: the release body lists the pre-built images; only the digest form is supported.
curl -fsSL https://api.github.com/repos/kubernetes-sigs/kind/releases/tags/v0.33.0 | python3 -c '
import json, re, sys
body = json.load(sys.stdin)["body"]
print("summary line:", body.splitlines()[0])
for line in body.splitlines():
    if "kindest/node:v1.37" in line:
        print(line.strip())
'
# Multi-arch index digests (what a manifest pins). Children are printed for the record.
for ref in busybox:1.37.0 postgres:16; do
  printf '%s index %s\n' "$ref" "$(docker buildx imagetools inspect "$ref" --format '{{.Manifest.Digest}}')"
done
docker buildx imagetools inspect busybox:1.37.0 --raw | python3 -c '
import json, sys
for m in json.load(sys.stdin)["manifests"]:
    p = m.get("platform", {})
    if p.get("os") == "linux" and p.get("architecture") in ("amd64", "arm64"):
        print("busybox:1.37.0", p["architecture"], m["digest"])
'
```

Expected (measured 2026-09-13):

```text
summary line: This release contains critical dependency updates, bug fixes, and defaults to Kubernetes 1.36.1.
* v1.37.0: `kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5`
busybox:1.37.0 index sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0
postgres:16 index sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94
busybox:1.37.0 amd64 sha256:7a3ebe5bfd1a4a19797d20b0c0bb39d44393e9a03fd852c0865b0f540d868df0
busybox:1.37.0 arm64 sha256:f10e809bcf667d8e9f01d2baf82869049a495cd448cdfe1f4dee94078b960ae9
```

The release summary line still says "defaults to Kubernetes 1.36.1" while the same body's Breaking Changes section and image list say the default node image is `v1.37.0`; the image list is what `kind create cluster` pulls and the digest is what the harness pins, so record the digest and note the discrepancy as correction C2 in §0. The node's `v1.37.0` matches the kubectl pin exactly, so client/server skew is zero.

- [ ] **Step 3: Measure the two clusterless facts: `kubectl kustomize` renders offline; `kubectl apply --dry-run=client` does not.**

The review measured that client-side dry-run still performs REST-mapper discovery; this step records the fact with the pinned binary so no later task puts a dry-run in a unit test or a clusterless job. Run it with an empty `HOME` so no kubeconfig can be found.

```bash
cd "$(git rev-parse --show-toplevel)" && export PATH="$PWD/.claude/lanes/k8s/bin:$PATH"
mkdir -p .claude/lanes/k8s/spike/kustomize-probe
cat > .claude/lanes/k8s/spike/kustomize-probe/pv.yaml <<'EOF'
apiVersion: v1
kind: PersistentVolume
metadata:
  name: elspeth-k0-rwx
spec:
  capacity: { storage: 1Gi }
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Delete
  storageClassName: ""
  claimRef: { name: elspeth-k0-rwx, namespace: default }
  hostPath:
    path: /var/elspeth-k0
    type: DirectoryOrCreate
EOF
cat > .claude/lanes/k8s/spike/kustomize-probe/kustomization.yaml <<'EOF'
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
labels:
  - pairs:
      app.kubernetes.io/name: elspeth-k0
    includeSelectors: false
resources:
  - pv.yaml
EOF
H="$(mktemp -d)"
env -i PATH="$PATH" HOME="$H" kubectl kustomize .claude/lanes/k8s/spike/kustomize-probe > /tmp/k8s-k0-kustomize.log 2>&1; echo "kustomize_exit=$?"; head -6 /tmp/k8s-k0-kustomize.log
env -i PATH="$PATH" HOME="$H" kubectl apply --dry-run=client --validate=false -f .claude/lanes/k8s/spike/kustomize-probe/pv.yaml > /tmp/k8s-k0-dryrun.log 2>&1; echo "dryrun_exit=$?"; tail -1 /tmp/k8s-k0-dryrun.log
```

Expected (measured 2026-09-13):

```text
kustomize_exit=0
apiVersion: v1
kind: PersistentVolume
metadata:
  labels:
    app.kubernetes.io/name: elspeth-k0
  name: elspeth-k0-rwx
dryrun_exit=1
error: unable to recognize ".claude/lanes/k8s/spike/kustomize-probe/pv.yaml": Get "http://localhost:8080/api?timeout=32s": dial tcp 127.0.0.1:8080: connect: connection refused
```

The rendered `labels:` block proves the kustomize v5 form K1 uses (the deprecated `commonLabels` field is never written in any kustomization of this plan). Record both exits and the error line in §2.4.

- [ ] **Step 4: Create the spike cluster on the pinned node image and time it.**

```bash
cd "$(git rev-parse --show-toplevel)" && export PATH="$PWD/.claude/lanes/k8s/bin:$PATH"
cat > .claude/lanes/k8s/spike/kind-config.yaml <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    image: kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5
EOF
KC="$PWD/.claude/lanes/k8s/spike/kubeconfig"
{ time kind create cluster --name elspeth-k0 --config .claude/lanes/k8s/spike/kind-config.yaml --kubeconfig "$KC" --wait 120s; } > /tmp/k8s-k0-create.log 2>&1; echo "create_exit=$?"; tail -4 /tmp/k8s-k0-create.log
kind get clusters
kubectl --kubeconfig "$KC" get nodes -o wide
kubectl --kubeconfig "$KC" get storageclass
```

Expected: `create_exit=0`; the `time` block's `real` line is the §2.5 "kind create cluster" value; `kind get clusters` prints `elspeth-k0`; `get nodes` shows one `Ready` node at `VERSION v1.37.0`; `get storageclass` lists one class, `standard (default)`, with `PROVISIONER rancher.io/local-path`, `RECLAIMPOLICY Delete`, `VOLUMEBINDINGMODE WaitForFirstConsumer`. If the image pull is refused with a platform error, the host is not amd64 — kind node images must match the host platform (release body: "You must use the same platform as your host"); this box and the `ubuntu-24.04` runner are both `x86_64`.

- [ ] **Step 5: Prove that a single-node `hostPath` PersistentVolume satisfies `ReadWriteMany` for two pods, including a rename made by the second pod.**

```bash
cd "$(git rev-parse --show-toplevel)" && export PATH="$PWD/.claude/lanes/k8s/bin:$PATH"; KC="$PWD/.claude/lanes/k8s/spike/kubeconfig"
cat > .claude/lanes/k8s/spike/rwx.yaml <<'EOF'
apiVersion: v1
kind: PersistentVolume
metadata:
  name: elspeth-k0-rwx
spec:
  capacity: { storage: 1Gi }
  accessModes: [ReadWriteMany]
  persistentVolumeReclaimPolicy: Delete
  storageClassName: ""
  claimRef: { name: elspeth-k0-rwx, namespace: default }
  hostPath:
    path: /var/elspeth-k0
    type: DirectoryOrCreate
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: elspeth-k0-rwx
  namespace: default
spec:
  accessModes: [ReadWriteMany]
  storageClassName: ""
  volumeName: elspeth-k0-rwx
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: rwx-a
  namespace: default
spec:
  restartPolicy: Never
  containers:
    - name: shell
      image: busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0
      command: ["sh", "-c", "sleep 3600"]
      volumeMounts:
        - name: state
          mountPath: /mnt/elspeth
  volumes:
    - name: state
      persistentVolumeClaim:
        claimName: elspeth-k0-rwx
---
apiVersion: v1
kind: Pod
metadata:
  name: rwx-b
  namespace: default
spec:
  restartPolicy: Never
  containers:
    - name: shell
      image: busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0
      command: ["sh", "-c", "sleep 3600"]
      volumeMounts:
        - name: state
          mountPath: /mnt/elspeth
  volumes:
    - name: state
      persistentVolumeClaim:
        claimName: elspeth-k0-rwx
EOF
kubectl --kubeconfig "$KC" apply -f .claude/lanes/k8s/spike/rwx.yaml
kubectl --kubeconfig "$KC" wait --for=condition=ready pod/rwx-a pod/rwx-b --timeout=180s
kubectl --kubeconfig "$KC" get pvc elspeth-k0-rwx
kubectl --kubeconfig "$KC" exec rwx-a -- sh -c 'echo from-a > /mnt/elspeth/a.tmp && ls -ln /mnt/elspeth'
# rename(2) by the OTHER pod, then read-back from both: the atomic-publish shape blobs/service.py uses.
kubectl --kubeconfig "$KC" exec rwx-b -- sh -c 'mv /mnt/elspeth/a.tmp /mnt/elspeth/a.final && cat /mnt/elspeth/a.final'
kubectl --kubeconfig "$KC" exec rwx-a -- sh -c 'ls /mnt/elspeth && cat /mnt/elspeth/a.final'
```

Expected: the PVC is `Bound` to `elspeth-k0-rwx` with `ACCESS MODES RWX`; both pods are `Ready`; `rwx-b` prints `from-a`; `rwx-a` lists only `a.final` and prints `from-a`. Record in §2.1 that this proves the harness shape (one node, one kernel, a bind mount), not NFS cross-client semantics, which stay `[doc]` (§4.1) and `[LIVE]` (§4.4).

- [ ] **Step 6: Prove that the default `standard` StorageClass refuses `ReadWriteMany`, so K4 must bind the base PVC to the hostPath PV explicitly.**

```bash
cd "$(git rev-parse --show-toplevel)" && export PATH="$PWD/.claude/lanes/k8s/bin:$PATH"; KC="$PWD/.claude/lanes/k8s/spike/kubeconfig"
cat > .claude/lanes/k8s/spike/rwx-standard.yaml <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: rwx-standard
  namespace: default
spec:
  accessModes: [ReadWriteMany]
  storageClassName: standard
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: rwx-standard-consumer
  namespace: default
spec:
  restartPolicy: Never
  containers:
    - name: shell
      image: busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0
      command: ["sh", "-c", "sleep 3600"]
      volumeMounts:
        - name: state
          mountPath: /mnt/elspeth
  volumes:
    - name: state
      persistentVolumeClaim:
        claimName: rwx-standard
EOF
kubectl --kubeconfig "$KC" apply -f .claude/lanes/k8s/spike/rwx-standard.yaml
sleep 30
kubectl --kubeconfig "$KC" get pvc rwx-standard
kubectl --kubeconfig "$KC" describe pvc rwx-standard | sed -n '/^Events:/,$p'
kubectl --kubeconfig "$KC" delete -f .claude/lanes/k8s/spike/rwx-standard.yaml --wait=false
```

Expected: `STATUS Pending` (the consumer pod is required because the class is `WaitForFirstConsumer`), and the Events block carries a `Warning  ProvisioningFailed` from `rancher.io/local-path` naming the access mode (on local-path-provisioner the message reads `Only support ReadWriteOnce access mode`; copy the verbatim line into §2.2). If the PVC instead reaches `Bound`, the class is not local-path — record `kubectl get sc -o yaml` and stop: K4's harness assumption is wrong and the plan must be re-measured before K1 lands.

- [ ] **Step 7: Measure share-root ownership: a 1654 pod with `fsGroup: 1654` cannot create the data subtree; the root Job can.**

This is the measurement behind K1's `job-provision-storage.yaml` (a root busybox Job, never an `initContainer` in the application pod) and mirrors ACA's `provision-storage` Job (`workload.bicep:430-452`, ACA facts C6). Upstream states the rule for both halves: `fsGroup` applies only to "volumes that support ownership management" (security-context task page), and for `hostPath` "some files or directories created on the underlying hosts might only be accessible by root. You then either need to run your process as root in a privileged container or modify the file permissions on the host" (volumes concept page, `hostPath` section).

```bash
cd "$(git rev-parse --show-toplevel)" && export PATH="$PWD/.claude/lanes/k8s/bin:$PATH"; KC="$PWD/.claude/lanes/k8s/spike/kubeconfig"
cat > .claude/lanes/k8s/spike/owner-before.yaml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: owner-before
  namespace: default
spec:
  restartPolicy: Never
  securityContext:
    runAsUser: 1654
    runAsGroup: 1654
    fsGroup: 1654
    runAsNonRoot: true
  containers:
    - name: probe
      image: busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0
      command: ["sh", "-c", "id; ls -ldn /mnt/elspeth; if mkdir /mnt/elspeth/data; then echo MKDIR_OK; else echo MKDIR_DENIED; fi"]
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
      volumeMounts:
        - name: state
          mountPath: /mnt/elspeth
  volumes:
    - name: state
      persistentVolumeClaim:
        claimName: elspeth-k0-rwx
EOF
cat > .claude/lanes/k8s/spike/provision-storage.yaml <<'EOF'
# The K1 Job verbatim, pointed at the spike PVC. Runs as root from the pinned busybox.
apiVersion: batch/v1
kind: Job
metadata:
  name: elspeth-provision-storage
  namespace: default
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsUser: 0
        runAsGroup: 0
      containers:
        - name: provision-storage
          image: busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0
          command: ["sh", "-c", "set -eu; mkdir -p /mnt/elspeth/data/blobs /mnt/elspeth/payloads; chown -R 1654:1654 /mnt/elspeth/data /mnt/elspeth/payloads; chmod 0700 /mnt/elspeth/data /mnt/elspeth/data/blobs /mnt/elspeth/payloads; ls -ln /mnt/elspeth"]
          volumeMounts:
            - name: state
              mountPath: /mnt/elspeth
      volumes:
        - name: state
          persistentVolumeClaim:
            claimName: elspeth-k0-rwx
EOF
cat > .claude/lanes/k8s/spike/owner-after.yaml <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: owner-after
  namespace: default
spec:
  restartPolicy: Never
  securityContext:
    runAsUser: 1654
    runAsGroup: 1654
    fsGroup: 1654
    runAsNonRoot: true
  containers:
    - name: probe
      image: busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0
      command: ["sh", "-c", "id; ls -ln /mnt/elspeth; if mkdir /mnt/elspeth/data/blobs/k0 && echo k0 > /mnt/elspeth/payloads/k0.tmp && mv /mnt/elspeth/payloads/k0.tmp /mnt/elspeth/payloads/k0; then echo MKDIR_OK; else echo MKDIR_DENIED; fi"]
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
      volumeMounts:
        - name: state
          mountPath: /mnt/elspeth
  volumes:
    - name: state
      persistentVolumeClaim:
        claimName: elspeth-k0-rwx
EOF
kubectl --kubeconfig "$KC" apply -f .claude/lanes/k8s/spike/owner-before.yaml
kubectl --kubeconfig "$KC" wait --for=jsonpath='{.status.phase}'=Succeeded pod/owner-before --timeout=120s
kubectl --kubeconfig "$KC" logs owner-before | tee /tmp/k8s-k0-owner-before.log
kubectl --kubeconfig "$KC" apply -f .claude/lanes/k8s/spike/provision-storage.yaml
kubectl --kubeconfig "$KC" wait --for=condition=complete job/elspeth-provision-storage --timeout=120s
kubectl --kubeconfig "$KC" logs job/elspeth-provision-storage | tee /tmp/k8s-k0-provision.log
kubectl --kubeconfig "$KC" apply -f .claude/lanes/k8s/spike/owner-after.yaml
kubectl --kubeconfig "$KC" wait --for=jsonpath='{.status.phase}'=Succeeded pod/owner-after --timeout=120s
kubectl --kubeconfig "$KC" logs owner-after | tee /tmp/k8s-k0-owner-after.log
```

Expected `owner-before` log: `uid=1654 gid=1654 groups=1654`; an `ls -ldn` line for `/mnt/elspeth` beginning `drwxr-xr-x` with numeric owner `0` and group `0` (the directory `DirectoryOrCreate` made, untouched by `fsGroup`); then `mkdir: can't create directory '/mnt/elspeth/data': Permission denied` and `MKDIR_DENIED`. Expected Job log: two `ls -ln` lines beginning `drwx------` with owner `1654` and group `1654`, named `data` and `payloads`. Expected `owner-after` log: the same `ls -ln` and `MKDIR_OK`. If `owner-before` prints `MKDIR_OK`, the kubelet applied `fsGroup` to the hostPath volume on this node image: record that verbatim in §2.3 — K1 keeps the root Job regardless (Azure Files share-root ownership is `[LIVE]`, ACA facts §3.1), but the rationale text in K1 changes from "measured on kind" to "measured on NFS only", so tell the K1 implementer.

- [ ] **Step 8: Measure the two image-side wall times the kind lane pays: the ELSPETH image build (house form, `docs/guides/docker.md:453-456`) and `kind load`.**

```bash
cd "$(git rev-parse --show-toplevel)" && export PATH="$PWD/.claude/lanes/k8s/bin:$PATH"
{ time docker build --build-arg INSTALL_EXTRAS=all --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" -t elspeth-web-spike:k0 . ; } > /tmp/k8s-k0-build.log 2>&1; echo "build_exit=$?"; tail -4 /tmp/k8s-k0-build.log
docker image inspect elspeth-web-spike:k0 --format 'size_bytes={{.Size}}'
{ time kind load docker-image elspeth-web-spike:k0 --name elspeth-k0 ; } > /tmp/k8s-k0-load.log 2>&1; echo "load_exit=$?"; tail -4 /tmp/k8s-k0-load.log
```

Expected: `build_exit=0` and `load_exit=0`; the two `real` lines and the image size go into §2.5 as this box's numbers (24 cores, 61 GiB). They are a floor for K4's `timeout-minutes`, not the value: the `[K4]` row is written by K4 Step 6 from two consecutive development-box smoke runs, checked against the `kubernetes-kind` job's `timeout-minutes: 60`.

- [ ] **Step 9: Delete the cluster and the spike image; time the delete.**

```bash
cd "$(git rev-parse --show-toplevel)" && export PATH="$PWD/.claude/lanes/k8s/bin:$PATH"
{ time kind delete cluster --name elspeth-k0 ; } > /tmp/k8s-k0-delete.log 2>&1; echo "delete_exit=$?"; tail -4 /tmp/k8s-k0-delete.log
kind get clusters
docker ps -a --filter name=elspeth-k0 --format '{{.Names}}'
docker image rm elspeth-web-spike:k0
rm -f .claude/lanes/k8s/spike/kubeconfig
```

Expected: `delete_exit=0`; `kind get clusters` prints `No kind clusters found.`; the `docker ps -a` filter prints nothing; the `real` line is the §2.5 "kind delete cluster" value.

- [ ] **Step 10: Record the AKS facts from documentation, each with its URL and page date.**

No Azure subscription exists on this box, so §4 is `[doc]` plus a `[LIVE]` residue list, the pattern of `docs/plans/2026-09-05-phase6b-azure-container-apps-platform-facts.md` §3 and §10. Fetch each source on the day and quote it; the values below are what the sources said on 2026-09-13 (every URL returned HTTP 200 that day; dates are the page's `ms.date`).

| fact K6 binds | source | value on 2026-09-13 |
|---|---|---|
| Azure Files NFS StorageClass | `https://learn.microsoft.com/en-us/azure/aks/azure-files-csi` (2026-08-03); parameter reference `https://github.com/kubernetes-sigs/azurefile-csi-driver/blob/master/docs/driver-parameters.md` | `provisioner: file.csi.azure.com`; `parameters.protocol: nfs`; `parameters.skuName: Premium_LRS` ("NFS file share only supports Premium account type"); `parameters.networkEndpointType: privateEndpoint` ("For nfs protocol, a service endpoint is created by default"); `parameters.rootSquashType`: `AllSquash` / `NoRootSquash` / `RootSquash`, "The default is NoRootSquash"; `parameters.mountPermissions` default `0777`; `parameters.encryptInTransit` default `false`; `parameters.allowSharedKeyAccess` — "NFS: safe to disable — NFS mount does not use the account key"; the driver sets `vers=4,minorversion=1,sec=sys` itself and "It is not supported to specify these NFS mount options"; `fsGroupChangePolicy: None` in `parameters` skips the ownership walk on large volumes |
| NFS protocol constraints | `https://learn.microsoft.com/en-us/azure/storage/files/files-nfs-protocol` (2026-07-08) | NFSv4.1 only; Premium `FileStorage` account; "you must set up either a private endpoint or a service endpoint"; root squash is a share property and `NoRootSquash` is the default when a share is created (ACA facts §3.1 quotes the same page) |
| Key Vault → Kubernetes Secret | `https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-driver` and `https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-configuration-options` (2025-01-03) | `SecretProviderClass` with `spec.provider: azure` and `spec.secretObjects[]` = `{secretName, type, data[]: {objectName, key}}`; "Make sure the `objectName` in the `secretObjects` field matches the file name of the mounted content"; the synced Secret exists only while a pod mounts the CSI volume (`csi.driver: secrets-store.csi.k8s.io`, `volumeAttributes.secretProviderClass`), which is why K6 mounts it in the Deployment even though the app reads the Secret through `envFrom`; autorotation via `az aks addon update --addon azure-keyvault-secrets-provider --enable-secret-rotation` (default `rotationPollInterval` `2m`) updates the synced Secret too; one `SecretProviderClass` may list several `secretObjects`, so `elspeth-web-secrets` and `elspeth-schema-owner-secrets` can come from one class or two |
| Cookie affinity, ingress-nginx | `https://kubernetes.github.io/ingress-nginx/user-guide/nginx-configuration/annotations/` (fetched 2026-09-13) | `nginx.ingress.kubernetes.io/affinity: "cookie"`, `affinity-mode` (`balanced` default / `persistent`), `session-cookie-name`, `session-cookie-expires`, `session-cookie-max-age`, `session-cookie-change-on-failure`; timeouts `proxy-read-timeout` / `proxy-send-timeout` / `proxy-connect-timeout` are per-Ingress annotations in seconds, "Annotation keys and values can only be strings" |
| Cookie affinity, AGIC | `https://learn.microsoft.com/en-us/azure/application-gateway/ingress-controller-annotations` (2026-05-12) | `appgw.ingress.kubernetes.io/cookie-based-affinity: "true"`; `appgw.ingress.kubernetes.io/request-timeout` (seconds) |
| Managed ingress-nginx on AKS | `https://learn.microsoft.com/en-us/azure/aks/app-routing` (2026-08-03) | the application routing add-on runs ingress-nginx under `ingressClassName: webapprouting.kubernetes.azure.com`, so the nginx annotation family above is the default choice for K6 and AGIC is the documented alternative |
| Azure Standard Load Balancer inbound TCP idle timeout (the binding hop for K6's transport ceiling) | `https://learn.microsoft.com/en-us/azure/aks/configure-load-balancer-standard` (2026-09-08); `https://learn.microsoft.com/en-us/azure/load-balancer/load-balancer-tcp-idle-timeout` (2026-08-05); `https://cloud-provider-azure.sigs.k8s.io/topics/loadbalancer/` (no page date; all three fetched 2026-09-14, HTTP 200) | Service annotation `service.beta.kubernetes.io/azure-load-balancer-tcp-idle-timeout` — "Specify the time in minutes for TCP connection idle timeouts to occur on the load balancer. The default and minimum value is 4. The maximum value is 100. The value must be an integer."; the LB page: "Standard Load Balancer supports an idle timeout range of 4 minutes to 100 minutes for load-balancing rules and inbound NAT rules … The default setting is 4 minutes for all rule types"; the AKS page separates it from the 30-minute outbound-rule SNAT timeout (`--load-balancer-idle-timeout`): that annotation "configures the inbound TCP idle timeout for an individual Kubernetes LoadBalancer service and has a default and minimum value of 4 minutes". K6 derives `ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS=230` (= 240 − 10) from the 4-minute default |
| Key Vault provider add-on Secret sync (whether a sync switch must be enabled) | the §4.6 sources: `https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-configuration-options` (2025-01-03), `https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-driver` (2026-05-05), the `az aks` and `az aks addon` CLI reference pages (2026-09-01), the upstream provider chart and sync page, Azure/AKS#4319 (all fetched 2026-09-14, HTTP 200) | Learn documents `secretObjects` sync for the add-on with no enablement step ("Your secrets sync after you start a pod to mount them."); the add-on's CLI options are only `--enable-secret-rotation` and `--rotation-poll-interval`; `secrets-store-csi-driver.syncSecret.enabled` (default `false`) is the upstream Helm chart's RBAC switch for self-managed installs. K6 enables nothing beyond the add-on |

`[LIVE]` residue (needs a subscription; each is a K6 README caveat, none blocks a render-only K6): share-root ownership and mode after the CSI driver creates the NFS share (the K1 root Job succeeds either way); whether the managed add-on honours `proxy-read-timeout` above its default; the AKS Kubernetes minor available in the target region (the base is validated on `v1.37.0`); the identity the `SecretProviderClass` uses (`useVMManagedIdentity` + `userAssignedIdentityID`, or workload identity) — an operator input, never a tracked value; whether the managed Key Vault provider add-on's driver carries the Secret-sync RBAC (§4.6).

- [ ] **Step 11: Write the facts document.**

Write `docs/plans/2026-09-13-kubernetes-platform-facts.md` with the content below, substituting each `⟨…⟩` token with the value the named step printed (every other value is measured and stands unless Step 1's two fetches moved). Never write a user-home path into it (`AGENTS.md` § Gotchas); the appendix uses repo-relative paths only.

````markdown
# Kubernetes (multi-replica) platform facts — spike K0

Plan of record: `docs/plans/2026-09-13-kubernetes-and-identity/2026-09-13-kubernetes-and-identity-master-plan.md`,
Task K0. Measured 2026-09-13 on branch `release/0.8.1` at `072141b75` from a
host with **Docker 29.7.2, buildx v0.36.1, no kubectl, no kind, no Azure
subscription**; the two binaries were installed into the gitignored lane
directory by this spike. Everything obtainable without a subscription is
measured here; the residue that needs one is in §4.4.

Every fact carries a provenance tag:

- `[local]` — executed on this host; the command is in the appendix.
- `[tree]` — read from the tree at `072141b75` (`path:line`).
- `[doc]` — upstream documentation, cited by URL with the page's `ms.date`
  (Microsoft Learn) or fetch date. Quoted text is verbatim.
- `[LIVE]` — cannot be established without a subscription; a claim until a
  live run records it.
- `[K4]` / `[K7]` — a row a later task of the same plan writes (§2.5, §5.2).

Downstream consumers pin the literals in §1 **verbatim**: the K3/K4 pin test
asserts `KUBECTL_SHA256`, `KIND_SHA256`, `` `v1.37.0` `` and `` `v0.33.0` `` appear
in this file; the `test` and `kubernetes-render` jobs carry the two
`KUBECTL_*` assignments; the kind lane's pins are bound through
`scripts/cicd/kubernetes-kind-smoke.sh`, whose prelude copies all four §1.1
assignments and which the `kubernetes-kind` job runs; K1's `job-provision-storage.yaml` copies §1.3; K4's
`kind-config.yaml` copies §1.2.

---

## 0. Corrections to the plan found by measurement

| # | plan / July design says | measured | consequence |
|---|---|---|---|
| C1 | July tool pins kubectl `v1.36.3`, kind `v0.32.0`, node `v1.36.1` ([archived plan](https://github.com/dta-au/elspeth/blob/888bfab53f298648379a92bc06e7bd996cae2ed9/docs/plans/2026-07-26-finish-deferred-deployment-platforms.md), lines 65–67) | `stable.txt` → `v1.37.0`; kind latest → `v0.33.0` (published 2026-08-26); default node image `v1.37.0` `[local]` §1 | every K task installs from §1; the July table is a historical record and is not edited by workstream K |
| C2 | — | the kind `v0.33.0` release body's summary line says "defaults to Kubernetes 1.36.1" while its Breaking Changes section and image list say `kindest/node:v1.37.0@sha256:a1ed56cf…` `[local]` | the image list is what `kind create cluster` pulls; the digest is authoritative, the prose is stale |
| C3 | a clusterless `kubectl apply --dry-run=client` could validate the render in a unit test or the render job | client dry-run performs REST-mapper discovery and exits 1 with `dial tcp 127.0.0.1:8080: connect: connection refused` `[local]` §2.4 | no unit test and no clusterless CI job runs a dry-run; server admission is proven by K4's `kubectl apply -k` in kind |
| C4 | `fsGroup: 1654` on the pod could make the share writable | `fsGroup` is not applied to `hostPath`; the share root arrives `0:0 0755` and a 1654 pod cannot `mkdir` under it until the root Job runs `[local]` §2.3 | K1 provisions the subtree from a root Job (`job-provision-storage.yaml`), never from the application pod or an initContainer |
| C5 | K4 could use the cluster's default StorageClass | `standard` is `rancher.io/local-path`, `WaitForFirstConsumer`, and refuses `ReadWriteMany` `[local]` §2.2 | K4 pre-creates a `hostPath` PV with `storageClassName: ""` and binds the base PVC to it by `volumeName` |

---

## 1. Toolchain pins `[local]`

### 1.1 CLI binaries (linux/amd64)

| variable | value | source |
|---|---|---|
| `KUBECTL_VERSION` | `1.37.0` (tag `v1.37.0`) | `https://dl.k8s.io/release/stable.txt` on 2026-09-13 |
| `KUBECTL_SHA256` | `6129359f4e1f3848a5572ccb0b26cf28b8ca08cef38c95a765b2f64a2c961a2f` | published at `https://dl.k8s.io/release/v1.37.0/bin/linux/amd64/kubectl.sha256`; the downloaded binary matched (`kubectl.tmp: OK`) |
| `KUBECTL_URL` | `https://dl.k8s.io/release/v1.37.0/bin/linux/amd64/kubectl` | |
| embedded kustomize | `v5.8.1` (`kubectl version --client -o json` → `kustomizeVersion`) | so kustomization `labels:` (v5) is supported; `commonLabels` is not used anywhere |
| `KIND_VERSION` | `0.33.0` (tag `v0.33.0`, published 2026-08-26) | `https://api.github.com/repos/kubernetes-sigs/kind/releases/latest` on 2026-09-13 |
| `KIND_SHA256` | `aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d` | published at `https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64.sha256sum`; the downloaded binary matched (`kind.tmp: OK`) |
| `KIND_URL` | `https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64` | `kind version` → `kind v0.33.0 go1.26.7 linux/amd64` |

Install shape (the three lines `ci.yaml:744-752` uses for Bicep), and the
directory convention every local run and `scripts/cicd/kubernetes-kind-smoke.sh`
share:

```bash
ELSPETH_K8S_TOOLS="${ELSPETH_K8S_TOOLS:-.claude/lanes/k8s/bin}"   # gitignored (.gitignore:67)
curl -fsSLo "$ELSPETH_K8S_TOOLS/kubectl.tmp" "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
printf '%s  %s/kubectl.tmp\n' "$KUBECTL_SHA256" "$ELSPETH_K8S_TOOLS" | sha256sum -c -
install -m 0755 "$ELSPETH_K8S_TOOLS/kubectl.tmp" "$ELSPETH_K8S_TOOLS/kubectl"
export PATH="$ELSPETH_K8S_TOOLS:$PATH"
```

The negative control (a zeroed digest) makes `sha256sum -c -` print
`kubectl: FAILED` and exit 1, so a moved upstream binary reds the install step
rather than passing silently.

### 1.2 kind node image

| variable | value |
|---|---|
| `KIND_NODE_IMAGE` | `kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5` |

From the `v0.33.0` release body ("Images pre-built for this release"): "You
*must* use the `@sha256` digest to guarantee an image built for this release"
and "These node images support amd64 and arm64 … You must use the same
platform as your host". This host and the `ubuntu-24.04` runner are `x86_64`.
Node and kubectl are the same minor, so version skew is zero.

### 1.3 Container images the manifests pin by digest

| variable | value | consumer |
|---|---|---|
| `PROVISION_STORAGE_IMAGE` | `busybox@sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0` (index digest of `busybox:1.37.0`; children amd64 `7a3ebe5b…`, arm64 `f10e809b…`) | K1 `job-provision-storage.yaml` (root Job); this spike's own Job (§2.3) |
| `HARNESS_POSTGRES_IMAGE` | `postgres:16@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94` (index digest of `postgres:16` on 2026-09-13) | K4 `postgresql.yaml` (harness only; the base ships no database) |
| ELSPETH image | production `ghcr.io/dta-au/elspeth@sha256:<release digest>`; the kind lane builds `elspeth-web-test:kind` locally and `kind load`s it | K1 base; K4 overlay `images:` rewrite |

---

## 2. kind harness facts `[local]`

Cluster `elspeth-k0`, one control-plane node on §1.2, `kubectl get nodes` →
`Ready` at `v1.37.0`; `kubectl get storageclass` →
`standard (default)   rancher.io/local-path   Delete   WaitForFirstConsumer`.

### 2.1 A single-node `hostPath` PersistentVolume satisfies `ReadWriteMany`

PV `hostPath: {path: /var/elspeth-k0, type: DirectoryOrCreate}`,
`accessModes: [ReadWriteMany]`, `storageClassName: ""`, bound by `claimRef` /
`volumeName` to a PVC with the same access mode; two busybox pods mount it.
`rwx-a` wrote `a.tmp`; `rwx-b` renamed it with `mv` (rename(2)) and read
`from-a`; `rwx-a` then listed only `a.final` and read `from-a`. This proves
the harness shape (one node, one kernel, a bind mount), not NFS cross-client
semantics, which stay `[doc]` §4.1 and `[LIVE]` §4.4.

### 2.2 The default `standard` StorageClass refuses `ReadWriteMany`

A `ReadWriteMany` PVC on `storageClassName: standard` with a consumer pod
stayed `Pending`; `describe` recorded
`Warning  ProvisioningFailed … ⟨Step 6 verbatim message⟩`. K4 therefore
binds the base PVC to a pre-created hostPath PV by `volumeName` and never
relies on dynamic provisioning.

### 2.3 `fsGroup` is not applied to `hostPath`; the root Job is required

Pod `owner-before` (`runAsUser/runAsGroup/fsGroup: 1654`, `runAsNonRoot`, no
capabilities) on the §2.1 PV printed:

```text
⟨Step 7 owner-before log, three lines: id / ls -ldn / mkdir result⟩
MKDIR_DENIED
```

Job `elspeth-provision-storage` (root, `PROVISION_STORAGE_IMAGE`, the K1
command `mkdir -p … chown -R 1654:1654 … chmod 0700 …`) then printed
`drwx------ … 1654 1654 … data` and `… payloads`; pod `owner-after` (same
1654 context) created `data/blobs/k0`, wrote and renamed a payload file and
printed `MKDIR_OK`. Upstream rule, both halves `[doc]`:
`fsGroup` — "Volumes that support ownership management are modified to be
owned and writable by the GID specified in `fsGroup`"
(`https://kubernetes.io/docs/tasks/configure-pod-container/security-context/`);
`hostPath` — "Some files or directories created on the underlying hosts might
only be accessible by root. You then either need to run your process as root
in a privileged container or modify the file permissions on the host"
(`https://kubernetes.io/docs/concepts/storage/volumes/`, hostPath section).
Same conclusion as ACA facts C6/§3.4: the runtime containers never need root;
the share is provisioned once by a root Job (`workload.bicep:430-452`).

### 2.4 Clusterless `kubectl`

| command (empty `HOME`, no kubeconfig) | exit | output |
|---|---|---|
| `kubectl kustomize <dir with labels: block>` | 0 | rendered the PV with `labels.app.kubernetes.io/name` applied |
| `kubectl apply --dry-run=client --validate=false -f pv.yaml` | 1 | `error: unable to recognize "…/pv.yaml": Get "http://localhost:8080/api?timeout=32s": dial tcp 127.0.0.1:8080: connect: connection refused` |

So the K1 unit test and the K3 `kubernetes-render` job stop at `kubectl kustomize`
plus structural assertions; admission is K4's `kubectl apply -k` inside kind.
No offline schema validator is pinned by this plan.

### 2.5 Wall times

| phase | wall time (`real`) | where | provenance |
|---|---|---|---|
| `kind create cluster --wait 120s` (§1.2 image already pulled: no / yes as measured) | ⟨Step 4 real⟩ | this host, 24 cores / 61 GiB | `[local]` |
| `docker build --build-arg INSTALL_EXTRAS=all` (image size ⟨Step 8 size_bytes⟩) | ⟨Step 8 build real⟩ | this host | `[local]` |
| `kind load docker-image` | ⟨Step 8 load real⟩ | this host | `[local]` |
| `kind delete cluster` | ⟨Step 9 real⟩ | this host | `[local]` |
| `kubernetes-kind` job end to end (`docker build` + `kind create` + `kind load` + both tests + delete) | written by Task K4 Step 6: the `wall=` figures of two consecutive smoke runs (cold, warm), checked against the job's `timeout-minutes: 60` budget | this host | `[K4]` |

---

## 3. What the tree already guarantees `[tree]`

- `Dockerfile:125-126` creates group and user `elspeth` UID/GID 1654; `:185`
  `USER elspeth`. The base pod runs `runAsUser/runAsGroup/fsGroup: 1654`,
  `runAsNonRoot: true`, listens on 8451, probes `/api/health` and `/api/ready`.
- `deploy/azure-container-apps/workload.bicep:430-452`: the ACA
  `provision-storage` Job (root image, `replicaRetryLimit: 0`,
  `mkdir -p … chown -R 1654:1654 … chmod 0700 …`) is the shape K1's
  `job-provision-storage.yaml` mirrors; `workload.bicep:48` takes the root
  image as a digest-pinned parameter, which §1.3 supplies for Kubernetes.
- `.github/workflows/ci.yaml:738-752`: the checksum-pinned Bicep install
  (`BICEP_VERSION` / `BICEP_SHA256` / `sha256sum -c -` / `install -m 0755`)
  the `test`-job kubectl step and the `kubernetes-render` job copy;
  `tests/unit/deployment/test_azure_container_apps_bundle.py:509-527` is the
  pin test that reads this kind of document (`PLATFORM_FACTS`) and asserts the
  version and digest appear in it and in both CI lanes.
- `tests/unit/docs/test_deleted_ci_script_references.py:12` excludes
  `docs/plans/` from its active-reference scan;
  `tests/unit/docs/test_agent_docs_privacy.py` scans every tracked `*.md`, so
  this file is checked once staged.

---

## 4. AKS facts `[doc]`

### 4.1 Azure Files NFS 4.1 through the azurefile CSI driver

Sources: `https://learn.microsoft.com/en-us/azure/aks/azure-files-csi`
(ms.date 2026-08-03);
`https://github.com/kubernetes-sigs/azurefile-csi-driver/blob/master/docs/driver-parameters.md`
(fetched 2026-09-13); `https://learn.microsoft.com/en-us/azure/storage/files/files-nfs-protocol`
(ms.date 2026-07-08).

- StorageClass: `provisioner: file.csi.azure.com`; `parameters.protocol: nfs`;
  `parameters.skuName: Premium_LRS` — "NFS file share only supports Premium
  account type"; `parameters.networkEndpointType: privateEndpoint` — "For
  `nfs` protocol, a service endpoint is created by default", a private
  endpoint when set; `parameters.rootSquashType` ∈ `AllSquash`,
  `NoRootSquash`, `RootSquash` — "The default is `NoRootSquash`";
  `parameters.mountPermissions` — "The default is `0777`, if set as `0`,
  driver will not perform `chmod` after mount"; `parameters.encryptInTransit`
  default `false`; `parameters.allowSharedKeyAccess` — "NFS: safe to disable —
  NFS mount does not use the account key".
- Mount options: "The default NFS mount options in this driver are
  `vers=4,minorversion=1,sec=sys`. It is not supported to specify these NFS
  mount options, including `nfsvers`." `nconnect` / `actimeo` remain
  operator-settable `mountOptions` (ACA facts §3.1: `actimeo` 30–60 recommended).
- Ownership: "By configuring `fsGroupChangePolicy: None` in the `parameters`
  of storage class or persistent volume, you can bypass the volume ownership
  setting step". Share-root ownership after creation is `[LIVE]`; the K1 root
  Job makes the answer irrelevant to correctness.
- Protocol page: NFSv4.1 only; Premium `FileStorage`; "you must set up either
  a private endpoint or a service endpoint"; `NoRootSquash` is the share
  default at creation.

K6 binds: StorageClass `elspeth-azurefile-nfs` with `protocol: nfs`,
`skuName: Premium_LRS`, `networkEndpointType: privateEndpoint`,
`rootSquashType: NoRootSquash`, `allowSharedKeyAccess: "false"`,
`reclaimPolicy: Retain`, `allowVolumeExpansion: true`; the PVC keeps
`accessModes: [ReadWriteMany]`.

### 4.2 Key Vault CSI → Kubernetes Secret

Sources: `https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-driver` (ms.date 2026-05-05, re-read 2026-09-14);
`https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-configuration-options`
(ms.date 2025-01-03).

- `SecretProviderClass` `spec.provider: azure`; `spec.secretObjects[]` =
  `{secretName, type, data[]: {objectName, key}}` — "Sync mounted content with
  a Kubernetes secret using the `secretObjects` field"; "Make sure the
  `objectName` in the `secretObjects` field matches the file name of the
  mounted content."
- The synced Secret is materialised by the driver when a pod mounts the CSI
  volume (`csi.driver: secrets-store.csi.k8s.io`,
  `volumeAttributes.secretProviderClass`), so K6 mounts the volume read-only
  at `/mnt/secrets-store` in the Deployment even though the application reads
  the Secret through `envFrom`.
- Autorotation: `az aks addon update --addon azure-keyvault-secrets-provider
  --enable-secret-rotation` (default `rotationPollInterval: 2m`) "updates the
  pod mount and the Kubernetes secret defined in the `secretObjects` field".
- One class may list several `secretObjects`; `elspeth-web-secrets` (runtime
  role URLs + keys) and `elspeth-schema-owner-secrets` (schema-owner URLs) are
  two entries of one class or two classes — K6's choice; both are `[doc]`-valid.

### 4.3 Ingress cookie affinity

| controller | source | annotations |
|---|---|---|
| ingress-nginx (also the AKS application routing add-on, `ingressClassName: webapprouting.kubernetes.azure.com`, `https://learn.microsoft.com/en-us/azure/aks/app-routing`, ms.date 2026-08-03) | `https://kubernetes.github.io/ingress-nginx/user-guide/nginx-configuration/annotations/` (fetched 2026-09-13) | `nginx.ingress.kubernetes.io/affinity: "cookie"`, `affinity-mode` (`balanced` / `persistent`), `session-cookie-name`, `session-cookie-expires`, `session-cookie-max-age`, `session-cookie-change-on-failure`; per-Ingress timeouts `proxy-read-timeout`, `proxy-send-timeout`, `proxy-connect-timeout` (seconds; "Annotation keys and values can only be strings") |
| Application Gateway Ingress Controller | `https://learn.microsoft.com/en-us/azure/application-gateway/ingress-controller-annotations` (ms.date 2026-05-12) | `appgw.ingress.kubernetes.io/cookie-based-affinity: "true"`; `appgw.ingress.kubernetes.io/request-timeout` (seconds) |

K6 defaults to the nginx family (`affinity: "cookie"`,
`session-cookie-name: elspeth-replica`, `proxy-read-timeout` ≥ 3600 for the
WebSocket) and documents AGIC in its README; K7 removes or keeps the
affinity annotations from the same file.

### 4.4 `[LIVE]` residue (needs a subscription; K6 README caveats)

1. Share-root ownership and mode after the CSI driver creates the NFS share.
2. Whether the application routing add-on honours a per-Ingress
   `proxy-read-timeout` above its global default.
3. The Kubernetes minor available in the target region (the base is validated
   on `v1.37.0` in kind).
4. The identity the `SecretProviderClass` authenticates with
   (`useVMManagedIdentity` + `userAssignedIdentityID`, or workload identity)
   — an operator input, never a tracked value.
5. `os.replace` + read-back timing across two AKS nodes under the driver's
   default attribute caching (the kind proof in §2.1 is single-node).
6. Whether the managed `azure-keyvault-secrets-provider` add-on's driver
   carries the Secret-sync RBAC (§4.6: Microsoft Learn documents the sync with
   no switch). The tell is `elspeth-web-secrets` appearing after the first pod
   mounts `elspeth-web-kv`, versus every pod staying in
   `CreateContainerConfigError`.

### 4.5 Azure Standard Load Balancer inbound TCP idle timeout

Sources: `https://learn.microsoft.com/en-us/azure/aks/configure-load-balancer-standard`
(ms.date 2026-09-08);
`https://learn.microsoft.com/en-us/azure/load-balancer/load-balancer-tcp-idle-timeout`
(ms.date 2026-08-05); `https://cloud-provider-azure.sigs.k8s.io/topics/loadbalancer/`
(no page date). All three fetched 2026-09-14, HTTP 200.

- Annotation on a `type: LoadBalancer` Service:
  `service.beta.kubernetes.io/azure-load-balancer-tcp-idle-timeout` — "Specify
  the time in minutes for TCP connection idle timeouts to occur on the load
  balancer. The default and minimum value is 4. The maximum value is 100. The
  value must be an integer." (AKS page; the cloud-provider-azure annotation
  table says the same: "Default and minimum value is 4. Maximum value is 100.
  Must be an integer.")
- Load Balancer page: "Standard Load Balancer supports an idle timeout range
  of 4 minutes to 100 minutes for load-balancing rules and inbound NAT rules.
  … The default setting is 4 minutes for all rule types. If a period of
  inactivity exceeds the timeout value, the TCP or HTTP session between the
  client and your service isn't guaranteed to be maintained."
- Not the outbound timeout: the AKS page states the 30-minute idle timeout
  (`az aks create|update --load-balancer-idle-timeout`) "applies to the load
  balancer outbound rule" and "is separate from the
  `service.beta.kubernetes.io/azure-load-balancer-tcp-idle-timeout` annotation,
  which configures the inbound TCP idle timeout for an individual Kubernetes
  LoadBalancer service and has a default and minimum value of 4 minutes."

K6 binds: the application routing add-on's controller Service is this load
balancer, so the inbound hop cuts an idle connection at 240 s unless the
operator sets the annotation (through `NginxIngressController.spec.loadBalancerAnnotations`).
`ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS` is the minimum idle
timeout across hops, so K6 declares `230` (= 240 − 10) even with
`proxy-read-timeout: "3600"` on the nginx hop; raising the annotation moves
the ceiling with it.

### 4.6 Key Vault provider add-on: Kubernetes Secret sync

Sources: `https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-configuration-options`
(ms.date 2025-01-03);
`https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-driver`
(ms.date 2026-05-05);
`https://learn.microsoft.com/en-us/cli/azure/aks/addon?view=azure-cli-latest` and
`https://learn.microsoft.com/en-us/cli/azure/aks?view=azure-cli-latest`
(ms.date 2026-09-01); upstream `https://github.com/Azure/secrets-store-csi-driver-provider-azure`
branch `master` — `charts/csi-secrets-store-provider-azure/values.yaml`,
`charts/csi-secrets-store-provider-azure/README.md` and
`website/content/en/configurations/sync-with-k8s-secrets.md` (no page date);
`https://github.com/Azure/AKS/issues/4319` (closed 2024-06-30). All fetched
2026-09-14, HTTP 200.

- Microsoft Learn states the feature and the field, and no default or switch
  for the add-on. Driver page, "Features": "Syncs with Kubernetes secrets."
  Configuration page, "Sync mounted content with a Kubernetes secret": "Your
  secrets sync after you start a pod to mount them. When you delete the pods
  that consume the secrets, your Kubernetes secret is also deleted." and "Sync
  mounted content with a Kubernetes secret using the secretObjects field when
  creating a SecretProviderClass to define the desired state of the Kubernetes
  secret". Neither page has a step between enabling the add-on and writing
  `secretObjects`.
- Add-on enablement (driver page): `az aks create --enable-addons
  azure-keyvault-secrets-provider` for a new cluster; `az aks enable-addons
  --addons azure-keyvault-secrets-provider --name <cluster> --resource-group
  <rg>` for an existing one. The CLI reference gives the add-on two options
  only: `--enable-secret-rotation` "Enable secret rotation. Use with
  azure-keyvault-secrets-provider addon." and `--rotation-poll-interval` "Set
  interval of rotation poll. Use with azure-keyvault-secrets-provider addon."
  A case-insensitive `grep -c sync` over the text of both CLI reference pages
  counts 0, while `grep -c secret-rotation` over the same text counts more
  than 0 (the positive control).
- `syncSecret.enabled` is an upstream Helm value, not an add-on setting. Chart
  README: "`secrets-store-csi-driver.syncSecret.enabled` | Enable rbac roles and
  bindings required for syncing to Kubernetes native secrets | `false`"
  (`values.yaml`: `syncSecret:` / `enabled: false`). Upstream sync page: "If the
  driver and provider have been installed using helm, ensure the
  `secrets-store-csi-driver.syncSecret.enabled=true` helm value is set as part
  of install/upgrade. This is required to install the RBAC clusterrole and
  clusterrolebinding required by the CSI driver to sync mounted content as
  Kubernetes secret." Azure/AKS#4319 put `'syncSecret.enabled': 'true'` into the
  add-on profile `config` and found "nothing about syncSecret setting" among the
  driver's arguments; it was closed as answered by a community reply that
  `secretObjects` plus a mounting pod is all that is needed (not a Microsoft
  statement).

K6 binds: the overlay and README enable nothing beyond the add-on itself —
there is no add-on sync flag to set, and the Helm value does not apply to the
managed add-on. That the managed driver carries the sync RBAC is documented
behaviour, not a measurement on a cluster: §4.4 item 6 keeps it as `[LIVE]`
residue, and the runbook's observable is `elspeth-web-secrets` appearing after
the first pod mounts `elspeth-web-kv`.

---

## 5. Slots written by later tasks

### 5.1 Kind lane wall time `[K4]`

See the last row of §2.5. Task K4 Step 6 replaces this paragraph with a table
of the `wall=` figures of two consecutive `scripts/cicd/kubernetes-kind-smoke.sh`
runs (cold, warm) and checks them against the `kubernetes-kind` job's
`timeout-minutes: 60`.

### 5.2 Affinity residuals `[K7]`

Empty until Task K7 Step 5a or 5b. If the no-affinity run fails (Step 5b), K7
records each failing surface here (surface, test, assertion text, owner) and
keeps `sessionAffinity: ClientIP`; if it passes (Step 5a), K7 writes "none —
affinity dropped in <commit subject>", naming the subject of its own flip
commit, because the sha does not exist until that commit is made.

---

## Appendix — commands behind the facts (all on 2026-09-13, repo-relative)

- Pins: `curl -fsSL https://dl.k8s.io/release/stable.txt`;
  `curl -fsSL https://dl.k8s.io/release/v1.37.0/bin/linux/amd64/kubectl.sha256`;
  `curl -fsSL https://api.github.com/repos/kubernetes-sigs/kind/releases/latest | python3 -c '…["tag_name"]…'`;
  `curl -fsSL …/v0.33.0/kind-linux-amd64.sha256sum`; download + `printf '%s  <file>\n' | sha256sum -c -` + `install -m 0755` into `.claude/lanes/k8s/bin`; `kubectl version --client -o json`; `kind version`; negative control with a zeroed digest → `FAILED`, exit 1.
- Images: `docker buildx imagetools inspect <ref> --format '{{.Manifest.Digest}}'` for `busybox:1.37.0` and `postgres:16`; `--raw` for the per-platform children; the kind release body via `…/releases/tags/v0.33.0`.
- Clusterless: `env -i PATH="$PATH" HOME="$(mktemp -d)" kubectl kustomize .claude/lanes/k8s/spike/kustomize-probe` (exit 0); the same env with `kubectl apply --dry-run=client --validate=false -f …/pv.yaml` (exit 1, connection refused).
- Cluster: `time kind create cluster --name elspeth-k0 --config .claude/lanes/k8s/spike/kind-config.yaml --kubeconfig … --wait 120s`; `kubectl apply -f …/rwx.yaml`, `exec rwx-a … echo > a.tmp`, `exec rwx-b … mv a.tmp a.final && cat`, `exec rwx-a … cat`; `apply -f …/rwx-standard.yaml`, `describe pvc rwx-standard`; `apply -f …/owner-before.yaml` → `logs`; `apply -f …/provision-storage.yaml` → `wait --for=condition=complete` → `logs job/…`; `apply -f …/owner-after.yaml` → `logs`; `time docker build --build-arg INSTALL_EXTRAS=all --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" -t elspeth-web-spike:k0 .`; `time kind load docker-image elspeth-web-spike:k0 --name elspeth-k0`; `time kind delete cluster --name elspeth-k0`; `kind get clusters` → `No kind clusters found.`
- Docs: each `[doc]` URL in §4.1–§4.3 fetched 2026-09-13 and the three §4.5 URLs and the §4.6 sources fetched 2026-09-14 (HTTP 200); `ms.date` quoted from the page's `<meta name="ms.date">`; the CSI driver parameter table read from the repository `docs/driver-parameters.md`.
- Tree: `sed -n` for every `path:line` cited; `git rev-parse HEAD` → `072141b754287ea0bed26adcf73c82d318f3da08`.
````

- [ ] **Step 12: Stage the document and run the doc gates that see a new tracked markdown file.**

`test_agent_docs_privacy.py` enumerates `git ls-files "*.md"`, so the file must be staged before the gate can see it; `test_deleted_ci_script_references.py:12` excludes `docs/plans/`.

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && git add docs/plans/2026-09-13-kubernetes-platform-facts.md && git status --short docs/plans/
pytest tests/unit/docs -n 0 > /tmp/k8s-k0-docs.log 2>&1; echo exit=$?; tail -5 /tmp/k8s-k0-docs.log
grep -rn "/home/" docs/plans/2026-09-13-kubernetes-platform-facts.md; echo "home_path_grep_exit=$? (1 means none)"
grep -c "⟨" docs/plans/2026-09-13-kubernetes-platform-facts.md; echo "unfilled_tokens_grep_exit=$? (1 means none)"
```

Expected: `git status --short docs/plans/` shows `A  docs/plans/2026-09-13-kubernetes-platform-facts.md`; `exit=0` with every test in `tests/unit/docs` passed; `home_path_grep_exit=1`; `unfilled_tokens_grep_exit=1` (every `⟨…⟩` token was replaced by a measured value).

- [ ] **Step 13: Commit the facts document by pathspec.**

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N docs/plans/2026-09-13-kubernetes-platform-facts.md
git add -- docs/plans/2026-09-13-kubernetes-platform-facts.md
git status --short
scripts/branch-safety-check.sh --intent commit
git commit -m "docs(kubernetes): platform facts spike for the multi-replica base" -- docs/plans/2026-09-13-kubernetes-platform-facts.md
git show --stat HEAD
```

Expected: `git status --short` shows `A  docs/plans/2026-09-13-kubernetes-platform-facts.md`; the safety check prints no `[FAIL]` line and exits 0 (on a FAIL, stop before the commit); `git show --stat HEAD` ends with `1 file changed`. The lane directory `.claude/lanes/k8s/` stays untracked and unstaged.
