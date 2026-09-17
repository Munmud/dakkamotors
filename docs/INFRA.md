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

---

## Migration in flight: Aurora -> DynamoDB + Cognito

**Both stacks are live at once, on purpose.** Everything the application reads and writes
is already in DynamoDB and Cognito; Aurora and Django's auth tables are still there
because they are the rollback. `zappa rollback production -n 1` restores the previous
Lambda in seconds, and that code reads Aurora, which is still running. Tearing either
down before the cutover has held would throw that away.

### What has been added

| Stack / resource | Region | Holds |
|---|---|---|
| `dakkamotors-data` | Tokyo | The DynamoDB table, the Cognito user pool, three groups, two app clients, the hosted-UI domain, the UserMigration trigger, and the backend's managed IAM policy |

Defined in `infra/data.yaml`. Deployed as its own stack sharing one with nothing
DNS-related -- the lesson of the `dakkamotors-dev` stack whose deletion took the Route53
hosted zone with it.

### Why DynamoDB

Aurora Serverless v2 at `MinCapacity: 0` costs $3-8/month and takes **~15 seconds to wake
on the first request after ten idle minutes**. DynamoDB on-demand has no idle cost and no
wake-up. The saving is about $5/month, which does not justify a migration on its own; the
cold start and getting out of the VPC do.

### Why Cognito never sends an email

The pool has `AutoVerifiedAttributes: []`, `admin_only` recovery and no
`EmailConfiguration`, so it has no trigger to fire. Verification and password-reset
messages still go through `mail.queue_email` -> S3 -> the mailer Lambda -> Brevo,
bilingual and branded.

This is deliberate and it is the decision that made Cognito adoptable here. The
alternatives are its plain-English default mail against a hard **50 messages/day** cap,
or SES -- which needs domain verification, a production-access request out of the
sandbox, and a second sender's DKIM/SPF/DMARC alongside Brevo's. `docs/TODO.md` records
the `dakkamotors-dev` stack stuck in `DELETE_FAILED` on an undeleted `CognitoEmailRole`,
which is the role Cognito creates when wired to SES. **Do not wire it to SES.**

### Three settings that will bite if changed

* **`MinimumLength: 6`, no complexity.** A pool has exactly one password policy.
  A stricter one rejects every existing customer whose password is six or seven
  characters *at the moment the migration trigger tries to verify it* -- on the sign-in
  screen, in Cognito's words. The stricter staff rule lives in application code because
  the customer-lax/staff-strict split cannot be expressed in a pool at all.
* **`Schema` is effectively immutable.** CloudFormation fails rather than replaces on
  most edits and there is no API to remove a custom attribute, so a wrong attribute list
  means rebuilding the pool and losing every user. Build and tear one down in a scratch
  account before the first real deploy.
* **`custom:phone`, not `phone_number`.** Cognito validates the standard attribute as
  E.164 and customers type `080-9282-3601`. The standard one would reject the existing
  data and make phone a sign-in alias and an MFA channel.

### Passwords cannot be migrated

Django stores `pbkdf2_sha256$...`; Cognito will not accept a hash on `AdminCreateUser`.
The alternative to solving this is emailing every customer to say their password no
longer works, which for a small dealership is a measurable loss of accounts.

Instead the importer writes a `LEGACYPW#<email>` item per customer and a Cognito
**UserMigration** trigger verifies against it on first sign-in, then deletes it. The
items carry a 90-day TTL regardless. The trigger is inline in `infra/data.yaml` and needs
no Django: the hash format is verifiable in about fifteen lines of stdlib `hashlib`.

---

## The cutover

### Before the window, over weeks

1. `aws cloudformation deploy --template-file infra/data.yaml --stack-name dakkamotors-data --capabilities CAPABILITY_NAMED_IAM`
2. Put the stack outputs into `zappa_settings.json` (`COGNITO_POOL_ID`,
   `COGNITO_CUSTOMER_CLIENT_ID`, `COGNITO_STAFF_CLIENT_ID`, `COGNITO_DOMAIN`) and the
   staff client secret into SSM at `/dakkamotors/COGNITO_STAFF_CLIENT_SECRET`.
3. Save the pool's JWKS to `backend/config/cognito_jwks.json`:
   `curl https://cognito-idp.ap-northeast-1.amazonaws.com/<pool-id>/.well-known/jwks.json`.
   Baked into the deploy so a cold start is not coupled to a Cognito endpoint being
   reachable; the network fetch is the fallback.
