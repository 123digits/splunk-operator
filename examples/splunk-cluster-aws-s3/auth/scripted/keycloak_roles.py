#!/usr/bin/env python3
"""Reference getUserInfo handler: username -> Keycloak groups -> Splunk roles.

Only use this with proxy SSO (see ../splunk-sso/nginx/). It deliberately does
NOT implement userLogin: nginx has already authenticated the user with a client
certificate, and this script cannot re-verify that - it never receives the
certificate. Splunk invokes it as:

    getUserInfo --username=bob
    getUsers

and expects on stdout:

    --status=success --userInfo=bob;Bob Smith;bob@corp;power:user

Configure with, in authentication.conf:

    [authentication]
    authType = Scripted
    authSettings = keycloak_script

    [keycloak_script]
    scriptPath = $SPLUNK_HOME/etc/apps/splunk_sso/bin/keycloak_roles.py
    scriptFunctions = getUserInfo, getUsers
    userInfoTTL = 300

Note userInfoTTL: roles are cached, so a Keycloak group removal does not take
effect until it expires. Short TTL costs latency on every login; long TTL means
stale authorization. That trade-off does not exist on the SAML path.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

KEYCLOAK = os.environ.get("KEYCLOAK_URL", "https://keycloak.example.com")
REALM = os.environ.get("KEYCLOAK_REALM", "splunk")
CLIENT_ID = os.environ.get("KEYCLOAK_CLIENT_ID", "splunk-role-lookup")
# Mounted from a Kubernetes secret; never bake this into the app package.
SECRET_FILE = os.environ.get("KEYCLOAK_SECRET_FILE", "/mnt/keycloak/client-secret")
CA_BUNDLE = os.environ.get("KEYCLOAK_CA", "/mnt/splunk-tls/ca.crt")
TIMEOUT = float(os.environ.get("KEYCLOAK_TIMEOUT", "3"))

# Keycloak group -> Splunk role. Anything unmapped yields no role, and Splunk
# grants no access. Keep this aligned with [roleMap_SAML] if both paths exist.
GROUP_TO_ROLE = {
    "splunk-admins": "admin",
    "splunk-power-users": "power",
    "splunk-users": "user",
}

_cache = {}
_CACHE_TTL = 60


def _fail(msg):
    """Fail closed. Never emit success with a default role on error."""
    sys.stderr.write("keycloak_roles: %s\n" % msg)
    print("--status=fail")
    sys.exit(0)


def _post_form(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    # Verified against our CA. No unverified fallback: an auth decision must
    # never rest on an unauthenticated connection.
    import ssl
    ctx = ssl.create_default_context(cafile=CA_BUNDLE)
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        return json.loads(r.read())


def _get_json(url, token):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    import ssl
    ctx = ssl.create_default_context(cafile=CA_BUNDLE)
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        return json.loads(r.read())


def _token():
    with open(SECRET_FILE) as fh:
        secret = fh.read().strip()
    tok = _post_form(
        "%s/realms/%s/protocol/openid-connect/token" % (KEYCLOAK, REALM),
        {"grant_type": "client_credentials",
         "client_id": CLIENT_ID,
         "client_secret": secret},
    )
    return tok["access_token"]


def _lookup(username):
    hit = _cache.get(username)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    token = _token()
    base = "%s/admin/realms/%s" % (KEYCLOAK, REALM)
    users = _get_json(
        "%s/users?username=%s&exact=true" % (base, urllib.parse.quote(username)), token)
    if not users:
        return None
    u = users[0]
    groups = _get_json("%s/users/%s/groups" % (base, u["id"]), token)

    roles = []
    for g in groups:
        role = GROUP_TO_ROLE.get(g.get("name"))
        if role and role not in roles:
            roles.append(role)
    if not roles:
        return None

    info = (u.get("username", username),
            ("%s %s" % (u.get("firstName", ""), u.get("lastName", ""))).strip() or username,
            u.get("email", ""),
            ":".join(roles))
    _cache[username] = (time.time(), info)
    return info


def main():
    if len(sys.argv) < 2:
        _fail("no function given")
    fn = sys.argv[1]
    args = dict(a[2:].split("=", 1) for a in sys.argv[2:] if a.startswith("--") and "=" in a)

    if fn == "getUserInfo":
        username = args.get("username")
        if not username:
            _fail("getUserInfo without --username")
        try:
            info = _lookup(username)
        except Exception as exc:
            _fail("keycloak lookup failed for %s: %s" % (username, exc))
        if not info:
            _fail("no mapped roles for %s" % username)
        print("--status=success --userInfo=%s;%s;%s;%s" % info)

    elif fn == "getUsers":
        # Splunk calls this to populate user lists in the UI. Enumerating a
        # whole realm is slow and rarely worth it; returning an empty success
        # leaves SSO logins working and only affects admin pickers.
        print("--status=success")

    else:
        # userLogin deliberately unimplemented - see the module docstring.
        _fail("unsupported function %s" % fn)


if __name__ == "__main__":
    main()
