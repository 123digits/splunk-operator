# Clustered Splunk on EKS with S3 — what to deploy after the operator

Reference deployment for a Splunk Validated Architecture **C3** (distributed clustered
indexers + search head cluster, single site) on AWS, with S3 for SmartStore and app
distribution. Manifests in this directory are validated and apply in numeric order.

The operator itself deploys **nothing**. It watches for CRs and reconciles them. After
`kubectl apply -f splunk-operator-cluster.yaml` you have zero Splunk pods until you
apply the resources below.

---

## 1. What a full cluster consists of

| CR | Count | Pods created | Why you need it |
|---|---|---|---|
| `LicenseManager` | 1 | `splunk-lm-license-manager-0` | Central license; every other tier points at it |
| `ClusterManager` | 1 | `splunk-cm-cluster-manager-0` | Owns SmartStore config, RF/SF, and cluster-wide app bundle |
| `IndexerCluster` | 1 (3+ peers) | `splunk-idxc-indexer-{0..n}` | Indexes and stores data |
| `SearchHeadCluster` | 1 (3+ members) | `splunk-shc-deployer-0`, `splunk-shc-search-head-{0..n}` | Search + your dashboards; deployer distributes apps |
| `MonitoringConsole` | 1 | `splunk-mc-monitoring-console-0` | Health/topology view; auto-wires to every CR referencing it |

Optional: `IngestorCluster` + `Queue` + `ObjectStorage` (Splunk 10.2+) for ingestion
separation, and `Standalone` for a heavy forwarder / syslog collector tier.

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

It defaults to an **empty string**. Patch it before applying any CR:

```bash
kubectl -n splunk-operator set env deploy/splunk-operator-controller-manager \
  SPLUNK_GENERAL_TERMS="--accept-sgt-current-at-splunk-com"
```

### EBS CSI driver + StorageClass
`00-storageclass.yaml` defines a gp3 class with `WaitForFirstConsumer` so each EBS
volume lands in the same AZ as its pod. Requires the `aws-ebs-csi-driver` addon.

### IAM (IRSA) — preferred over static keys
One service account used by the CM, indexers, search heads, and the operator pod:

```bash
eksctl create iamserviceaccount \
  --name splunk-s3 --namespace splunk --cluster <cluster> \
  --attach-policy-arn arn:aws:iam::<acct>:policy/SplunkS3Access \
  --approve
```

`SplunkS3Access` needs, at minimum:
- SmartStore bucket: `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket`
- Apps bucket: `s3:GetObject`, `s3:ListBucket`
- With ingestion separation: `sqs:SendMessage`, `sqs:ReceiveMessage`,
  `sqs:DeleteMessage`, `sqs:GetQueueUrl`, `sqs:GetQueueAttributes` on the queue + DLQ

Then set `serviceAccount: splunk-s3` on each CR (already done in these manifests) and
**omit `secretRef`** from the S3 volume stanzas.

If you must use static keys instead:

```bash
kubectl -n splunk create secret generic s3-secret \
  --from-literal=s3_access_key=AKIA... \
  --from-literal=s3_secret_key=...
```
and add `secretRef: s3-secret` to each volume stanza.

The **operator pod itself** downloads app packages from S3, so it needs the same S3
read access — annotate its service account too, and give it a staging PVC (see §6).

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

`ObjectStorage` (file 07) is a different thing entirely — it is the overflow bucket
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
`deployerLocalApps` in `04-searchheadcluster.yaml`.

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
`06-ingest-endpoints.yaml` puts an internal NLB in front of the indexer pods for both
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

Without a PVC, the operator stages downloaded app packages **in RAM**. With any
meaningful app set this will OOM the operator pod. Add a PVC named
`splunk-operator-app-download` and mount it at `/opt/splunk/appframework/` — see
`docs/AppFramework.md`, "Add a persistent storage volume to the Operator pod".

---

## 7. Apply

```bash
kubectl apply -f 00-storageclass.yaml
kubectl -n splunk create configmap splunk-licenses --from-file=enterprise.lic
kubectl apply -f 01-licensemanager.yaml
kubectl apply -f 02-clustermanager.yaml
kubectl -n splunk wait --for=jsonpath='{.status.phase}'=Ready clustermanager/cm --timeout=15m
kubectl apply -f 03-indexercluster.yaml
kubectl apply -f 04-searchheadcluster.yaml
kubectl apply -f 05-monitoringconsole.yaml
kubectl apply -f 06-ingest-endpoints.yaml
```

Watch:
```bash
kubectl -n splunk get pods -w
kubectl -n splunk get clustermanager,indexercluster,searchheadcluster,licensemanager,monitoringconsole
```

Admin password:
```bash
kubectl -n splunk get secret splunk-splunk-secret -o jsonpath='{.data.password}' | base64 -d
```

Splunk Web:
```bash
kubectl -n splunk port-forward service/splunk-shc-search-head-service 8000
```

---

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
- **The in-repo `config/examples/advanced/c3.yaml` and `c1.yaml` do not parse** — the
  `smartstore.volumes` list is misindented. Use the manifests here instead.

---

## 9. Multisite

For bucket replicas spread across AZs with site awareness, use one `IndexerCluster`
per AZ pointing at a shared `ClusterManager`, each with a hardcoded `site` and zone
affinity. See `docs/MultisiteExamples.md`. The single-site C3 here relies on
`topologySpreadConstraints` for AZ spread, which gives you scheduling spread but not
site-aware bucket placement.
