### Task K6: AKS overlay

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: K1. Runs before: K7. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

**Files:**
- Create: `deploy/kubernetes/overlays/aks/kustomization.yaml`
- Create: `deploy/kubernetes/overlays/aks/storageclass-azurefile-nfs.yaml`
- Create: `deploy/kubernetes/overlays/aks/secretproviderclass-web.yaml`
- Create: `deploy/kubernetes/overlays/aks/secretproviderclass-schema-owner.yaml`
- Create: `deploy/kubernetes/overlays/aks/ingress.yaml`
- Create: `deploy/kubernetes/overlays/aks/patch-pvc.yaml`
- Create: `deploy/kubernetes/overlays/aks/patch-deployment.yaml`
- Create: `deploy/kubernetes/overlays/aks/patch-job-schema-init.yaml`
- Create: `deploy/kubernetes/overlays/aks/README.md`
- Test: `tests/unit/deployment/test_kubernetes_bundle.py` (append the overlay section; the file is created by K1)

**Interfaces:**
- Consumes (K1, `deploy/kubernetes/base/`): the rendered base document set — `ConfigMap elspeth-web-config` (a plain resource, not a generator), `PersistentVolumeClaim elspeth-state`, `Service elspeth-web` (port `http`, 8451), `Deployment elspeth-web` (one container named `web`, volume `state` at `/mnt/elspeth`, `envFrom` the runtime Secret `elspeth-web-secrets`), `Job elspeth-provision-storage`, `Job elspeth-schema-init` (one container named `schema-init`; `envFrom` is exactly configMapRef `elspeth-web-config` then secretRef `elspeth-web-secrets`, and the two database URLs `ELSPETH_WEB__SESSION_DB_URL` / `ELSPETH_WEB__LANDSCAPE_URL` come from `env[].valueFrom.secretKeyRef` on `elspeth-schema-owner-secrets`, which Kubernetes resolves ahead of every `envFrom` source — K1's `job-schema-init.yaml` and `test_schema_init_runs_as_the_schema_owner_and_the_web_pod_does_not`; `ttlSecondsAfterFinished: 600`); the two placeholders `elspeth.io/revision: REPLACE_PER_ROLLOUT` and `ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE: REPLACE_PER_ROLLOUT`, which the overlay leaves in place.
- Consumes (K1, `tests/unit/deployment/test_kubernetes_bundle.py`): `REPO_ROOT`, `BASE`, `_require_kubectl(reason: str) -> None`, `_render() -> tuple[dict[str, Any], ...]`, and the module constants `RUNTIME_SECRET_KEYS` (the four runtime keys), `SCHEMA_OWNER_SECRET_KEYS` (the two URL keys) and `REQUIRED_COMPOSER_SETTINGS` (the four composer keys and the values K1's base ConfigMap carries: `COMPOSER_MAX_COMPOSITION_TURNS` `"15"`, `COMPOSER_MAX_DISCOVERY_TURNS` `"10"`, `COMPOSER_TIMEOUT_SECONDS` `"85"`, `COMPOSER_RATE_LIMIT_PER_MINUTE` `"10"`); this task reads them and does not redefine them. K1's `_one(kind: str) -> dict[str, Any]` and `_named(kind: str, name: str) -> dict[str, Any]` take no document set — they always read the BASE render — so this task does not call them on the overlay; it adds docs-taking siblings (Produces).
- Consumes (K0, `docs/plans/2026-09-13-kubernetes-platform-facts.md` §4 AKS facts): already recorded there — the azurefile CSI StorageClass parameter table (`protocol`, `skuName`, `rootSquashType`, `networkEndpointType`; §4.1), the Key Vault CSI `secretObjects` sync-on-mount contract (§4.2) and identity modes (§4.4 item 4), the application-routing add-on ingress class `webapprouting.kubernetes.azure.com` (§4.3), the Azure Standard Load Balancer inbound TCP idle timeout (default and minimum 4 minutes, maximum 100, annotation `service.beta.kubernetes.io/azure-load-balancer-tcp-idle-timeout`; §4.5), and the add-on's Secret sync (§4.6): Microsoft Learn documents `secretObjects` sync for the `azure-keyvault-secrets-provider` add-on with no enablement step, the `az aks` add-on options are only `--enable-secret-rotation` and `--rotation-poll-interval`, and `secrets-store-csi-driver.syncSecret.enabled` (default `false`) is the upstream Helm chart's RBAC switch for self-managed installs, not an add-on setting — so this task enables nothing beyond the add-on itself (`--enable-addons azure-keyvault-secrets-provider`, or `az aks enable-addons --addons azure-keyvault-secrets-provider` on an existing cluster). That the managed driver actually syncs is documented, not measured on a cluster: §4.4 item 6 keeps it as `[LIVE]` residue. The values below were re-read from the Microsoft Learn pages on 2026-09-13 and are the numbers K0 must record; if K0 measures differently, K0's number wins and this task re-derives the ceiling.
- Consumes (HEAD): `WebSettings.composer_transport_idle_ceiling_seconds` (`config.py:319`, derivation rule `:47-68`), `composer_transport_headroom_seconds` (default 30.0, `:68`), the boot validator `composer_timeout_seconds <= ceiling - headroom` (`:1119-1121`), `composer_timeout_seconds`, `composer_max_composition_turns` and `composer_max_discovery_turns` REQUIRED with no default (`:309`, `:306-307`; `elspeth web` at `cli.py:4680-4726` seeds only HOST/PORT/AUTH_PROVIDER, and `settings_from_env` at `:1417` refuses to construct without them), `max_upload_bytes` default 100 MiB (`:440`, enforced at `blobs/routes.py:166,279`); the ACA analogues — Key Vault secret names (`docs/runbooks/azure-container-apps-cold-install.md:249-253`), NFS share `rootSquash: NoRootSquash` (`deploy/azure-container-apps/environment.bicep:327`), mount options `actimeo=30,nconnect=4,noresvport` (`workload.bicep:308`), the two-vault split (`environment.bicep:397-448`).
- Produces: `deploy/kubernetes/overlays/aks/` rendering the base plus exactly `StorageClass elspeth-azurefile-nfs`, `SecretProviderClass elspeth-web-kv`, `SecretProviderClass elspeth-schema-owner-kv`, `Ingress elspeth-web`; the eight cookie-affinity annotation keys K7 Step 5a conditionally comments out — `nginx.ingress.kubernetes.io/affinity`, `affinity-mode` and the six `session-cookie-*` keys (`session-cookie-name` value `elspeth-replica`) — of which `test_aks_ingress_pins_cookie_affinity_websocket_timeouts_and_upload_size` pins five (`affinity`, `affinity-mode`, `session-cookie-name`, `session-cookie-change-on-failure`, `session-cookie-secure`); the test-module names `AKS`, `_render_aks() -> list[dict]`, `_one_of(docs: Iterable[dict], kind: str) -> dict`, `_named_of(docs: Iterable[dict], kind: str, name: str) -> dict`, `_csi_volumes(pod_spec)`, `_kv_object_names(spc)` (K7 reads the overlay's Ingress through `_one_of(_render_aks(), "Ingress")`); the overlay's ConfigMap merge sets exactly two composer settings, each of which changes the rendered value: it adds `ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS=230` (the base does not set it, so without the merge the `WebSettings` default applies) and raises `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS` from the base's `85` to `180`. K1's base ConfigMap already carries all four required composer settings (`config.py:306`, `:307`, `:309`, `:338`), so the turn counts (`15`, `10`) and the rate limit (`10`) reach the AKS pods from the base unchanged and the merge does not restate them: a literal equal to the base value changes nothing and would only be a second copy to drift. K8's runbook documents the site overlay that replaces the `REPLACE_WITH_*` placeholders and the per-rollout stamp.
- Ordering: after K1 (needs the base and the K1 test helpers); in parallel with K2–K5; K7 Step 5a (the conditional ingress edit) waits on this task. No `.github/workflows/ci.yaml` edit: K3's `kubernetes-render` job runs the whole `test_kubernetes_bundle.py`, so the overlay render is CI-gated the moment K3 lands. Server admission of the overlay is NOT proven anywhere in this plan (no AKS cluster in CI, and a client-side apply dry run is not clusterless — it needs an API server, which K0 measures and which is why K3's render gate carries no dry-run step); the structural render is the gate.

Design decisions this task fixes (each is checked by a test in Step 1):

1. **Two SecretProviderClasses, not one.** A Key Vault CSI mount materialises every object in its class as a file inside the mounting pod, and the synced Kubernetes Secret only comes into existence when some pod mounts the class. The base (K1) gives the web pod `elspeth-web-secrets` only and `job-schema-init` both Secrets, and the Global Constraint says no manifest gives the web pod the schema-owner credentials. So: `elspeth-web-kv` (runtime vault, four runtime keys → `elspeth-web-secrets`) is mounted by the Deployment and by the Job; `elspeth-schema-owner-kv` (schema-owner vault, two URL keys → `elspeth-schema-owner-secrets`) is mounted by the Job only. The two classes name different vaults, mirroring ACA's separate runtime and schema-owner vaults.
2. **Identity mode = the add-on's user-assigned managed identity** (`useVMManagedIdentity: "true"`, `userAssignedIdentityID: <client id of azurekeyvaultsecretsprovider-<cluster>>`): no ServiceAccount, no pod label, so the overlay adds exactly four kinds. The alternative (workload identity: a ServiceAccount per role, `azure.workload.identity/use: "true"` on the pod, `clientID:` in the class) reproduces ACA's two-identity split and is an operator decision recorded in the README (see open questions).
3. **The transport ceiling is derived from the binding hop, not from nginx.** The application-routing add-on's controller Service is an Azure Standard Load Balancer whose inbound TCP idle timeout defaults to 4 minutes (annotation `service.beta.kubernetes.io/azure-load-balancer-tcp-idle-timeout`, default and minimum 4, maximum 100). `config.py:47-68` requires the MINIMUM across hops, so the overlay declares `230` (= 240 − 10, the same margin ACA's acceptance uses at 210 ≤ 240) even though `proxy-read-timeout` is 3600, and raises `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS` from the base's 85 to 180 beside it (`180 <= 230 - 30`, the `config.py:1119-1128` validator). The two REQUIRED turn counts (`config.py:306-307`) and the REQUIRED rate limit (`:338`) come from K1's base ConfigMap unchanged (15, 10, 10), so the merge carries only the ceiling and the timeout; with 180 s and the base's 15 + 10 turns the turn-budget disclosure at `config.py:1144-1153` logs that 25 configured turns exceed the 12 fundable ones — a WARNING by design (`cli.py:180-186`), not a refusal, and the same disclosure the compose deployment makes at 85 s. Raising the LB timeout via `NginxIngressController.spec.loadBalancerAnnotations` moves BOTH numbers; the README says so.
4. **ingress-nginx defaults that would break production are pinned:** `proxy-body-size` (default 1m) is set to `100m` to match `max_upload_bytes`; `affinity-mode: persistent` (the default `balanced` re-shards cookies on scale events, which defeats the sticky-until-K7 contract); `proxy-read-timeout`/`proxy-send-timeout` 3600 so an idle run WebSocket survives the nginx hop (the LB hop is the one that cuts it, item 3). The base Service's `sessionAffinity: ClientIP` is inert behind ingress-nginx, which proxies to pod endpoints rather than through the ClusterIP; the cookie is what keeps a browser on one replica.
5. **StorageClass mirrors the ACA storage decisions:** `protocol: nfs`, `skuName: Premium_LRS` (NFS needs a Premium FileStorage account; the AKS page also lists `PremiumV2_LRS`), `rootSquashType: NoRootSquash` (the K1 root provision Job chowns the share subtree), `networkEndpointType: privateEndpoint`, `reclaimPolicy: Retain` (this share is the blob store), `allowVolumeExpansion: true`, `mountOptions` `actimeo=30,nconnect=4,noresvport`.

- [ ] **Step 1: Write the failing overlay tests.**

Append to `tests/unit/deployment/test_kubernetes_bundle.py` (K1's module; add `from collections.abc import Iterable` and `from elspeth.web.config import WebSettings` to its imports — `test_web_settings_exports_resolve.py:32` already imports it in this directory). The helpers `REPO_ROOT`, `BASE`, `_require_kubectl`, `_render` and the constants `RUNTIME_SECRET_KEYS`, `SCHEMA_OWNER_SECRET_KEYS`, `REQUIRED_COMPOSER_SETTINGS` are K1's and are not redefined; the tests below read K1's names. K1's `_one(kind)` / `_named(kind, name)` always read the base render, so the overlay tests use the docs-taking `_one_of(docs, kind)` / `_named_of(docs, kind, name)` defined below; K1's two helpers are left untouched.

```python
# --- AKS overlay (Task K6) --------------------------------------------------

AKS = REPO_ROOT / "deploy" / "kubernetes" / "overlays" / "aks"
AKS_README = AKS / "README.md"
# Azure Standard Load Balancer inbound TCP idle timeout: default and minimum
# 4 minutes (service.beta.kubernetes.io/azure-load-balancer-tcp-idle-timeout);
# recorded in docs/plans/2026-09-13-kubernetes-platform-facts.md §4 (K0).
AZURE_LB_DEFAULT_IDLE_TIMEOUT_SECONDS = 240
# Read from the live field set, never retyped: the validator at config.py:1119
# subtracts this headroom, and blobs/routes.py:166 enforces this upload cap.
COMPOSER_TRANSPORT_HEADROOM_SECONDS = float(WebSettings.model_fields["composer_transport_headroom_seconds"].default)
MAX_UPLOAD_BYTES = int(WebSettings.model_fields["max_upload_bytes"].default)
# RUNTIME_SECRET_KEYS, SCHEMA_OWNER_SECRET_KEYS and REQUIRED_COMPOSER_SETTINGS are K1's (defined above in this module).


def _render_aks() -> list[dict]:
    _require_kubectl("the AKS overlay cannot be rendered")
    result = subprocess.run(["kubectl", "kustomize", str(AKS)], check=True, capture_output=True, text=True)
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _one_of(docs: Iterable[dict], kind: str) -> dict:
    """The single document of ``kind`` in ``docs`` (K1's ``_one`` reads only the base render)."""
    matches = [doc for doc in docs if doc["kind"] == kind]
    assert len(matches) == 1, f"expected exactly one {kind}, found {len(matches)}"
    return matches[0]


def _named_of(docs: Iterable[dict], kind: str, name: str) -> dict:
    """The single ``kind``/``name`` document in ``docs`` (K1's ``_named`` reads only the base render)."""
    matches = [doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name]
    assert len(matches) == 1, f"expected exactly one {kind}/{name}, found {len(matches)}"
    return matches[0]


def _csi_volumes(pod_spec: dict) -> dict[str, str]:
    """volume name -> SecretProviderClass for every secrets-store CSI volume of a pod spec."""
    return {
        v["name"]: v["csi"]["volumeAttributes"]["secretProviderClass"]
        for v in pod_spec["volumes"]
        if "csi" in v
    }


def _kv_object_names(spc: dict) -> set[str]:
    """The Key Vault object names a SecretProviderClass fetches (its `objects` block is YAML-in-YAML)."""
    objects = yaml.safe_load(spc["spec"]["parameters"]["objects"])["array"]
    return {yaml.safe_load(entry)["objectName"] for entry in objects}


def test_aks_overlay_renders_the_base_plus_exactly_the_azure_resources() -> None:
    rendered = sorted((d["kind"], d["metadata"]["name"]) for d in _render_aks())
    base = sorted((d["kind"], d["metadata"]["name"]) for d in _render())
    assert rendered == sorted(
        base
        + [
            ("StorageClass", "elspeth-azurefile-nfs"),
            ("SecretProviderClass", "elspeth-web-kv"),
            ("SecretProviderClass", "elspeth-schema-owner-kv"),
            ("Ingress", "elspeth-web"),
        ]
    )


def test_aks_storage_is_azure_files_nfs_with_rwx_and_the_aca_mount_contract() -> None:
    docs = _render_aks()
    sc = _one_of(docs, "StorageClass")
    assert sc["provisioner"] == "file.csi.azure.com"
    assert sc["parameters"]["protocol"] == "nfs"
    assert sc["parameters"]["skuName"] == "Premium_LRS"
    # The K1 provision-storage Job chowns the share root as UID 0; root squash would turn that into nobody.
    assert sc["parameters"]["rootSquashType"] == "NoRootSquash"
    assert sc["parameters"]["networkEndpointType"] == "privateEndpoint"
    assert sc["reclaimPolicy"] == "Retain"
    assert sc["allowVolumeExpansion"] is True
    assert sc["mountOptions"] == ["actimeo=30", "nconnect=4", "noresvport"]
    pvc = _one_of(docs, "PersistentVolumeClaim")
    assert pvc["spec"]["storageClassName"] == sc["metadata"]["name"]
    assert pvc["spec"]["accessModes"] == ["ReadWriteMany"]


def test_aks_runtime_secret_syncs_from_key_vault_under_the_base_secret_name() -> None:
    docs = _render_aks()
    spc = _named_of(docs, "SecretProviderClass", "elspeth-web-kv")
    assert spc["spec"]["provider"] == "azure"
    (synced,) = spc["spec"]["secretObjects"]
    assert synced["secretName"] == "elspeth-web-secrets"
    assert synced["type"] == "Opaque"
    assert {d["key"] for d in synced["data"]} == RUNTIME_SECRET_KEYS
    # Every synced key is fetched, and nothing fetched is a schema-owner object.
    assert {d["objectName"] for d in synced["data"]} == _kv_object_names(spc)
    assert not any(name.endswith("-schema-owner") for name in _kv_object_names(spc))
    assert spc["spec"]["parameters"]["usePodIdentity"] == "false"
    assert spc["spec"]["parameters"]["useVMManagedIdentity"] == "true"


def test_aks_schema_owner_secret_syncs_from_its_own_vault_class() -> None:
    docs = _render_aks()
    spc = _named_of(docs, "SecretProviderClass", "elspeth-schema-owner-kv")
    (synced,) = spc["spec"]["secretObjects"]
    assert synced["secretName"] == "elspeth-schema-owner-secrets"
    assert {d["key"] for d in synced["data"]} == SCHEMA_OWNER_SECRET_KEYS
    assert _kv_object_names(spc) == {"elspeth-session-db-url-schema-owner", "elspeth-landscape-url-schema-owner"}
    runtime = _named_of(docs, "SecretProviderClass", "elspeth-web-kv")
    # Two vaults, mirroring ACA (environment.bicep:397-448): a runtime vault-wide read never reaches the schema owner.
    assert spc["spec"]["parameters"]["keyvaultName"] != runtime["spec"]["parameters"]["keyvaultName"]


def test_aks_web_pod_mounts_only_the_runtime_vault_class() -> None:
    docs = _render_aks()
    pod = _one_of(docs, "Deployment")["spec"]["template"]["spec"]
    (web,) = pod["containers"]  # a patch whose container name is not `web` ADDS a container under strategic merge
    assert web["name"] == "web"
    assert _csi_volumes(pod) == {"kv-runtime": "elspeth-web-kv"}
    mounts = {m["name"]: m for m in web["volumeMounts"]}
    assert mounts["kv-runtime"]["mountPath"] == "/mnt/secrets-store" and mounts["kv-runtime"]["readOnly"] is True
    assert mounts["state"]["mountPath"] == "/mnt/elspeth"
    assert "elspeth-schema-owner" not in yaml.safe_dump(pod)


def test_aks_schema_init_job_mounts_both_vault_classes() -> None:
    docs = _render_aks()
    pod = _named_of(docs, "Job", "elspeth-schema-init")["spec"]["template"]["spec"]
    (init,) = pod["containers"]
    assert init["name"] == "schema-init"
    assert _csi_volumes(pod) == {"kv-runtime": "elspeth-web-kv", "kv-schema-owner": "elspeth-schema-owner-kv"}
    assert {m["name"] for m in init["volumeMounts"]} == {"state", "kv-runtime", "kv-schema-owner"}
    # K1's Secret wiring survives the patch: envFrom mounts only the runtime Secret,
    # and the two database URLs come from the schema-owner Secret through `env`
    # secretKeyRefs, which Kubernetes resolves ahead of every envFrom source.
    assert [ref["secretRef"]["name"] for ref in init["envFrom"] if "secretRef" in ref] == ["elspeth-web-secrets"]
    overrides = {entry["name"]: entry["valueFrom"]["secretKeyRef"]["name"] for entry in init["env"] if "valueFrom" in entry}
    assert overrides == {key: "elspeth-schema-owner-secrets" for key in SCHEMA_OWNER_SECRET_KEYS}
    provision = _named_of(docs, "Job", "elspeth-provision-storage")["spec"]["template"]["spec"]
    assert _csi_volumes(provision) == {}


def test_aks_ingress_pins_cookie_affinity_websocket_timeouts_and_upload_size() -> None:
    ing = _one_of(_render_aks(), "Ingress")
    ann = ing["metadata"]["annotations"]
    # K7 Step 5a comments out all eight cookie-affinity keys only if routing without affinity qualifies.
    assert ann["nginx.ingress.kubernetes.io/affinity"] == "cookie"
    assert ann["nginx.ingress.kubernetes.io/affinity-mode"] == "persistent"
    assert ann["nginx.ingress.kubernetes.io/session-cookie-name"] == "elspeth-replica"
    assert ann["nginx.ingress.kubernetes.io/session-cookie-change-on-failure"] == "true"
    assert ann["nginx.ingress.kubernetes.io/session-cookie-secure"] == "true"
    assert int(ann["nginx.ingress.kubernetes.io/proxy-read-timeout"]) >= 3600
    assert int(ann["nginx.ingress.kubernetes.io/proxy-send-timeout"]) >= 3600
    # ingress-nginx defaults proxy-body-size to 1m; the app accepts max_upload_bytes.
    assert ann["nginx.ingress.kubernetes.io/proxy-body-size"] == f"{MAX_UPLOAD_BYTES // (1024 * 1024)}m"
    assert ing["spec"]["ingressClassName"] == "webapprouting.kubernetes.azure.com"
    (rule,) = ing["spec"]["rules"]
    (path,) = rule["http"]["paths"]
    assert path["backend"]["service"] == {"name": "elspeth-web", "port": {"name": "http"}}
    (tls,) = ing["spec"]["tls"]
    assert tls["hosts"] == [rule["host"]] and tls["secretName"] == "elspeth-web-tls"


def test_aks_transport_ceiling_is_the_minimum_hop_and_leaves_composer_headroom() -> None:
    docs = _render_aks()
    cm = _named_of(docs, "ConfigMap", "elspeth-web-config")["data"]
    ann = _one_of(docs, "Ingress")["metadata"]["annotations"]
    ceiling = float(cm["ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS"])
    timeout = float(cm["ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS"])
    binding_hop = min(AZURE_LB_DEFAULT_IDLE_TIMEOUT_SECONDS, int(ann["nginx.ingress.kubernetes.io/proxy-read-timeout"]))
    assert ceiling <= binding_hop - 10
    assert timeout <= ceiling - COMPOSER_TRANSPORT_HEADROOM_SECONDS
    # The merge exists because it changes a value: the base carries timeout 85 (K1), the overlay needs its own.
    assert cm["ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS"] != REQUIRED_COMPOSER_SETTINGS["ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS"]
    # The other three REQUIRED composer settings (config.py:306, :307, :338) come from the base ConfigMap unchanged.
    inherited = {k: v for k, v in REQUIRED_COMPOSER_SETTINGS.items() if k != "ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS"}
    assert {k: cm.get(k) for k in inherited} == inherited
    # The merge set the ceiling and the timeout and changed nothing else the base pins.
    assert cm["ELSPETH_WEB__DEPLOYMENT_TARGET"] == "kubernetes"
    assert cm["ELSPETH_WEB__DEPLOYMENT_STATE_MODE"] == "external-postgresql"
    assert cm["ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE"] == "REPLACE_PER_ROLLOUT"
    dep = _one_of(docs, "Deployment")
    assert dep["spec"]["template"]["metadata"]["annotations"]["elspeth.io/revision"] == "REPLACE_PER_ROLLOUT"
    assert dep["spec"]["replicas"] == 2


def test_aks_readme_names_the_binding_hop_and_the_lever_that_moves_it() -> None:
    readme = AKS_README.read_text(encoding="utf-8")
    assert "service.beta.kubernetes.io/azure-load-balancer-tcp-idle-timeout" in readme
    assert "loadBalancerAnnotations" in readme
    assert "COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" in readme and "COMPOSER_TIMEOUT_SECONDS" in readme
    assert "azure.workload.identity/use" in readme  # the recorded alternative to the add-on identity
```

- [ ] **Step 2: Run the new tests and watch them fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && PATH=.claude/lanes/k8s/bin:$PATH pytest tests/unit/deployment/test_kubernetes_bundle.py -n 0 -k aks -v > /tmp/k6-step2.log 2>&1; echo exit=$?; tail -30 /tmp/k6-step2.log`
Expected: with the K0 kubectl on PATH, 9 FAILED — the eight render tests with `subprocess.CalledProcessError` from `kubectl kustomize` whose stderr reads `Error: must build at directory: not a valid directory: evalsymlink failure on '<repo>/deploy/kubernetes/overlays/aks'` (measured with kubectl v1.37.0), and the README test with `FileNotFoundError`. Without kubectl the eight render tests SKIP and only the README test fails — install the K0 pin into `.claude/lanes/k8s/bin` and prepend it to `PATH` first (the smoke script prelude from K4 does the same download-and-verify).

- [ ] **Step 3: Write the overlay.**

```yaml
# deploy/kubernetes/overlays/aks/kustomization.yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base
  - storageclass-azurefile-nfs.yaml
  - secretproviderclass-web.yaml
  - secretproviderclass-schema-owner.yaml
  - ingress.yaml
patches:
  - path: patch-pvc.yaml
  - path: patch-deployment.yaml
  - path: patch-job-schema-init.yaml
configMapGenerator:
  # Merge into the base's plain ConfigMap. The name suffix hash is disabled so
  # the two Jobs and the Deployment keep referencing `elspeth-web-config` and
  # the base contract test can still find the map by name.
  - name: elspeth-web-config
    behavior: merge
    options:
      disableNameSuffixHash: true
    literals:
      # The composer transport ceiling is the MINIMUM idle timeout of every hop
      # in front of the pod (config.py:47-68). Behind the application-routing
      # add-on that hop is the Azure Standard Load Balancer in front of
      # ingress-nginx: inbound TCP idle timeout default and minimum 4 minutes
      # (service.beta.kubernetes.io/azure-load-balancer-tcp-idle-timeout),
      # NOT the nginx proxy-read-timeout in ingress.yaml. 230 = 240 - 10.
      # Raise the LB timeout on the NginxIngressController (README) and move
      # BOTH numbers together: timeout <= ceiling - 30 (config.py:1119).
      - ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS=230
      # Raises the base's 85. The base ConfigMap already carries the other three
      # settings WebSettings requires with no default (the two turn counts and
      # the composer rate limit, config.py:306, :307, :338) at the values this
      # overlay wants, so they are not restated here: a literal equal to the
      # base value would change nothing and only drift.
      - ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS=180
```

```yaml
# deploy/kubernetes/overlays/aks/storageclass-azurefile-nfs.yaml
# One NFS 4.1 Azure Files share for data/, data/blobs and payloads/ on every
# replica and every Job (Phase 6b §4 row 3). Parameters mirror the ACA bundle:
# Premium FileStorage, private endpoint, NoRootSquash (environment.bicep:327),
# and the mount options workload.bicep:308 uses. SMB is not supported.
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: elspeth-azurefile-nfs
provisioner: file.csi.azure.com
parameters:
  protocol: nfs
  skuName: Premium_LRS
  # The K1 provision-storage Job chowns the share subtree as UID 0; root squash
  # would map that to nobody and the web pod (UID 1654) could never write.
  rootSquashType: NoRootSquash
  networkEndpointType: privateEndpoint
# The share is the blob store: deleting the claim must never delete the bytes.
reclaimPolicy: Retain
allowVolumeExpansion: true
volumeBindingMode: Immediate
mountOptions:
  - actimeo=30
  - nconnect=4
  - noresvport
```

```yaml
# deploy/kubernetes/overlays/aks/patch-pvc.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: elspeth-state
spec:
  storageClassName: elspeth-azurefile-nfs
```

```yaml
# deploy/kubernetes/overlays/aks/secretproviderclass-web.yaml
# Runtime credentials only. The Key Vault CSI driver syncs these objects into
# the Kubernetes Secret `elspeth-web-secrets` (the name the base Deployment
# and job-schema-init reference) the first time a pod mounts this class; the
# web Deployment mounts it (patch-deployment.yaml). Object names are the ones
# the ACA runbook creates (azure-container-apps-cold-install.md §4).
apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: elspeth-web-kv
spec:
  provider: azure
  secretObjects:
    - secretName: elspeth-web-secrets
      type: Opaque
      data:
        - objectName: elspeth-session-db-url-runtime
          key: ELSPETH_WEB__SESSION_DB_URL
        - objectName: elspeth-landscape-url-runtime
          key: ELSPETH_WEB__LANDSCAPE_URL
        - objectName: elspeth-secret-key
          key: ELSPETH_WEB__SECRET_KEY
        - objectName: elspeth-shareable-link-signing-key
          key: ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY
  parameters:
    usePodIdentity: "false"
    # The add-on's user-assigned identity (azurekeyvaultsecretsprovider-<cluster>):
    # az aks show --resource-group <rg> --name <cluster> --query addonProfiles.azureKeyvaultSecretsProvider.identity.clientId -o tsv
    # It needs "Key Vault Secrets User" on the RUNTIME vault. See README for the
    # workload-identity alternative.
    useVMManagedIdentity: "true"
    userAssignedIdentityID: REPLACE_WITH_KEYVAULT_PROVIDER_IDENTITY_CLIENT_ID
    keyvaultName: REPLACE_WITH_RUNTIME_KEY_VAULT_NAME
    tenantId: REPLACE_WITH_TENANT_ID
    objects: |
      array:
        - |
          objectName: elspeth-session-db-url-runtime
          objectType: secret
        - |
          objectName: elspeth-landscape-url-runtime
          objectType: secret
        - |
          objectName: elspeth-secret-key
          objectType: secret
        - |
          objectName: elspeth-shareable-link-signing-key
          objectType: secret
```

```yaml
# deploy/kubernetes/overlays/aks/secretproviderclass-schema-owner.yaml
# Schema-owner credentials, from their OWN vault (ACA keeps them in a separate
# vault so a runtime vault-wide read never reaches them: environment.bicep:445).
# Only job-schema-init mounts this class (patch-job-schema-init.yaml); the web
# Deployment never does, so the schema-owner URLs are never on a web pod.
apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: elspeth-schema-owner-kv
spec:
  provider: azure
  secretObjects:
    - secretName: elspeth-schema-owner-secrets
      type: Opaque
      data:
        - objectName: elspeth-session-db-url-schema-owner
          key: ELSPETH_WEB__SESSION_DB_URL
        - objectName: elspeth-landscape-url-schema-owner
          key: ELSPETH_WEB__LANDSCAPE_URL
  parameters:
    usePodIdentity: "false"
    # The same add-on identity; it needs "Key Vault Secrets User" on the
    # SCHEMA-OWNER vault as well (that is the one-identity trade-off the README
    # records; workload identity gives each role its own identity).
    useVMManagedIdentity: "true"
    userAssignedIdentityID: REPLACE_WITH_KEYVAULT_PROVIDER_IDENTITY_CLIENT_ID
    keyvaultName: REPLACE_WITH_SCHEMA_OWNER_KEY_VAULT_NAME
    tenantId: REPLACE_WITH_TENANT_ID
    objects: |
      array:
        - |
          objectName: elspeth-session-db-url-schema-owner
          objectType: secret
        - |
          objectName: elspeth-landscape-url-schema-owner
          objectType: secret
```

```yaml
# deploy/kubernetes/overlays/aks/patch-deployment.yaml
# Strategic merge: the container is matched by name (`web`, K1), so the mount
# is ADDED to the existing container rather than a second container appearing.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: elspeth-web
spec:
  template:
    spec:
      containers:
        - name: web
          volumeMounts:
            # The mount is what triggers the Key Vault -> Secret sync; the
            # process reads the Secret through envFrom, not these files.
            - name: kv-runtime
              mountPath: /mnt/secrets-store
              readOnly: true
      volumes:
        - name: kv-runtime
          csi:
            driver: secrets-store.csi.k8s.io
            readOnly: true
            volumeAttributes:
              secretProviderClass: elspeth-web-kv
```

```yaml
# deploy/kubernetes/overlays/aks/patch-job-schema-init.yaml
# job-schema-init reads both Secrets (K1: envFrom configMap then elspeth-web-secrets,
# plus `env` secretKeyRefs on elspeth-schema-owner-secrets for the two database
# URLs, which beat every envFrom source), so it must mount BOTH classes: each
# Secret is synced only by a pod that mounts its class. This patch touches only
# volumeMounts and volumes; K1's envFrom and env lists are left as they are.
# Job templates are immutable; K1's ttlSecondsAfterFinished: 600 lets a redeploy
# recreate this Job. The provision-storage Job needs no secret and is untouched.
apiVersion: batch/v1
kind: Job
metadata:
  name: elspeth-schema-init
spec:
  template:
    spec:
      containers:
        - name: schema-init
          volumeMounts:
            - name: kv-runtime
              mountPath: /mnt/secrets-store/runtime
              readOnly: true
            - name: kv-schema-owner
              mountPath: /mnt/secrets-store/schema-owner
              readOnly: true
      volumes:
        - name: kv-runtime
          csi:
            driver: secrets-store.csi.k8s.io
            readOnly: true
            volumeAttributes:
              secretProviderClass: elspeth-web-kv
        - name: kv-schema-owner
          csi:
            driver: secrets-store.csi.k8s.io
            readOnly: true
            volumeAttributes:
              secretProviderClass: elspeth-schema-owner-kv
```

```yaml
# deploy/kubernetes/overlays/aks/ingress.yaml
# ingress-nginx as managed by the AKS application-routing add-on (ingress class
# webapprouting.kubernetes.azure.com). Session affinity stays ON until Task K7
# qualifies routing without it: the cookie, not the base Service's
# sessionAffinity: ClientIP, is what pins a browser to one replica here, because
# ingress-nginx proxies to pod endpoints and never passes through the ClusterIP.
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: elspeth-web
  annotations:
    nginx.ingress.kubernetes.io/affinity: cookie
    # `balanced` (the default) re-shards cookies when the Deployment scales;
    # `persistent` keeps an issued cookie on its replica until that pod goes.
    nginx.ingress.kubernetes.io/affinity-mode: persistent
    nginx.ingress.kubernetes.io/session-cookie-name: elspeth-replica
    nginx.ingress.kubernetes.io/session-cookie-max-age: "86400"
    nginx.ingress.kubernetes.io/session-cookie-expires: "86400"
    nginx.ingress.kubernetes.io/session-cookie-change-on-failure: "true"
    nginx.ingress.kubernetes.io/session-cookie-secure: "true"
    nginx.ingress.kubernetes.io/session-cookie-samesite: Lax
    # WebSocket idle time on the nginx hop (run progress at /ws/runs/{run_id}).
    # The Azure LB in front of the controller cuts idle TCP at 4 minutes by
    # default; that hop, not these, sets the composer ceiling (kustomization.yaml).
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    # ingress-nginx defaults to 1m; WebSettings.max_upload_bytes is 100 MiB.
    nginx.ingress.kubernetes.io/proxy-body-size: 100m
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
spec:
  ingressClassName: webapprouting.kubernetes.azure.com
  tls:
    - hosts:
        - elspeth.example.com
      # Provisioned outside this overlay: cert-manager, or a Key Vault
      # certificate synced by a third SecretProviderClass (README).
      secretName: elspeth-web-tls
  rules:
    - host: elspeth.example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: elspeth-web
                port:
                  name: http
```

````markdown
# ELSPETH on AKS — Kustomize overlay

Layers Azure specifics on the provider-neutral base in [`../../base`](../../base):
an Azure Files **NFS 4.1** StorageClass for the shared `/mnt/elspeth` volume,
two Key Vault CSI `SecretProviderClass` objects that sync the base's two
Secrets, an ingress-nginx `Ingress` with cookie affinity, and the composer
transport pair derived for this platform. The databases stay external: Azure
Database for PostgreSQL Flexible Server, two databases, two roles
([`../../base/bootstrap-roles.sql`](../../base/bootstrap-roles.sql)). The
operator procedure is [`docs/runbooks/kubernetes-deployment.md`](../../../../docs/runbooks/kubernetes-deployment.md);
the platform literals below are measured in
[`docs/plans/2026-09-13-kubernetes-platform-facts.md`](../../../../docs/plans/2026-09-13-kubernetes-platform-facts.md) §4.

## What the cluster must already have

| dependency | why |
|---|---|
| AKS with the **application routing add-on** (`az aks approuting enable`) | provides the managed ingress-nginx and the ingress class `webapprouting.kubernetes.azure.com` used by `ingress.yaml` |
| AKS **Azure Key Vault provider for Secrets Store CSI Driver** add-on (`--enable-addons azure-keyvault-secrets-provider` on `az aks create`, or `az aks enable-addons --addons azure-keyvault-secrets-provider` on an existing cluster) | syncs Key Vault objects into `elspeth-web-secrets` and `elspeth-schema-owner-secrets`; the add-on has no separate sync switch (platform facts §4.6) |
| Azure Files CSI driver (built in) and a VNet the storage account's private endpoint can reach | `storageclass-azurefile-nfs.yaml` creates a Premium FileStorage account with an NFS share behind a private endpoint |
| Flexible Server with `elspeth_sessions` and `elspeth_landscape`, roles `elspeth_schema_owner` and `elspeth_runtime` | the base's external-PostgreSQL contract; no SQLite mode exists at replicas > 1 |
| Two Key Vaults — runtime and schema-owner — holding `elspeth-session-db-url-runtime`, `elspeth-landscape-url-runtime`, `elspeth-secret-key`, `elspeth-shareable-link-signing-key` (runtime vault) and `elspeth-session-db-url-schema-owner`, `elspeth-landscape-url-schema-owner` (schema-owner vault) | the same names the ACA runbook creates; the CSI identity needs **Key Vault Secrets User** on both vaults |
| A TLS Secret `elspeth-web-tls` for the ingress host | from cert-manager, or a Key Vault certificate synced by an additional `SecretProviderClass` with `type: kubernetes.io/tls` |

## Files

| file | adds |
|---|---|
| `storageclass-azurefile-nfs.yaml` | `StorageClass elspeth-azurefile-nfs`: `file.csi.azure.com`, `protocol: nfs`, `skuName: Premium_LRS`, `rootSquashType: NoRootSquash`, `networkEndpointType: privateEndpoint`, `reclaimPolicy: Retain`, mount options `actimeo=30,nconnect=4,noresvport` (the ACA bundle's) |
| `patch-pvc.yaml` | binds the base claim `elspeth-state` (ReadWriteMany) to that class |
| `secretproviderclass-web.yaml` | `elspeth-web-kv` → Secret `elspeth-web-secrets` (four runtime keys); mounted by the Deployment and by `job-schema-init` |
| `secretproviderclass-schema-owner.yaml` | `elspeth-schema-owner-kv` → Secret `elspeth-schema-owner-secrets` (two URL keys); mounted by `job-schema-init` ONLY |
| `patch-deployment.yaml` | the CSI mount that triggers the runtime sync on the `web` container |
| `patch-job-schema-init.yaml` | both CSI mounts on the `schema-init` container |
| `ingress.yaml` | `Ingress elspeth-web`: cookie affinity (`elspeth-replica`, `persistent`), 3600 s proxy read/send timeouts, `proxy-body-size: 100m`, TLS, host `elspeth.example.com` |
| `kustomization.yaml` | the ConfigMap merge that adds `ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS=230` and raises the base's `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS` from 85 to 180; the turn budgets and the composer rate limit come from the base ConfigMap unchanged |

The base's per-rollout placeholders (`elspeth.io/revision: REPLACE_PER_ROLLOUT`
and `ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE: REPLACE_PER_ROLLOUT`) are left in
place. The runbook's site overlay (`resources: [<path to this directory>]` plus
patches) replaces them and the `REPLACE_WITH_*` values below on every rollout;
do not edit this tracked overlay per site.

| placeholder | where | value |
|---|---|---|
| `REPLACE_WITH_KEYVAULT_PROVIDER_IDENTITY_CLIENT_ID` | both SecretProviderClass files | `az aks show -g <rg> -n <cluster> --query addonProfiles.azureKeyvaultSecretsProvider.identity.clientId -o tsv` |
| `REPLACE_WITH_RUNTIME_KEY_VAULT_NAME` / `REPLACE_WITH_SCHEMA_OWNER_KEY_VAULT_NAME` | one each | the two vault names |
| `REPLACE_WITH_TENANT_ID` | both | `az keyvault show -n <vault> --query properties.tenantId -o tsv` |
| `elspeth.example.com` | `ingress.yaml` (rule host and TLS host) | the public host |

## Secrets: why two classes and why the mount matters

The Key Vault CSI driver creates the synced Kubernetes Secret only when a pod
mounts the class, and every object in a mounted class appears as a file in
that pod. The base gives the web pod `elspeth-web-secrets` alone and gives
`job-schema-init` both Secrets (the schema-owner URLs through `env`
secretKeyRefs, which win over `envFrom`), and
no manifest may give a web pod the schema-owner credentials. Hence the split:
the Deployment mounts `elspeth-web-kv` only; the Job mounts both. The process
never reads `/mnt/secrets-store`; it reads the Secrets through `envFrom` and `env`.

Three consequences of sync-on-mount:

- The add-on needs no separate sync switch: Microsoft Learn documents
  `secretObjects` sync with no enablement step, and the `az aks` add-on options
  are only `--enable-secret-rotation` and `--rotation-poll-interval` (platform
  facts §4.6; `syncSecret.enabled` is a Helm value for self-managed installs).
  If no Secret appears after the first pod mounts the class, every pod stays in
  `CreateContainerConfigError`: check the add-on is enabled before anything else.
- On first start a pod can sit in `CreateContainerConfigError` for a few
  seconds: the kubelet resolves `envFrom` before the driver has written the
  Secret, then retries. That is expected, not a failed rollout.
- A synced Secret is deleted when the last pod mounting its class goes away.
  For `elspeth-schema-owner-secrets` that is the point: once the Job's TTL
  removes its pod the schema-owner URLs leave the cluster. For
  `elspeth-web-secrets` it means `kubectl scale --replicas=0` deletes the
  Secret and the next scale-up re-syncs it; a `RollingUpdate` (maxUnavailable
  0) always keeps a mounting pod, so the Secret persists across rollouts.

**Identity.** The overlay uses the add-on's user-assigned managed identity
(`useVMManagedIdentity: "true"` + `userAssignedIdentityID`). It is one identity
for both vaults and is reachable by any pod on the node through IMDS. The
alternative that reproduces ACA's two-identity split is Microsoft Entra
Workload ID: one ServiceAccount per role annotated
`azure.workload.identity/client-id`, the pod label
`azure.workload.identity/use: "true"`, and `clientID:` instead of
`useVMManagedIdentity` in each class. That adds two `ServiceAccount` objects
to this overlay and a `serviceAccountName` patch on the Deployment and the Job;
switch when the operator decides (open question in the plan).

## Transport ceiling: the load balancer is the binding hop

`ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS` must be the smallest
idle timeout of every hop in front of the pod. Behind the application-routing
add-on the hops are: the Azure Standard Load Balancer fronting the controller
(inbound TCP idle timeout **default and minimum 4 minutes**, maximum 100,
set with the Service annotation
`service.beta.kubernetes.io/azure-load-balancer-tcp-idle-timeout`) and
ingress-nginx (`proxy-read-timeout`, 3600 here). The overlay therefore declares
230 (= 240 − 10) and pins `ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS=180`, which
satisfies the boot validator `timeout <= ceiling − 30` (headroom). The turn
budgets and the composer rate limit, which `WebSettings` also requires with no
default, come from the base ConfigMap unchanged; at 180 s with the base's
15 + 10 turns the boot log discloses that 25 configured turns exceed the 12
the clock can fund — a warning, not a refusal.

To lengthen it, set the annotation on the controller's Service through the
add-on's custom resource, then raise both values together:

```yaml
apiVersion: approuting.kubernetes.azure.com/v1alpha1
kind: NginxIngressController
metadata:
  name: default
spec:
  ingressClassName: webapprouting.kubernetes.azure.com
  controllerNamePrefix: nginx
  loadBalancerAnnotations:
    service.beta.kubernetes.io/azure-load-balancer-tcp-idle-timeout: "30"
```

With 30 minutes on the LB the ceiling may rise to 1790 and the timeout to
1760; a Front Door or WAF placed in front lowers the ceiling again to its own
origin timeout. Declaring a ceiling higher than the real minimum does not
raise anything — it only silences the wall-clock guard.

## Affinity

`ingress.yaml` pins cookie affinity (`elspeth-replica`, mode `persistent`) so
a browser stays on the replica that issued its WebSocket ticket and holds its
composer progress. The base Service's `sessionAffinity: ClientIP` has no
effect behind ingress-nginx, which load-balances to pod endpoints directly.
Task K7 qualifies routing without affinity; until it lands these annotations
are load-bearing, not optional.

## Alternatives recorded, not shipped

- **Ingress.** The application-routing add-on's NGINX is supported by
  Microsoft through November 2026 (upstream ingress-nginx maintenance ended
  March 2026); Microsoft's stated successors are the application-routing
  Gateway API implementation and Application Gateway for Containers. AGIC
  spells cookie affinity `appgw.ingress.kubernetes.io/cookie-based-affinity: "true"`
  and has its own request-timeout annotation; the transport-ceiling derivation
  above must be redone for whichever controller ships.
- **Storage SKU.** `Premium_LRS` mirrors the ACA storage account; the AKS
  driver also accepts `PremiumV2_LRS` (provisioned v2). Only Premium SKUs
  support NFS.
- **Secrets identity.** Workload ID, as above.

## Verify

`kubectl kustomize deploy/kubernetes/overlays/aks` renders; the contract is
`tests/unit/deployment/test_kubernetes_bundle.py` (the `aks` tests), which CI
runs in the `kubernetes-render` job. Nothing in this repository applies the
overlay to an AKS cluster; the kind lane proves the base, and the runbook's
rollout proof (`kubectl rollout status`, `/api/ready`, `/api/system/status`)
is the acceptance for a real cluster.
````

- [ ] **Step 4: Stage the overlay and run the deployment gates.**

Two reasons the stage comes first: the exports pin (`test_web_settings_exports_resolve.py`) scans `git ls-files`, so the new files must be in the index before it can see them; and Step 5's `git commit -m "<msg>" -- <paths>` aborts with `pathspec did not match` on an untracked path, so the nine new files must be added before that form can commit them. Stage by explicit file, never the directory.

```bash
cd "$(git rev-parse --show-toplevel)" && git add -- deploy/kubernetes/overlays/aks/kustomization.yaml deploy/kubernetes/overlays/aks/storageclass-azurefile-nfs.yaml deploy/kubernetes/overlays/aks/secretproviderclass-web.yaml deploy/kubernetes/overlays/aks/secretproviderclass-schema-owner.yaml deploy/kubernetes/overlays/aks/ingress.yaml deploy/kubernetes/overlays/aks/patch-pvc.yaml deploy/kubernetes/overlays/aks/patch-deployment.yaml deploy/kubernetes/overlays/aks/patch-job-schema-init.yaml deploy/kubernetes/overlays/aks/README.md tests/unit/deployment/test_kubernetes_bundle.py
```

Run: `cd "$(git rev-parse --show-toplevel)" && PATH=.claude/lanes/k8s/bin:$PATH pytest tests/unit/deployment/test_kubernetes_bundle.py tests/unit/deployment/test_web_settings_exports_resolve.py tests/unit/deployment/test_deploy_ignore_policy.py -n 0 > /tmp/k6-step4.log 2>&1; echo exit=$?; tail -20 /tmp/k6-step4.log`
Expected: exit 0. With kubectl on PATH the nine `aks` tests PASS alongside K1's; without it the render tests SKIP and the README test passes (CI's `test` and `kubernetes-render` jobs carry the K3 kubectl pin, so the render is gated there). `test_every_exported_web_setting_resolves_to_a_live_field` passes because every `ELSPETH_WEB__*` name on a non-comment line of the overlay and README (`SESSION_DB_URL`, `LANDSCAPE_URL`, `SECRET_KEY`, `SHAREABLE_LINK_SIGNING_KEY`, `COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS`, `COMPOSER_TIMEOUT_SECONDS`, `OPERATOR_TELEMETRY_RELEASE`) is a `WebSettings` field. The overlay no longer names `COMPOSER_MAX_COMPOSITION_TURNS` / `COMPOSER_MAX_DISCOVERY_TURNS`; K1's base ConfigMap does, and K1 Step 7 gates those names.

Discrimination controls (measured on 2026-09-13 with kubectl v1.37.0 against a stand-in base built to the K1 decisions): removing the Job's schema-owner CSI volume, renaming the patch container to `web2`, adding a `*-schema-owner` object to the runtime class, pointing both classes at one vault, mounting `elspeth-schema-owner-kv` on the Deployment, raising the ceiling to 300, raising the timeout to 220, `proxy-body-size: 1m`, `affinity-mode: balanced`, dropping `rootSquashType`, dropping the PVC patch, and adding a `ServiceAccount` each turned exactly one of the nine tests red; with no mutation all nine pass. That list predates K1's base ConfigMap carrying the four required composer settings, and one of its mutations (dropping `ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS` from the merge) no longer exists, because the merge no longer restates the turn counts. Control the two assertions that replace it now, on the real K1 base, re-running the Step 4 command after each mutation. First add the literal `- ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS=11` to the merge in `kustomization.yaml`: only `test_aks_transport_ceiling_is_the_minimum_hop_and_leaves_composer_headroom` fails (the inherited-settings assertion). Remove the line. Then delete the `- ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS=180` literal: only the same test fails, on the timeout-differs-from-base assertion (the base's 85 still satisfies `85 <= 230 - 30`, which is why that assertion exists). Restore it and confirm `exit=0` with all nine `aks` tests passing.

- [ ] **Step 5: Commit.**

Stage first, then run the safety check, then commit: `scripts/branch-safety-check.sh` inspects the STAGED set. The nine created files get `git add -N` (Step 4 already staged them, so this changes nothing unless a path was missed); then all ten paths are staged by name, the check runs, and the message goes before `--`.

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N deploy/kubernetes/overlays/aks/kustomization.yaml deploy/kubernetes/overlays/aks/storageclass-azurefile-nfs.yaml deploy/kubernetes/overlays/aks/secretproviderclass-web.yaml deploy/kubernetes/overlays/aks/secretproviderclass-schema-owner.yaml deploy/kubernetes/overlays/aks/ingress.yaml deploy/kubernetes/overlays/aks/patch-pvc.yaml deploy/kubernetes/overlays/aks/patch-deployment.yaml deploy/kubernetes/overlays/aks/patch-job-schema-init.yaml deploy/kubernetes/overlays/aks/README.md
git add -- deploy/kubernetes/overlays/aks/kustomization.yaml deploy/kubernetes/overlays/aks/storageclass-azurefile-nfs.yaml deploy/kubernetes/overlays/aks/secretproviderclass-web.yaml deploy/kubernetes/overlays/aks/secretproviderclass-schema-owner.yaml deploy/kubernetes/overlays/aks/ingress.yaml deploy/kubernetes/overlays/aks/patch-pvc.yaml deploy/kubernetes/overlays/aks/patch-deployment.yaml deploy/kubernetes/overlays/aks/patch-job-schema-init.yaml deploy/kubernetes/overlays/aks/README.md tests/unit/deployment/test_kubernetes_bundle.py
git status --short
scripts/branch-safety-check.sh --intent commit
git commit -m "feat(deploy): AKS overlay on the Kubernetes base" -- deploy/kubernetes/overlays/aks/kustomization.yaml deploy/kubernetes/overlays/aks/storageclass-azurefile-nfs.yaml deploy/kubernetes/overlays/aks/secretproviderclass-web.yaml deploy/kubernetes/overlays/aks/secretproviderclass-schema-owner.yaml deploy/kubernetes/overlays/aks/ingress.yaml deploy/kubernetes/overlays/aks/patch-pvc.yaml deploy/kubernetes/overlays/aks/patch-deployment.yaml deploy/kubernetes/overlays/aks/patch-job-schema-init.yaml deploy/kubernetes/overlays/aks/README.md tests/unit/deployment/test_kubernetes_bundle.py
git show --stat HEAD
```

Expected: `git status --short` shows the nine overlay paths as `A ` and `tests/unit/deployment/test_kubernetes_bundle.py` as `M `; the safety check prints no `[FAIL]` line and exits 0 (on a FAIL, stop before the commit); `git show --stat HEAD` ends with `10 files changed`. Any other count means a sibling lane staged into the shared index: `git reset --mixed HEAD~1`, restage only the 10 paths above, and commit again.
