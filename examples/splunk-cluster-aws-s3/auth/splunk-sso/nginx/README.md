# Proxy SSO: nginx sidecar with client-certificate auth

Answers the question directly: **Splunk has no way to identify a person from a
certificate on its own.** Splunk's SSO consumes a *username in a header*. It
never sees or parses the certificate. What makes cert login work is that nginx
does the X.509 validation, extracts the CN, and hands Splunk the username.

So yes, you can log in with a certificate and no IdP at all — the identity
mapping just lives in nginx rather than in Splunk.

```
browser ──TLS + client cert──> Traefik (passthrough) ──> nginx sidecar :8443
                                                            │ verifies cert vs user CA
                                                            │ checks CRL
                                                            │ CN -> REMOTE_USER
                                                            ▼
                                            Splunk Web 127.0.0.1:8000 (loopback only)
```

## Correction: the operator does not support sidecars

Your premise was that the Helm chart can run a sidecar. At 3.1.0 it cannot, and
neither can the operator:

- The pod spec is built in Go with a hardcoded one-element container list —
  `Containers: []corev1.Container{{... Name: "splunk"}}` in
  `pkg/splunk/enterprise/configuration.go`.
- There is no `sidecar`, `extraContainers`, or pod-template override field in
  any CRD or in `api/v4/common_types.go`.
- The `splunk-enterprise` Helm chart only templates **custom resources**. It
  never constructs a pod, so it has nowhere to add a container.

`02-kyverno-sidecar-policy.yaml` therefore injects the sidecar at pod admission.
The operator compares StatefulSet specs, not pod specs, so it neither reverts
the injection nor fights it. Verify Kyverno is installed before relying on this.

If you cannot run an admission controller, the fallback is nginx as a **separate
Deployment** in front of the Splunk service — but read the next section first,
because it costs you the property that makes this design sound.

## Why the sidecar matters so much here

Header-based SSO is normally a bad trade: Splunk believes whatever username
arrives in a header from a trusted IP, so anything that can reach the port
becomes any user, including admin. That is exactly the objection raised against
oauth2-proxy in `../../saml/`.

The sidecar dissolves that objection, because two settings line up:

```ini
server.socket_host = 127.0.0.1   # Splunk Web listens on loopback ONLY
trustedIP          = 127.0.0.1   # and only loopback may assert a username
```

Containers in a pod share a network namespace, so nginx reaches Splunk over
genuine loopback — while nothing outside the pod can reach `:8000` at all, or
originate a connection that satisfies `trustedIP`. The trust boundary is the pod
itself.

Run nginx as a separate Deployment instead and both properties evaporate:
Splunk must bind `0.0.0.0`, `trustedIP` must widen to a pod CIDR, and any
workload in that range can impersonate any user. If you go that route, a
NetworkPolicy restricting `:8000` to the nginx pods is not optional, and it is
still weaker than the sidecar.

## What must not move to loopback

**Only Splunk Web (8000).** Do not bind the management port:

Port 8089 carries cluster-manager↔peer traffic, search-head↔peer search,
replication, and the operator's own REST calls — all of which cross pod
boundaries. Binding it to loopback breaks the cluster and locks the operator
out. It is protected by mutual TLS instead (`../../../tls/`).

## The line that carries the security

```nginx
proxy_set_header REMOTE_USER $client_cn;
```

`proxy_set_header` **replaces** any inbound header of that name, so a client
cannot smuggle its own `REMOTE_USER`. Never make it conditional, and never
switch it to `add_header`. The config also returns 403 when
`$ssl_client_verify` is not `SUCCESS` or the CN is empty, so it fails closed.

## Revocation is the weak spot

nginx supports **CRL only** for client certificates — there is no OCSP check on
the client side. `ssl_crl` reads a file at startup/reload, so:

- the CRL must be refreshed and nginx reloaded on a schedule, and
- between refreshes a revoked certificate still works.

This is a genuine regression versus the Keycloak path, which can do live OCSP.
If your PKI revokes often, prefer `../../saml/keycloak-x509/`. Automate the CRL
with a CronJob that updates the secret and triggers a rolling restart.

## Identity source

The config parses the CN out of `$ssl_client_s_dn`. If your PKI puts the real
identity in a SAN (common for smartcards and SPIFFE-style certs), that regex is
the wrong hook — nginx exposes SANs only via `$ssl_client_cert` parsing or njs.
Check what your certificates actually carry before wiring this up.

The extracted CN must match a Splunk username — either a native user or one
resolvable through LDAP. Proxy SSO authenticates; it does not create users or
assign roles. Role assignment still happens in Splunk.

## Applying

```bash
kubectl -n splunk create secret generic user-pki-ca \
  --from-file=user-ca.pem --from-file=user-ca.crl

kubectl apply -f 01-nginx-configmap.yaml
kubectl apply -f 02-kyverno-sidecar-policy.yaml
kubectl apply -f 03-service-and-route.yaml

tar -czf splunk_sso_app.tgz splunk_sso_app/
aws s3 cp splunk_sso_app.tgz s3://my-splunk-apps/search-apps/
```

The sidecar appears only on pod recreation — Kyverno mutates at admission, so
existing pods keep running unchanged until they restart.

Start with `SSOMode = permissive` so the normal login page still works, confirm
certificate login end to end, then switch to `strict`. Going straight to strict
with a broken CN mapping locks every user out, including you.
