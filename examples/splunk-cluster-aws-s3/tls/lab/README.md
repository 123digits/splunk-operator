# TLS lab (Docker)

A five-container Splunk 10.4.4 cluster wired the way the operator wires this
example's pods, for testing `tls/` end to end without Kubernetes. Everything in
`../02-tls-defaults.yaml`, `../ansible-library/` and the CR changes was
developed and verified here.

| Container | Pod it stands in for | Cert (`.generated/pki/…`) | Defaults |
|---|---|---|---|
| `lab-lm` | `splunk-lm-license-manager-0` | `manager` | `common.yml` |
| `lab-cm` | `splunk-cm-cluster-manager-0` | `manager` | `common.yml` |
| `lab-idx0`, `lab-idx1` | `splunk-idxc-indexer-0/1` | `indexer` | `indexer.yml` |
| `lab-sh` | `splunk-shc-search-head-0` (standalone, attached to the CM) | `searchhead` | `common.yml` |

## Running it

```bash
cd examples/splunk-cluster-aws-s3/tls/lab
./lab.sh up      # PKI + defaults, then docker compose up
./lab.sh wait    # ~10 min on first start
./lab.sh check
./lab.sh down
```

`up` **accepts the Splunk license and General Terms** on your behalf
(`--accept-license`, `--accept-sgt-current-at-splunk-com`). Needs Docker with
compose v2 and about 10 GB of free memory. Works from Git Bash on Windows.

## How it mirrors a pod

| Pod | Lab |
|---|---|
| StatefulSet `/etc/hosts`: `<ip> <pod>.<headless>.splunk.svc.cluster.local <pod>` | `hostname` + `domainname`, which produce the same line |
| Service and pod DNS names | network aliases |
| CR volumes `splunk-tls`, `splunk-ca`, `tls-defaults`, `ansible-library` | bind mounts at the same `/mnt/<name>` paths |
| `defaultsUrl`, then the operator's secrets file, last | `SPLUNK_DEFAULTS_URL` in the same order |
| `extraEnv` `ANSIBLE_LIBRARY`, `SPLUNK_TLS_CA_PATH` | same env |
| `SPLUNK_*_URL` once CR refs carry `namespace: splunk` | the same FQDNs; the CM points at itself as `localhost`, as the operator does |

`.generated/defaults/` is extracted from `../02-tls-defaults.yaml` on every
`up`, so the lab always runs the file you will deploy, never a copy.

`gen-pki.py` builds certs with the same shape as the org's:

- two roots in the trust bundle;
- only `.svc.cluster.local` and `localhost` SANs, with CN of 63 characters or fewer;
- an `O=` field;
- serverAuth and clientAuth;
- cert-manager's `tls-combined.pem` / `tls.crt` / `tls.key` layout.

It also builds one org-issued **user** cert for the negative tests.

**Lab-only differences.** There is no license file, so the trial license
refuses remote license peers. That surfaces as `This license does not support
being a remote manager`, a licensing message that only appears after a verified
TLS connection. The lab also runs no SmartStore, no search head cluster, no
monitoring console and no Traefik.

## What `check` verifies

1. **ansible** finishes with `failed=0` everywhere. `Check for required
   restarts`, the task that failed in production, is `ok` on its first attempt.
   It now goes to `https://localhost:8089` and is verified against the bundle.
2. **Effective config** comes from `btool`: versions, the four TLS 1.2 ciphers,
   the TLS 1.3 suite, verification flags, `serverName` (pod FQDN),
   `register_*_address` and `replication_port-ssl`.
3. **Indexer cluster:** all peers are Up, RF and SF are met, and replication
   runs over TLS.
4. **Distributed search** from the SH returns events from both indexers and
   from every tier. That proves forwarding with client certs and pinned
   identities.
5. **KV store** on the SH reaches `ready` with the four-cipher TLS 1.2 list.
6. **splunkd.log** has no certificate, TLS or host name errors.
7. **TLS probes** (`tlsprobe.py`) run against indexer 0 on 8089, 8000, 8088,
   9997 and 9887:
   - TLS 1.3 accepts only `TLS_AES_256_GCM_SHA384`, and is refused entirely on
     9887 (TLS 1.2 only);
   - TLS 1.2 accepts the ECDHE-RSA GCM suites and refuses non-PFS and CBC
     suites;
   - TLS 1.1 is refused;
   - 9997 and 9887 refuse a missing client cert and an org **user** cert, and
     accept the pinned tier cert;
   - 9887 refuses the wrong tier's cert;
   - 8089 accepts a connection with no client cert, by design.

