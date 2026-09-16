# IAM for kiam

kiam is **not** IRSA. There is no OIDC provider and no service account in the
trust path. Instead the kiam agent intercepts pod calls to the EC2 metadata
endpoint, and the kiam **server** — running on its own nodes — assumes the role
on the pod's behalf and hands back temporary credentials.

That means the role's trust policy trusts the **kiam server's node role**, not an
OIDC federated principal. See `TRUST-POLICY-kiam.json`.

## Files

| File | Attach to | Notes |
|---|---|---|
| `splunk-s3-policy.json` | `SplunkS3Access` | SmartStore read/write + apps read |
| `splunk-ingestion-separation-policy.json` | `SplunkS3Access` | Only if using `optional/ingestion-separation.yaml` |
| `TRUST-POLICY-kiam.json` | `SplunkS3Access` (trust relationship) | Names the kiam server node role |

## Setup

```bash
aws iam create-role --role-name SplunkS3Access \
  --assume-role-policy-document file://TRUST-POLICY-kiam.json

aws iam put-role-policy --role-name SplunkS3Access \
  --policy-name splunk-s3 \
  --policy-document file://splunk-s3-policy.json
```

The kiam server's node role also needs permission to assume it:

```json
{
  "Effect": "Allow",
  "Action": "sts:AssumeRole",
  "Resource": "arn:aws:iam::<ACCOUNT_ID>:role/SplunkS3Access"
}
```

Both halves are required. Missing the trust policy or missing this grant on the
server node role produces the same symptom: the pod gets no credentials and
splunkd logs generic S3 access failures.

## Checking it works

kiam failures are quiet on the Splunk side, so verify from kiam:

```bash
kubectl -n kube-system logs -l app=kiam,component=server | grep -i "assume\|denied"
```

From inside a Splunk pod, the metadata endpoint should return the role:

```bash
kubectl -n splunk exec splunk-idxc-indexer-0 -- \
  curl -s http://169.254.169.254/latest/meta-data/iam/security-credentials/
```

Empty output means the pod annotation, the namespace regex, or the trust policy
is wrong — in that order of likelihood.
