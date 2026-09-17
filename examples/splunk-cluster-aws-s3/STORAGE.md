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
bucket from S3 onto local disk before it can be searched. The cache manager
evicts by the settings in `04-clustermanager.yaml`:

```yaml
cacheManager:
  hotlistRecencySecs: 86400              # keep the last day resident
  hotlistBloomFilterRecencyHours: 360    # keep bloom filters longer than the data
```

Undersize this volume and searches thrash: every query faults buckets down from
S3, evicting others, which the next query re-fetches. It shows up as slow
searches and heavy S3 GET charges rather than as an error.

**3. Everything else splunkd writes is local.** The dispatch directory (search
artifacts and results), splunkd logs, and the KV store on search heads.

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
