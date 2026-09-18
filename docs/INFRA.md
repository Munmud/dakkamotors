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
| `dakkamotors-core` | Tokyo | Both S3 buckets. The VPC and Aurora were deleted 2026-09-18. |
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
| Final Aurora snapshot | `dakkamotors-core-snapshot-dbcluster-m8ulwb0bdvyl` — the cluster was deleted 2026-09-18 |
| Route53 hosted zone | `Z051521126KCXW6R2RVF8` |
| ACM certificate | `arn:aws:acm:us-east-1:484907516843:certificate/3eed3285-d186-4ea1-abf5-a980ffab5647` |

The VPC (`vpc-07b008c32ab530661`), its two private subnets and the Lambda security group
were removed in the same update. The Lambda holds no `VpcConfig` and reaches DynamoDB,
Cognito, SSM and S3 over the public internet.

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
| Aurora | **deleted**, final snapshot `dakkamotors-core-snapshot-dbcluster-m8ulwb0bdvyl` |
| VPC, subnets, security groups | deleted in the same stack update |

The cluster was stopped first to halt ACU billing, then started again only because a
stopped Aurora cluster cannot be deleted -- `DeleteDBCluster` refuses one. Worth knowing
before planning a teardown around a stopped cluster: the stop is a cost measure, not a
step towards deletion.

Deleting the RDS resources took about fifteen minutes. The VPC took longer, because four
Lambda ENIs stayed `in-use` after the function left it. That is the documented 20-40
minute release, and it is why the order in this runbook puts the VPC last.

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

6. The teardown, once step 5 had been seen to work. Aurora was the rollback until then.

### The teardown, done 2026-09-18

Run as a stack **update**, never `delete-stack`: `dakkamotors-core` holds the frontend and
media buckets alongside the database, and deleting the stack would take the site and every
photo with it. The change set was inspected first and showed twelve removals, all VPC and
RDS, with no bucket or bucket policy in scope.

```bash
aws cloudformation deploy --region ap-northeast-1   --template-file infra/network-db.yaml --stack-name dakkamotors-core   --parameter-overrides FrontendBucketName=dakkamotors-frontend                         MediaBucketName=dakkamotors-backend-media
```

The `DBPassword` parameter is gone from the template, so it is no longer passed. The four
surviving outputs keep their names because `infra/edge.yaml` takes all four as parameters.

Still outstanding, none of it costing anything:

1. **Trim the deploy role.** `infra/github-oidc.yaml` no longer grants
   `ec2:DescribeSubnets`, `DescribeSecurityGroups` or `DescribeVpcs` -- Zappa needed them
   to look up a VPC it no longer joins -- but that stack has not been redeployed, so the
   live role still has them:

   ```bash
   aws cloudformation deploy --region ap-northeast-1      --template-file infra/github-oidc.yaml --stack-name dakkamotors-github-oidc      --capabilities CAPABILITY_NAMED_IAM
   ```

2. **The Lambda execution role still grants ENI management.** `zappa-permissions`, the
   inline policy on `dakkamotors-production-ZappaLambdaExecutionRole`, carries
   `ec2:CreateNetworkInterface` and friends. Zappa writes that policy for VPC functions
   and `manage_roles: false` means nothing regenerates it, so it is a hand edit. Harmless
   -- a grant with nothing to act on -- but it is dead privilege.

3. **Three stale SSM parameters.** `/dakkamotors/DB_PASSWORD` was the Aurora master
   password and the cluster is gone. `/dakkamotors/ADMIN_PASSWORD` and
   `/dakkamotors/MAHSIUL_PASSWORD` were Django admin credentials for accounts that no
   longer exist. Standard parameters are free, so this is hygiene rather than cost, but a
   live-looking password that opens nothing is worse than no parameter.

4. **Retire `remote_env`.** SSM is reachable now, so
   `ssm.get_parameters_by_path("/dakkamotors/", WithDecryption=True)` at settings import
   replaces `config/env.json`. The VPC was the only reason those values ever had to be
   hand-copied into an S3 object, and that dance is the most error-prone procedure in this
   repo. Keep `remote_env` alongside for one release so a rollback needs no settings
   change.

