# TLS

Two goals, and they are not the same thing:

1. **Encryption** — nobody on the network can read the traffic.
2. **Authentication** — each side proves it is the machine it claims to be.

Encryption without authentication is the `curl --insecure` failure: the channel
is private, but you have no idea who is on the other end of it.

This configuration assumes an **organisation PKI**:

- Certificates come from the org's ACME service, which signs with one of
  several org issuers. The same PKI signs **user** certificates.
- The trust bundle is the existing Secret **`all-trusted-partners`** (key
  `ca.crt`): every root and intermediate of every org issuer.
- **SAN policy:** names ending in `.svc.cluster.local`, plus `localhost`. No IP
  addresses, no short service names.

Most of what follows is about working within that SAN policy.

| File | What it is |
|---|---|
| `00-issuers.yaml` | Example org ACME `ClusterIssuer`, plus notes on `all-trusted-partners` |
| `01-certificates.yaml` | Per-tier certs, `.svc.cluster.local` + `localhost` SANs |
| `ansible-library/splunk_api.py` | splunk-ansible module override: verified calls to `localhost` |
| `02-tls-defaults.yaml` | splunk-ansible defaults that configure all Splunk TLS |
| `probes/` | Health probes that verify instead of `--insecure` |
| `lab/` | Docker lab that runs all of the above on Splunk 10.4.4 - how it was verified |

## What is and is not secure out of the box

| Channel | Port | Default | After this config |
|---|---|---|---|
| splunkd mgmt / REST | 8089 | TLS, but with **Splunk's shipped certs** | Org cert; clients verify chain + host name + pin |
| S2S forwarder data | 9997 | plaintext | TLS + client cert required, pinned |
| Splunk Web | 8000 | HTTP | HTTPS (tier cert, verified by Traefik) |
| HEC | 8088 | TLS, shipped cert | TLS, tier cert, token auth |
| Index replication | 9887 | plaintext | TLS 1.2 only + client cert required, indexer-only pin |
| All of the above | | TLS 1.2 + 1.3, Splunk's long cipher list | TLS 1.2 (four suites) + TLS 1.3 (one suite); 9887 TLS 1.2 only |

The 8089 row is the one that surprises people. splunkd *is* TLS by default — but
with certificates whose private keys ship inside every Splunk distribution on
earth. Verifying against that CA authenticates nothing.

## Ansible: `check_for_required_restarts` / `CERTIFICATE_VERIFY_FAILED`

The symptom:

```
check_for_required_restarts.yml ... FAILED ... attempts 5
failed with NO RESPONSE and EXCEP_STR as URL: https://127.0.0.1:8089/services/messages/restart_required?output_mode=json
... SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate
verify failed: self signed certificate in certificate chain')
```

### Cause

splunk-ansible's REST calls go through its `splunk_api` module
(`/opt/ansible/library/splunk_api.py`), which always dials
`https://127.0.0.1:8089`. Its tasks never pass `verify`, so the module hands
`verify=None` to python-requests. That means: verify against
**`REQUESTS_CA_BUNDLE`, else `CURL_CA_BUNDLE`**, else don't verify at all.

Those variables do **not** come from the pod environment. The image's
`ansible.cfg` runs every module through `sudo -i`, and the sudoers
`env_reset` drops the pod environment before the module starts. Only two
things reach the module:

- **login-shell files** — `/etc/profile.d/*`, `/etc/environment`. An org base
  image that exports the CA bundle there is the usual source.
- **splunk-ansible's `ansible_environment`** — `SPLUNK_ANSIBLE_ENV`, or
  `ansible_environment:` in a defaults file.

Once a bundle reaches the module, it fails one of two ways. Both were reproduced
in `splunk/splunk:10.2.0` with real `ansible-playbook` and the image's own
`ansible.cfg`:

| Bundle reaching the stock module | Result |
|---|---|
| lacks the org root | `self signed certificate in certificate chain` — **your error** |
| has the org root | `hostname '127.0.0.1' doesn't match …` |

The second row is why "fix the bundle" alone cannot work: ansible dials an IP,
and IP SANs are not allowed.

### Fix: a verifying `splunk_api` that dials `localhost`

`ansible-library/splunk_api.py` is the image's module with three changes:

1. dials **`https://localhost:8089`** instead of `127.0.0.1`. Every tier cert
   now carries `localhost` as a SAN (`01-certificates.yaml`);
2. **always verifies** against `/mnt/splunk-ca/ca.crt` (the
   `all-trusted-partners` bundle), passed explicitly. An injected
   `REQUESTS_CA_BUNDLE` no longer matters;
3. **fails closed** — if the bundle is unreadable it fails the task rather
   than making an unverified call.

