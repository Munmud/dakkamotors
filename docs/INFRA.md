# Infrastructure

Everything Dakka Motors runs on, and how to rebuild it from nothing.

**Account** `484907516843` · **Region** `ap-northeast-1` (Tokyo)

Buyers are in Japan, so the API, database and buckets all live in Tokyo. Three things
sit in `us-east-1` because AWS requires it, not by choice:

| In `us-east-1` | Why |
|---|---|
| ACM certificate | CloudFront only accepts certificates issued in `us-east-1` |
| CloudFront | A global service whose control-plane endpoint is `us-east-1` |
| AWS Budgets | Billing data is only published in `us-east-1` |

Every other command inherits the CLI default (`aws configure set region ap-northeast-1`).
The exceptions above always carry an explicit `--region us-east-1`.

---

## What exists

| Stack / resource | Region | Holds |
|---|---|---|
| `dakkamotors-core` | Tokyo | VPC, 2 private subnets, security groups, S3 Gateway Endpoint, Aurora Serverless v2, both S3 buckets |
| `dakkamotors-github-oidc` | Tokyo | GitHub OIDC provider + `dakkamotors-github-deploy` role |
| `dakkamotors-edge` | **us-east-1** | CloudFront distribution, SPA router function, API cache policy |
| Zappa (`dakkamotors-production`) | Tokyo | Lambda + API Gateway. Managed by Zappa, not by our templates. |
| Route53 hosted zone | global | `dakkamotors.com` |
| ACM certificate | **us-east-1** | `dakkamotors.com`, `www.dakkamotors.com` |
| Budget `dakkamotors-monthly` | **us-east-1** | Emails at $10 and $25 of monthly spend |

### Live resource ids

| | |
|---|---|
| CloudFront distribution | `E2IW2C27CUGP1D` — `dakkamotors.com`, `www.dakkamotors.com`, `d2y8zvbmyas7y1.cloudfront.net` |
| API Gateway (Zappa) | `https://q0zvyay9pa.execute-api.ap-northeast-1.amazonaws.com/production` |
| Aurora writer endpoint | `dakkamotors-core-dbcluster-xaurpfprbqso.cluster-cnicq6qeyk3e.ap-northeast-1.rds.amazonaws.com` |
| Route53 hosted zone | `Z051521126KCXW6R2RVF8` |
| ACM certificate | `arn:aws:acm:us-east-1:484907516843:certificate/3eed3285-d186-4ea1-abf5-a980ffab5647` |
| VPC | `vpc-07b008c32ab530661` |
| Private subnets | `subnet-0d0828c388d998a72`, `subnet-02157e24d4ccd96e8` |
| Lambda security group | `sg-04c0624b553366b7a` |

### Why the database is Aurora Serverless v2

This account's 12-month free tier has expired, so `db.t4g.micro` would cost roughly
$15/month to sit idle. Aurora Serverless v2 with `MinCapacity: 0` pauses after 10
minutes of inactivity and bills only storage while asleep.

The tradeoff is a **~15 second wake-up on the first request after a quiet spell**. Two
things blunt it: `/api/cars*` is cached at CloudFront for 60 seconds, so most visitors
never reach the database at all, and `CONN_MAX_AGE = 0` in Django ensures connections
close so the cluster can actually pause.

If the cold start ever becomes a real complaint, raise `MinCapacity` to `0.5` in
`infra/network-db.yaml` — that removes the pause entirely, at roughly $40/month.

### Why there is no NAT Gateway

Lambda sits inside the VPC to reach Aurora. The only other thing it needs is S3, and a
**Gateway VPC Endpoint is free**, where a NAT Gateway would cost about $32/month. There
is deliberately no internet gateway and no route to the internet in this VPC.

### What the VPC can and cannot reach

Lambda runs in a private subnet with **no NAT gateway**, and the only VPC endpoint is
the free S3 *gateway* endpoint. That keeps the bill near zero, but it has a consequence
worth knowing before designing anything asynchronous:

**The function can reach Aurora and S3, and nothing else.** There is no network path to
the Lambda API, SQS, SNS or Secrets Manager. A Lambda self-invoke - the natural way to
push image resizing into the background - does not fail fast; it *hangs* until the
function times out, which surfaces as a 504 from API Gateway and looks nothing like a
networking problem. Reaching any of those services needs an interface VPC endpoint at
roughly $10/month per service across two AZs, which is more than this entire site costs.

Two things do still work, because they are *push* rather than *pull*: an S3 event
notification and a CloudWatch Events schedule can both invoke the function, since the
Lambda service places the invocation rather than the function reaching out.

A scheduled sweeper is nonetheless avoided, for a different reason: every run would
query the database, and Aurora only scales to zero after ten idle minutes. Polling on a
timer would keep it permanently awake and undo the saving. Image resizing therefore runs
inline under a time budget - see `cars/tasks.py`.

### Why there is no CORS configuration

One CloudFront distribution serves the React app at `/` and proxies `/api/*` to API
Gateway, so the browser only ever talks to one origin. Locally, the Vite dev server
proxies `/api` to Django to reproduce the same arrangement. If you ever find yourself
adding `django-cors-headers`, something has drifted from this design.