5. **Move derivative generation to a real asynchronous invoke** and delete the inline time
   budget in `cars/tasks.py`. Its opening docstring stopped being true the moment the
   function left the VPC: `lambda:InvokeFunction` is reachable, so a self-invoke no longer
   hangs until timeout.

The `LEGACYPW#` cleanup from the original list does not apply -- there was no data to
migrate, so no items were ever written.


### What the VPC cost, kept as a record

Three sections used to live here: why the database was Aurora Serverless v2, why there was
no NAT Gateway, and what the VPC could and could not reach. None of it is true any more,
and the shape of it is worth remembering because it explains code that is still in the
repo.

The Lambda sat in a VPC with no NAT Gateway, because a NAT is ~$32/month and the site's
entire bill was under $16. A free S3 Gateway Endpoint let it reach S3. It could reach
Aurora and S3, and **nothing else** -- no SSM, no SQS, no SES, no `lambda:InvokeFunction`.
A self-invoke did not fail fast; it hung until the 120-second timeout and surfaced as a
504.

That single constraint shaped three things:

* **`cars/tasks.py` resizes images inline, against a time budget**, because it could not
  invoke itself asynchronously.
* **`mail.queue_email` writes to an S3 outbox** for a second Lambda outside the VPC to
  send. Brevo was unreachable from inside.
* **Secrets were hand-copied into `config/env.json` on S3**, because SSM was unreachable
  at settings import.

All three are now choices rather than constraints. The outbox is worth keeping on its
merits -- a booking must not fail, or wait 600ms, because of an email. The other two are
in the outstanding list above.


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

### The smoke test is the deploy gate

Zappa probes `/` immediately after uploading and calls a non-200 a failed deploy. It is
not a reliable gate on its own: the probe can hit a cold start, and on any release that
changes `vpc_config` it runs while the ENIs are still detaching -- which is exactly what
the DynamoDB cutover did. The workflow treats it as a warning and gates on the smoke test
at the end of the job, which hits the origin directly so a cached page cannot mask a
broken deploy.

There are no migrations to race any more. A schema change is a code change to
`cars/store/`: an attribute nothing writes is simply absent from an item rather than
NULL, so adding one is free. Changing what an existing one *means* needs a backfill you
write yourself, and `reconcile_counters` is the model for how.


### Why there is no CORS configuration

One CloudFront distribution serves the React app at `/` and proxies `/api/*` to API
Gateway, so the browser only ever talks to one origin. Locally, the Vite dev server
proxies `/api` to Django to reproduce the same arrangement. If you ever find yourself
adding `django-cors-headers`, something has drifted from this design.

---

## Staff accounts

Cognito groups, not Django permissions. `cars/staff/permissions.py` is the whole policy
and is the only place a role is defined.

| Group | Can do |
|---|---|
| `owners` | Everything, including staff administration |
| `inventory-managers` | Cars, photos, questions, schedules, slots; view and change bookings; view customers |
| `staff` | Nothing by itself -- it is what gets somebody *past* the door, not through any particular one |

### Why staff administration is owners-only

`OWNER_ONLY` in `permissions.py` holds back `staff.*` and `group.change` whatever else a
group is given. Whoever can edit staff accounts can open the owner's account, reset its
password, or add themselves to `owners` -- that is not a bug in the permission, it is what
the permission *means*, so it belongs to owners alone.

**This is stricter than the arrangement it replaced.** `StaffAccountAdmin` let inventory
managers administer colleagues inside a sandbox: superusers filtered from the queryset
*and* refused by the permission hooks, `is_superuser` and `user_permissions` absent from
the fieldsets *and* forced on save, group choices limited to an allowlist, and nobody able
to deactivate themselves. Four guards that all had to agree, because the underlying page
was powerful and access had to be clawed back. Here the page is simply unreachable, and
`tests_staff_accounts.py` asserts the shut door instead of re-proving the sandbox.

What survives from that design, because it still bites somebody who *does* have the
power: an owner cannot deactivate their own account, and the group list on the form offers
`ASSIGNABLE_GROUPS` only -- `owners` is not a choice, so a POST carrying it fails
validation rather than being silently dropped.

**There is no delete.** Deactivating is the reversible verb, and a removed account would
orphan every booking, question and notification keyed on that sub.


### Adding someone

