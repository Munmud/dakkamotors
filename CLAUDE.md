# Dakka Motors

A used-car dealership site for a shop in Hamura, Japan. Django + DRF on AWS Lambda,
React SPA, bilingual (English/Japanese). Buyers browse inventory, ask questions about a
car, and book test drives; staff answer, confirm and manage stock.

Everything runs in `ap-northeast-1` except the three things AWS forces into `us-east-1`
(CloudFront, its ACM certificate, and Budgets).

## Layout

```
backend/         Django 5.2 + DRF, deployed to Lambda by Zappa
  cars/          the single app -- inventory, bookings, Q&A, notifications
  config/        settings, root urls, wsgi
frontend/        React 19 + Vite, deployed to S3 behind CloudFront
infra/           hand-written CloudFormation (no CDK, no Terraform)
docs/INFRA.md    the real runbook: live resource ids, cost rationale, teardown
docs/ADS.md      advertising one car at a time: the pixel, the token, the campaign
```

## Commands

```bash
# Backend
cd backend
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
cp .env.example .env
python manage.py runserver

# Tests. DynamoDB Local is required.
docker compose up -d dynamodb          # from the repo root
cd backend
DYNAMODB_ENDPOINT_URL=http://localhost:8123 DDB_TABLE=dakkamotors_test \
  AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local \
  python manage.py test

# Frontend
cd frontend && npm ci && npm run dev
npm run check-i18n                     # en/ja parity, plus a warning for unused keys
```

Deploys are automatic: push to `main`, path-filtered GitHub Actions workflows deploy the
backend (Zappa) and the frontend (S3 + CloudFront invalidation) via OIDC. No static keys.

Migration commands (see `docs/INFRA.md`):

```bash
git checkout pre-dynamo    # the only commit that can still run the exporter
python manage.py export_aurora --out ./migration/<date>/
python manage.py import_dynamo --from ./migration/<date>/  # on the new code
python manage.py reconcile_counters [--fix]                # after a restore, or on doubt
```

Run the import from a laptop with SSO credentials rather than through `zappa manage` --
it is an ops script, and nothing about it needs a Lambda.

## The data layer: DynamoDB and Cognito

**There is no relational database and no Django auth.** The ORM models,
`django.contrib.auth`, `django.contrib.sessions`, `django.contrib.admin` and
`cars/migrations/` are gone; `DATABASES` is `{}`, so anything reaching for the ORM fails
at the call rather than quietly opening SQLite. `cars/store/` and `cars/cognito.py` are
the only ways in.

**The cutover has run** (2026-09-18) and **Aurora is deleted**, leaving one final
snapshot. `main` is deployed, the Lambda is out of the VPC, and the site serves from
DynamoDB and Cognito. RDS was $13.26 of a $15.75 monthly bill; the bill is now about
$1.60, nearly all the Route 53 hosted zone.

`backend/cars/store/` is the data layer: PynamoDB 6.1, one table, single-table design with
a discriminator. Cognito holds identity; DynamoDB holds only what Cognito has nowhere to
put (a pending sign-up's name and phone, reset tokens, carried-over password hashes).
**The pool sends no email at all** -- verification and reset messages go through Brevo,
bilingual and branded. Do not wire it to SES; that is where the previous attempt stalled.

Passwords cannot be migrated -- Cognito will not accept a hash on `AdminCreateUser` -- so
a `UserMigration` Lambda trigger (inline in `infra/data.yaml`) verifies the carried-over
Django hash on a customer's first sign-in. `tests_user_migration.py` extracts that inline
source from the template and tests it against hashes Django actually produces, so the
thing that will run is the thing that was checked.

**Sign-up is confirmed by a six-digit code typed into the page, and the code signs
the customer in.** A link was tried first and lost the page: opened in the phone's mail
app, it left the laptop tab behind. The code is checked against the *address*
(`store/auth.check_code`, `PENDING#<email>`), never looked up by itself -- six digits
are not unique across customers -- and a wrong one counts, five and the sign-up is
dead (`MAX_ATTEMPTS`). Cognito never hands a password back, so a second inline
trigger, `LinkAuthFunction`, runs the pool's custom auth flow with one challenge:
present the code. `cognito.sign_in_with_link` answers it right after `confirm` and
**before** `finish_registration`, because the trigger recognises the code by reading
the same pending item -- it builds that key by hand, as a Lambda cannot import
`store/keys.py`, and `tests_link_auth.py` holds the two spellings together. A refused
sign-in still verifies; the response says `signed_in: false` and the app falls back
to the sign-in form. `ALLOW_CUSTOM_AUTH` on the customer client is deliberate and the
template comment says why it is safe; do not add a password challenge to that trigger.
`PendingToken`/`PENDTOK#` are no longer written and exist only to delete link-era items.

**A guest is asked to sign up before the calendar**, not after picking a time, and
the register form shows the code screen in place with `next` intact, so they come
back to the page they were on, signed in.

**The staff masthead counts what is waiting** -- unanswered questions, open requests,
bookings awaiting confirmation -- through the `cars.staff.context.attention` context
processor: three Queries per staff page, uncached because a count that lags lies, and
each the same definition the section's own page uses.

Ids are strings, so URL patterns take `<str:pk>`. A slot's id is derived from (schedule,
start time) -- that is what makes materialising slots idempotent, and why two fixtures
wanting distinct slots at the same instant need distinct rules.

