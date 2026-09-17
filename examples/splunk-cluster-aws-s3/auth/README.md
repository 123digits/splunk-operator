# User authentication — SAML 2.0 with Keycloak

Users authenticate to Keycloak with a client certificate; Keycloak validates it
and issues a signed assertion to Splunk.

Splunk cannot identify a person from a certificate itself. Verified against the
10.4 `authentication.conf` reference: there is no x509 or client-certificate
user authentication anywhere in it (the one `clientCert` setting is Splunk's own
outbound TLS to LDAP/SAML), and `authType` accepts exactly `Splunk`, `LDAP`,
`Scripted`, `SAML`, `ProxySSO`. There is no OIDC authorization-code flow either
— no `redirect_uri`, `authorization_endpoint` or `response_type` exists in the
file. So validation happens at Keycloak, and Splunk learns the identity over
signed SAML.

## Files

| File | Purpose |
|---|---|
| `splunk_saml_app/` | The Splunk app. `default/` nesting is Splunk's required layout |
| `keycloak-realm.json` | Realm roles and the flow denying everyone else |
| `keycloak-mtls-passthrough.yaml` | Certificate login — Keycloak terminates TLS (**preferred**) |
| `keycloak-mtls-traefik.yaml` | Certificate login — Traefik terminates, forwards cert in a header |

## Realm and roles

| | |
|---|---|
| Keycloak realm | `jcsc-oauth` |
| Client | `splunk-hs` |
| Admin | realm role `s4k_hs_admin` -> Splunk `admin` |
| Standard | realm role `s4k_hs_user` -> Splunk `user` |

Both map onto built-in Splunk roles, so there is no `authorize.conf` to ship.
`user` is the least-privilege built-in but is **not** strictly read-only — it can
create its own knowledge objects and schedule searches.

`s4k_hs_access` is a third role existing only as a composite marker inside the
other two, so the login gate has a single role to test. Nobody is assigned it
directly.

---

## 1. Keycloak realm setup

### Roles and the access gate

Realm isolation is structural and free: a user belongs to one realm, the client
lives in one realm, and Splunk trusts exactly one realm signing certificate, so
an assertion from anywhere else fails signature verification.

The gap is **inside** the realm. Keycloak's default is that any authenticated
user can log into any client, so realm membership alone is not a restriction.

`keycloak-realm.json` imports three roles:

| Role | Composite of | Assign to people? |
|---|---|---|
| `s4k_hs_access` | — | No, marker only |
| `s4k_hs_admin` | `s4k_hs_access` | Yes |
| `s4k_hs_user` | `s4k_hs_access` | Yes |

and a `splunk-browser` flow containing a conditional sub-flow:

- **Condition - user role** → `s4k_hs_access`, **Negate output: ON**
- **Deny Access**

Reads as *if the user has neither Splunk role, deny*. Adding a third Splunk role
later means making it composite of `s4k_hs_access` too; the flow is unchanged.

If your Keycloak version lacks *Negate output* on the role condition, invert it:
make authentication conditional on holding the role, with Deny Access terminal.

### Bind the flow to the client only

Clients → `splunk-hs` → Advanced → **Authentication flow overrides** →
Browser Flow = `splunk-browser`.

This step is what makes it safe. Editing the realm's default browser flow would
deny those users **every** client in the realm.

Provider IDs and config keys in the JSON are stable across recent Keycloak but
not guaranteed across major versions — import into a test realm and compare
against a UI-built flow first. Importing grants nothing until the flow is bound.

### SAML client

| Setting | Value |
|---|---|
| Client ID | `splunk-hs` (must equal `entityId` in `authentication.conf`) |
| Valid redirect URIs | `https://splunk.example.com/*` |
| Master SAML Processing URL | `https://splunk.example.com/saml/acs` |
| Name ID format | `username` or `email` |
| Sign documents / assertions | On |

Add a **Role list** mapper so realm roles reach Splunk in the assertion. Splunk
cannot query Keycloak for attributes afterwards — if roles are not in the
assertion, the user logs in with nothing.

### IdP signing certificate

From `https://keycloak.example.com/realms/jcsc-oauth/protocol/saml/descriptor`,
take the `<ds:X509Certificate>` value, wrap it in PEM headers, and load it:

```bash
kubectl -n splunk create secret generic splunk-saml-idp \
  --from-file=keycloak-signing.pem
```

The SearchHeadCluster CR already mounts it as volume `saml`, landing at
`/mnt/saml/keycloak-signing.pem` to match `idpCertPath`.

---

## 2. Certificate login

The certificate is checked at **Keycloak**, not by Splunk and not on the Splunk
route:

```
browser ──TLS + client cert──> Keycloak    validates cert, checks issuer + CRL,
                                   │        maps subject -> user,
                                   │        signs assertion
                                   ▼
browser <──────── 302 with SAML assertion ─┘
   │
   └──POST /saml/acs──> Traefik ──> Splunk Web
```

Splunk's configuration is indifferent to how the user proved who they are — swap
Keycloak to WebAuthn or OTP later and nothing here changes. Browsers also prompt
for certificates **per hostname**, so only the Keycloak host asks for one.

### Which topology

`keycloak-mtls-passthrough.yaml` is preferred: Keycloak terminates TLS and takes
the certificate from its own stack. No header, no escaping, no cert-lookup
provider config, and no header-spoofing path.

```
KC_HTTPS_CLIENT_AUTH=request      # 'required' to refuse connections without one
KC_TRUSTSTORE_PATHS=/opt/keycloak/conf/truststores
```

`keycloak-mtls-traefik.yaml` terminates at Traefik and forwards the certificate
as `X-Forwarded-Tls-Client-Cert`. Use it only if you need HTTP-level routing in
front of Keycloak, and note two costs:

- Keycloak's cert-lookup providers expect nginx's or haproxy's header name and
  escaping, so you must point it at the right one and verify the PEM parses:
  `--spi-x509cert-lookup-provider=nginx`
  `--spi-x509cert-lookup-nginx-ssl-client-cert=X-Forwarded-Tls-Client-Cert`
- Anything reaching Keycloak directly can forge that header. Needs a
  NetworkPolicy restricting Keycloak's port to Traefik, and Traefik must strip
  any inbound value before setting its own.

### Keycloak browser flow for X509

Authentication → Flows → add **X509/Validate Username Form** to the
`splunk-browser` flow.

| Setting | Notes |
|---|---|
| User Identity Source | `Subject's Common Name`, or a SAN field |
| User Mapping Method | Username or Email, matching your realm |
| **CRL Checking Enabled** | Turn on |
| **OCSP Checking Enabled** | Turn on if your PKI has a responder |
| Revalidate Client Certificate | On |

Revocation checking is the setting people skip. Without it a revoked
certificate authenticates for its full validity period — a departed employee's
smartcard keeps working.

**The stock form is not click-free.** It shows a confirmation page with the
identified subject and a Continue button. Truly automatic login needs a theme
override that auto-submits, or a custom authenticator. Budget for it rather
than assuming.

### Three CAs, kept separate

| Purpose | CA |
|---|---|
| Pod-to-pod Splunk traffic | cert-manager internal CA (`../tls/`) |
| Browser-facing server certs | Public / Let's Encrypt (`../tls/`) |
| **User client certs** | Your corporate/smartcard PKI |

If the internal CA could also sign user certs, any pod's service certificate
would authenticate as a person.

---

## 3. Splunk side

### The reverse-proxy trap

Splunk builds its SAML callback URL from its *own* view of the world — Splunk
Web on port 8000. Users arrive through Traefik on 443. Uncorrected, Splunk hands
Keycloak a callback pointing at `:8000`, the browser cannot reach it, and login
dies after the IdP redirect with no useful error.

```ini
fqdn = https://splunk.example.com
redirectPort = 443
```

Set these to what the **browser** sees, not what the pod sees.

### Role mapping direction

```ini
[roleMap_SAML]        # <Splunk role> = <Keycloak realm role>
admin = s4k_hs_admin
user  = s4k_hs_user
```

Splunk role on the **left**. (`roleMap_proxySSO` is the same way round;
`oauth2_external_role_mapping` is the reverse — worth knowing if you ever add
one.)

Keycloak's Role list mapper emits the attribute as `Role`, while Splunk looks
for `role`, so `roleAttributeName = Role` is set. A mismatch here is the single
most common cause of "login works but the user has no roles".

### Distribution

`authentication.conf` must be identical on every search head, so it ships
through the **deployer** at cluster scope. The SearchHeadCluster CR already has
the app source:

```yaml
  appRepo:
    appSources:
      - name: samlAuth
        location: saml-auth/
```

```bash
tar -czf splunk_saml_app.tgz splunk_saml_app/
aws s3 cp splunk_saml_app.tgz s3://my-splunk-apps/saml-auth/
```

All members share one SP `entityId` and one hostname, which is correct —
Keycloak sees a single service, and Traefik's sticky cookie pins a session to
one member. Without that stickiness SAML sessions break exactly as ordinary
Splunk Web sessions do.

The Cluster Manager and Monitoring Console are separate SPs on separate
hostnames. Either register a Keycloak client per host, or leave them on local
admin auth and reach them by port-forward — for two operator-facing UIs that is
often the saner trade.

---

## Verifying

```bash
# Splunk parsed the config
kubectl -n splunk exec splunk-shc-search-head-0 -- \
  /opt/splunk/bin/splunk btool authentication list --debug | grep -A20 '\[jcsc-oauth\]'
```

Test the gate with a realm user holding neither role. Correct behaviour is
rejection **at Keycloak**, before any redirect — you should never reach
`/saml/acs`. Landing on a Splunk error page instead means the flow override did
not bind, and you are relying on Splunk's role mapping as the gate. Do not:
authentication has already succeeded by then, a session and audit entry exist,
and the decision would live in an app a bad bundle push could widen.

| Symptom | Cause |
|---|---|
| Redirect lands on `:8000` and hangs | `fqdn` / `redirectPort` not browser-facing |
| Login succeeds, user sees nothing | Role attribute name mismatch (`Role` vs `role`) |
| "Assertion signature verification failed" | Wrong or stale realm signing cert |
| Works on one member, fails on others | App not distributed via deployer, or sticky sessions off |
| Unauthorized user still gets in | Flow not bound to the client, or brokered identity granted a real role |

**Identity brokering re-opens the realm boundary.** Brokered users become realm
users and inherit the same default. Check your first-login flow and any identity
provider role mappers, which can grant roles at account creation.

Keep a local-auth path open while testing. Splunk accepts `admin` at
`/account/login?loginType=splunk` even with SAML enabled — worth knowing before
you lock yourself out.

---

## What applies regardless

- **The local `admin` account must keep working.** The operator authenticates as
  `admin` using the password in `splunk-<ns>-secret` for bundle pushes and
  cluster operations. SSO is for humans; do not disable native auth.
- **Port 8089 stays on the network.** Cluster traffic, replication and the
  operator's REST calls all cross pods on 8089; it is protected by mutual TLS in
  `../tls/`.
- **These are Splunk product settings, not operator features.** The operator has
  no authentication support in 3.1.0 — it just ships the app. Unlike the CR
  manifests, nothing here is schema-validated by this repo, so check key names
  against the `.spec` files for your Splunk version. This deployment pins
  `splunk/splunk:10.2.0` while the reference consulted was 10.4.
