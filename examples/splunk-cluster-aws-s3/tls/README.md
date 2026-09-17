# TLS

Two goals, and they are not the same thing:

1. **Encryption** — nobody on the network can read the traffic.
2. **Authentication** — each side proves it is the machine it claims to be.

Encryption without authentication is the `curl --insecure` failure: the channel
is private, but you have no idea who is on the other end of it. Everything here
is configured for both.

## What is and is not secure out of the box

| Channel | Port | Default | After this config |
|---|---|---|---|
| splunkd mgmt / REST | 8089 | TLS, but with **Splunk's shipped certs** | Our CA, verified both ways |
| Index replication | 9887 | plaintext | TLS + `requireClientCert` |
| S2S forwarder data | 9997 | plaintext | TLS + `requireClientCert` |
| Splunk Web | 8000 | HTTP | HTTPS, public cert |
| HEC | 8088 | HTTP | HTTPS, token auth |

The 8089 row is the one that surprises people. splunkd *is* TLS by default — but
with certificates whose private keys ship inside every Splunk distribution on
earth. Anyone can present one. So `sslVerifyServerCert = true` against the
default CA authenticates nothing at all; replacing the CA is what makes it mean
something.

## The two settings that matter

```ini
sslVerifyServerCert = true   # the peer's cert chains to our CA
sslVerifyServerName = true   # the peer's CN/SAN matches who we dialled
```

**Both, always.** `sslVerifyServerCert` on its own checks only the chain, so any
certificate our CA ever issued is accepted from any peer — an attacker with a
single valid cert from our CA could impersonate any node. `sslVerifyServerName`
is what turns "signed by someone we trust" into "is the host we meant".

On the forwarding side the equivalent is `sslCommonNameToCheck` /
`sslAltNameToCheck` in `outputs.conf`, which pin the acceptable identity.

Mutual TLS — `requireClientCert = true` — is set on every receiving channel, so
an indexer will not accept replication or events from a peer that cannot prove
its own identity.

## Certificate layout

`tls-combined.pem` (key + cert in one file) is what Splunk's `serverCert` wants.
cert-manager emits it via `additionalOutputFormats: [{type: CombinedPEM}]`,
which needs the `AdditionalCertificateOutputFormats` feature gate on older
installs. If that file is missing from the secret, the gate is why.

`encoding: PKCS1` is set deliberately — Splunk expects traditional RSA PEM, and
cert-manager's PKCS8 default fails to load with an unhelpful error.

SANs must cover both the service name and the per-pod headless names, because
peers connect to pods directly. A missing SAN shows up as a handshake failure
once `sslVerifyServerName` is on, even though the chain is perfectly valid.

## Health probes

The operator's shipped probes call `curl --insecure` against
`https://localhost:8089/`. `probes/` replaces all three with versions that pass
`--cacert` instead and **fail closed** if the CA bundle is unreadable, rather
than silently falling back to an unverified request.

This is why every internal certificate carries a `localhost` DNS SAN and
`127.0.0.1` — without them a verifying probe fails on hostname mismatch.

Install them **before the first CR in the namespace**:

```bash
kubectl -n splunk create configmap splunk-splunk-probe-configmap \
  --from-file=probes/livenessProbe.sh \
  --from-file=probes/readinessProbe.sh \
  --from-file=probes/startupProbe.sh
```

All three scripts must be present. The operator creates this ConfigMap with its
defaults if it does not exist, and **never overwrites it** afterwards — so if
you miss the window, edit it in place rather than expecting a re-create.

## Applying

```bash
kubectl apply -f 00-issuers.yaml
kubectl apply -f 01-certificates.yaml
kubectl -n splunk get certificate    # wait for READY=True on all four

# Package and upload the conf app, then reference it from appRepo
tar -czf splunk_tls_app.tgz splunk_tls_app/
aws s3 cp splunk_tls_app.tgz s3://my-splunk-apps/cm-apps/
aws s3 cp splunk_tls_app.tgz s3://my-splunk-apps/search-apps/
```

Ship the app to the indexers through the **ClusterManager** `appRepo` with
`scope: cluster`, and to the search heads through the **SearchHeadCluster**
`appRepo`, same scope. The conf files reference `/mnt/splunk-tls/...`, which is
where the CR `volumes:` stanza mounts the cert-manager secret.

## Rollout order

Turning on `requireClientCert` before every peer has a certificate will break
the cluster — peers that cannot authenticate are simply refused. Stage it:

1. Mount certs and set `serverCert` / `sslRootCAPath` everywhere. Restart.
2. Confirm every tier is healthy and talking.
3. Then enable `sslVerifyServerCert`, `sslVerifyServerName`, `requireClientCert`.

Rotation is handled by cert-manager, but Splunk reads certs at startup: a
renewed secret does **not** take effect until the pods restart.
