# Scripted authentication: evaluation

Short answer: **scripted auth cannot see the peer certificate**, so the design
as proposed — script reads the cert, takes Subject DN, uses Issuer DN as an
attribute — cannot be built. A useful *part* of it can, just not that part.

## What the interface actually gives a script

From the `authentication.conf` reference and the scripted-auth guide, a script
implements `userLogin`, `getUserInfo`, `getUsers`, and optionally
`getSearchFilter`. Splunk invokes them as command lines:

```
userLogin   --username=alice --password=correctpassword
getUserInfo --username=bob
getUsers
```

That is the whole input surface. **Username and password. No certificate, no
DN, no issuer, no TLS context of any kind.** splunkd calls the script as a
credential oracle; by the time it runs, the TLS handshake is long finished and
its peer certificate was never captured for the script to read.

`scriptFunctions` accepts `getUsers`, `getUserInfo`, and `login`, with cache
controls `userLoginTTL` and `userInfoTTL`.

Return format for the role-bearing call:

```
--status=success --userInfo=bob;bob;bob;user
--status=success --userInfo=alice;alice;alice;admin:super
```

i.e. `username;realname;email;roles`, roles colon-separated.

## What you can build instead

The half that works is the half you actually want — **dynamic role lookup
against Keycloak**:

```
browser ──cert──> nginx sidecar        validates cert, checks issuer,
                      │                 extracts CN
                      │ REMOTE_USER: <cn>
                      ▼
                  Splunk Web (loopback, SSOMode=strict)
                      │ getUserInfo --username=<cn>
                      ▼
                  auth script ──REST──> Keycloak Admin API
                      │                  (user's groups/roles)
                      ▼
                  --userInfo=<cn>;<name>;<email>;power:user
```

nginx does the certificate work because it is the only component that holds the
TLS peer certificate. Splunk's proxy SSO turns that into a username. Scripted
auth turns the username into roles. Each layer does the part it can actually
see.

**But read the next section before building this** — `[roleMap_proxySSO]` does
the last step natively, so the script is usually redundant.

## Where the Issuer DN has to go

It cannot reach the script: `getUserInfo` receives only `--username`. Three ways
people try to force it, and what happens:

| Approach | Verdict |
|---|---|
| Encode issuer into the username nginx sets | Works, but the issuer string becomes part of the Splunk username — it lands in audit logs, search ownership and knowledge-object ACLs. Renaming a CA then orphans objects. Avoid. |
| Side-channel the DN→issuer map to the script | Fragile, and a second source of truth for an authorization decision. |
| **Enforce the issuer in nginx** | Correct. |

Issuer validation is a TLS-layer decision, so `../splunk-sso/nginx/` now pins it
there with an `$ssl_client_i_dn` allow-list. This matters because
`ssl_verify_client on` accepts **any** CA in `ssl_client_certificate` — if you
trust more than one issuer, the allow-list is what distinguishes them. Splunk
never needs to know the issuer; a cert from the wrong one is refused before a
username is ever produced.

The full subject and issuer are still forwarded as `X-Client-Subject-DN` and
`X-Client-Issuer-DN` for audit logging. They carry no authentication weight —
Splunk's SSO reads `REMOTE_USER` and nothing else.

## The bigger reason not to: ProxySSO already does the role mapping

Verified in the 10.4 reference: `authType` accepts `ProxySSO` as a first-class
value, and `[roleMap_proxySSO]` maps **groups supplied in proxy headers**
directly to Splunk roles. `defaultRoleIfMissing`, `excludedUsers` and
`excludedAutoMappedRoles` round it out.

That removes the original motivation for a script. nginx sends the user and
their groups; Splunk maps groups to roles natively. No Python in the login path,
no Keycloak admin credential on every search head, no cache staleness.

`../splunk-sso/nginx/` now uses `authType = ProxySSO` for exactly this reason.

**One trap in that stanza**, quoting the reference: *"If a group is not
explicitly mapped to a Splunk role, but has the same name as a valid Splunk
role, then, for ease of configuration, it is auto-mapped to that Splunk role."*

A proxy-supplied group named `admin` therefore becomes the admin role with no
mapping entry at all. Set `excludedAutoMappedRoles` — Splunk's own example
excludes `admin`, and that is the minimum.

A script is only still needed if groups cannot reach nginx: not in the
certificate, and not obtainable by a subrequest. That is a narrow case.

## Should you use it?

Probably not. Compare against the SAML path already built in `../saml/`:

| | Scripted + proxy SSO | SAML |
|---|---|---|
| Roles from Keycloak | Yes, via Admin API call | Yes, in the assertion |
| Keycloak admin credentials on every search head | **Required** | Not needed |
| Role freshness | Cached per `userInfoTTL`; revocation lags | Fresh each login |
| Latency | A REST call in the auth path | None |
| Code you maintain | A Python script on every SH, in the login path | None |
| Trust model | Header over loopback | Signed assertion |

SAML already delivers "look up the person in Keycloak and map their groups to
Splunk roles" — that is precisely what the assertion carries. Scripted auth
reaches the same outcome with an extra moving part in the login path, a
long-lived Keycloak admin credential on every search head, and stale roles
between cache expiries.

**Choose scripted auth only if** groups are unavailable to nginx *and* you
cannot run SAML. Between `[roleMap_proxySSO]` and SAML assertions, that case is
rare. Otherwise both alternatives are less machinery for the same result.

If you do build it: the script runs on every search head and sits in the login
path, so give it a short timeout and a local cache, never let a Keycloak outage
block logins, and keep its Keycloak service-account credential scoped to
read-only user/group queries.

## Version note

You linked the 10.4 reference; this deployment pins `splunk/splunk:10.2.0`. The
scripted-auth interface is unchanged between them, but check any setting against
the 10.2 reference before relying on it.

## Sources

- [authentication.conf, Splunk Enterprise 10.4](https://help.splunk.com/en/splunk-enterprise/administer/admin-manual/10.4/configuration-file-reference/10.4.0-configuration-file-reference/authentication.conf)
- [authentication.conf, Splunk Enterprise 10.2](https://help.splunk.com/en/data-management/splunk-enterprise-admin-manual/10.2/configuration-file-reference/10.2.0-configuration-file-reference/authentication.conf)
- [Create the authentication script](https://help.splunk.com/en/splunk-enterprise/administer/manage-users-and-security/9.4/authenticate-into-splunk-enterprise-using-scripts/create-the-authentication-script)
