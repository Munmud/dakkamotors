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
| `dakkamotors-data` | Tokyo | The DynamoDB table, the Cognito pool, three groups, two app clients, the hosted-UI domain, the UserMigration trigger, the backend's managed IAM policy |
| `dakkamotors-core` | Tokyo | Both S3 buckets. **Still holds the VPC, its subnets, security groups, the S3 Gateway Endpoint and a stopped Aurora cluster** until the teardown runs. |
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
| DynamoDB table | `dakkamotors` |
| Cognito user pool | `ap-northeast-1_czNqEUiq5` |
| Cognito customer client | `61o4ucujbcoknsqtnt4pd68lmr` (no secret) |
| Cognito staff client | `kti6vuj2t4e927pav3rq3cst0` (secret in SSM at `/dakkamotors/COGNITO_STAFF_CLIENT_SECRET`) |
| Cognito hosted UI | `dakkamotors-staff.auth.ap-northeast-1.amazoncognito.com` |
| Aurora cluster (**stopped**) | `dakkamotors-core-dbcluster-xaurpfprbqso` — auto-restarts ~2026-09-25 |
| Route53 hosted zone | `Z051521126KCXW6R2RVF8` |
| ACM certificate | `arn:aws:acm:us-east-1:484907516843:certificate/3eed3285-d186-4ea1-abf5-a980ffab5647` |
| VPC | `vpc-07b008c32ab530661` |
| Private subnets | `subnet-0d0828c388d998a72`, `subnet-02157e24d4ccd96e8` |
| Lambda security group | `sg-04c0624b553366b7a` |

---

## DynamoDB and Cognito

The application reads and writes DynamoDB and Cognito, and nothing else. `dakkamotors-data`
is its own stack, sharing one with nothing DNS-related -- the lesson of the
`dakkamotors-dev` stack whose deletion took the Route53 hosted zone with it.

### Why DynamoDB

Two reasons, and the first was measured rather than estimated.

**Cost.** Aurora Serverless v2 at `MinCapacity: 0` was billing **$13.26 a month** --
$12.85 of it ServerlessV2 ACU-hours -- against `DatabaseConnections` averaging 0.00 over
seven days. It was paid for around the clock and almost nobody connected to it. An
earlier version of this file said `MinCapacity: 0` meant it "costs almost nothing on an
idle day"; Cost Explorer disagreed. DynamoDB for the same period: **$0.0015**.

**The cold start.** Aurora took ~15 seconds to wake on the first request after ten idle
minutes. DynamoDB has no wake-up at all.

Getting out of the VPC came free with both, and retired three workarounds: the inline
image-resize budget, the S3 outbox as a *necessity* rather than a choice, and the
hand-copying of secrets into `config/env.json`.

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

**There is no data to migrate.** The Aurora cluster held test data only, confirmed on
2026-09-18, so `export_aurora` and `import_dynamo` are not part of this. They stay in the
repo and at the `pre-dynamo` tag because they were written, tested and may be wanted for
a future restore -- but the cutover does not run them, and nothing here depends on the
export being correct. That removes the riskiest part of the original plan outright.

It also means the `UserMigration` trigger has nothing to carry: there are no `LEGACYPW#`
items and no existing customers whose passwords must survive. It is still deployed,
because it is correct and costs nothing, and because the first real customer import --
if there ever is one -- would need it.

### It has run. State as of 2026-09-18

| | |
|---|---|
| `dakkamotors-data` | deployed, twelve resources |
| Deployed Lambda | the new code, **out of the VPC** (`VpcConfig` empty) |
| Site | `/` 200, `/api/cars/` 200, `/api/staff/cars/` 302 to the hosted UI |
| Owner account | `moontasir042@gmail.com`, in `owners` and `staff` |
| Buckets | frontend and media emptied; `config/env.json` kept and updated |
| Aurora cluster | **stopped**, still present, pending the teardown below |

**AWS restarts a stopped Aurora cluster automatically after seven days**, so the charge
resumes around **2026-09-25** unless the cluster is gone by then. Stopping it again buys
another seven.

Two things the deploy found that no local check could, both now fixed in `data.yaml`:

* **`MfaConfiguration` without `EnabledMfas`.** Cognito assumes `SMS_MFA` the moment MFA
  is anything but `OFF`, and then refuses the pool because there is no SMS configuration
  and no auto-verified `phone_number` -- neither of which this pool has, deliberately.
  `SOFTWARE_TOKEN_MFA` is what was always meant.
* **`email` left out of the customer client's `WriteAttributes`**, described as the
  CloudFormation-level expression of `LOCKED_PROFILE_FIELDS`. Cognito will not allow a
  *required* attribute to be non-writable, and email is required because it is the
  sign-in name. That control only ever worked in `cognito.update_attributes`.

The first failure also left two orphans, because rollback respects both policies that
exist to prevent loss: the table survived on `DeletionPolicy: Retain` and the pool on
`DeletionProtection: ACTIVE`. The table was **imported** into the stack rather than
deleted and recreated; the empty duplicate pool was removed. If a first create fails
again, check for both before redeploying -- an existing table fails early validation with
nothing but `[AWS::EarlyValidation::ResourceExistenceCheck]` to go on.

### How it was done

Steps 1 to 5 have run. Kept because the next environment -- a staging stage, a rebuild
after a disaster, somebody doing this again elsewhere -- needs the order, and because
step 4 is the one nobody guesses.

1. Create the data stack. Purely additive, costs about nothing, and nothing else can
   proceed without it:

   ```bash
   aws cloudformation deploy --region ap-northeast-1      --template-file infra/data.yaml      --stack-name dakkamotors-data      --capabilities CAPABILITY_NAMED_IAM
   ```

2. Read the outputs and put them where the app reads them:

   ```bash
   aws cloudformation describe-stacks --region ap-northeast-1      --stack-name dakkamotors-data      --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output table
   ```

   `COGNITO_POOL_ID`, `COGNITO_CUSTOMER_CLIENT_ID`, `COGNITO_STAFF_CLIENT_ID` and
   `COGNITO_DOMAIN` go into `backend/zappa_settings.json`, which has all four as empty
   strings today. The staff client secret goes to SSM at
   `/dakkamotors/COGNITO_STAFF_CLIENT_SECRET`, and into `config/env.json`. Then bake the
   pool's signing keys into the deploy, so a cold start is not coupled to a Cognito
   endpoint being reachable:

   ```bash
   curl -s https://cognito-idp.ap-northeast-1.amazonaws.com/<pool-id>/.well-known/jwks.json      > backend/config/cognito_jwks.json
   ```

3. Merge `dynamodb-migration` to `main` and push. That is the deploy: the workflow is
   path-filtered on pushes to `main`. **Not before step 2** -- with the Cognito ids empty
   and no table, every request 500s.

4. Create an owner account, since there is no data and no staff user:

   ```bash
   aws cognito-idp admin-create-user --region ap-northeast-1      --user-pool-id <pool-id> --username <you@example.com>      --user-attributes Name=email,Value=<you@example.com> Name=email_verified,Value=true      --temporary-password '<a strong one>' --message-action SUPPRESS
   aws cognito-idp admin-add-user-to-group --region ap-northeast-1      --user-pool-id <pool-id> --username <you@example.com> --group-name owners
   aws cognito-idp admin-add-user-to-group --region ap-northeast-1      --user-pool-id <pool-id> --username <you@example.com> --group-name staff
   ```

   `MessageAction SUPPRESS` because the pool sends no email at all. After this, every
   further account is made through `/api/staff/accounts/`.

5. Smoke test against the API Gateway origin directly, bypassing the CDN: `/`,
   `/api/cars/`, `/sitemap.xml`, `/llms.txt`, `/robots.txt`, and `/api/staff/cars/`
   (expect 302 to the hosted UI). Sign in as the owner, add a car, upload a photo,
   confirm the derivative appears.

6. **The teardown below. This is the only step still outstanding.** Aurora was the
   rollback until step 5 had been seen to work. It has, so the cluster is now $13.26 a
   month of nothing, and a stopped cluster restarts itself after seven days.

### After it has held, over days

