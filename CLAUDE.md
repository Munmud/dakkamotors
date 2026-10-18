# Dakka Motors

A used-car dealership site for a shop in Hamura, Japan. Django + DRF on AWS Lambda,
React SPA, bilingual (Japanese by default, English a click away).

**The site has one job: turn a stranger into a phone number.** A visitor browses the
lot, opens a car and leaves a name and a number; the shop rings them back. That is the
whole funnel and everything else was removed to make it the only thing on the screen.

It was not always this. The site had customer accounts, emailed sign-up codes, a
question-and-answer thread per car, notifications, and test-drive booking against a
calendar of materialised slots. A ¥1,000 Meta promotion produced ten bookings, every
one of them cancelled and every one either the owner's own address or a throwaway.
The premise was that a cold click would create an account, confirm it by email and
then commit to standing in a car park at 15:00 on Saturday. It will not. All of it is
gone -- see "What was removed, and why it is not coming back".

Everything runs in `ap-northeast-1` except the three things AWS forces into `us-east-1`
(CloudFront, its ACM certificate, and Budgets).

## Layout

```
backend/         Django 5.2 + DRF, deployed to Lambda by Zappa
  cars/          the single app -- inventory, photos, videos, leads
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

The Aurora migration commands are gone with the data they moved: `export_aurora`,
`import_dynamo` and `reconcile_counters` all existed for one cutover in September 2026,
which has run. `docs/INFRA.md` records what they did. The exporter was only ever
runnable from the `pre-dynamo` commit and the counters they reconciled were booking
counters.

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

`backend/cars/store/` is the data layer: PynamoDB 6.1, one table, single-table design
with a discriminator. Five item types are left -- `Car`, `CarImage`, `CarVideo`, the
two guards, the legacy id pointer, and `CarRequest`.

**Cognito holds staff and nobody else.** There is no customer account, no sign-up, no
password reset and no verification code, so DynamoDB holds no identity at all: the
pending registrations, reset tokens and carried-over Django password hashes went with
the flow that needed them. The pool is deliberately unchanged -- it is `Retain`, its
`Schema` is effectively immutable, and the people in it are the shop's staff.

**The pool sends no email at all.** It has no `EmailConfiguration`, which is why
`--message-action SUPPRESS` below is required rather than tidy. Do not wire it to SES;
that is where an earlier attempt stalled. The one message this site sends -- the staff
alert when somebody leaves their number -- goes through Brevo.

**The pool has no Lambda triggers.** `infra/data.yaml` used to carry two, inline:
`UserMigrationFunction`, which verified a password carried over from Django on a
customer's first sign-in, and `LinkAuthFunction`, which ran a one-challenge custom auth
flow so a sign-up code could open a session as well as confirm an address. Both were
customer machinery and both are deleted, along with `ALLOW_CUSTOM_AUTH`, the customer
app client and its output. Staff sign in at the hosted UI against a password Cognito
itself holds.

**The staff masthead counts what is waiting** -- open leads -- through the
`cars.staff.context.attention` context processor. It was three counts; questions and
test drives went with their features. It is one Query per staff page, uncached because
a count that lags lies, and it is the same definition the leads page itself uses.

Ids are strings, so URL patterns take `<str:pk>`.

### Rules

* **Never build a `pk`/`sk` inline.** Every key comes from `store/keys.py`.
* **Never index `cancellation_reasons` by hand.** PynamoDB regroups transaction items by
  operation type (ConditionCheck, Delete, Put, Update) regardless of call order, so the
  list is parallel to *that*, not to the order you added them. Use the labels
  `store/txn.py` provides. Getting this wrong misattributes a failure and reports the
  wrong one.
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

### The lead, which is the whole site

`cars/requests.py` is the domain module, `store/requests.py` the store, `POST
/api/requests/` the only write the public can make. `AllowAny`, throttled at 20/hour by
`LeadThrottle` -- the `"auth"` scope it inherited is what stops a form with no sign-in
becoming a free mailbox.

**Name and phone are required; email is optional and a message is optional.** Three
fields, two of them mandatory. That is not minimalism for its own sake: forms of three
fields or fewer convert at roughly 25% against 10% for longer ones, and the shop rings
people back, so an address it will not use has no business being a barrier. The email
is format-checked only when one was given.

**One system, with the car optional.** A lead naming a car and a lead asking the shop
to find one are the same record; `car_id`, `car_slug` and `car_label` are set when
there is a car and absent when there is not. They are *denormalised* rather than
referenced, because a lead outlives the car it is about -- a sold car is deleted and
takes its photos with it, and staff reading the queue a month later still need to know
what the person asked about.

**Nothing a buyer sees carries dealer shorthand.** The trim grade -- `FX`, `X`, `S`,
`G` -- used to render as a bare letter under each car's name on the lot and on the car
page. It means something to a dealer and nothing to the person this site is for. It is
still on the `Car`, still editable, and still in the spec table where it has a label
beside it; it is simply never shown on its own. The same test applies to anything else
added to a card or a heading: if it needs explaining, it needs a label, and if it
cannot have one it does not go there.

When in doubt on the public pages, take the smaller option. The owner has cut fields
from this form twice.

**The person gets no automated reply.** Saying "we will call you" and then emailing
them is a promise about a channel the shop is not using. One message is sent, to staff,
and it leads with the phone number because the difference between ringing back in five
minutes and ringing back tomorrow is most of the difference between a sale and no sale.

Staff see leads at `/api/staff/requests/` and mark them resolved; the detail page keeps
its "Copy for a post" block with the wish and nothing personal, because the owner
reuses those for advertising.

### Advertising

The shop makes a video for one car, spends about **¥1,000** showing it around Hamura,
and stops the moment somebody asks about that car. `docs/ADS.md` is the runbook.

`META_PIXEL_ID` is not a secret and lives in `zappa_settings.json`; `pages._pixel`
injects Meta's snippet **only when it is set**, so tests, local development and the
screenshot passes carry no tracking and `window.fbq` never exists -- the same bargain
`CLOUDFRONT_DISTRIBUTION_ID` makes for `cdn.invalidate`. The id is checked to be digits
first: it lands in a `<script>` block on a public page cached at the edge. The app never
learns it; `frontend/src/lib/pixel.js` calls `window.fbq` when it is there and does
nothing when it is not. Three events -- `PageView` per client-side navigation (the
snippet fires only the first), `ViewContent` on a car page, and **`Lead`** once the
**server accepted** the contact form. `Lead` is Meta's own standard event for exactly
this, which matters: a custom event would be a column in a report, a standard one is
something the platform understands.

**The snippet loads after first paint**, on `load` or `requestIdleCallback`, rather
than in `<head>`. `fbevents.js` and its config are **773 KB** -- measured, on the live
site, where they and Google Fonts were 74% of the page. Nothing on this site depends
on the pixel having loaded, so nothing should wait for it.

**The ad set is deliberately not optimised for those conversions.** Meta wants roughly
fifty a week to leave the learning phase and ¥1,000 buys single digits, so the campaign
buys landing page views and the events measure rather than bid. Written down in two
places because it is exactly the decision somebody reverses a year later.

`Car.ad_set_id` is typed into the staff form; `advertising.stop_for` pauses it from
`requests.submit`, **fire-and-forget beside `mail.notify_staff_of_request`** -- a lead
must never fail over an advertisement, which would lose the thing the advertisement was
bought to produce. The trigger used to be a booking and moved when booking went; it is
the same moment by another name, the point at which the advertisement has produced what
it was bought to produce. `store.cars.claim_ad_pause` is a **conditional** update on
`ad_paused_at`, so two leads in the same second cannot both pause and both email; and
`ad_paused_at` rides with `ad_set_id` in `store.cars.update`, because a stamp left over
from the last campaign would make the next one unable to ever stop itself. It rides on
the id **changing**, so re-running the *same* ad set takes two saves -- clear, save,
retype, save -- and the staff form says so, and names the date it stopped, because that
state is otherwise invisible and unguessable. When Meta refuses, the owner gets the
**opposite** message -- the ad set is still spending and only a person can stop it now.

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
their photos and videos, leads, and staff accounts. It was eight sections; bookings,
slots, schedules, questions and customers went with their features.
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

**The masthead offers only what the viewer may open.** `context.NAV_ACTIONS` maps each
menu item to the action its view requires, and `context.can` resolves it per request --
one table, so a link and the `@requires` behind it cannot disagree, and a test follows
every offered link to prove it. Same rule `views_auth.LANDING` uses to decide where
`/api/staff/` lands. It is not the control: every view keeps its guard. It exists
because the menu used to show all eight to everybody, including a `staff`-only account
that may open **none** of them -- `GROUP_ACTIONS` gives that group nothing, and Roles is
optional on the add form.

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

**Nobody registers as staff, and nobody registers at all.** Self sign-up is gone with
customer accounts, so the only way into this pool is an owner using the page below or
the CLI. Two independent things still guard it: the staff pages accept only tokens
minted by the *staff* app client, which has no password auth flow at all, and group
membership is admin-only. Being *in* the pool is not a permission -- `admin-create-user`
leaves somebody in no group, and a test asserts that such an account can sign nothing.

An owner adds people at `/api/staff/accounts/add/`. **The pool sends no email**, so the
page shows the temporary password once, in the response to the request that created it --
pass it on there and then. Cognito forces a change at first sign-in.

**An address already in the pool cannot be created, and is promoted instead.** Email
is the username, so a colleague who has an account for any reason is not addable --
`create_staff` gets `UsernameExistsException`. This was written when the public site
had sign-up and the pool was full of customers; it stays because the situation it
handles is now *any* duplicate, including the common one of an owner who reissued an
account and forgot. The page now recognises that and offers `grant_staff`, on a
second submit, with no password issued: they sign in with the one they already have, and
they keep the same `sub`, so anything recorded against them stays attached. It is no
escalation -- `staff.add` is `OWNER_ONLY` and the form still offers only
`ASSIGNABLE_GROUPS` -- but it *is* handing somebody the staff pages, which is why it is
confirmed rather than silent. A duplicate who is already staff is still refused, with a
link to their page.

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

`tests_fake_cognito.py`. The staff cookie flow resolves `cars.authentication.verify` at
call time, so patching that one name covers DRF and the staff pages, and **nothing in
production branches on being tested**. Use `sign_in(self.client, user)`.

`sign_in` keeps a `staff=` keyword that now defaults to true, because there is one
cookie left and every caller means the same thing by signing in. `staff=False` survives
for the one place the distinction is real: minting a token from a *different* app
client and proving `StaffCookieAuthentication` refuses it.

moto is used only where the token itself is the subject -- `tests_cognito.py` and
`tests_staff_auth.py` mint and verify real RS256 signatures. It is deliberately not used
for the rest: moto is an optional dependency, so those tests would be `skipUnless`-gated
and would vanish on any machine without `requirements-dev.txt`.

Every class is a `SimpleTestCase`. That is load-bearing rather than tidiness: with no
database configured, an ORM call that survived the teardown fails instead of quietly
opening SQLite. `tests_store.count_dynamo_calls` replaces `assertNumQueries` and is worth
more than it was -- the car detail page is deliberately one Query, and a per-row read
in a loop is exactly what would undo that.

## What was removed, and why it is not coming back

Deleted in one change in September 2026, along with every test, template, route,
stylesheet, store module, model and translation key that served them.

| Gone | Why |
|---|---|
| Test-drive booking, slots, schedules, availability | Asking a cold click to commit to a time and place in a car park. Ten bookings ever, all cancelled, all tests. |
| Customer accounts, sign-up, email confirmation codes, password reset, the `UserMigration` and `LinkAuth` triggers | A stranger will not create an account to enquire about a used car. The account existed only to hang a booking off. |
| Per-car questions and answers, and the staff queue for them | Nobody asked one. A person with a question telephones. |
| Notifications and the bell | They notified you about bookings and answers. |
| The customers list | Nothing writes a customer any more. |
| Google Fonts | 439 KB of CJK webfont, larger than the entire application, for type nobody remarked on. System stack instead. |

**Before concluding the design was the problem, note what was never established.**
CloudFront access logging is **off**, so the site has no record of whether anybody ever
arrived; the only evidence is in Meta's Events Manager. And on 2026-09-23 the new ad
account had **no payment method**, so the promotion may never have delivered at all. Two
things are true regardless, which is why this was worth doing either way: the promotion
ran while the booking page demanded an account, a password and an emailed code before it
would show a calendar, and "turn up in Hamura at 15:00 next Saturday" is a far larger
ask of a cold click than "here is my number".

Enabling that logging is still open, so the next campaign has ground truth that does not
depend on Meta. See `docs/TODO.md`.

**What the removal was worth, measured.** Car page, phone viewport, before and after:

| | before | after |
|---|---|---|
| Page weight | 2.03 MB, 28 requests | ~390 KB compressed, 13 requests |
| Third-party share | 74% of bytes | none on the critical path |
| CLS | 0.226 | **0.0243** |
| LCP | — | ~900 ms |

The after figures come from `tools/` against the real server-rendered document. Measure
that, not `vite preview`: preview serves the SPA's own `index.html` with an empty
`#root`, so the app appearing in it registers as a 0.42 layout shift that production
does not have. This is the sort of thing that gets "fixed" for a week before somebody
notices the measurement was wrong.

