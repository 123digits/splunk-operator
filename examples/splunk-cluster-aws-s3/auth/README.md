# User authentication

Two working ways to log a person into Splunk with a client certificate. Both are
built here; they suit different constraints.

**Neither involves Splunk reading the certificate.** Verified against the 10.4
`authentication.conf` reference: there is no x509 or client-certificate user
authentication anywhere in it (the one `clientCert` setting is for Splunk's own
outbound TLS to LDAP/SAML, not for authenticating people). `authType` accepts
exactly `Splunk`, `LDAP`, `Scripted`, `SAML` and `ProxySSO`.

So the certificate is always validated by something in front, which then tells
Splunk who the user is.

Splunk 10.x does validate external OAuth2 JWTs — see `oauth2/`. That is
bearer-token access for **API clients**, with no redirect flow of any kind, so
it complements the options below rather than replacing them. Both it and the
SAML path map the same Keycloak realm roles to the same Splunk roles.

| | `saml/keycloak-x509/` | `splunk-sso/nginx/` |
|---|---|---|
| Who checks the cert | Keycloak | nginx sidecar |
| How Splunk learns identity | Signed SAML assertion | `REMOTE_USER` header over loopback |
| Extra infrastructure | Keycloak realm | Kyverno (for sidecar injection) |
| Revocation | CRL **and** OCSP | CRL only, needs refresh automation |
| Roles | From IdP group attributes | Assigned in Splunk (LDAP or native) |
| Restricting who may log in | Role-gated deny flow per client (`saml/keycloak-realm/`) | Any CN your user CA signs — scope via the CA itself |
| Trust boundary | Signed assertion | The pod's network namespace |
| Auth method changes later | Reconfigure Keycloak, Splunk untouched | Rewrite the nginx layer |

## Which to choose

**Keycloak SAML** if you already run Keycloak, want live revocation checking, or
expect to change authentication methods later — the assertion is signed, so the
trust does not depend on network position, and swapping certs for WebAuthn or
OTP leaves Splunk's config alone.

**nginx sidecar SSO** if you want no IdP dependency in the login path and are
comfortable that revocation is CRL-on-a-timer. The loopback design makes the
header trustworthy in a way a shared reverse proxy never is.

They are not mutually exclusive — `SSOMode = permissive` plus SAML is a valid
migration path, though running both in production means two things can grant
access and both need auditing.

## Realm and roles used throughout

| | |
|---|---|
| Keycloak realm | `jcsc-oauth` |
| Client | `splunk-hs` |
| Admin | realm role `s4k_hs_admin` -> Splunk `admin` |
| Standard | realm role `s4k_hs_user` -> Splunk `user` |

Both map onto built-in Splunk roles, so there is no `authorize.conf` to ship.

Worth knowing what `user` actually grants: it is not read-only. It can create
and edit its own knowledge objects and schedule searches. For viewing existing
dashboards that is usually fine and it is the least-privilege built-in, but if
you later need true read-only, define a custom role rather than assuming `user`
is one.

**The two role-map stanzas run in opposite directions**, which is easy to invert:

```ini
[roleMap_SAML]                     # <Splunk role> = <Keycloak role>
user = s4k_hs_user

[oauth2_external_role_mapping_...] # <Keycloak role> = <Splunk role>
s4k_hs_user = user
```

## What is shared regardless

- **The local `admin` account must keep working.** The operator authenticates as
  `admin` using the password in `splunk-<ns>-secret` for bundle pushes and
  cluster operations. SSO is for humans; do not disable native auth.
- **Port 8089 stays on the network.** Only Splunk Web (8000) can be restricted.
  Cluster traffic, replication and the operator's REST calls all need 8089
  across pods; it is protected by mutual TLS in `../tls/`.
- **User certificates come from your own PKI**, never the cert-manager internal
  CA in `../tls/`. Sharing them would let a pod's service certificate
  authenticate as a person.
- **These are Splunk product settings, not operator features.** The operator has
  no authentication support at all in 3.1.0 — it just ships the app. Unlike the
  CR manifests, nothing here is validated against a schema in this repo, so
  check key names against the `.spec` files for your Splunk version.
