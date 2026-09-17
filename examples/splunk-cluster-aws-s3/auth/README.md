# User authentication

Two working ways to log a person into Splunk with a client certificate. Both are
built here; they suit different constraints.

**Neither involves Splunk reading the certificate.** Splunk Enterprise cannot
map an X.509 certificate to a user — it has no OIDC relying party either. Its
only options are native auth, LDAP, SAML 2.0, and header-based proxy SSO. So the
certificate is always validated by something in front, which then tells Splunk
who the user is.

| | `saml/keycloak-x509/` | `splunk-sso/nginx/` |
|---|---|---|
| Who checks the cert | Keycloak | nginx sidecar |
| How Splunk learns identity | Signed SAML assertion | `REMOTE_USER` header over loopback |
| Extra infrastructure | Keycloak realm | Kyverno (for sidecar injection) |
| Revocation | CRL **and** OCSP | CRL only, needs refresh automation |
| Roles | From IdP group attributes | Assigned in Splunk (LDAP or native) |
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