## The phone is the design, and it was measured

The car page is the landing page an advertisement buys, and on a phone it had
**nothing sellable above the fold**. Measured at 390x664 with the gallery first: the
car's name sat at 768px, the price at 812, the first input at 1013 and the submit
button at **1170**. Somebody who tapped a video thirty seconds ago got a masthead, a
back link and a photograph, and scrolled most of two screens to learn what the car
was called.

What fixed it, in order of how much each was worth:

* **`.detail` puts `.summary` before `.gallery` below 900px**, with `order`. Source
  order stays gallery-first, because that is the order the document should read in
  and what the desktop grid wants. A price makes somebody scroll to a picture; a
  picture does not make anybody scroll to a price.
* **The masthead is one row on a phone**, 63px instead of 170. "ホーム" is hidden
  below 767px -- the monogram goes home and a car page has a back link -- and
  `.callbtn--compact`, which `CallButton` had been asking for since it was written,
  finally has a rule.
* **The thumbnails scroll sideways** instead of wrapping to three rows: 69px instead
  of 207. Sideways scroll is normally the thing to avoid and `table.stack` exists in
  the staff sheet to prevent it. It is right here because nothing is hidden by it --
  every thumbnail duplicates a photograph the main frame already reaches.
* **`.detail > * { min-width: 0 }`**, without which none of the above works. A grid
  item's default `min-width` is `auto`, `overflow-x` on a descendant does not change
  that, and the un-wrapped thumbnail row measured the gallery column at 766px inside
  a 390px phone -- a horizontal scrollbar on every page. **Check for page overflow at
  320, 360 and 390 after any layout change here**; it has arrived by two different
  routes now.