4. **Rehearse.** Restore an Aurora snapshot, run `export_aurora` against it and
   `import_dynamo` into a *second* table, and run `verify_migration`-style checks. Repeat
   until it is boring. Run it twice in a row to confirm it is idempotent.
5. Tag the pre-cutover commit: `git tag pre-dynamo`. The exporter needs the ORM models
   and they are removed afterwards, so this tag is the only place it will still run.

### The window, 30-45 minutes, weekday morning JST, announced

1. Point CloudFront's default and `/api/*` behaviours at a maintenance response.
2. Wait for in-flight Lambdas to drain.
3. `python manage.py export_aurora --out ./migration/<date>/` from the `pre-dynamo` tag.
4. `python manage.py import_dynamo --from ./migration/<date>/` on the new code, run from
   a laptop with SSO credentials -- DynamoDB is not in a VPC, so this needs no Lambda.
5. `zappa update production`, then `collectstatic`.
6. Smoke test against the API Gateway origin directly, bypassing the CDN: `/`,
   `/api/cars/`, a car by slug, the same car by its old numeric id (expect 301),
   `/sitemap.xml`, `/llms.txt`, `/robots.txt`, and `/api/staff/cars/` (expect 302).
7. **Sign in as a pre-arranged real customer account.** This is what proves the
   UserMigration trigger works, and nothing before it does.
8. Sign in as staff through the hosted UI; edit a car, confirm a booking.
9. Lift maintenance, invalidate CloudFront.

**Rollback is `zappa rollback production -n 1` plus lifting maintenance -- about two
minutes** -- and it is real only because nothing is deleted during the window. The one
thing it cannot undo is a booking or sign-up made in the interim, which would need
re-entering by hand. Keep the window short.

### After it has held, over days

Only once the cutover has run and been quiet for a week:

1. Empty `vpc_config`'s lists in `zappa_settings.json` -- **do not delete the key**,
   Zappa only sends `VpcConfig` when it is present, so removing it leaves the function
   attached to the old subnets. Verify with
   `aws lambda get-function-configuration --function-name dakkamotors-production`.
2. Detach `AWSLambdaVPCAccessExecutionRole` from the Lambda role once the ENIs release.
   That takes 20-40 minutes and the subnets cannot be deleted until it does.
3. Delete the ORM models, `django.contrib.auth`, `django.contrib.sessions` and the
   Django admin, and set `DATABASES = {}`. The staff pages' Django fallback in
   `cars/staff/auth.py` goes at the same time.
4. Delete the Aurora cluster (its `DeletionPolicy: Snapshot` leaves a final snapshot,
   which is the second net) and remove the VPC, subnets, security groups and
   `DBSubnetGroup` from `infra/network-db.yaml`. The buckets stay.
5. Retire `remote_env` in favour of reading SSM at settings import -- the VPC was the
   only reason the values had to be hand-copied into `config/env.json`, and that dance
   is the most error-prone procedure in this repo.
6. Move derivative generation to a real asynchronous invoke and delete the inline time
   budget in `cars/tasks.py`. Its opening docstring stops being true the moment the
   function leaves the VPC.
7. Delete the `LEGACYPW#` items at 90 days (the TTL does it; check that it did).

Once the VPC is gone, **the section below about what it can and cannot reach no longer
applies** -- SSM, SES, SQS and `lambda:InvokeFunction` all become reachable, which is
what retires three separate workarounds.

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

### Why Django serves the HTML

CloudFront's default behaviour points at Django, not at the S3 bundle. A static
`index.html` can only ever carry one title and one description, so every URL returned
the same document: sharing a car showed a generic "Dakka Motors" with no photo or
price, and anything that does not run JavaScript saw an empty `<div>`.

Django reads the **already-built** `index.html` out of the frontend bucket and injects a
real `<head>` plus a text summary. There is still one build of the app and the script
tags always match whatever the frontend workflow last deployed, with no manifest to keep
in step. The page also embeds the data the app needs for its first paint, which removed
a 0.309 cumulative layout shift and a round-trip.

HTML is cached at the edge for five minutes, so crawlers and repeat visitors rarely reach
Lambda and Aurora keeps scaling to zero. Hashed bundles under `/assets/*` and the favicon
still come straight from S3 and never touch the origin.