The defaults are hardcoded in the file because pod env vars cannot reach it.
To override them, set `SPLUNK_API_HOST` / `SPLUNK_API_CA_BUNDLE` through
`ansible_environment`. The connection is still loopback; only the name being
verified changes.

It replaces the stock module without rebuilding the image. `ansible.cfg` sets
`library = ./library:…`, and the `ANSIBLE_LIBRARY` env var overrides that. It
is read by `ansible-playbook` itself, which does run with the pod environment.
Every CR mounts the module and puts it first:

```yaml
volumes:
  - name: ansible-library
    configMap:
      name: splunk-ansible-library
extraEnv:
  - name: ANSIBLE_LIBRARY
    value: /mnt/ansible-library:/opt/ansible/library:/opt/ansible/apps/library:/opt/ansible/ansible_commands
```

```bash
kubectl -n splunk create configmap splunk-ansible-library \
  --from-file=ansible-library/splunk_api.py
```

Tested end to end in the image, through `sudo -i`:

| Case | Result |
|---|---|
| patched, `localhost` SAN, org bundle | **OK, verified** |
| patched, plus a bad `REQUESTS_CA_BUNDLE` in `/etc/profile.d` | OK — explicit bundle wins |
| patched, bundle lacks the issuer | fails (verify error) |
| patched, cert without `localhost` SAN | fails (hostname) |
| patched, bundle not mounted | fails: `refusing unverified call` |

**Keep the copy matched to the image.** The override is a whole module, so a
Splunk image upgrade can bring a changed stock `splunk_api.py`. Diff it on every
upgrade and carry the three changes across:

```bash
kubectl -n splunk exec splunk-lm-license-manager-0 -- cat /opt/ansible/library/splunk_api.py \
  | diff - ansible-library/splunk_api.py
```

**What the override does not touch.** A few splunk-ansible tasks call
`127.0.0.1:8089` with Ansible's `uri` module and hardcode
`validate_certs: false`. They are unverified but cannot fail on certificates.
Changing them would mean overriding the roles, which no setting allows.


**What `localhost` proves.** Any org server cert may carry `localhost`, so this
check proves "an org-issued cert on our own loopback". That is the meaningful
threat for a loopback call. The probes check the stronger pod FQDN instead
(below).
## Host name validation, pinning and client certificates

Three separate checks, all on:

| Check | Setting | Where |
|---|---|---|
| Peer cert chains to the org bundle | `sslVerifyServerCert = true` | `[sslConfig]`, `[pythonSslClientConfig]`, `[kvstoreSslClientConfig]`, `[tcpout:group1]` |
| Peer cert names **the host dialled** | `sslVerifyServerName = true` | same stanzas, **except** `[kvstoreSslClientConfig]` (see KV store below) |
| Peer cert is a **Splunk tier** cert | `sslAltNameToCheck = <tier service FQDNs>` | `[sslConfig]`, `[tcpout:group1]`, inputs `[SSL]`, `[replication_port-ssl]` |
| Client presents a cert (mutual TLS) | `requireClientCert = true` | inputs `[SSL]` (S2S 9997), `[replication_port-ssl://9887]` |

**Host name validation** checks the host part of the URL splunkd dialled. The
SAN policy allows no IPs and no short names, so every name a peer dials must be
a `.svc.cluster.local` FQDN. Splunk and the operator do not do that on their
own. Four changes, all found in the lab, make it so:

1. **`serverName` = pod FQDN, set before the first start.** Every defaults
   file writes `[general] serverName = {{ ansible_fqdn }}` through
   `splunk.conf`, which is applied pre-start, and also sets `server_name`.
   Otherwise `serverName` stays `$HOSTNAME`, the short pod name, and the cluster
   manager calls peers back at `https://splunk-idxc-indexer-0:8089`.
   `server_name` alone is applied too late: after the first join attempt.
2. **`register_forwarder_address` / `register_search_address`** set to the pod
   FQDN on indexers. Otherwise indexer discovery and distributed search hand
   out pod IPs.
3. **`namespace: splunk` on every `licenseManagerRef` / `clusterManagerRef`.**
   The operator then emits FQDN `SPLUNK_*_URL`s instead of short service
   names.
4. **`SPLUNK_DEPLOYER_URL`** set to the deployer's FQDN in the SHC and MC
   `extraEnv`. The operator always passes it short, and `extraEnv` wins over
   both the operator's env and the MC's `envFrom`.

The cluster manager reaches itself as `localhost`, which is why `localhost` is
a SAN.

**Pinning** matters because the org PKI also issues user certificates. A valid
chain plus a matching name could otherwise be any org-issued cert for that
name. The pin limits peers to the five tier service names, each in exactly one
tier cert. Pinning proves the peer is *a* node of an allowed tier, and host name
validation narrows that to *the* node dialled.