Result: name at 222px, price 266, button 583. **Do not put anything above
`.summary` on that page** without measuring what it costs.

**The submit button is never disabled.** It was grey until both fields were filled,
and on a dark theme a disabled button is the same family as the panel behind it -- so
the one obviously-pressable thing on the page appeared only *after* the visitor had
done all the work, and a disabled control never says why it is disabled. It validates
on submit instead, sets the error and focuses the field that is missing. `busy` is the
only thing that disables it, because that is a real reason.

**The photographs open.** `Gallery` renders the main frame as a button onto a
lightbox: Escape closes, arrows move, focus starts on the close button and returns to
the opener, and the page behind is scroll-locked. The frame is 358px on a phone and
crops with `object-fit: cover`, so before this there was no way to see a photograph
whole or large -- on a page where the photographs are the product and the 1600px
derivative already existed.

## Two classes of bug that do not fail anywhere

Both were found by looking at the site, not by the suite, and both will recur.

**A class the JSX uses and no stylesheet defines.** `.pricebox__flag` rendered 商談中
as default body text under a full-brightness yellow price, while the listing card
greyed its plate -- so the lot said "spoken for" and the car's own page said "this is
the live price". `.callbtn--compact` had never had a rule at all. Nothing errors; the
element simply arrives unstyled and looks deliberate.

