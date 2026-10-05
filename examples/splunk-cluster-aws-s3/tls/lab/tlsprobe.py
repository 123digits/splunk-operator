# TLS probes, run inside lab-sh against indexer 0 (./lab.sh check).
# Python ssl for TLS 1.2 ciphers, versions and client certs; Splunk's bundled
# openssl for TLS 1.3 suites (Python cannot restrict TLS 1.3 suites).
#
# Expected policy (../02-tls-defaults.yaml):
#   8089 / 8000 / 8088 / 9997  TLS 1.2 (4 suites) + TLS 1.3 (1 suite)
#   9887 replication            TLS 1.2 only - its listener ignores [tls1.3], so
#                               TLS 1.3 is switched off there to keep the suite
#                               list enforced
#   9997 / 9887                 client cert required and pinned to Splunk tiers
#   8089                        no client cert required (operator constraint)
import ssl, socket, subprocess

CA = "/mnt/splunk-ca/ca.crt"
D = ".splunk.svc.cluster.local"
IDX0 = "splunk-idxc-indexer-0.splunk-idxc-indexer-headless" + D
P = "/tmp/pki"
V12, V13, V11 = ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3, ssl.TLSVersion.TLSv1_1


def cert(tier):
    return (f"{P}/{tier}/tls.crt", f"{P}/{tier}/tls.key")


def py(port, ver, ciphers=None, cc=None):
    """Handshake (verifying the server: CA + host name), then decide.

    S2S (9997) and replication (9887) are binary protocols where the CLIENT
    speaks first and the server drops anything it does not understand - so send
    nothing there: an accepted client is left waiting (read timeout = accepted);
    a refused client cert gets an alert or a close. On the HTTP ports, send a
    request and require a response.
    """
    quiet = port in (9997, 9887)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(CA)
    ctx.minimum_version = ctx.maximum_version = ver
    if ciphers:
        ctx.set_ciphers(ciphers)
    if cc:
        ctx.load_cert_chain(*cc)
    try:
        with socket.create_connection((IDX0, port), timeout=8) as s, \
             ctx.wrap_socket(s, server_hostname=IDX0) as t:
            v, c = t.version(), t.cipher()[0]
            t.settimeout(3)
            try:
                if not quiet:
                    t.sendall(b"GET / HTTP/1.0\r\n\r\n")
                if not t.recv(1):
                    return f"REJ closed after handshake ({v})"
            except socket.timeout:
                if not quiet:
                    return f"REJ no HTTP response ({v})"
            except (ssl.SSLError, ConnectionResetError, OSError) as e:
                return f"REJ {type(e).__name__}: {str(e)[:70]}"
            return f"OK  {v} {c}"
    except Exception as e:
        return f"REJ {str(e).splitlines()[0][:80]}"


def ossl13(port, suite, cc=None):
    cmd = ["/opt/splunk/bin/splunk", "cmd", "openssl", "s_client",
           "-connect", f"{IDX0}:{port}", "-servername", IDX0,
           "-tls1_3", "-ciphersuites", suite, "-CAfile", CA, "-verify_return_error", "-brief"]
    if cc:
        cmd += ["-cert", cc[0], "-key", cc[1]]
    r = subprocess.run(cmd, input=b"", capture_output=True, timeout=20)
    out = (r.stdout + r.stderr).decode(errors="replace")
    if "CONNECTION ESTABLISHED" in out and r.returncode == 0:
        cs = [l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("Ciphersuite")]
        return "OK  TLSv1.3 " + (cs[0] if cs else "")
    return "REJ " + next((l for l in out.splitlines() if "alert" in l or "error" in l.lower()),
                         "handshake failed")[:80]


rows = []


def t(label, want, got):
    rows.append(("PASS" if got.startswith(want) else "FAIL", label, got))


ports = [(8089, "splunkd", None, True), (8000, "web", None, True), (8088, "hec", None, True),
         (9997, "s2s", cert("searchhead"), True), (9887, "repl", cert("indexer"), False)]
for port, n, cc, tls13 in ports:
    want13 = "OK" if tls13 else "REJ"
    t(f"{n}:{port} TLS1.3 TLS_AES_256_GCM_SHA384", want13, ossl13(port, "TLS_AES_256_GCM_SHA384", cc))
    t(f"{n}:{port} TLS1.3 TLS_AES_128_GCM_SHA256", "REJ", ossl13(port, "TLS_AES_128_GCM_SHA256", cc))
    t(f"{n}:{port} TLS1.3 TLS_CHACHA20_POLY1305_SHA256", "REJ", ossl13(port, "TLS_CHACHA20_POLY1305_SHA256", cc))
    t(f"{n}:{port} TLS1.2 ECDHE-RSA-AES256-GCM-SHA384", "OK", py(port, V12, "ECDHE-RSA-AES256-GCM-SHA384", cc))
    t(f"{n}:{port} TLS1.2 ECDHE-RSA-AES128-GCM-SHA256", "OK", py(port, V12, "ECDHE-RSA-AES128-GCM-SHA256", cc))
    t(f"{n}:{port} TLS1.2 AES256-GCM-SHA384 (no PFS)", "REJ", py(port, V12, "AES256-GCM-SHA384", cc))
    t(f"{n}:{port} TLS1.2 ECDHE-RSA-AES256-SHA384 (CBC)", "REJ", py(port, V12, "ECDHE-RSA-AES256-SHA384", cc))
    t(f"{n}:{port} TLS1.1", "REJ", py(port, V11, "DEFAULT:@SECLEVEL=0", cc))

# Client certificate enforcement and pinning.
for port, n, good, vers in [(9997, "s2s", "searchhead", [(V12, "1.2"), (V13, "1.3")]),
                            (9887, "repl", "indexer", [(V12, "1.2")])]:
    for v, vn in vers:
        t(f"{n}:{port} TLS{vn} no client cert", "REJ", py(port, v))
        t(f"{n}:{port} TLS{vn} org USER cert (not pinned)", "REJ", py(port, v, cc=cert("user")))
        t(f"{n}:{port} TLS{vn} pinned tier cert ({good})", "OK", py(port, v, cc=cert(good)))
t("s2s:9997 TLS1.2 indexer cert (not an allowed sender)", "REJ", py(9997, V12, cc=cert("indexer")))
t("repl:9887 TLS1.2 searchhead cert (wrong tier)", "REJ", py(9887, V12, cc=cert("searchhead")))
t("splunkd:8089 TLS1.2 no client cert (mTLS off by design)", "OK", py(8089, V12))

for st, label, got in rows:
    print(f"{st} {label:54} {got}")
print("SUMMARY", sum(r[0] == "PASS" for r in rows), "/", len(rows))
