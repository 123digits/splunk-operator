# Example: full clustered Splunk on EKS with S3

`examples/splunk-cluster-aws-s3/`

Reference deployment for a Splunk Validated Architecture **C3** (distributed clustered
indexers + search head cluster, single site) on AWS, with S3 for SmartStore and app
distribution. Manifests in this directory are validated and apply in numeric order.

The operator itself deploys **nothing**. It watches for CRs and reconciles them. After
`kubectl apply -f splunk-operator-cluster.yaml` you have zero Splunk pods until you
apply the resources below.

---

## 0. Version provenance

Everything here was verified against **tag `3.1.0`** (commit `85b1dcca`).

The working checkout of `main` is only 5 commits ahead of that tag, and the diff
touches just three files — `helm-chart/splunk-operator/values.yaml`,
`pkg/splunk/enterprise/util.go`, and its test. The paths this guide is built on —
`api/v4/`, `config/crd/bases/`, `config/examples/`, `docs/`, `.env`, and the port and
label logic in `pkg/splunk/enterprise/{configuration,types,names}.go` — are
**byte-identical at 3.1.0**. So the CRD fields, scopes, service ports, and pod labels
below all hold for the 3.1.0 release.

Two version-specific cautions:

- **The Helm chart at 3.1.0 pins `docker.io/splunk/splunk:10.0.0`**, not 10.2.0. The
  bump to 10.2.0 landed *after* the tag. If you install via Helm at 3.1.0 and take
  the default, you get Splunk 10.0.0 — which is **too old for the ingestion
  separation stack in file 07** (`IngestorCluster`/`Queue`/`ObjectStorage` require
  10.2+). The CRDs are present at 3.1.0 either way, so this fails at runtime, not at
  apply time. Override the image or pin it per CR.
- **`RELATED_IMAGE_SPLUNK_ENTERPRISE` in the published release YAML is injected at
  build time** from a CI secret, and the Makefile's own default is the untagged
  `docker.io/splunk/splunk`. Do not rely on it. Every CR in this directory sets
  `image:` explicitly, which is the behaviour you want anyway — it is what gives you
  control of the upgrade cycle.

---

## 1. What a full cluster consists of

| CR | Count | Pods created | Why you need it |
|---|---|---|---|
| `LicenseManager` | 1 | `splunk-lm-license-manager-0` | Central license; every other tier points at it |
| `ClusterManager` | 1 | `splunk-cm-cluster-manager-0` | Owns SmartStore config, RF/SF, and cluster-wide app bundle |
| `IndexerCluster` | 1 (3+ peers) | `splunk-idxc-indexer-{0..n}` | Indexes and stores data |
| `SearchHeadCluster` | 1 (3+ members) | `splunk-shc-deployer-0`, `splunk-shc-search-head-{0..n}` | Search + your dashboards; deployer distributes apps |
| `MonitoringConsole` | 1 | `splunk-mc-monitoring-console-0` | Health/topology view; auto-wires to every CR referencing it |

Everything above is in the numbered files and is required. Under `optional/`:

`IngestorCluster` + `Queue` + `ObjectStorage` (CRDs present at 3.1.0, but
the feature requires a Splunk **10.2+** image — see §0) for ingestion
separation, and a `Standalone` heavy-forwarder tier for syslog.

Order matters only loosely — the operator retries — but license first, then cluster
manager, then peers, then search heads, is the clean path.

---

## 2. Prerequisites before the first CR

### Namespace and operator scope
The operator installs into `splunk-operator` and, with the cluster-scoped manifest,
watches all namespaces. If you use `WATCH_NAMESPACE`, add `splunk` to it.

```bash
kubectl create namespace splunk
```

### Splunk General Terms (required for 10.x images, easy to miss)
Splunk Enterprise 10.x containers will not start unless the operator deployment sets:

```yaml
- name: SPLUNK_GENERAL_TERMS
  value: "--accept-sgt-current-at-splunk-com"
```