Only once the cutover has run and been quiet for a week. Nothing below is reversible in
the way the cutover is, so the order matters more than the speed.

**The code side is already done** and is on the branch: the ORM models, the Django admin,
`django.contrib.auth`, `django.contrib.sessions` and the transitional bridges are gone,
`DATABASES` is `{}`, and `infra/network-db.yaml` no longer describes a VPC or a database.
What is left is the AWS side, which has to be done by hand with credentials this repo
deliberately does not hold.

1. **Leave the VPC.** Empty both lists in `zappa_settings.json` -- **do not delete the
   `vpc_config` key**, Zappa only sends `VpcConfig` when it is present, so removing it
   leaves the function attached to the old subnets. Then:

   ```bash
   zappa update production
   aws lambda get-function-configuration      --function-name dakkamotors-production      --query 'VpcConfig' --region ap-northeast-1
   ```

   Expect empty `SubnetIds` and `SecurityGroupIds`. If they are not empty, stop -- every
   step below will hang.

2. **Detach the VPC execution policy**, then wait for the ENIs to release. That takes
   20-40 minutes and the subnets cannot be deleted until it has happened.

   ```bash
   aws iam detach-role-policy      --role-name dakkamotors-production-ZappaLambdaExecutionRole      --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole

   # Poll until this returns nothing.
   aws ec2 describe-network-interfaces --region ap-northeast-1      --filters Name=vpc-id,Values=vpc-07b008c32ab530661      --query 'NetworkInterfaces[].NetworkInterfaceId'
   ```

3. **Take a final Aurora snapshot by hand**, before anything deletes anything. `DBCluster`
   carries `DeletionPolicy: Snapshot`, so CloudFormation will take one too -- this is the
   one you control the name of, and it is the only copy of the pre-cutover data that is
   not on somebody's laptop.

   ```bash
   aws rds create-db-cluster-snapshot --region ap-northeast-1      --db-cluster-identifier dakkamotors-core-dbcluster-xaurpfprbqso      --db-cluster-snapshot-identifier dakkamotors-final-pre-dynamo
   aws rds wait db-cluster-snapshot-available --region ap-northeast-1      --db-cluster-snapshot-identifier dakkamotors-final-pre-dynamo
   ```

4. **Update the core stack.** This is the destructive step, and it is an **update, not a
   delete**: `dakkamotors-core` holds the VPC, Aurora *and both S3 buckets*
   (`infra/network-db.yaml`). `delete-stack` would take the site and every photo with it.

   ```bash
   aws cloudformation deploy --region ap-northeast-1      --template-file infra/network-db.yaml      --stack-name dakkamotors-core      --parameter-overrides        FrontendBucketName=<frontend-bucket> MediaBucketName=<media-bucket>
   ```

   The new template has no `DBPassword` parameter, so drop it from the overrides. Watch
   the events: the eleven VPC and Aurora resources go, the buckets and their policies
   stay, and the four outputs `infra/edge.yaml` consumes keep their names.

   ```bash
   aws cloudformation describe-stack-events --region ap-northeast-1      --stack-name dakkamotors-core --max-items 40      --query 'StackEvents[].[LogicalResourceId,ResourceStatus]' --output table
   ```

5. **Confirm the buckets survived**, because this is the failure that is silent until
   somebody loads the site:

   ```bash
   aws s3 ls | grep dakkamotors
   curl -sI https://dakkamotors.com/ | head -1
   ```

6. **Trim the deploy role.** `infra/github-oidc.yaml` grants `ec2:DescribeSubnets`,
   `ec2:DescribeSecurityGroups` and `ec2:DescribeVpcs` purely for Zappa's VPC lookup.
   Redeploy that stack without them.

7. **Retire `remote_env`** in favour of reading SSM at settings import. SSM is reachable
   now, and the VPC was the only reason the values ever had to be hand-copied into
   `config/env.json` -- the most error-prone procedure in this repo.

8. **Move derivative generation to a real asynchronous invoke** and delete the inline
   time budget in `cars/tasks.py`. Its opening docstring stopped being true the moment
   the function left the VPC.

9. **Delete the `LEGACYPW#` items at 90 days.** The TTL does it; check that it did.

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