### Rules

* **Never build a `pk`/`sk` inline.** Every key comes from `store/keys.py`.
* **Never index `cancellation_reasons` by hand.** PynamoDB regroups transaction items by
  operation type (ConditionCheck, Delete, Put, Update) regardless of call order, so the
  list is parallel to *that*, not to the order you added them. Use the labels
  `store/txn.py` provides. Getting this wrong misattributes a failure and tells the
  customer the wrong thing.
* **Never write an unconditional `UpdateItem` against an item that may not exist.** It is
  an upsert, and the stub it creates carries no discriminator -- invisible to every
  polymorphic read in this package, and enough to block the real item from ever being
  created. Guard with `.pk.exists()`. This cost real debugging time once already.
* **Every path that can change the listing card photo must call
  `images.refresh_primary`.** `store/images.py` names all seven. Six are in that module;
  the seventh is `media.set_order`, which is there because it reorders photos and videos
  together and `images.py` deliberately cannot see a video. Moving a video to the front
  never touches a photo and still changes the card, because the photos beneath it
  re-rank.
* **`store/questions.py` is the only permitted writer of `is_published`.** That
  exclusivity replaces the `CheckConstraint` DynamoDB cannot express, and a test enforces
  it mechanically.
* **Deleting a car must clear its `CARID#` pointer too.** It lives in its own partition,
  so neither the car's partition sweep nor the guards touch it -- `store.cars.delete`
  does it explicitly. A pointer outliving its car makes `config/urls.py` **301** an old
  numeric URL to a slug that no longer resolves, and a cached 301 to a 404 is worse than
  the 404 because nothing asks again. This shipped broken once; there is a test.
* **Test truncation drops to the raw client** (`tests_store.truncate_table`), for the same
  discriminator reason. `tests_store.ensure_table` creates the table, and **both** entry
  points call it -- Django orders tests by module, so `tests.py` runs before
  `tests_store.py` and leaning on the other one having gone first is how the suite came to
  pass locally against a leftover container and fail against a fresh one.

### The gallery: photos and videos are separate item types

A car has many photos (`CarImage`, `IMG#`) and many videos (`CarVideo`, `VID#`) in **one
order**, over one shared `order` number space. `store/media.py` is the only definition of
that merge; `images.py` and `videos.py` each know only their own type.

That separation is load-bearing rather than tidiness. `images.for_car` queries
`begins_with(sk, "IMG#")`, so `pick_primary` receives a homogeneous list *by
construction* -- a video can never become the listing card, with no code at all. A
unified item with a `kind` flag would put that filter on all seven refresh paths and on
`Car.primary_image`, which rebuilds a detached `CarImage` from `primary_image_ref` and
would otherwise happily build one out of a video row. Derivatives are the secondary
argument: they are most of what `CarImage` is, and meaningless on half the instances of
a merged type.

Both lists sort on `(order, sk)`, never `(order, id)`. Within a type those are the same
ordering; across types they are what makes one sequence possible.