It defaults to an **empty string**. At 3.1.0 this env var is injected into the
deployment by kustomize (`config/default/kustomization.yaml`, placeholder
`SPLUNK_GENERAL_TERMS_VALUE`) and the Makefile default is `""` — so the published
`splunk-operator-cluster.yaml` ships it empty. Either pass it at deploy time
(`make deploy SPLUNK_GENERAL_TERMS="--accept-sgt-current-at-splunk-com"`) or patch
the running deployment before applying any CR:

```bash
kubectl -n splunk-operator set env deploy/splunk-operator-controller-manager \
  SPLUNK_GENERAL_TERMS="--accept-sgt-current-at-splunk-com"
```

### EBS CSI driver + StorageClass
`01-storageclass.yaml` defines a gp3 class with `WaitForFirstConsumer` so each EBS
volume lands in the same AZ as its pod. Requires the `aws-ebs-csi-driver` addon.

### IAM via kiam

This deployment uses **kiam**, not IRSA. The difference matters: kiam authorises
**pods**, IRSA authorises **service accounts**. So there is no
`eks.amazonaws.com/role-arn` service account here, and `serviceAccount:` on the CRs
does nothing for AWS access.

kiam needs authorisation in two places, and both must line up:

| Where | Annotation | Set in |
|---|---|---|
| Namespace | `iam.amazonaws.com/permitted: "^SplunkS3Access$"` | `00-namespace.yaml` |
| Pod | `iam.amazonaws.com/role: SplunkS3Access` | each CR's `metadata.annotations` |

**Why annotating the CR works.** The operator copies the CR's own labels and
annotations onto the StatefulSet's *pod template* — `AppendParentMeta` in
`pkg/splunk/enterprise/configuration.go`, called from `getSplunkStatefulSet`, which
every tier including the SHC deployer goes through. So an annotation on the CR lands
on the pods, which is exactly what kiam reads. Verified identical at 3.1.0.

Two consequences worth knowing:

- `AppendParentMeta` **will not clobber** an annotation the operator already set on
  the pod template. `iam.amazonaws.com/role` is not one the operator sets, so it
  propagates cleanly — but this is why you annotate the CR rather than editing the
  StatefulSet, which the operator would revert on its next reconcile.
- Changing the annotation changes the pod template, which **triggers a rolling
  restart** of that tier. Plan role changes accordingly.

**Do not forget the operator itself.** The App Framework downloads app packages in
the *operator* pod before copying them into the Splunk pods, so the
`splunk-operator` namespace and the operator deployment need the same two
annotations. See `00b-operator-namespace-kiam.yaml`. Missing this is a confusing
failure: every Splunk tier is healthy and apps simply never install.

Role setup, trust policy, and how to verify credentials are actually reaching the
pods: see `iam/README.md`.

### Static keys instead

If you would rather not use kiam for the S3 volumes:

```bash
kubectl -n splunk create secret generic s3-secret \
  --from-literal=s3_access_key=AKIA... \
  --from-literal=s3_secret_key=...
```
and add `secretRef: s3-secret` to each volume stanza in `04-` and `06-`. The two
mechanisms are mutually exclusive per volume — a `secretRef` takes precedence and
kiam credentials are ignored for that volume.

### Buckets
```
my-splunk-smartstore/idxc/        # SmartStore warm/cold buckets
my-splunk-apps/indexer-apps/      # TAs for indexer peers
my-splunk-apps/cm-apps/           # cluster manager local apps
my-splunk-apps/search-apps/       # your dashboard apps
my-splunk-apps/search-tas/        # search-time TAs
```

### License file
```bash
kubectl -n splunk create configmap splunk-licenses --from-file=enterprise.lic
```

---

## 3. Why you still need EBS even though SmartStore writes to S3

SmartStore does not replace local disk — it changes what local disk is *for*. Every
Splunk pod gets two PVCs, and both are still required with SmartStore enabled:

| Volume | Default | Holds | Does SmartStore remove it? |
|---|---|---|---|
| `/opt/splunk/etc` | 10 GiB | Config, apps, user objects, dashboards | **No** — nothing about this lives in S3 |
| `/opt/splunk/var` | 100 GiB | **Hot buckets**, SmartStore cache, splunkd logs, search artifacts | **No** — see below |

