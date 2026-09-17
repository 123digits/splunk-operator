# Certificate login via Keycloak

## Where the certificate is actually checked

Not at Splunk, and not on the Splunk route:

```
browser ──TLS+client cert──> Traefik (keycloak.example.com) ──> Keycloak
                                                                   │  validates cert,
                                                                   │  maps CN -> user,
                                                                   │  signs assertion
                                                                   ▼
browser <──────────── 302 with SAML assertion ─────────────────────┘
   │
   └──POST /saml/acs──> Traefik (splunk.example.com) ──> Splunk Web
```

Splunk configuration is **unchanged** from `../README.md`. It consumes a signed
SAML assertion and neither knows nor cares that a certificate produced it. Swap
Keycloak to password, OTP, or WebAuthn later and Splunk still needs no edits —
that indirection is the main argument for doing it here rather than trying to
teach Splunk about certificates.

It also means **only the Keycloak hostname prompts for a certificate**. Browsers
prompt per-host, so users are not nagged when they hit Splunk directly.

## Full flow

1. User opens `https://splunk.example.com`. No Splunk session.
2. Splunk issues a SAML AuthnRequest, redirecting to Keycloak.
3. The TLS handshake for `keycloak.example.com` requests a client certificate.
4. Keycloak validates it against the user PKI CA, checks revocation, and maps
   the subject to a realm user.
5. Keycloak returns a signed assertion carrying the `Role` attribute.
6. Browser POSTs it to `https://splunk.example.com/saml/acs`.
7. Splunk maps roles per `[roleMap_SAML]` and the session starts.

## Keycloak setup

**Trust store** — the CA that signed your user certs:

```
KC_HTTPS_CLIENT_AUTH=request
KC_TRUSTSTORE_PATHS=/opt/keycloak/conf/truststores
```

`request` makes the cert optional so you keep a password fallback;
`required` refuses connections without one. With `required`, lock yourself out
of Keycloak admin at your peril — keep a separate admin hostname.

**Browser flow** — Authentication → Flows → duplicate `browser` → add the
**X509/Validate Username Form** execution → bind as the browser flow.

Configure the authenticator:

| Setting | Notes |
|---|---|
| User Identity Source | `Subject's Common Name`, or a SAN field |
| User Mapping Method | Username or Email, matching your realm |
| **CRL Checking Enabled** | Turn on |
| **OCSP Checking Enabled** | Turn on if your PKI has a responder |
| Revalidate Client Certificate | On |

Revocation checking is the setting people skip. Without it a revoked
certificate still authenticates for its full validity period — a departed
employee's smartcard keeps working. Given the trouble you have gone to
verifying machine identity, do not leave human identity weaker.

## Two honest caveats

**The built-in X509 form is not click-free.** Keycloak's
*X509/Validate Username Form* shows a confirmation page with the identified
subject and a Continue button. That is one click, not zero. Making it fully
automatic needs a theme override that auto-submits the form, or a custom
authenticator. If "automatically" means literally no interaction, budget for
that customisation — do not assume the stock flow delivers it.

**The header approach has a format mismatch to resolve.** Traefik's
`passTLSClientCert` emits `X-Forwarded-Tls-Client-Cert`, while Keycloak's
built-in cert-lookup providers expect nginx's or haproxy's header name and
escaping. You must point Keycloak at the right header:

```
--spi-x509cert-lookup-provider=nginx
--spi-x509cert-lookup-nginx-ssl-client-cert=X-Forwarded-Tls-Client-Cert
```

and confirm the PEM escaping matches what that provider parses. Verify this in
a test realm before committing to it.

**This is why `passthrough.yaml` is the safer default.** Keycloak terminates TLS
itself and takes the certificate from its own stack — no header, no escaping, no
provider config. Use the Traefik-terminated variant only if you specifically
need HTTP-level routing in front of Keycloak.

## The header-spoofing risk, and why it does not apply here

If Traefik forwards the cert in a header, anything that can reach Keycloak
directly can *set* that header and impersonate any certificate subject. Two
mitigations, both required:

- A NetworkPolicy restricting Keycloak's port to Traefik pods only.
- Traefik must **strip** any inbound `X-Forwarded-Tls-Client-Cert` before
  setting its own. `passTLSClientCert` overwrites the header, but confirm no
  other route reaches the same backend without the middleware.

Passthrough has no such exposure — there is no header to forge.

## Certificates: which CA signs what

Three distinct trust domains. Do not merge them:

| Purpose | CA | Where |
|---|---|---|
| Pod-to-pod Splunk traffic | cert-manager internal CA | `../../tls/00-issuers.yaml` |
| Browser-facing server certs | Public / Let's Encrypt | `../../tls/01-certificates.yaml` |
| **User client certs** | Your corporate/smartcard PKI | external — referenced as `user-pki-ca` |

The user PKI is not cert-manager's job. If the internal CA could also sign user
certs, any Splunk pod's service certificate would authenticate as a person.