`cars.detail()` returns videos from the Query it already makes. Reading them separately
would turn the car page's single round trip into two, which is what
`tests_store.count_dynamo_calls` exists to catch.

Nothing transcodes a video and nothing extracts a poster frame, so there is no `sources`
and no thumbnail: a video's thumbnail is the car's primary photo with a play badge.
`direct-upload.js` picks its size cap from the input's *name* -- it tests `/video/`
before `/image/` -- which is why the staff formset is prefixed `videos` and its field is
called `video`.

The `video` field on the car API is a shim for the JS bundle CloudFront is still
serving, not an interface. Remove it, and `detail.video` from both locale files, once
that cache has turned over.

### Names in Japanese

`brand_ja`, `model_name_ja` and `color_ja` are optional twins of the English fields.
The bare field **is** the English -- slugs, JSON-LD, emails and the staff pages are
built from it -- so `frontend/src/lib/format.js` `carField` shows the Japanese only to a
Japanese reader and only when staff typed one, field by field. Do not run these
through `pickLocalized`, which expects `_en`/`_ja` pairs and would show a Japanese
name to an English reader as its "fallback"; that shipped for about a minute.

### Car requests

`cars/requests.py` is the domain module, `store/requests.py` the store, `POST
/api/requests/` the one public write on the site that needs no account -- a person
looking for a car the shop does not have is exactly who has none. `AllowAny` with the
registration throttle. A guest gives name, email and phone; a signed-in customer's are
copied from Cognito onto the request at the time and the posted ones are ignored.
Staff see them at `/api/staff/requests/` and mark them resolved; the detail page has a
"Copy for a post" block with the wish and nothing personal, because the owner reuses
them for advertising.

### Advertising

The shop makes a video for one car, spends about **¥1,000** showing it around Hamura,
and stops the moment that car gets a test drive booked. `docs/ADS.md` is the runbook.

`META_PIXEL_ID` is not a secret and lives in `zappa_settings.json`; `pages._pixel`
injects Meta's snippet **only when it is set**, so tests, local development and the
screenshot passes carry no tracking and `window.fbq` never exists -- the same bargain
`CLOUDFRONT_DISTRIBUTION_ID` makes for `cdn.invalidate`. The id is checked to be digits
first: it lands in a `<script>` block on a public page cached at the edge. The app never
learns it; `frontend/src/lib/pixel.js` calls `window.fbq` when it is there and does
nothing when it is not. Four events -- `PageView` per client-side navigation (the
snippet fires only the first), `ViewContent` on a car page, `InitiateCheckout` on its
booking page, `Schedule` once the **server accepted** the booking.

**The ad set is deliberately not optimised for those conversions.** Meta wants roughly
fifty a week to leave the learning phase and ¥1,000 buys single digits, so the campaign
buys landing page views and the events measure rather than bid. Written down in two
places because it is exactly the decision somebody reverses a year later.

`Car.ad_set_id` is typed into the staff form; `advertising.stop_for` pauses it from
`create_booking`, **fire-and-forget beside `mail.notify_staff_of_booking`** -- a booking
must never fail over an advertisement, which would lose the thing the advertisement was
bought to produce. `store.cars.claim_ad_pause` is a **conditional** update on
`ad_paused_at`, so two bookings in the same second cannot both pause and both email; and
`ad_paused_at` rides with `ad_set_id` in `store.cars.update`, because a stamp left over
from the last campaign would make the next one unable to ever stop itself. It rides on
the id **changing**, so re-running the *same* ad set takes two saves -- clear, save,
retype, save -- and the staff form says so, and names the date it stopped, because that
state is otherwise invisible and unguessable. When Meta
refuses, the owner gets the **opposite** message -- the ad set is still spending and only
a person can stop it now.

### Free-form specs

`Car.specs` is a JSON list of `{label_en, label_ja, value_en, value_ja}`; list order is
display order. Rules live in `cars/specs.py` so the form, the importer and the tests
share one definition. The form refuses a half-filled row; the module drops it -- the
importer has nobody to tell.

