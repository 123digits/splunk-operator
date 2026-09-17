# External OAuth2 (Keycloak JWTs)

## What this is not

**Not a browser login.** Splunk 10.x has no OIDC authorization-code flow. The
10.4 `authentication.conf` reference contains no `redirect_uri`, no
`authorization_endpoint`, no `response_type`, no `authorization_code` — and
`authType` still accepts only `Splunk`, `LDAP`, `Scripted`, `SAML`, `ProxySSO`.
The single mention of "login flow through a browser" in the whole file is in the
SAML section.

So the flow you described — hit Splunk, redirect to Keycloak, redirect back —
**is SAML**, and it is already built in `../saml/`, now using your realm
`jcsc-oauth`, client `splunk-hs`, and roles `s4k_hs_admin` / `s4k_hs_user`. The
protocol differs; the user experience you described is identical.

## What it is

Bearer-token validation for **API and application access**. A script or service
presents a Keycloak-issued JWT to Splunk's REST API instead of a Splunk token:

```
service ──JWT──> Splunk REST :8089
                    │ validates signature via jwks_uri
                    │ checks iss / aud
                    │ reads groupsClaim -> maps to Splunk roles
                    ▼
                 authorized as s4k_hs_admin / s4k_hs_user
```

Genuinely useful alongside SAML: people log in with SAML, automation
authenticates with Keycloak JWTs, and both map through the same two realm roles.
No Splunk-local service accounts to rotate.

`[oauth2_external_app_client_<name>]` additionally pins a named client to a
fixed role list, and `[oauth2_settings]` is separate again — that is Splunk
issuing *its own* OAuth2 tokens, not consuming Keycloak's.

## The trap that will bite you in-cluster

`[oauth2_restricted_endpoints]` is SSRF protection on the JWKS fetch, and its
**defaults block private address space**:

```
ipv4_cidrs = 127.0.0.0/8, 169.254.0.0/16, 10.0.0.0/8,
             172.16.0.0/12, 192.168.0.0/16, 0.0.0.0/8
```

A Keycloak running in the same Kubernetes cluster resolves to exactly those
ranges. Point `jwks_uri` at `keycloak.keycloak.svc.cluster.local` or a ClusterIP
and **Splunk will refuse to fetch the keys**, so every token fails validation.

Options, best first:

1. Use the externally-resolvable hostname (`keycloak.example.com`) so the fetch
   leaves and re-enters through Traefik. Costs a hairpin; keeps the SSRF
   protection intact. This is what the shipped config does.
2. Narrow the CIDR list to carve out only your Keycloak service IP. You are
   weakening an anti-SSRF control — scope it to a single address, never drop
   the whole range.

## Keycloak client setup

On the existing `splunk-hs` client in realm `jcsc-oauth`:

- **Audience mapper** — Keycloak does not put the client ID in `aud` by default.
  Without one, `audience = splunk-hs` never matches and tokens are rejected with
  nothing obvious in the logs. Add a dedicated audience mapper.
- **Realm roles in the token** — confirm `realm_access.roles` actually carries
  `s4k_hs_admin` / `s4k_hs_user`. Decode a real token rather than assuming:

```bash
curl -s -d grant_type=client_credentials -d client_id=splunk-hs \
     -d client_secret=... \
     https://keycloak.example.com/realms/jcsc-oauth/protocol/openid-connect/token \
  | python3 -c 'import sys,json,base64;t=json.load(sys.stdin)["access_token"].split(".")[1];print(json.dumps(json.loads(base64.urlsafe_b64decode(t+"==")),indent=2))'
```

Check `iss` ends with a trailing slash, `aud` contains `splunk-hs`, and the
roles are where `groupsClaim` expects them. All three are exact-match and all
three fail silently.

## Roles

Both paths map to the same two Splunk roles, so a user and a service with the
same realm role get the same access:

| Keycloak realm role | Splunk role |
|---|---|
| `s4k_hs_admin` | `admin` (built-in) |
| `s4k_hs_user` | `user` (built-in) |

Both are built-in Splunk roles, so no `authorize.conf` is needed. Mind the
direction: this stanza is `<Keycloak role> = <Splunk role>`, the reverse of
`[roleMap_SAML]`.

## Deploy

Ship through the ClusterManager `appRepo` at cluster scope if API clients hit
the indexers, and through the SearchHeadCluster deployer for search-head REST.
Splunk validates the token wherever the REST call lands, so the config must
exist on every tier you expect to be called.