## Steps taken to get here

Each step is recorded because each one was a failure the lab exposed.

1. **Reproduced the original failure.** The stock `splunk_api` module dials
   `https://127.0.0.1:8089`. Its tasks never pass `verify`, so python-requests
   falls back to `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE`.
   - With a bundle lacking the org root, it fails with `self signed
     certificate in certificate chain`, which is your error.
   - With the right bundle, it fails with `hostname '127.0.0.1' doesn't match`.
2. **Found why the pod env could not fix it.** `ansible.cfg` runs every
   module through `sudo -i`, whose `env_reset` drops the pod environment.
   Variables only reach a module through login-shell files or
   `ansible_environment`. Blanking the variables in `extraEnv` therefore did
   nothing.
3. **Replaced the module.** `../ansible-library/splunk_api.py` dials
   `localhost` and always verifies against `/mnt/splunk-ca/ca.crt`.
   `ANSIBLE_LIBRARY`, read by `ansible-playbook` before any `sudo`, puts it
   ahead of the stock module.
4. **Moved to 10.4.4.** TLS 1.3 and its cipher control only exist from 10.4.0.
   10.4.4's splunk-ansible also handles `replication_port-ssl` (CSPL-4006).
   `splunk_api.py` is byte-identical between 10.2.0 and 10.4.4, so the override
   carried over unchanged.
5. **`{{ splunk.server_name }}` recursed.** A value inside the `splunk` dict
   cannot reference `splunk`. Ansible failed at `Provision role` with an empty
   message. Switched to `{{ ansible_fqdn }}`.
6. **The CM called peers back by short name.** The operator sets no
   pod-identity env vars, so splunk-ansible left `serverName` at `$HOSTNAME`,
   the short pod name. The CM registered peers and called them at
   `https://splunk-idxc-indexer-0:8089`, which host name validation rejected:
   `certificate verify failed`. Setting `server_name: "{{ ansible_fqdn }}"`
   alone was **not enough**. splunk-ansible applies `server_name` only after
   splunkd has started and the peer has already tried to join, so the first
   registration still carried the short name and the join looped forever. The
   fix that worked also writes `[general] serverName = {{ ansible_fqdn }}`
   through `splunk.conf`, which splunk-ansible applies **before** the first
   start.
7. **Peers advertise FQDNs.** `register_forwarder_address` and
   `register_search_address` are set to `{{ ansible_fqdn }}`. Indexer discovery
   and distributed search would otherwise hand out pod IPs, which no SAN may
   carry.
8. **KV store needed `clientCert`.** With only verification flags in
   `[kvstoreSslClientConfig]`, mongod got an empty `--tlsClusterFile` and KV
   store failed (`Invalid MongoSSLOption: pem_file`). The 10.4 spec requires
   `clientCert` once that stanza is used.
9. **KV store cannot pass a host name check on a single instance.** splunkd
   dials its own mongod at `<short-hostname>:8191`. `[kvstore]
   replication_host` was tried and has no effect there, as its spec says. So
   `sslVerifyServerName = false` in that one stanza only; chain verification
   and the client cert stay on. KV store then reached `ready`.
10. **The replication port ignores `[tls1.3]`.** It accepted all three TLS 1.3
    suites, including with the suite named in its own `cipherSuite`. With
    `sslVersions = tls1.2` on that port, TLS 1.3 is refused and only the
    allowed TLS 1.2 suites connect. RF and SF were met again after the change.
11. **The S2S probe was wrong, not the config.** It sent HTTP to 9997, and the
    indexer closes non-S2S connections whether or not the cert was accepted.
    The probe now sends nothing on 9997 and 9887, and treats being left waiting
    as accepted.
12. **Two non-TLS messages were ruled out.** The CM logged `Failed to verify
   HMAC signature` on `/services/indexer_discovery` only before its first
   restart; it stopped once ansible's `pass4SymmKey` was loaded. The LM's
   `does not support being a remote manager` is the trial license.