**Do not add specs to `build_search_blob`.** `Car` is an AllProjection into GSI1 and
every listing page reads the whole item, so forty pairs in two languages would roughly
double that read for a search nobody asked for. `MAX_PAIRS = 40` exists for the same
reason -- it is a read cost, not a storage one, and nowhere near the 400KB item limit. They are not in the JSON-LD either; `seo.py` records why, and why videos are not
a `VideoObject`.

**Everything is on the add page**, photos and videos included. Specs ride on the car
item itself, so they land in the same conditional write as its guards and no orphan is
possible. Media are different and the ordering is the rule: `images.create` writes an
`IMG#` row into the car's partition and then calls `refresh_primary`, which GETs the
META item -- so a photo written before the car exists is a `NotFound`, and one written
before the guards are checked is an orphan child in a partition that never gets a car.
`_create` therefore attaches media only after `cars.create` has returned.

That works at all because `uploads.py` keys objects by a fresh uuid rather than by car
id, so the bytes reach S3 before the form is even posted. The corollary is that a
refused create can strand an object in the bucket, which is why `_create` re-renders
with the **bound** formsets: the hidden `image_key` survives, so the retry reuses the
object instead of uploading a second one.

### Staff pages

`cars/staff/` is server-rendered and replaces `django.contrib.admin` entirely: cars,
images, bookings, slots, schedules, questions, customers and staff accounts.
`django.contrib.admin` is built on `QuerySet` and `ModelForm`, so an entity that leaves
the ORM takes its admin page with it -- which is why each page was built in the same step
as its store module.

**The staff stylesheet is inline in `templates/staff/base.html`, and stays inline.**
It began that way because `collectstatic` once ran after `zappa update` and every
deploy served seconds of unstyled admin; that window is gone (see "Static files" under
"Things that will bite"), and it stays inline because it is one request fewer on a
phone in the lot and nothing to fetch before the first paint. It has three containers that mean three different
things -- `.sheet` is the page's one working surface, `.panel` is a section inside it
with a heading and a rule and no box, and `.aside` is the genuinely separate thing
(deleting, a warning), which keeps its border precisely because nothing else has one.
`table.stack` restacks a table into labelled rows below 560px; use it wherever a
sideways scroll would hide the controls, which is how the gallery's Move column came to
be off-screen on every phone. The pages are for one or two non-technical people, often
on a phone in the lot: 16px base so iOS does not zoom on focus, 44px on every control,
and no inline `style=` attributes -- `grep -rn 'style="' templates/staff/` is meant to
return nothing, because inline styles are how the previous drift happened.

`cars/tests_staff_snapshot.py` renders every staff page to a folder for screenshotting
(`STAFF_SNAPSHOT_DIR=... manage.py test cars.tests_staff_snapshot`; it skips otherwise).
It is a test module because signing in is only possible in-process -- `sign_in` patches
`cars.authentication.verify`, which a separate `runserver` would never see, and the
alternative is a settings flag that skips auth, which is the kind of thing that ships.
Serve the folder over http, not `file://`: the templates ask for `/static/cars/*` by
absolute path, and `formset-rows.js` is what reveals the "+ Add a row" buttons.

Authentication is Cognito's hosted UI, isolated in `staff/auth.py`; the views, forms and
templates never learn how somebody signed in. **`staff/permissions.py` is the whole
policy.** `OWNER_ONLY` keeps staff administration to owners, which is stricter than the
sandbox it replaces: whoever can edit staff accounts can open the owner's, so the
escalation is refused before a page is reached rather than by four guards agreeing.

### Getting a staff account

**Nobody registers as staff.** Self sign-up exists, but it is the customer path and
produces an account in no groups. Two independent things stop that becoming staff access:
the staff pages accept only tokens minted by the *staff* app client, which has no
password auth flow at all, and group membership is admin-only.

An owner adds people at `/api/staff/accounts/add/`. **The pool sends no email**, so the
page shows the temporary password once, in the response to the request that created it --
pass it on there and then. Cognito forces a change at first sign-in.

The form offers `ASSIGNABLE_GROUPS`, which is `inventory-managers` only. `owners` is
deliberately absent, so a POST carrying it fails validation rather than being quietly
dropped -- which means **a second owner can only be made from the CLI**:

```bash
POOL=ap-northeast-1_czNqEUiq5
aws cognito-idp admin-create-user --region ap-northeast-1 --user-pool-id $POOL   --username "$EMAIL"   --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true   --temporary-password '<strong>' --message-action SUPPRESS
for g in staff owners; do
  aws cognito-idp admin-add-user-to-group --region ap-northeast-1     --user-pool-id $POOL --username "$EMAIL" --group-name $g
done
```

`--message-action SUPPRESS` is required, not tidiness: the pool has no
`EmailConfiguration`, so Cognito has nothing to send an invitation with and errors if
asked. The same command is how the **first** owner is made on a fresh pool, which is
otherwise a chicken-and-egg -- the page that creates accounts needs an owner signed in.

A temporary password lasts **seven days** and Cognito forces a change on first use, so
its real lifetime is one sign-in. If one expires unused, reissue rather than creating a
second account -- the address is the username, so a second `admin-create-user` fails with
`UsernameExistsException`:

```bash
aws cognito-idp admin-set-user-password --region ap-northeast-1 --user-pool-id $POOL   --username "$EMAIL" --password '<strong>' --no-permanent
```

### Signing a test in

`tests_fake_cognito.py`. Both cookie flows resolve `cars.authentication.verify` at call
time, so patching that one name covers DRF and the staff pages, and **nothing in
production branches on being tested**. Use `sign_in(self.client, user)`, or
`staff=True` for the staff pages.

moto is used only where the token itself is the subject -- `tests_cognito.py` and
`tests_staff_auth.py` mint and verify real RS256 signatures. It is deliberately not used
for the rest: moto is an optional dependency, so those tests would be `skipUnless`-gated
and would vanish on any machine without `requirements-dev.txt`.

Every class is a `SimpleTestCase`. That is load-bearing rather than tidiness: with no
database configured, an ORM call that survived the teardown fails instead of quietly
opening SQLite. `tests_store.count_dynamo_calls` replaces `assertNumQueries` and is worth
more than it was -- the car detail page is deliberately one Query, and a per-question read
in a loop is exactly what would undo that.

## Conventions

**Rules live in domain modules, not views.** `booking.py`, `qa.py`, `notifications.py`
hold every check, so the same rule applies from the API, a management command, curl or a
test. Views are thin and translate errors into responses. The store raises typed errors
and knows no wording; the domain module owns every sentence a customer reads.

**Comments explain why, not what.** The codebase is deliberately heavy on rationale --
why a slug never changes, why `bump_car` exists, why the pool sends no email. Match
that. A comment that restates the code is noise; one that records a decision is the most
valuable thing in the file.

**Tests assert behaviour and wording.** Customer-facing strings are asserted verbatim in
`cars/tests.py`. If a change makes you edit one of those strings, suspect the change.

**Nothing runs on a timer.** Slots are materialised when availability is read, not by a
cron job. This began as a way to let Aurora scale to zero, and Aurora is gone; it
survives because it is one fewer moving part, and because the expiry sweeps it replaced
are now TTL attributes the table handles itself.

**Uploads go straight to S3.** Lambda has a ~4.5 MB request ceiling, so the staff pages
sign a presigned POST and the browser uploads directly. `direct-upload.js` is
progressive enhancement -- file inputs stay file inputs, so a JS failure falls back to a
normal upload. `formset-rows.js` beside it makes the same bargain the other way round:
its "+ Add a row" buttons ship with `hidden` set and the script removes it, so a page
that never got the script shows the rows Django rendered rather than a button that does
nothing. Both files are referenced through `{% static %}` and served under
content-hashed names; a literal `/static/` path in a template is refused by a test,
because that is how a changed script once failed to reach anybody.

**Secrets come from SSM at settings import**, via `config/ssm.py`, which runs only when
`AWS_LAMBDA_FUNCTION_NAME` is set -- so tests and local development make no network call,
and a real environment variable always wins. Zappa's `remote_env` is gone: it existed
because the VPC put the SSM API out of reach, and it meant every secret lived in two
places that had to be kept in step by hand. Non-secrets live in `zappa_settings.json`,
in git, where they can be reviewed.