**A page that renders in the wrong language and looks perfect.** `?lang=en` was
honoured by `pages.py`, published in `hreflang`, and then overridden by React on
mount, so the English site existed for about 200ms. It survived a screenshot pass:
fourteen "English" screenshots were byte-identical to their Japanese twins and every
one of them looked like a good screenshot. `initialLanguage()` now reads `?lang`
first, and the screenshot step compares hashes and says so.

## Looking at the site

`tools/` holds three scripts and its own README: `seed_lot.py` writes four cars covering
every branch the templates have, `screenshot.py` photographs every page at 390px and
1440px in both languages, and `contrast.py` checks WCAG AA over every rendered text
node. `docs/screenshots/` is the committed output.

None of it runs in CI. It needs a browser, a seeded table and two servers, and what it
produces is something a person reads. It is here because the two things it found could
not have been found by the suite: a contrast ratio and a line break are not assertions
anybody thinks to write, and both were invisible. The price was white on the yellow
plate, and every Japanese car name on the desktop lot broke mid-word.

**`pages.asset_tags` falls back to `frontend/dist/index.html` off Lambda**, and
`config/urls.py` serves `/assets/*` from the same place under `DEBUG`. Without those
two, a Django running on a laptop serves every page unstyled with no application on it
-- `_fetch_shell` reaches S3 and only S3, the failure is swallowed, and nothing looks
broken from the outside. The consequence was that the one document production actually
serves could not be opened locally at all.

