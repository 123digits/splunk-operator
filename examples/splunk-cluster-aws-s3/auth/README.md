# User authentication

**SAML 2.0 against Keycloak.** Users authenticate to Keycloak with a client
certificate; Keycloak issues a signed assertion to Splunk.

Splunk cannot identify a person from a certificate itself. Verified against the
10.4 `authentication.conf` reference: there is no x509 or client-certificate
user authentication anywhere in it, and `authType` accepts exactly `Splunk`,
`LDAP`, `Scripted`, `SAML` and `ProxySSO`. There is also no OIDC
authorization-code flow — no `redirect_uri`, `authorization_endpoint` or
`response_type` exists in the file. So the certificate is validated by Keycloak,
which then tells Splunk who the user is over signed SAML.

## Layout

| Path | What it holds |
|---|---|
| `saml/` | The Splunk app (`authentication.conf`) and Keycloak SAML client setup |
| `saml/keycloak-realm/` | Realm roles and the flow that denies everyone else |
| `saml/keycloak-x509/` | Certificate login, terminated at Keycloak |

## Realm and roles

| | |
|---|---|
| Keycloak realm | `jcsc-oauth` |
| Client | `splunk-hs` |
| Admin | realm role `s4k_hs_admin` -> Splunk `admin` |
| Standard | realm role `s4k_hs_user` -> Splunk `user` |

Both map onto built-in Splunk roles, so there is no `authorize.conf` to ship.
Note `user` is the least-privilege built-in but is not strictly read-only — it
can create its own knowledge objects and schedule searches.

A third role, `s4k_hs_access`, exists only as a composite marker inside
`s4k_hs_admin` and `s4k_hs_user` so the login gate has a single role to test.
Nobody is assigned it directly.

## Setup order

1. `saml/keycloak-realm/` — import the roles, bind the deny flow to `splunk-hs`
2. `saml/` — create the SAML client, load the realm signing cert as the
   `splunk-saml-idp` secret, ship the app through the deployer
3. `saml/keycloak-x509/` — certificate login (passthrough variant recommended)

## What applies regardless

- **The local `admin` account must keep working.** The operator authenticates as
  `admin` using the password in `splunk-<ns>-secret` for bundle pushes and
  cluster operations. SSO is for humans; do not disable native auth.
- **Port 8089 stays on the network.** Only Splunk Web (8000) could ever be
  restricted. Cluster traffic, replication and the operator's REST calls all
  need 8089 across pods; it is protected by mutual TLS in `../tls/`.
- **User certificates come from your own PKI**, never the cert-manager internal
  CA in `../tls/`. Sharing them would let a pod's service certificate
  authenticate as a person.
- **These are Splunk product settings, not operator features.** The operator has
  no authentication support in 3.1.0 — it just ships the app. Unlike the CR
  manifests, nothing here is schema-validated by this repo, so check key names
  against the `.spec` files for your Splunk version.