**Email is queued, never sent inline.** `mail.queue_email` writes JSON to an S3 outbox
and a second Lambda sends it via Brevo (not SES). Brevo is reachable directly now, so
the outbox is a choice rather than a constraint: it is fire-and-forget by design, and a
customer's booking must never fail -- or wait 600ms -- because of an email.

**Every customer-facing email is bilingual, off a stored language.** `CarQuestion`,
`PendingRegistration`, `CarRequest` and now `Booking` each carry one, snapshotted when
the record is made -- not looked up from the account, because the three messages a
booking sends are days apart and a guest has no account to consult. `mail._when` uses
Django's `date_format`, never `strftime`: its Japanese line was `%Y年%-m月%-d日` for a
year and had never once run, and `%-m` is glibc-only while Windows raises on 年 outright.

**A guest has nowhere to sign in, and `mail._can_sign_in` is the only place that
decides.** `identity.is_guest` reads the `guest:` prefix (`identity.GUEST_PREFIX`, not
`booking`'s -- `booking` imports `mail`, so the constant cannot live there). Three
messages pointed at `/account` for a day after guest booking shipped, which is a sign-in
form with nothing behind it for them: cancel and reschedule are `IsAuthenticated`. They
are told to phone. `identity.user_for_sub` short-circuits on the same prefix rather than
spending an `AdminGetUser` discovering what it already said.

**The stylesheet is partials, and the order is the cascade.** `frontend/src/styles.css`
is an import barrel and nothing else; the numbered files under `frontend/src/styles/`
are the sheet. Reordering the imports changes what wins. Vite inlines them into one
hashed stylesheet, which `pages.py` depends on -- it scrapes the asset tags out of the
built `index.html`, so a stylesheet delivered any way other than a `<link>` in `<head>`
would leave every server-rendered page unstyled for crawlers and link previews.

**The frontend is bilingual.** Every user-visible string goes through `react-i18next`
with keys in both `en.json` and `ja.json`. `npm run check-i18n` fails the build otherwise.
It also warns about keys no component references -- comparing the two files only against
each other cannot catch a key both of them have and nothing reads, which is how seven
accumulated. That half warns rather than fails, because `t(`status.${x}`)` cannot be
resolved statically and a check that cries wolf gets disabled.

**The brand is two assets, used at two scales.** `docs/Logo.png` is the master artwork
-- a kei car, a swoosh and a chrome DM, about 1167x600 once trimmed. It is an
illustration, so it only goes where it is large enough to read, which is the Open Graph
share card alone. The band carries the monogram and nothing else. At 32px it is a grey smudge and at 16px nothing survives, so
the small sizes get the **DM monogram** instead -- the favicon and the 26px logo in an
email masthead. `docs/brand/generate.py` bakes every raster from both and explains each
format choice; nothing redraws the artwork, the cuts are a trim and a resize.

The monogram's path data exists in three files (`frontend/public/plate.svg`, the inline
`BrandMark` in `Header.jsx`, and the generator) because the favicon must stand alone,
the header must be inline for the gradient to paint with the masthead, and the generator
has no SVG renderer. `test_the_monogram_is_the_same_shape_everywhere` fails if they
drift. **`/plate.svg` keeps its name** though it has not been a number plate since the
rebrand: the name is pinned in `infra/edge.yaml` as its own CloudFront behaviour, and
renaming it is a distribution update that would have to land in step with a frontend
deploy or the site serves a 404 for its own icon.

`--ink` is `#1c2b33` and is free to move: nothing on the site composites against it.
`generate.py`'s own `INK` is still the artwork's `#1b2734`, and must stay that, because
`share_card()` extends the canvas around pixels painted on it. The two are different
numbers on purpose -- one is the page, one is the picture.

The artwork has been the home masthead twice and been taken off twice; the second time
it went it took a `--hero-ground` token with it, which existed only because an opaque
rectangle has to sit on its own baked colour. If it comes back a third time, that token
comes back with it, and `hero()` in `generate.py` (also removed) cannot re-ground the
picture -- its ground is baked into `docs/Logo.png`.