---

## Secrets

Stored as SSM **SecureString** parameters (Parameter Store, not Secrets Manager — no
per-secret monthly charge):

```
/dakkamotors/DB_PASSWORD
/dakkamotors/DJANGO_SECRET_KEY
/dakkamotors/ADMIN_PASSWORD
```

Read one back:

```bash
aws ssm get-parameter --name "/dakkamotors/ADMIN_PASSWORD" \
  --with-decryption --query Parameter.Value --output text
```

> On Git Bash for Windows, prefix SSM commands with `MSYS_NO_PATHCONV=1` or the leading
> `/` in the parameter name is rewritten into a Windows path and the lookup fails.

The Lambda receives these through Zappa's `remote_env`: a private JSON file at
`s3://dakkamotors-backend-media/config/env.json`, fetched on cold start. Secrets are
therefore never committed to git and never appear in the Lambda console.

---

## Building it from scratch

```bash
aws configure set region ap-northeast-1

# 1. Secrets
python - <<'PY'
import secrets, string, boto3
ssm = boto3.client("ssm", region_name="ap-northeast-1")
pw = string.ascii_letters + string.digits + "!#%*+-_=?"   # Aurora rejects / @ " and space
for name, value in {
    "/dakkamotors/DB_PASSWORD": "".join(secrets.choice(pw) for _ in range(32)),
    "/dakkamotors/DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
    "/dakkamotors/ADMIN_PASSWORD": "".join(secrets.choice(pw) for _ in range(20)),
}.items():
    ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
PY

# 2. Network, database, buckets
DBPW=$(aws ssm get-parameter --name "/dakkamotors/DB_PASSWORD" --with-decryption \
       --query Parameter.Value --output text)
aws cloudformation deploy --stack-name dakkamotors-core \
  --template-file infra/network-db.yaml \
  --parameter-overrides DBPassword="$DBPW"

# 3. CI/CD role
aws cloudformation deploy --stack-name dakkamotors-github-oidc \
  --template-file infra/github-oidc.yaml --capabilities CAPABILITY_NAMED_IAM

# 4. Backend. Fill vpc_config in backend/zappa_settings.json from the stack outputs first.
cd backend
zappa deploy production
zappa manage production migrate
zappa manage production "collectstatic --noinput"
zappa manage production create_admin_user

# 5. CDN. us-east-1, and no certificate on the first pass so it works before DNS.
aws cloudformation deploy --stack-name dakkamotors-edge --region us-east-1 \
  --template-file infra/edge.yaml \
  --parameter-overrides \
    FrontendBucketDomain=... MediaBucketDomain=... ApiGatewayDomain=...

# 6. Frontend, and the final DNS cutover once the registrar is updated.
cd frontend && npm ci && npm run build
aws s3 sync dist/ s3://dakkamotors-frontend --delete
bash infra/finish-dns-cutover.sh
```

### A note on CloudFront managed policy ids

`edge.yaml` hardcodes two managed policy ids (`Managed-CachingOptimized`,
`Managed-CachingDisabled`) and one origin request policy
(`Managed-AllViewerExceptHostHeader`). If a distribution create ever fails with
**"The specified cache policy does not exist"**, the id is wrong — not missing. The
error names the cache policy even when the real problem is a different behavior's id,
which makes it easy to misread as a timing or propagation issue. Confirm against:

```bash
aws cloudfront list-cache-policies --type managed \
  --query "CachePolicyList.Items[].CachePolicy.[Id,CachePolicyConfig.Name]" --output text
aws cloudfront list-origin-request-policies --type managed \
  --query "OriginRequestPolicyList.Items[].OriginRequestPolicy.[Id,OriginRequestPolicyConfig.Name]" --output text
```

---

## DNS

The domain is registered at **GoDaddy** and delegated to Route53.

Whenever the hosted zone is recreated it receives a **new set of four nameservers**, and
GoDaddy must be updated to match. A zone that has been deleted leaves the old delegation
pointing at nameservers that answer `REFUSED`, which looks exactly like a domain that was
never configured. Check the live delegation with:

```bash
nslookup -type=NS dakkamotors.com a.gtld-servers.net   # what the registrar publishes
aws route53 get-hosted-zone --id <zone-id>             # what Route53 expects
```

The ACM certificate validates over DNS, so it stays `PENDING_VALIDATION` until the
delegation is correct. That is normal and self-resolving — no need to re-request it.

---

## Tearing it down

```bash
cd backend && zappa undeploy production
aws cloudformation delete-stack --stack-name dakkamotors-edge --region us-east-1
aws s3 rm s3://dakkamotors-frontend --recursive
aws s3 rm s3://dakkamotors-backend-media --recursive
aws cloudformation delete-stack --stack-name dakkamotors-core
aws cloudformation delete-stack --stack-name dakkamotors-github-oidc
aws route53 delete-hosted-zone --id <zone-id>
```

Order matters: buckets must be emptied before their stack will delete, and the Zappa
stack must go before the VPC it has network interfaces in. The Aurora cluster is set to
`DeletionPolicy: Snapshot`, so deleting the core stack leaves a final snapshot behind —
delete that separately if you truly want everything gone.