Three reasons `/opt/splunk/var` cannot go away:

1. **Hot buckets are always local.** Data lands on local disk first and stays there
   while the bucket is open for writing. Only on roll to warm does it upload to S3.
   Lose the volume with hot buckets and you lose data not yet uploaded — which is
   exactly why index replication (RF ≥ 2) still matters with SmartStore.
2. **Warm buckets are cached locally to be searched.** SmartStore fetches a bucket
   from S3 into the local cache before searching it. The cache manager evicts by the
   `hotlistRecencySecs` / eviction policy settings. Size `/opt/splunk/var` for your
   working set, not your retention — that is the real saving: 500 GiB of cache
   instead of 10 TiB of retention.
3. **splunkd logs, the dispatch directory, and KV store** are local regardless.

So the split is: **S3 = durable long-term retention; EBS = hot tier + cache.** The
`storageCapacity` in these manifests reflects that — indexers get 500 GiB of `var`
for cache and hot, not enough for full retention, because full retention is in S3.

`ObjectStorage` (optional/) is a different thing entirely — it is the overflow bucket
for oversized *ingestion queue messages*, not bucket storage. It does not replace
SmartStore, and neither replaces EBS.

---

## 4. Getting your existing dashboards in

Dashboards are just apps. The App Framework polls an S3 prefix and installs what it
finds, so the workflow is: package → upload → reference in `appRepo`.

### Package
Your app directory needs the standard layout; dashboards are XML under
`default/data/ui/views/`:

```
my_dashboards/
├── default/
│   ├── app.conf
│   └── data/ui/views/*.xml        # dashboards
│   └── data/ui/nav/default.xml    # nav menu
├── local/                          # leave empty in the package
└── metadata/default.meta           # set export = system for global visibility
```

```bash
tar -czf my_dashboards.tgz my_dashboards/
aws s3 cp my_dashboards.tgz s3://my-splunk-apps/search-apps/
```

Exporting an existing app from a running Splunk instance works too — grab
`$SPLUNK_HOME/etc/apps/<app>` and tar it. Strip `local/` overrides you do not want
frozen into the package, since on a search head cluster the deployer overwrites
member-local changes.

### Where each app goes

| Put it in | Via | Lands on |
|---|---|---|
| Dashboards, search-time knowledge objects | `SearchHeadCluster.appRepo`, `scope: cluster` | Deployer `etc/shcluster/apps` → all members |
| Deployer-only app | `SearchHeadCluster.appRepo`, `scope: local` | Deployer `etc/apps` |
| Index-time TAs (props/transforms) | `ClusterManager.appRepo`, `scope: cluster` | CM `etc/manager-apps` → bundle push → peers `etc/peer-apps` |
| CM-only app | `ClusterManager.appRepo`, `scope: local` | CM `etc/apps` |

`IndexerCluster` has **no** `appRepo` field — that is intentional, not an omission.
Indexer apps always go through the ClusterManager bundle.

An app needed on both the deployer and the members must appear twice in `appSources`
with **different names but the same location** and different scopes — see
`deployerLocalApps` in `06-searchheadcluster.yaml`.

### Updates
`appsRepoPollIntervalSeconds: 900` re-checks S3 every 15 min and installs changes.
To push immediately, patch the auto-created configmap:

```bash
kubectl -n splunk patch cm splunk-splunk-manual-app-update \
  --type=merge -p '{"data":{"shc":"status: off\nrefCount: 0"}}'
```

Verify:
```bash
kubectl -n splunk get shc shc -o jsonpath='{.status.appContext.appsSrcDeployStatus}' | jq
```

---

## 5. Ingest points

The operator exposes **9997 (S2S)** and **8088 (HEC)** only on `Standalone`,
`IndexerCluster`, `MonitoringConsole`, and `IngestorCluster` services. Cluster manager,
search head, and deployer services expose only 8000/8089 — do not point forwarders
at them.