`robots.txt`, `sitemap.xml` and `llms.txt` are generated from the database, so the sitemap
cannot advertise a car that has been sold. Business facts - address, hours, service area -
live in `cars/seo.py`.

### Deploys must not race the schema

Zappa probes `/` immediately after uploading, before migrations run, so **any release
that adds a column fails that probe even though the deploy is fine**. Retrying it while
the schema is behind is how a half-applied migration happens. The workflow therefore
treats the probe as a warning and gates on a smoke test that runs *after* migrations,
hitting the origin directly so a cached page cannot mask a broken deploy.

One Django trap worth remembering: `SlugField` sets `db_index=True` by default. Adding
one and then altering it to `unique=True` in the same migration makes Postgres build the
same `..._like` index twice and the migration dies on "relation already exists". The
intermediate column has to be `db_index=False`.

### Why there is no CORS configuration

One CloudFront distribution serves the React app at `/` and proxies `/api/*` to API
Gateway, so the browser only ever talks to one origin. Locally, the Vite dev server
proxies `/api` to Django to reproduce the same arrangement. If you ever find yourself
adding `django-cors-headers`, something has drifted from this design.

---

## Staff accounts

Two levels of access exist.

| | Can do | Cannot do |
|---|---|---|
| `admin` (superuser) | Everything, including deleting accounts and editing other superusers | — |
| **Inventory Managers** group | Add, edit, delete cars and photos; upload media; add and edit staff colleagues | Reach the owner's account, become a superuser, grant permissions, or delete an account |

### Why staff administration is a proxy model

Members manage colleagues through **`cars.StaffAccount`**, a proxy over `auth.User`, so
the permissions are `cars.*_staffaccount` and the group **never holds an `auth`
permission**. That is deliberate: `/api/admin/auth/user/` keeps returning 403 for them,
and the real user admin stays superuser-only.

Handing a non-superuser `auth.change_user` with Django's stock `UserAdmin` is a complete
privilege escalation — the holder can reset the owner's password, tick "superuser" on
themselves, or grant themselves any permission. `StaffAccountAdmin` closes each route:
superusers are filtered from the queryset *and* refused by the permission hooks, the
`is_superuser` and `user_permissions` fields are absent from the fieldsets *and* forced
on save, group choices are limited to an allowlist, and nobody can deactivate their own
account. Superusers get Django's untouched behaviour.

Removing someone means unticking **Active**, not deleting: reversible, and it keeps the
admin history of their edits readable. The group has no `delete_staffaccount`.

The restriction is not cosmetic. Django's admin renders only the models a user holds
permissions for, *and* re-checks on every view, so a member sees no Authentication
section and gets a 403 on `/api/admin/auth/user/` if the URL is typed directly. They
cannot escalate because granting rights needs `auth.change_user`, which the group does
not include.

`is_staff` is what the presigned upload endpoint checks (`IsAdminUser` in DRF means
staff, not superuser), so members can upload photos and video without extra permissions.

The group is defined in `cars/management/commands/ensure_inventory_group.py` and
reconciled on every deploy, so that file is the source of truth — permissions added by
hand in the admin are removed again on the next release.

### Adding someone

```bash
# 1. Generate a password and keep it somewhere durable
python -c "import secrets,string;print(''.join(secrets.choice(string.ascii_letters+string.digits+'!#%-_') for _ in range(20)))"
MSYS_NO_PATHCONV=1 aws ssm put-parameter --name "/dakkamotors/<NAME>_PASSWORD"   --type SecureString --value '<generated>' --region ap-northeast-1

# 2. Put it where the Lambda can read it. It cannot reach the SSM API from inside the
#    VPC, so the password travels through remote_env (config/env.json in S3) exactly as
#    DJANGO_ADMIN_PASSWORD does. Add INVENTORY_USER_PASSWORD, then run:
cd backend && source .venv/Scripts/activate
zappa manage production "create_inventory_user --username <user> --email <email>   --first-name '<First>' --last-name '<Last>'"

# 3. Remove INVENTORY_USER_PASSWORD from config/env.json afterwards.
```

Re-running `create_inventory_user` never resets an existing password, and it refuses to
modify a superuser, so a mistyped username cannot quietly demote the owner's account.

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
