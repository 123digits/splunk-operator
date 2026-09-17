# SAML SSO with Keycloak

Keycloak is a full SAML 2.0 IdP, so Splunk talks to it natively — no
oauth2-proxy, no ForwardAuth, no trusted-header bypass risk. Keycloak serves
SAML and OIDC clients from the same realm, so this costs you nothing even if
everything else you run there is OIDC.

**Scope warning:** the operator has no SSO support whatsoever — there is no
SAML, LDAP, or auth handling anywhere in the 3.1.0 source or docs. Everything
here is Splunk product configuration delivered as an app. The operator's only
involvement is shipping the app to the right pods. Check the setting names
against `authentication.conf.spec` for your Splunk version; they are stable but
this is not validated by anything in this repo the way the CR manifests are.

## The load-bearing constraint

**Do not disable native Splunk auth.** The operator authenticates as the local
`admin` account for cluster operations — bundle pushes are literally:

```
splunk apply cluster-bundle -auth admin:`cat /mnt/splunk-secrets/password`
```

Turn SAML on for humans. Leave `admin` alone, and keep its password in
`splunk-<ns>-secret` where the operator expects it. If you rotate that password
out of band, the operator loses the ability to manage the cluster.

## Keycloak side

Create a **SAML** client in your realm:

| Setting | Value |
|---|---|
| Client ID | `splunk-shc` (must equal `entityId` in authentication.conf) |
| Valid redirect URIs | `https://splunk.example.com/*` |
| Master SAML Processing URL | `https://splunk.example.com/saml/acs` |
| Name ID format | `username` or `email` |
| Sign documents / assertions | On |

Add a **Role list** (or **Group list**) mapper so roles reach Splunk in the
assertion. Splunk cannot query Keycloak for attributes afterwards — if the role
is not in the assertion, the user logs in with no roles and sees nothing.

Grab the realm signing certificate from:

```
https://keycloak.example.com/realms/splunk/protocol/saml/descriptor
```

Extract the `<ds:X509Certificate>` value, wrap it in PEM headers, and load it:

```bash
kubectl -n splunk create secret generic splunk-saml-idp \
  --from-file=keycloak-signing.pem
```

Mount it by adding to the SearchHeadCluster CR:

```yaml
  volumes:
    - name: saml
      secret:
        secretName: splunk-saml-idp
        defaultMode: 0400
```

which lands at `/mnt/saml/keycloak-signing.pem`, matching `idpCertPath`.

## The reverse-proxy trap

This is the one that costs people an afternoon. Splunk constructs its SAML
callback URL from its *own* view of the world — Splunk Web on port 8000. Your
users arrive through Traefik on 443. Without correction Splunk hands Keycloak a
callback pointing at `:8000`, the browser cannot reach it, and login dies after
the IdP redirect with no useful error.

```ini
fqdn = https://splunk.example.com
redirectPort = 443
```

Set these to what the **browser** sees. Same principle as the `Host` header:
the pod's self-knowledge is wrong once something fronts it.

Because Traefik re-encrypts to Splunk Web over HTTPS (see `../traefik/`), no leg
of the SAML round trip is plaintext, and the assertion is signed end to end.

## Search head clustering

`authentication.conf` must be identical on every member, so ship it through the
**deployer** with `scope: cluster`:

```yaml
  appRepo:
    appSources:
      - name: samlAuth
        location: saml-auth/
        scope: cluster
```

```bash
tar -czf splunk_saml_app.tgz splunk_saml_app/
aws s3 cp splunk_saml_app.tgz s3://my-splunk-apps/saml-auth/
```

All members share one SP `entityId` and one external hostname, which is correct:
Keycloak sees a single service, and Traefik's sticky cookie keeps a given
session pinned to one member. Without that stickiness, SAML sessions break in
the same way ordinary Splunk Web sessions do.

The Cluster Manager and Monitoring Console are separate SPs on separate
hostnames. Either register a Keycloak client per host, or leave them on local
admin auth and reach them by port-forward — for two operator-facing UIs that is
often the saner trade.

## Restricting who can log in

Realm isolation is automatic — a user in another realm cannot authenticate to
this client at all. But **within** the realm, Keycloak's default is that every
user can reach every client, so being in the realm is not by itself a
restriction. `keycloak-realm/` adds a role-gated deny flow bound to the Splunk
client only.

## Roles

Both Keycloak realm roles map onto built-in Splunk roles, so nothing needs
defining in `authorize.conf`:

| Keycloak realm role | Splunk role |
|---|---|
| `s4k_hs_admin` | `admin` |
| `s4k_hs_user` | `user` |

`user` is the least-privilege built-in and is fine for viewing existing
dashboards, but note it is not strictly read-only — it can create its own
knowledge objects and schedule searches.

## Certificate-based login

If users carry client certificates, the certificate is validated by **Keycloak**,
not by Splunk and not on the Splunk route — Splunk's config here is unchanged.
See `keycloak-x509/`.

## Verifying

```bash
# Confirm Splunk parsed it
kubectl -n splunk exec splunk-shc-search-head-0 -- \
  /opt/splunk/bin/splunk btool authentication list --debug | grep -A20 '\[keycloak\]'
```

Failure modes, in the order you will hit them:

| Symptom | Cause |
|---|---|
| Redirect lands on `:8000` and hangs | `fqdn` / `redirectPort` not set to the browser-facing values |
| Login succeeds, user sees nothing | Role attribute name mismatch — Keycloak sends `Role`, Splunk expects `role` |
| "Assertion signature verification failed" | Wrong or stale realm signing cert in `idpCertPath` |
| Works on one member, fails on others | App not distributed via deployer, or sticky sessions off |

Keep a local-auth path open while testing. Splunk's login page accepts
`admin` at `/account/login?loginType=splunk` even with SAML enabled — worth
knowing before you lock yourself out.
