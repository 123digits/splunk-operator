# Test PKI shaped like the org's (see ../README.md, "Lab"):
#   - two roots in the trust bundle (ca/ca.crt), like all-trusted-partners
#   - tier certs with ONLY .svc.cluster.local + localhost SANs, CN <= 63 chars,
#     an O= field (KV store requires O, OU or DC), serverAuth + clientAuth
#   - cert-manager's file layout: tls-combined.pem (key, then chain),
#     tls.crt (chain), tls.key (PKCS#1)
#   - one org-issued USER cert, for the negative pinning tests
# Runs inside the splunk image (it ships python3 + cryptography):
#   docker run --rm -v "$PWD/.generated/pki:/pki" -v "$PWD/gen-pki.py:/gen-pki.py:ro" \
#     --entrypoint python3 splunk/splunk:10.4.4 /gen-pki.py
import datetime, os
from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

now = datetime.datetime.now(datetime.timezone.utc)
D = "/pki"
S = ".splunk.svc.cluster.local"


def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(c):
    return c.public_bytes(serialization.Encoding.PEM)


def pkcs1(k):
    return k.private_bytes(serialization.Encoding.PEM,
                           serialization.PrivateFormat.TraditionalOpenSSL,
                           serialization.NoEncryption())


def subject(cn):
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Example Org"),
                      x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def root(cn):
    k = key()
    c = (x509.CertificateBuilder().subject_name(subject(cn)).issuer_name(subject(cn))
         .public_key(k.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(hours=1))
         .not_valid_after(now + datetime.timedelta(days=30))
         .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
         .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), True)
         .sign(k, hashes.SHA256()))
    return k, c


ca_key, ca = root("org-root-A")
_, other = root("org-root-B")
os.makedirs(f"{D}/ca", exist_ok=True)
open(f"{D}/ca/ca.crt", "wb").write(pem(other) + pem(ca))


def leaf(name, cn, sans):
    assert len(cn) <= 63, f"CN over the org's 63-char limit: {cn}"
    k = key()
    c = (x509.CertificateBuilder().subject_name(subject(cn)).issuer_name(ca.subject)
         .public_key(k.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(hours=1))
         .not_valid_after(now + datetime.timedelta(days=30))
         .add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), False)
         .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH,
                                               ExtendedKeyUsageOID.CLIENT_AUTH]), False)
         .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), True)
         .sign(ca_key, hashes.SHA256()))
    os.makedirs(f"{D}/{name}", exist_ok=True)
    open(f"{D}/{name}/tls-combined.pem", "wb").write(pkcs1(k) + pem(c) + pem(ca))
    open(f"{D}/{name}/tls.crt", "wb").write(pem(c) + pem(ca))
    open(f"{D}/{name}/tls.key", "wb").write(pkcs1(k))


def pod(sts, i):
    return f"{sts}-{i}.{sts}-headless{S}"


# Same names as ../01-certificates.yaml, trimmed to the pods the lab runs.
leaf("manager", "splunk-cm-cluster-manager-service" + S, [
    "localhost",
    "splunk-cm-cluster-manager-service" + S, "splunk-cm-cluster-manager-headless" + S,
    pod("splunk-cm-cluster-manager", 0),
    "splunk-lm-license-manager-service" + S, "splunk-lm-license-manager-headless" + S,
    pod("splunk-lm-license-manager", 0)])
leaf("indexer", "splunk-idxc-indexer-service" + S, [
    "localhost",
    "splunk-idxc-indexer-service" + S, "splunk-idxc-indexer-headless" + S,
    pod("splunk-idxc-indexer", 0), pod("splunk-idxc-indexer", 1)])
leaf("searchhead", "splunk-shc-search-head-service" + S, [
    "localhost",
    "splunk-shc-search-head-service" + S, "splunk-shc-search-head-headless" + S,
    pod("splunk-shc-search-head", 0)])
# Org-issued, valid chain, but not a Splunk tier: must be refused everywhere.
leaf("user", "jdoe.users.example.com", ["jdoe.users.example.com"])
print("pki written to", D)
