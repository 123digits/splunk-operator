# Restricting who can log in

Two different controls get confused here. Realm isolation is free; client
authorization is not.

## Realm isolation is structural — you already have it

A Keycloak **realm** is a tenant boundary. A user belongs to exactly one realm,
and the Splunk SAML client lives in exactly one realm. A user in realm `staff`
cannot authenticate to a client in realm `contractors`: different user database,
different login endpoint, different signing keys. There is nothing to configure
and no way to accidentally allow it.

So if your question is "can someone outside the realm get in" — no, and the
assertion signature is what enforces it. Splunk trusts exactly one realm signing
certificate (`idpCertPath`). An assertion from any other realm fails signature
verification.

**A dedicated realm for Splunk users is therefore a perfectly valid answer.**
Put only the people who should reach Splunk in it. The cost is managing another
user store, or brokering, which brings its own caveat below.

## The real gap: every realm user can reach every client

This is the part that bites. Within a realm, Keycloak's default is that **any
authenticated user can log into any client**. There is no per-client allow-list
out of the box. If your realm holds 5,000 employees and 50 should have Splunk,
all 5,000 can authenticate to the Splunk client unless you add a restriction.

Realm isolation does not help here — everyone is legitimately in the realm.

## Fixing it: a role plus a deny flow bound to the Splunk client

1. Create a realm role, e.g. `splunk-access`, and assign it to the group that
   should have Splunk (typically the same groups you map in `[roleMap_SAML]`).

2. Authentication → Flows → duplicate `browser` → name it `splunk-browser`.
   Inside it add a **conditional sub-flow**:

   - **Condition - user role** → role `splunk-access`, **Negate output: ON**
   - **Deny Access** (Required)

   Reads as: *if the user does NOT have `splunk-access`, deny*. Everything else
   in the copied flow — including your X509 step — runs unchanged.

   If your Keycloak version does not expose *Negate output* on the role
   condition, invert it instead: make the whole authentication conditional on
   holding the role, and leave Deny Access as the terminal step.

3. Bind the flow **to the Splunk client only**:
   Clients → `splunk-shc` → Advanced → **Authentication flow overrides** →
   Browser Flow = `splunk-browser`.

   This is the important step. Editing the realm's default browser flow would
   deny those users access to *every* client in the realm, which is almost never
   what you want.

## Do not rely on Splunk's role mapping as the gate

It is tempting to skip the above and let `[roleMap_SAML]` do the work — a user
with no matching group maps to no Splunk role and gets nowhere useful.

Treat that as defence in depth, not the control:

- Authentication has already succeeded at that point. A session exists, the
  login is in the audit trail, and behaviour for a role-less user varies by
  Splunk version.
- The decision lives in an app shipped through the deployer, so a bad bundle
  push silently widens access.

Deny at the IdP, where the decision is made before an assertion is ever issued,
and keep the role mapping tight as a second layer.

## Two caveats

**Identity brokering re-opens the realm boundary.** If the realm brokers to an
external IdP or another realm, users originating there become realm users and
inherit the same default: they can reach every client. Whatever gate you build
must be a role the brokered users do not get automatically — check your
first-login flow and any role mappers on the identity provider, which can grant
roles on account creation.

**Organizations, if you are on Keycloak 26+,** offer a membership boundary
inside a realm that can be cleaner than roles for multi-tenant cases. The role
approach above works on every supported version, so start there unless you are
already using Organizations.

## Verifying

Test with a user who is in the realm but lacks `splunk-access`. The correct
result is rejection **at Keycloak**, before any redirect back to Splunk — you
should never reach `/saml/acs`. If you land on a Splunk error page instead, the
flow override did not bind and you are relying on Splunk's role mapping.

```bash
# Splunk-side: confirm only intended groups map to roles
kubectl -n splunk exec splunk-shc-search-head-0 -- \
  /opt/splunk/bin/splunk btool authentication list roleMap_SAML --debug
```
