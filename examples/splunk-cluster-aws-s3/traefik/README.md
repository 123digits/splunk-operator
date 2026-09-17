# Traefik

Note up front: the operator's own docs cover Istio and NGINX only — there is no
Traefik guidance upstream at 3.1.0. These manifests are written against
`traefik.io/v1alpha1` (Traefik v3). On Traefik v2 the group is
`traefik.containo.us/v1alpha1`; the schemas are otherwise the same here.

## Which pods actually have a UI

**All of them.** The operator gives port 8000 (Splunk Web) and 8089 (splunkd) to
every instance type — see `getSplunkPorts` in
`pkg/splunk/enterprise/configuration.go`. So all 9 pods in this deployment serve
a UI. That is not the same as all 9 being worth exposing:

| Service | UI | Expose? |
|---|---|---|
| `splunk-shc-search-head-service` | searching, dashboards | Yes — this is the one users want |
| `splunk-mc-monitoring-console-service` | health, topology | Yes — for operators |
| `splunk-cm-cluster-manager-service` | indexer cluster status, bundle pushes | Yes — for operators |
| `splunk-lm-license-manager-service` | license usage | Rarely; port-forward is usually enough |
| `splunk-shc-deployer-service` | deployer status | No — port-forward when needed |
| `splunk-idxc-indexer-service` | per-peer UI | No — use the MC instead |

The three routes in `02-ingressroute-ui.yaml` cover the first three. Reach the
rest with `kubectl port-forward` rather than widening your attack surface for
something you open twice a year.

## Termination model, and why it differs per channel

Splunk's own support matrix (`docs/Security.md`) is blunt about this:

| Traffic | Gateway termination | End-to-end |
|---|---|---|
| Splunk Web | **NO** | YES |
| REST API | **NO** | YES |
| Forwarders | YES | YES |

"Gateway termination" there means terminating TLS at the edge and sending
**plaintext** to the pod. That is unsupported for Web and REST. What these
manifests do instead is terminate at Traefik and **re-encrypt** to the pod, so
no leg is ever plaintext — which satisfies the constraint while still letting
Traefik act as an HTTP router.

That re-encryption is only worth anything if Traefik verifies the backend,
which is what `01-serverstransport.yaml` is for. A `scheme: https` backend
without a `ServersTransport` gets you an encrypted, unauthenticated connection —
the same weakness as `--insecure`, just relocated to the proxy. Hence
`insecureSkipVerify: false` and an explicit `serverName`.

## Why S2S is passthrough and Web is not

`03-ingressroutetcp-s2s.yaml` uses **TLS passthrough** for forwarder data,
deliberately. The indexers set `requireClientCert = true`, and mutual TLS cannot
survive edge termination: if Traefik terminated, Traefik would become the client
and the indexer would authenticate *Traefik*, not the forwarder. Every forwarder
would collapse into one identity. Passthrough keeps the client certificate
end to end.

HEC is different again — it authenticates with a bearer token, not a client
cert, so terminating and re-encrypting loses no identity and buys you normal
HTTP routing.

## The stickiness trap

Splunk Web needs **session stickiness**. On a 3-member search head cluster,
round-robining a logged-in session produces random logouts and blank pages.

This is the real reason the UI routes are `IngressRoute` and not
`IngressRouteTCP`: passthrough is raw TCP, so Traefik cannot set a cookie and
cannot do sticky sessions. If you must use passthrough for Splunk Web — for
example because policy forbids terminating anywhere but the pod — you have to
either point the route at a single search head, or accept the breakage. The
`sticky.cookie` block in `02-ingressroute-ui.yaml` is not optional decoration.

## Prerequisites

The S2S route needs a dedicated entrypoint in Traefik's **static** config;
IngressRouteTCP cannot create one:

```yaml
entryPoints:
  websecure:
    address: ":443"
  splunk-s2s:
    address: ":9997"
```

If you use these Traefik routes, the NLB services in `08-ingest-endpoints.yaml`
become redundant for external access — Traefik is the front door instead. Keep
them only if you also want forwarders bypassing Traefik.

`HostSNI` requires the client to send SNI. Splunk forwarders do when configured
with a server name; if yours do not, use `HostSNI(\`*\`)` on the dedicated
entrypoint, which matches any connection on that port.