## Conventions

**Rules live in domain modules, not views.** `requests.py` and `advertising.py` hold
every check, so the same rule applies from the API, a management command, curl or a
test. Views are thin and translate errors into responses. The store raises typed errors
and knows no wording; the domain module owns every sentence a customer reads.

**Comments explain why, not what.** The codebase is deliberately heavy on rationale --
why a slug never changes, why `bump_car` exists, why the pool sends no email. Match
that. A comment that restates the code is noise; one that records a decision is the most
valuable thing in the file.

**Tests assert behaviour and wording.** Customer-facing strings are asserted verbatim in
`cars/tests.py`. If a change makes you edit one of those strings, suspect the change.

**Nothing runs on a timer**, and now there is nothing that could want to. Slot
materialisation was the last scheduled-looking thing and it went with booking. No cron,
no EventBridge rule, no periodic Lambda: every write happens because somebody asked for
it, and the two expiries that remain are TTL attributes the table handles itself.

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
lead must never fail -- or wait 600ms -- because of an email. That is the whole bargain:
the one thing the site exists to capture is a phone number, and it is captured before
anything is told about it.

**The site sends one email, and it goes to staff.** `mail.notify_staff_of_request`,
plus `notify_staff_of_paused_ad` when an advertisement stops. Nothing is sent to the
person who filled the form: they asked for a telephone call and they get one.

`CarRequest` still carries a `language`, snapshotted when the record is made, because
the staff alert says which language the person wrote in -- whoever rings them back
needs to know that before they dial.

Four customer-facing emails went with booking. `mail._when` went with them, and is
worth remembering: it used Django's `date_format` rather than `strftime` because its
Japanese line was `%Y年%-m月%-d日` for a year and had never once run -- `%-m` is
glibc-only and Windows raises on 年 outright. Any date formatting added here should
use `date_format` for the same reason.