Through the staff pages: sign in as an owner and use `/api/staff/accounts/add/`. The pool
sends no email, so the page shows the temporary password once, in the response to the
request that created it -- pass it on there and then. Cognito forces a change at first
sign-in, so its lifetime is one use.

The `create_inventory_user` command that used to live here is gone with
`django.contrib.auth`, and with it the three-step dance of putting a password into SSM,
copying it into `config/env.json` because the Lambda could not reach SSM from inside the
VPC, and remembering to take it out again.

### The first owner

A chicken and egg: the staff page needs an owner signed in, and a new pool has nobody.
Make the first one by hand, once.

```bash
POOL=ap-northeast-1_czNqEUiq5
EMAIL=you@example.com
aws cognito-idp admin-create-user --region ap-northeast-1 --user-pool-id $POOL   --username "$EMAIL"   --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true   --temporary-password '<a strong one>' --message-action SUPPRESS
for g in staff owners; do
  aws cognito-idp admin-add-user-to-group --region ap-northeast-1     --user-pool-id $POOL --username "$EMAIL" --group-name $g
done
```

`MessageAction SUPPRESS` is required, not optional: the pool has no `EmailConfiguration`,
so Cognito has nothing to send the invitation with and fails if asked to.


---

## Secrets

Stored as SSM **SecureString** parameters (Parameter Store, not Secrets Manager — no
per-secret monthly charge):

```
/dakkamotors/DJANGO_SECRET_KEY
/dakkamotors/COGNITO_STAFF_CLIENT_SECRET
/dakkamotors/BREVO_API_KEY
```

Read one back:

```bash
aws ssm get-parameter --name "/dakkamotors/DJANGO_SECRET_KEY" \
  --with-decryption --query Parameter.Value --output text
```

> On Git Bash for Windows, prefix SSM commands with `MSYS_NO_PATHCONV=1` or the leading
> `/` in the parameter name is rewritten into a Windows path and the call fails with
> "Parameter name must be a fully qualified name", which does not point at the cause.

Three more are stored and open nothing: `/dakkamotors/DB_PASSWORD` was the Aurora master
password and the cluster is gone; `/dakkamotors/ADMIN_PASSWORD` and
`/dakkamotors/MAHSIUL_PASSWORD` were Django admin credentials. Free to keep, worth
deleting anyway -- a live-looking password that opens nothing is worse than no parameter,
because the next person has to work out which it is.

The Lambda receives these through Zappa's `remote_env`: a private JSON file at
`s3://dakkamotors-backend-media/config/env.json`, fetched on cold start. Secrets are
therefore never committed to git and never appear in the Lambda console.

---

## Building it from scratch

```bash
aws configure set region ap-northeast-1

# 1. Secrets
python - <<'PY'
import secrets, boto3
ssm = boto3.client("ssm", region_name="ap-northeast-1")
# (no other secret is generated here: Cognito owns passwords now)
for name, value in {
    "/dakkamotors/DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
}.items():
    ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
PY

# 2. Buckets
aws cloudformation deploy --stack-name dakkamotors-core \
  --template-file infra/network-db.yaml

# 3. CI/CD role
aws cloudformation deploy --stack-name dakkamotors-github-oidc \
  --template-file infra/github-oidc.yaml --capabilities CAPABILITY_NAMED_IAM

# 4. Table, pool, migration trigger, backend IAM policy
aws cloudformation deploy --stack-name dakkamotors-data \
  --template-file infra/data.yaml --capabilities CAPABILITY_NAMED_IAM

# 5. Backend. Put the four Cognito outputs into backend/zappa_settings.json first, and
#    save the pool JWKS to backend/config/cognito_jwks.json. Step 4 must come before
#    this: with the ids empty there is nothing to authenticate against.
cd backend
zappa deploy production
zappa manage production "collectstatic --noinput"
# Then make the first owner by hand - see "The first owner" above.

# 6. CDN. us-east-1, and no certificate on the first pass so it works before DNS.
aws cloudformation deploy --stack-name dakkamotors-edge --region us-east-1 \
  --template-file infra/edge.yaml \
  --parameter-overrides \
    FrontendBucketDomain=... MediaBucketDomain=... ApiGatewayDomain=...

# 7. Frontend, and the final DNS cutover once the registrar is updated.
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