```
splunk-idxc-indexer-service    8000, 8088, 8089, 9997   <- ingest target
splunk-idxc-indexer-headless   8000, 8088, 8089, 9997
splunk-shc-search-head-service 8000, 8089
splunk-cm-cluster-manager-service 8000, 8089
```

### From outside the cluster
`08-ingest-endpoints.yaml` puts an internal NLB in front of the indexer pods for both
9997 and 8088.

**Indexer Discovery does not work on Kubernetes.** Forwarders cannot query the cluster
manager for a peer list; they must target the load balancer. The LB must resolve to
**two or more IPs**, otherwise forwarder auto-load-balancing collapses onto one peer.
Cross-zone NLB with `target-type: ip` gives you that.

Forwarder `outputs.conf`:
```ini
[tcpout]
defaultGroup = k8s_indexers

[tcpout:k8s_indexers]
server = splunk-ingest.internal.example.com:9997
autoLBFrequency = 30
forceTimebasedAutoLB = true
```

### HEC
The HEC token is generated into the namespace-wide secret:

```bash
kubectl -n splunk get secret splunk-splunk-secret -o jsonpath='{.data.hec_token}' | base64 -d
```

```bash
curl -k https://splunk-ingest-hec.internal.example.com:8088/services/collector/event \
  -H "Authorization: Splunk <hec_token>" \
  -d '{"event":"hello","index":"main","sourcetype":"manual"}'
```

Pre-seed the token instead of taking the generated one by creating
`splunk-splunk-secret` with a `hec_token` key **before** the first CR.

### TLS
Convention in the Splunk docs is to keep 9997 plaintext for intra-cluster traffic and
add a second port (9998) for TLS from outside, via `serviceTemplate` on the CR:

```yaml
spec:
  serviceTemplate:
    spec:
      type: LoadBalancer
      ports:
        - name: tls-s2s
          port: 9998
          targetPort: 9998
          protocol: TCP
```

`serviceTemplate` is applied by the operator to the regular service only (not the
headless one), and the operator overwrites `selector` with the correct labels — so
this is safer than hand-writing a Service. See `docs/Ingress.md` for Istio and NGINX
gateway configurations including end-to-end TLS and TLS termination.

### Syslog / other sources
Nothing in the operator terminates syslog. Run a `Standalone` CR as a heavy forwarder
tier, or an independent collector (e.g. Splunk Connect for Syslog) writing to the HEC
endpoint above.

---

## 6. Operator app-staging volume