**Everybody on the public site is a guest**, so nothing has to decide whether they
are. `identity.py`, its `guest:` prefix and `mail._can_sign_in` are all gone -- they
existed to stop three booking emails pointing a guest at `/account`, a sign-in form
with nothing behind it for them. There is no `/account` and no customer sign-in, which
is the same fix arrived at by deleting rather than by branching.

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

**Japanese is the default, and two places have to agree on it.**
`i18n/index.js` `initialLanguage()` and `pages._language_from` both return `"ja"` when
nobody has asked for anything else. They must move together: if the server shell picks
one language and the app picks the other, a reader gets a Japanese page that turns
English the moment React mounts, which is worse than either. English is `?lang=en` and
the switcher, and the saved preference still wins over both.

It was English for about an hour during the restructure. Everybody who walks into this
shop is in Hamura.

**The site is dark, and the tokens are the whole theme.** `00-tokens.css` carries a
surface scale -- `--bg`, `--surface`, `--surface-2`, `--line`, `--text`, `--text-dim` --
and no partial should contain a colour literal.

Inverting a theme is not a palette swap, and the two ways it goes wrong are both silent.
A token whose *definition* is deleted leaves `var(--gone)` calls behind, and an
undefined custom property invalidates the whole declaration, so the element quietly
inherits instead: twelve elements meant to be dim rendered at full brightness. A token
whose definition *survives* is worse, because it keeps working and stops being right --
`--ink-soft` was legible on white and became a 2.03:1 back link. Neither fails anywhere.
`tools/contrast.py` is the only thing that catches either.

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
two things: this is the price, and this is the one thing you came here to know.

**On the dark theme it says only the first of those.** It used to carry the primary
button as well, and on a dark ground it cannot: the yellow reads as *a number plate*,
so a yellow button beside a yellow price is two things shouting the same word. The one
action is `--go` green, which already means "in stock" in the other place it appears.
Selection is ink reversed out and never yellow, a rule inherited from the booking flow,
which once had a yellow selected day, a yellow selected slot and a yellow button on one
short screen, and none of them led.

**`--plate` carries `--ink`, never `--text`.** This is not a preference. `--text`
inverted with the theme -- it was near-black and is now near-white -- and four rules
that paired it with a light fill inverted with it and said nothing. The price rendered
white on yellow at **1.51:1**, on the one element the whole page exists for. Anything
painted on `--plate`, `--go` or `--flag` takes `--ink`, and `tools/contrast.py` is what
notices when it does not.

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
* **Changing `DERIVATIVE_WIDTHS` is not a code change on its own.** Every photo in S3
  keeps the widths it was built with, so the site goes on serving the old set while
  the code advertises the new one, and nothing anywhere fails. It sat like that in
  production from the restructure until October, when adding a car surfaced it:
  72 photos on 320/800/1600 against a constant that said 320/640/1000/1600.
  `manage.py rebuild_derivatives --all` is what makes them agree, and it needs a CDN
  invalidation after it, because neither the importer nor the rebuild calls
  `cdn.invalidate` -- only the staff form does.

  **And pick the widths by working out which file a browser chooses**, not by
  spacing numbers evenly. The rule is the smallest candidate at least as wide as
  (CSS px x DPR). 320/640/1000/1600 was chosen to close "the gap in the middle" and
  did the opposite: a 390pt phone at 2x went from 800 to 1000, and the 3x case it
  was aimed at still took the 1600, because 1000 is below the 1170 it asks for. The
  set is now 320/800/1200/1600 -- the 3x phone takes the 1200, measured at 34% fewer
  bytes per photo, and 800 stays so the copies already in S3 are not orphaned.

* **Japanese breaks between almost any two characters**, so a katakana model name
  longer than its box splits inside the word -- `トヨタ ヴェルファ` / `イア`. Titles
  carry `word-break: keep-all` with `overflow-wrap: break-word` behind it, which
  moves the break to the space between maker and model and still cannot overflow.
  Check any new text that holds a Japanese proper noun in a narrow box.

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