**Client certificates** are required on S2S and replication. Each also pins
which tiers may connect: S2S takes SH, CM/LM/MC and HWF; replication takes
indexers only. They are **not** required on 8089. The operator's REST client
(`pkg/splunk/client/enterprise.go`, `NewSplunkClient`) presents no client cert
and connects from the operator pod. Splunk exempts only localhost-only
connections from mTLS, which covers splunk-ansible and the probes but not the
operator. 8089 stays server-verified and host-name-checked; admin credentials
and `pass4SymmKey` gate it.

**KV store** needed three things, all found in the lab:

- **`clientCert` in `[kvstoreSslClientConfig]`.** Once any setting is in that
  stanza, the 10.4 spec requires `clientCert`. Without it splunkd launches
  mongod with an empty `--tlsClusterFile` and KV store fails with
  `Invalid MongoSSLOption: pem_file`.
- **Host name check off for KV store only.** On every non-SHC instance (LM,
  CM, MC, deployer, HWF, ingestors) splunkd dials its own mongod as
  `<short-hostname>:8191`, and the SAN policy forbids short names: `hostname
  mismatch calling hello on 'splunk-shc-search-head-0:8191'`.
  `[kvstore] replication_host` was tried and has no effect on a single
  instance, as its spec says. So `[kvstoreSslClientConfig] sslVerifyServerName
  = false`. Chain verification and the client cert stay on, and the connection
  never leaves the pod.
- **An O, OU or DC field in the certificate subject.** The tier certs set
  `subject.organizations`. If your ACME service strips subject fields, KV store
  fails; check with `openssl x509 -noout -subject -in tls.crt`.

With those three settings and the four-cipher TLS 1.2 list, KV store reaches
`ready`.

## Why splunk-ansible defaults and not a conf app

splunk-ansible writes TLS and forwarding settings into `etc/system/local` on
every pod start, and `system/local` beats any app's `default/`. An earlier
version of this example shipped a conf app through App Framework and lost on
every count:

- **outputs.conf** — ansible sets `[tcpout] defaultGroup = group1` for indexer
  discovery, so the app's `[tcpout:idxc]` group was never used.
- **inputs.conf** — ansible's S2S task points `[SSL] rootCA` at Splunk's
  shipped `cacert.pem` unless `splunk.s2s.ca` is given.
- **server.conf** — see replication below.
- **web.conf** — it presented the public cert, which Traefik's re-encrypt leg
  cannot verify by a `.svc.cluster.local` name.

`02-tls-defaults.yaml` sets the same things through ansible's own keys
(`splunk.ssl`, `splunk.s2s`, `splunk.hec`, `http_enableSSL*`, `splunk.conf`).
They land in `system/local` before splunkd first starts. Each CR mounts the
ConfigMap and loads it with `defaultsUrl` (`common.yml`, or `indexer.yml` for
the IndexerCluster). It merges with any inline `defaults:`; `splunk.conf` is a
list, so it must be defined in exactly one of them.

## Index replication (9887) over TLS

`indexer.yml` sets `idxc.replication_ssl: true` and a
`[replication_port-ssl://9887]` stanza with `requireClientCert`, the
indexer-only pin and the four TLS 1.2 ciphers. 10.4.4's splunk-ansible
(CSPL-4006) sees the SSL stanza and removes the plaintext
`[replication_port://9887]` that `splunk edit cluster-config` writes. The
10.2.0 image lacked that fix, which is one reason this example moved to 10.4.

**Replication is TLS 1.2 only.** Its listener ignores the global `[tls1.3]`
`cipherSuite`. In the lab it accepted all three TLS 1.3 suites, including with
the suite named in its own `cipherSuite`, and the stanza has no TLS 1.3 setting
of its own. Limiting the port to `sslVersions = tls1.2` makes its four-suite
list apply. Peers negotiate 1.2 automatically, and the lab confirmed RF and SF
are met afterwards.
## TLS versions and ciphers (10.4)

Both TLS 1.2 and 1.3 are on; 1.0 and 1.1 are off (10.4 refuses to configure
them without `deprecatedTlsVersionSupport`). Everywhere:

```ini
sslVersions          = tls1.2,tls1.3     # [sslConfig], web [settings], inputs [SSL]/[http], outputs  (replication: tls1.2)
sslVersionsForClient = tls1.2,tls1.3     # [sslConfig] - splunkd as a client
cipherSuite = ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256

[tls1.3]                                 # server.conf only, GLOBAL
cipherSuite = TLS_AES_256_GCM_SHA384
```