The operator stages downloaded app packages on a PVC mounted at
`/opt/splunk/appframework/`. **You do not create this** — at 3.1.0 the release
manifest ships it, because `config/default` includes `../persistent-volume` in its
bases. The shipped object is:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: app-download          # in the splunk-operator namespace
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 10Gi
```

Two things to check, because both bite silently:

1. **It declares no `storageClassName`**, so it binds via your cluster's *default*
   StorageClass. EKS clusters often have no default. If none exists the PVC stays
   `Pending`, and because the mount is unconditional in the deployment, the
   **operator pod never starts**. Either mark a class default or patch the PVC:

   ```bash
   kubectl -n splunk-operator get pvc app-download
   kubectl -n splunk-operator get sc    # look for (default)
   ```

2. **10Gi is the default size and it is not resizable in place** unless your
   StorageClass sets `allowVolumeExpansion`. Size it up front if your app set is
   large — the framework stages every package it downloads before installing.

## 7. Apply

Contents of this directory:

```
00-namespace.yaml                  Namespace, kiam permitted-role regex
00b-operator-namespace-kiam.yaml   Notes: kiam wiring for the operator itself
01-storageclass.yaml               gp3 class, WaitForFirstConsumer
02-license-configmap.yaml.template NOT appliable - generate from your .lic
03-licensemanager.yaml             LicenseManager
04-clustermanager.yaml             ClusterManager + SmartStore(S3) + indexer apps
05-indexercluster.yaml             IndexerCluster (3 peers)
06-searchheadcluster.yaml          SearchHeadCluster (deployer + 3) + dashboards
07-monitoringconsole.yaml          MonitoringConsole
08-ingest-endpoints.yaml           NLB services for S2S 9997 + HEC 8088
kustomization.yaml                 kubectl apply -k . (see caveat in the file)
iam/                               IAM policies + kiam trust policy + README
optional/ingestion-separation.yaml Queue + ObjectStorage + IngestorCluster (10.2+)
optional/heavy-forwarder-syslog.yaml Standalone HF tier for syslog
```

Edit before applying: the role name in `00-namespace.yaml` and each CR's
`iam.amazonaws.com/role`, `<ACCOUNT_ID>` and `<KIAM_SERVER_NODE_ROLE>` in `iam/*`, the bucket names and
region throughout `04-*` and `06-*`, and the LB hostnames in `08-*`.

First bring-up, in order:

```bash
# Prereqs: operator installed, SPLUNK_GENERAL_TERMS patched, buckets + IRSA role created
kubectl apply -f 00-namespace.yaml
kubectl apply -f 01-storageclass.yaml
kubectl -n splunk create configmap splunk-licenses --from-file=enterprise.lic
kubectl apply -f 03-licensemanager.yaml
kubectl apply -f 04-clustermanager.yaml

# Let the CM settle before the peers - it owns the bundle they pull.
kubectl -n splunk wait --for=jsonpath='{.status.phase}'=Ready clustermanager/cm --timeout=15m

kubectl apply -f 05-indexercluster.yaml
kubectl apply -f 06-searchheadcluster.yaml
kubectl apply -f 07-monitoringconsole.yaml
kubectl apply -f 08-ingest-endpoints.yaml
```

Watch:
```bash
kubectl -n splunk get pods -w
kubectl -n splunk get clustermanager,indexercluster,searchheadcluster,licensemanager,monitoringconsole
```

Expected steady state — 9 pods:
```
splunk-lm-license-manager-0        splunk-cm-cluster-manager-0
splunk-idxc-indexer-0..2           splunk-shc-deployer-0
splunk-shc-search-head-0..2        splunk-mc-monitoring-console-0
```

Admin password:
```bash
kubectl -n splunk get secret splunk-splunk-secret -o jsonpath='{.data.password}' | base64 -d
```

Splunk Web:
```bash
kubectl -n splunk port-forward service/splunk-shc-search-head-service 8000
```

## 8. Things that bite

- **`SPLUNK_GENERAL_TERMS` empty** → 10.x pods never start. Most common failure.
- **The `finalizers: enterprise.splunk.com/delete-pvc` entry** deletes PVCs when the
  CR is deleted. Convenient in test, destructive in production. Remove it if you want
  PVCs to survive a `kubectl delete`.
- **SmartStore on the wrong CR.** Only `ClusterManager` and `Standalone` accept a
  `smartstore` stanza. Putting it on `IndexerCluster` silently does nothing.
- **Migrating existing indexes to SmartStore** requires a data migration *before* you
  list them in the CR — you cannot just add an existing index to the stanza.
- **Custom apps can override SmartStore settings.** The operator writes config into an
  app named `splunk-operator`; a higher-precedence app with its own `indexes.conf`
  wins. Keep index/volume definitions out of your custom apps.
- **Changing admin passwords via Splunk UI/CLI breaks the operator.** Change them in
  `splunk-splunk-secret` only.
- **`queueRef` / `objectStorageRef` are immutable.** Decide on ingestion separation
  before creating the IndexerCluster.
- **Scaling down a SearchHeadCluster below 3** is not valid Splunk clustering.
- **The in-repo `config/examples/advanced/c3.yaml` and `c1.yaml` do not parse at 3.1.0** — the
  `smartstore.volumes` list is misindented. Confirmed by parsing the blobs straight
  from the tag. Use the manifests here instead.

---

## 9. Multisite

For bucket replicas spread across AZs with site awareness, use one `IndexerCluster`
per AZ pointing at a shared `ClusterManager`, each with a hardcoded `site` and zone
affinity. See `docs/MultisiteExamples.md`. The single-site C3 here relies on
`topologySpreadConstraints` for AZ spread, which gives you scheduling spread but not
site-aware bucket placement.
