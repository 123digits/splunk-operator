# Storage: what the EBS volumes are actually for

Short version: **SmartStore does not replace local disk — it changes what local
disk is for.** Every Splunk pod gets two EBS volumes, and both are still
required with SmartStore enabled. What SmartStore changes is how big one of
them has to be.

## The two volumes

The operator creates two PVCs per pod, always, from
`pkg/splunk/enterprise/configuration.go`:

| Mount | Default | Spec field | Holds |
|---|---|---|---|
| `/opt/splunk/etc` | **10Gi** | `etcVolumeStorageConfig` | Config, installed apps, user objects, dashboards |
| `/opt/splunk/var` | **100Gi** | `varVolumeStorageConfig` | Hot buckets, SmartStore cache, splunkd logs, search artifacts |

They are `volumeClaimTemplates` on the StatefulSet, so each pod ordinal gets its
own pair, named `pvc-etc-splunk-<cr>-<tier>-<n>` and `pvc-var-...`.

## Why `/opt/splunk/etc` cannot go to S3

Nothing about it lives in S3, ever. This is the Splunk installation: `system/`,
`apps/`, `users/`, every `.conf` file, and the dashboards the App Framework
installed. SmartStore is an *index* storage feature and has no bearing on it.

It is small and slow-growing. 10Gi is fine for most tiers; the search heads get
more here because user objects and saved searches accumulate.

## Why `/opt/splunk/var` cannot go to S3 either

Three separate reasons, each sufficient on its own.

**1. Hot buckets are always local.** Incoming data is written to a hot bucket on
local disk and stays there while the bucket is open for writing. Only when the
bucket rolls to warm does SmartStore upload it to S3. There is no configuration
that makes Splunk write hot buckets directly to object storage.

The consequence worth internalising: **data in hot buckets is not yet in S3**.
Lose the volume and you lose whatever had not rolled. This is precisely why
index replication (RF ≥ 2) still matters with SmartStore — S3 durability does
not protect the newest data, peer replication does.

**2. Warm buckets are cached locally to be searched.** SmartStore fetches a
bucket from S3 onto local disk before it can be searched. Undersize this volume
and searches thrash: every query faults buckets down, evicting others the next
query re-fetches. It shows up as slow searches and heavy S3 GET charges rather
than as an error. Sizing detail in the next section.

**3. Everything else splunkd writes is local.** The dispatch directory (search
artifacts and results), splunkd logs, and the KV store on search heads.

## What is actually on `/opt/splunk/var`, in detail

### Hot buckets

A **bucket** is a directory of indexed data covering a time range. Buckets have
a lifecycle, and **hot** is the only stage that is open for writing:

```
hot ──roll──> warm ──upload──> S3        (SmartStore)
 │
 └─ local, being written to, NOT yet in S3
```

Incoming events land in a hot bucket on local disk. It rolls to warm when
**either** limit is hit, whichever comes first:

| Setting | Meaning | Common value |
|---|---|---|
| `maxDataSize` | Size cap per bucket | `auto` = 750MB, `auto_high_volume` = 10GB |
| `maxHotSpanSecs` | Time span per bucket | varies |

A restart also rolls hot buckets. On roll, SmartStore uploads the bucket to S3
and it becomes a cache entry like any other.

**How much space:** roughly
`number of indexes × maxHotBuckets per index × maxDataSize`. With a handful of
indexes at `auto_high_volume` and a few hot buckets each, that is tens of GB,
not hundreds. It is the smaller part of `var`.

**Why it matters more than its size:** hot buckets are the data **not yet in
S3**. Object-storage durability does nothing for them. This is the whole reason
`replication_factor: 2` is still set in `04-clustermanager.yaml` — peer
replication is what protects data between arrival and upload.

### SmartStore cache — how much?

There is no "cache size" you set to a number and forget. The cache manager
evicts when **either** condition is met:

```
occupied space  >  max_cache_size
        ...OR...
partition free space  <  (minFreeSpace + eviction_padding)
```

`max_cache_size = 0` disables the first rule, leaving only the free-space rule —
so the cache grows until the partition is nearly full, then evicts LRU. That is
the usual choice when the volume is dedicated to Splunk, which it is here.

**In practice the cache is "whatever is left on the volume."** You do not size
the cache; you size the volume, and the cache manager fills it. A `var` of 500Gi
with ~50GB of hot buckets and overhead gives roughly 400GB+ of usable cache.

Protected from eviction regardless:

| CR setting | server.conf | What it protects |
|---|---|---|
| `hotlistRecencySecs: 86400` | `hotlist_recency_secs` | Buckets newer than 24h stay resident |
| `hotlistBloomFilterRecencyHours: 360` | `hotlist_bloom_filter_recency_hours` | Bloom filters kept 15 days — they let Splunk skip buckets without downloading them |

The bloom filter setting is the cheap win: keeping filters far longer than the
data means a search over old data rejects irrelevant buckets without faulting
them down from S3 at all.

**Operator gotcha:** the operator only writes a `[cachemanager]` setting when it
is **non-zero** — see `GetServerConfigEntries` in
`pkg/splunk/enterprise/configuration.go`. So setting `maxCacheSize: 0` in the CR
does **not** write `max_cache_size = 0`; it writes nothing and leaves Splunk's
built-in default in force. If you need the value set explicitly, confirm what
your build actually defaults to and, if it is not what you want, ship it in a
custom app rather than through the CR.

Verify what is really in effect:

```bash
kubectl -n splunk exec splunk-idxc-indexer-0 -- \
  /opt/splunk/bin/splunk btool server list cachemanager --debug
kubectl -n splunk exec splunk-idxc-indexer-0 -- \
  /opt/splunk/bin/splunk btool server list diskUsage --debug   # minFreeSpace
```

### splunkd logs

`$SPLUNK_HOME/var/log/splunk/` — `splunkd.log`, `metrics.log`, audit and
introspection logs. These rotate with size caps, so the footprint is bounded and
modest: a few GB per pod. Not a sizing factor, but it shares the volume, so a
full `var` also means splunkd cannot write its own logs.

### Search artifacts

`$SPLUNK_HOME/var/run/splunk/dispatch/` — one directory per search job holding
results and metadata. Bounded by two things: a TTL after which the job is
reaped, and `srchDiskQuota` per role.

Largest on **search heads**, where interactive and scheduled searches run. On
indexers the peers keep their portion of distributed searches, which is smaller.
This is why the search heads here get 100Gi of `var` despite indexing nothing.

## So what does SmartStore actually save

Sizing shifts from **retention** to **working set**.

Without SmartStore, `/opt/splunk/var` must hold every bucket you intend to keep
searchable — retention period × daily volume × replication factor. With
SmartStore it holds hot buckets plus the cache: roughly the data people
actually search, usually recent.

That is why `05-indexercluster.yaml` asks for 500Gi rather than the multi-TB a
non-SmartStore cluster of the same retention would need. The retention lives in
`my-splunk-smartstore/idxc/`.

**S3 = durable long-term retention. EBS = hot tier + cache.**

## Not to be confused with

**`ObjectStorage`** (the CRD, in `optional/ingestion-separation.yaml`) is not
SmartStore and not bucket storage. It is the overflow location for ingestion
queue messages too large for SQS, used only by the 10.2+ ingestion separation
feature. It replaces neither SmartStore nor EBS.

**The operator's `app-download` PVC** is a third, unrelated volume in the
`splunk-operator` namespace, where the App Framework stages app packages before
copying them into pods. See §6 of `README.md`.

## Sizing per tier

What the manifests here request, and why:

| Tier | etc | var | Reasoning |
|---|---|---|---|
| Cluster Manager | 20Gi | 50Gi | Holds the app bundle; indexes nothing |
| Indexer | 15Gi | **500Gi** | Hot buckets + SmartStore cache — size to working set |
| Search Head | 30Gi | 100Gi | User objects and saved searches in etc; dispatch dir in var |
| License Manager | 10Gi | 20Gi | Minimal |
| Monitoring Console | 10Gi | 20Gi | Minimal |

Indexer `var` is the number worth calculating rather than copying. Start from
the volume of data searched in a typical window, not total retention, and leave
headroom for hot buckets and eviction padding.

## StorageClass choices

`01-storageclass.yaml` uses gp3 with:

```yaml
volumeBindingMode: WaitForFirstConsumer
```

This matters on EKS. EBS volumes are zonal: a volume in `us-east-1a` cannot
attach to a pod in `us-east-1b`. `WaitForFirstConsumer` defers provisioning
until the scheduler picks a node, so the volume is created in the right zone.
With the default `Immediate` binding, a volume can be created in a zone the pod
cannot be scheduled into, and the pod hangs `Pending` forever.

```yaml
reclaimPolicy: Retain
allowVolumeExpansion: true
```

`Retain` keeps the underlying volume when a PVC is deleted — deliberate for
indexed data. `allowVolumeExpansion` lets you grow a volume in place later;
without it, resizing means recreating the pod's storage.

IOPS and throughput are set well above gp3 defaults because indexing is
write-heavy and cache faulting is read-heavy. Raise them for indexers before
raising capacity if searches are slow but the cache is not full.

## Two things that delete data

**The `delete-pvc` finalizer.** Every CR here carries:

```yaml
finalizers:
  - enterprise.splunk.com/delete-pvc
```

This is convenient in test and destructive in production: deleting the CR
deletes the PVCs, and with them the indexed data. `reclaimPolicy: Retain` keeps
the EBS volume itself, but it is then unattached and unreferenced. **Remove this
finalizer for a production cluster** unless you specifically want
`kubectl delete` to reclaim storage.

**`ephemeralStorage: true`.** Both storage configs accept it, which swaps the
PVC for an `emptyDir` — data lives and dies with the pod. Only ever for testing.
The CRD enforces that it is mutually exclusive with `storageClassName` and
`storageCapacity`.

## Pre-provisioned volumes

If your platform team creates PVs rather than letting the CSI driver provision
them, annotate the CR:

```yaml
metadata:
  annotations:
    enterprise.splunk.com/admin-managed-pv: "true"
```

The operator then clears `storageClassName` and builds the claim with a label
selector instead, binding to PVs you created and labelled to match. Without the
annotation it always requests dynamic provisioning.

## Checking

```bash
kubectl -n splunk get pvc -o custom-columns=\
NAME:.metadata.name,SIZE:.spec.resources.requests.storage,SC:.spec.storageClassName,STATUS:.status.phase

# Actual usage inside a pod
kubectl -n splunk exec splunk-idxc-indexer-0 -- df -h /opt/splunk/etc /opt/splunk/var
```

Watch `/opt/splunk/var` on the indexers. Steady growth toward full is normal
with SmartStore — the cache fills and then evicts. Sustained 100% with slow
searches means the working set exceeds the cache, so grow the volume or tighten
`hotlistRecencySecs`.