- **TLS 1.2: exactly four suites.** `cipherSuite` is repeated in every stanza
  that has its own, because a per-stanza value replaces, not inherits, the
  `[sslConfig]` one. The certs are RSA, so only the two `ECDHE-RSA` suites are
  ever negotiated. The `ECDHE-ECDSA` ones are listed for an ECDSA cert.
- **TLS 1.3: exactly one suite.** `[tls1.3]` is global: one stanza in
  `server.conf` governs splunkd, Splunk Web, HEC and S2S. There is no
  per-component override. The **exception is replication (9887)**, which ignores
  it, so that port is TLS 1.2 only (see above).
- Splunk's own default list includes non-PFS suites "added to support the kv
  store". The lab confirmed KV store reaches `ready` without them (see
  `lab/README.md`).
- Both ends must share a version and a suite, so roll every tier together.

## Certificate layout

- **`tls-combined.pem`** (key + chain in one file) is what `serverCert` /
  `clientCert` want. cert-manager emits it via
  `additionalOutputFormats: [{type: CombinedPEM}]`.
- **`encoding: PKCS1`** — Splunk expects traditional RSA PEM.
- **`usages: [server auth, client auth, …]`** — S2S and forwarding present
  these certs as *client* certs. If your ACME service only issues `serverAuth`,
  mutual TLS fails; check with
  `openssl x509 -noout -ext extendedKeyUsage -in tls.crt`.
- **Every pod FQDN** (`<pod>.<headless>.splunk.svc.cluster.local`) is listed
  explicitly, because ACME services often refuse wildcards. **Scaling a tier
  past the listed pods requires adding names first**: a pod missing from its
  cert fails its own readiness probe.
- **`ca.crt` from cert-manager is not used** — ACME issuers leave it empty.
  Trust always comes from `all-trusted-partners`, mounted at
  `/mnt/splunk-ca/ca.crt`.

## Health probes

The operator's shipped probes call `curl --insecure https://localhost:8089/`.
`probes/` replaces all three. Each one connects to the pod's **own FQDN**, read
straight from the kubelet-written `/etc/hosts`. The image has no `hostname`
binary, and `getent hosts` returns `::1` for the short name via
nss-myhostname instead of the FQDN line. It pins that name to 127.0.0.1 with
`--resolve` and verifies against the org bundle:

```bash
curl --cacert /mnt/splunk-ca/ca.crt \
  --resolve "$FQDN:8089:127.0.0.1" "https://$FQDN:8089/"
```

The traffic never leaves the pod; only the name being verified changes, to one
the SAN policy allows. The probes **fail closed** if the bundle is unreadable or
no `.svc` FQDN is found. Set `SPLUNK_TLS_PROBE_HOST` in `extraEnv` to override
the name.

Install them **before the first CR in the namespace**:

```bash
kubectl -n splunk create configmap splunk-splunk-probe-configmap \
  --from-file=probes/livenessProbe.sh \
  --from-file=probes/readinessProbe.sh \
  --from-file=probes/startupProbe.sh
```

All three scripts must be present. The operator creates this ConfigMap with its
defaults if it does not exist and **never overwrites it** afterwards. It is
shared by every CR in the namespace.

## Applying

```bash
# all-trusted-partners must already exist in the splunk namespace
kubectl -n splunk get secret all-trusted-partners

kubectl apply -f 00-issuers.yaml        # skip if your org ClusterIssuer exists
kubectl apply -f 01-certificates.yaml
kubectl -n splunk get certificate       # wait for READY=True on all four
kubectl apply -f 02-tls-defaults.yaml
kubectl -n splunk create configmap splunk-ansible-library \
  --from-file=ansible-library/splunk_api.py
# probe ConfigMap (above), then the CRs
```

## Rollout on an existing cluster

Defaults are re-applied on every pod start, so changing them means a rolling
restart. Enabling enforcement before every peer has its cert will cut the
cluster apart. Stage it:

1. Apply certs and mounts, with `sslVerifyServerCert`, `requireClientCert` and
   the `sslAltNameToCheck` lines removed from `02-tls-defaults.yaml`. Roll every
   tier.
2. Confirm every tier is healthy and talking. A pod whose ansible run ends
   without `check_for_required_restarts` failures is verifying its own REST
   calls - the override has no unverified fallback.
3. Restore the verification lines and roll again.

Then check splunkd for peer rejections:

```bash
kubectl -n splunk exec splunk-cm-cluster-manager-0 -- \
  grep -iE 'X509|certificate|ssl' /opt/splunk/var/log/splunk/splunkd.log | tail -50
```

A rejected peer usually means an issuer missing from `all-trusted-partners`, a
pod name missing from its cert, or a cert without `clientAuth`.

Rotation is handled by cert-manager, but Splunk reads certs at startup: a
renewed secret does **not** take effect until the pods restart.