**The yellow is `--plate`, and it is the kei number plate**, which is the legal marker
of the class this shop sells rather than an accent picked for contrast. It says exactly
two things: this is the price, and this is the one action on the screen. Selection is
ink reversed out, not yellow -- the booking flow once had a yellow selected day, a
yellow selected slot and a yellow button on one short screen, and none of them led.

Email inverts the mark -- ink letters on a chrome tile, rather than chrome on ink --
because the masthead band it sits in is already `--ink`. The tile is a table cell with a
`bgcolor`, not an image, so the logo still reads with images blocked; that is the whole
point of it and there is a test. **`email-mark-d.png` is kept although nothing renders
it**: messages delivered before the rebrand still fetch it when they are opened.

## Things that will bite

* **Removing an attribute declaration erases live data.** `store.cars.update` ends in
  `car.save()`, a full PutItem, and PynamoDB serialises only *declared* attributes -- so
  deleting one from the model silently wipes it from every item on the next save. Retire
  an attribute only after whatever reads it has been migrated. `video_name` was removed
  this way, and was safe only because the table had just been emptied.
* **There are no migrations.** A schema change is a code change to `cars/store/`, and
  an attribute that is not written is simply absent from an item rather than NULL. Adding
  one is free; changing the meaning of an existing one needs a backfill you write.
* **`/api/cars/*` is a separate CloudFront behaviour** that allows only GET/HEAD/OPTIONS
  and strips cookies. A POST there is refused by the CDN with no Django log line. New
  authenticated endpoints must not live under that prefix.
* **The Lambda is no longer in a VPC.** That retired the hand-copy of secrets into
  `config/env.json` and demoted the S3 outbox from necessity to choice. It has **not**
  yet retired the inline image-resize budget in `tasks.py` -- `lambda:InvokeFunction` is
  reachable now, so that can become a real async invoke, and it is the last outstanding
  item from the cutover. `vpc_config` in `zappa_settings.json` holds empty lists and
  **the key must stay**: Zappa only sends `VpcConfig` when it is present, so deleting the
  key leaves a deployed function attached to subnets nothing mentions.
* **A staff save invalidates the CDN** (`cars/cdn.py`, called from `staff/views_cars.py`).
  The public pages are cached at the edge for up to fifteen minutes with the car's JSON
  embedded as initial data, and a status change once stayed invisible for a quarter
  of an hour. The call is fire-and-forget and a no-op without
  `CLOUDFRONT_DISTRIBUTION_ID`; the grant is in `BackendPolicy` in `infra/data.yaml`.
  `CarDetail.jsx` also refetches quietly behind the seeded first paint.
* **Static files are collected from the CI runner, before `zappa update`, under
  content-hashed names** (`S3ManifestStaticStorage`). Never run `collectstatic` through
  `zappa manage`: Zappa's package gives every file a 1980 timestamp, so inside the
  Lambda `collectstatic` judges the copy already on S3 newer and skips it -- a file is
  uploaded the first time it exists and never again, silently, with a green workflow.
  That is how the staff form's "+ Add" buttons shipped broken for a day. The order
  matters too: the new code reads the manifest from the bucket on its first cold start
  and renders the hashed names it lists, so those objects must already exist. Hashed
  copies are never deleted, which is what keeps a warm old container working.
* **DynamoDB reserved keywords** include `capacity`, `status`, `order`, `year` and
  `name`, all of which appear in this schema. PynamoDB aliases them automatically; raw
  boto3 does not.
* **Cognito's `Schema` is effectively immutable.** CloudFormation fails rather than
  replaces on most edits and there is no API to remove a custom attribute, so a wrong
  attribute list means rebuilding the pool and losing every user.
* **An access token's `username` claim is the sub, not the email**, because the pool uses
  email as the username attribute. Deriving an address from it would look right and be a
  UUID.
* **`dakkamotors-core` holds both S3 buckets.** It also held the VPC and Aurora until
  September 2026, and removing those was a stack **update**, never `delete-stack` -- that
  would have taken the site and every photo with it. The same applies to anything removed
  from it in future. `infra/network-db.yaml` keeps its filename for a related reason:
  CloudFormation identifies a stack by name, and a renamed file invites somebody to
  create a second one.
