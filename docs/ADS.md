# Advertising one car at a time

The shop makes a video for one car, spends about **¥1,000** showing it to people around
Hamura, and stops the moment somebody books a test drive for that car. This file is the
half of that which is not code: what to create in Meta, what to paste where, and how to
set the campaign up so ¥1,000 actually buys something.

---

## What exists

Created 2026-09-23. The login had no business portfolio and no developer account, so
most of this had to be built before a system user was even possible.

| | |
|---|---|
| Business portfolio | `Dakka Motors` |
| Pixel | `Dakka Motors` — **`2158231298411165`**, in `zappa_settings.json` |
| App | `Dakka Motors Site` — Marketing API use case, **unpublished**, contact `moontasir042@gmail.com` |
| Ad account | `Dakka Motors`, inside the portfolio. **JPY, Asia/Tokyo** |
| System user | `dakkamotors-site` — `61594633623059`, role Employee |
| Its assets | the ad account with *Manage campaigns*; the app with *Develop app* (Meta offers no token permissions without it) |
| Its token | `ads_management`, expiry **Never**, in SSM at `/dakkamotors/META_ADS_TOKEN` |

The pixel was **`1652862756270917`** until 2026-09-23. Events recorded before the swap
belong to that one and did not move: a pixel keeps its own history, and the new one
started empty. If anything in Ads Manager still points at the old id — an ad set, a
custom audience, a conversion — repoint it by hand; changing `META_PIXEL_ID` only moves
what the site sends.

The **old personal ad account `339950103411609` is not used** and is unchanged. Meta
refused to move it into the portfolio because it has never taken a payment; a fresh
account was created instead. Its currency and time zone can never be changed, and it
cannot be taken back out of the portfolio.

### Before the first campaign can run

* **The ad account has no payment method.** Nothing will deliver until one is added.
* **The app is unpublished**, which is fine for this: a system user calling the
  Marketing API against an ad account the same business owns does not need App Review.
  If the pause ever fails with a permissions error rather than a token error, that
  assumption is the thing to check first.

---

## What the site does on its own

| When | What happens |
|---|---|
| Any page loads | The Meta pixel fires `PageView` |
| A car page opens | `ViewContent`, carrying that car's slug as `content_ids` |
| Its booking page opens | `InitiateCheckout` |
| A booking is accepted | `Schedule` — and the car's ad set is **paused** |

The pause emails the owner. If Meta refuses the pause, it emails too, and says the
opposite thing: *Pause this ad by hand* — because the ad set is still spending and only
a person can stop it. See `backend/cars/advertising.py`.

---

## One-time setup

Already done — see *What exists* above. Kept because it is how the next pixel, the
next token, or a rebuild from nothing would be made.

### 1. The pixel

Events Manager → **Connect data sources** → Web → Meta Pixel. Name it `Dakka Motors`.
Copy the **Pixel ID** — a long number. Do not install any code it offers; the site
already has the snippet and only needs the number.

Paste it into `backend/zappa_settings.json`:

```json
"META_PIXEL_ID": "1234567890123456"
```

and deploy. It is not a secret — it is in the page source of every site that runs a
pixel — which is why it lives in git where it can be reviewed, rather than in SSM.

Blank means no snippet at all, so local development and the test suite send Meta
nothing. Check it landed by opening the site and looking for `connect.facebook.net` in
View Source.

### 2. The system user, so the ad can pause itself

Business Settings → **Users → System users** → Add. Name it `dakkamotors-site`, role
**Employee**.

Then, still on that system user:

1. **Add assets** → Ad accounts → the ad account → toggle **Manage campaigns**.
   This is the `ads_management` permission. Without it the pause fails with a
   permissions error, and the site emails "pause this by hand" every time.
2. **Generate new token** → pick the app → tick **`ads_management`** → Generate.
   Copy it now; Meta shows it once.
3. Set the token's expiry to **Never**. A 60-day token means the pause silently stops
   working two months later, on a Tuesday, with no error anybody sees.

Put it in SSM — never in the repo, never in `zappa_settings.json`:

```bash
aws ssm put-parameter --region ap-northeast-1 \
  --name /dakkamotors/META_ADS_TOKEN --type SecureString \
  --value '<the token>' --overwrite
```

`config/ssm.py` loads everything under `/dakkamotors/` at settings import, so the next
deploy picks it up with no other change. Blank means `meta_ads.pause_ad_set` is a no-op.

### Rotating the token

Do this whenever the token has been somewhere it should not have been -- pasted into a
chat, a ticket, an email -- and not only when it stops working. It is two minutes and
it invalidates the copy that got out.

1. Business Settings → Users → System users → `dakkamotors-site` → the token →
   **Revoke**. The old one stops working immediately.
2. Generate a new one, same app, same `ads_management`, expiry Never.
3. Run the `put-parameter` above with `--overwrite`.
4. **Redeploy.** `config/ssm.py` reads SSM at settings import, so a warm Lambda keeps
   using the revoked token until it is replaced -- and a revoked token means every
   booking sends a *Pause this ad by hand* email until then.

---

## Running an ad for one car

### In Meta

**Campaign objective: Traffic.** Not Sales, not Leads.

This is the single most important choice here and it is counter-intuitive, so the
reason is worth keeping: an ad set optimised for conversions needs roughly **50
conversions a week** to leave Meta's learning phase. ¥1,000 will produce single digits.
Below that threshold Meta keeps under-delivering while it waits for data that never
arrives — the budget goes unspent and the video is barely shown. Optimising for landing
page views asks Meta for something it can actually find fifty of, and the pixel still
records every `ViewContent` and `Schedule` for you to read afterwards.

| Setting | Value |
|---|---|
| Objective | **Traffic** |
| Optimisation | **Landing page views** (not link clicks — it waits for the page to load) |
| Budget | **Lifetime ¥1,000**, over 3–5 days |
| Location | Hamura + 10 km — which covers Fussa, Akiruno, Mizuho, Musashimurayama and Ome |
| Age | 20+ (a kei car buyer needs a licence) |
| Detailed targeting | **Leave it empty.** ¥1,000 over one town is already a small audience; narrowing it further just raises the price per view |
| Placements | Automatic |
| Destination | `https://dakkamotors.com/cars/<slug>` — the car's own page |
| Creative | The video, 4:5 or 9:16, first three seconds showing the car |

Send the ad to the **car's page**, not the booking page. The video sells the car and the
page confirms it — photos, price, specs — and it is the car page that fires
`ViewContent` for that slug. The yellow Book button is right there.

### Then, in the staff pages

Copy the **Ad set ID** from Ads Manager (the ad set, not the campaign or the ad) and
paste it into **Meta ad set ID** on the car's page, under *Listing*.

That is the whole connection. The next booking for that car pauses that ad set.

---

## Afterwards

**A booking arrives.** Two emails: the usual test drive request, and *Ad stopped*.
Nothing more is spent.

**To advertise the same car again** — it fell through, or the buyer never turned up —
what matters is that the **Meta ad set ID changes**. That is what clears the car's
record of having already stopped an ad; a record left over from the last campaign would
leave the new advertisement unable to ever stop itself. Silent, and visible only as a
bill. So:

* **A new ad set** (the normal case, and what a new campaign gets): paste its id
  straight over the old one and Save. Nothing else.
* **The same ad set**, unpaused in Ads Manager: the id is not changing, so empty the
  box, **Save**, type it back in, **Save** again. Two saves, on purpose — leaving the
  id alone is indistinguishable from not touching the car at all.

The car's page says which state it is in: a car whose ad has already stopped itself
shows the date it happened under the ad set id box.

**To move the budget to a different car**, clear the id on the old car so nothing is
left pointing at a dead ad set, and set it on the new one.

**Reading the results.** Ads Manager will show landing page views. Events Manager →
your pixel → **Overview** shows `ViewContent`, `InitiateCheckout` and `Schedule`, and
filtering `ViewContent` by `content_ids` gives the numbers for one car. The question
worth asking is the ratio: views → booking page → booking. A video that gets views and
no `InitiateCheckout` is a video problem; `InitiateCheckout` and no `Schedule` is a
calendar or price problem.

---

## When it goes wrong

| Symptom | Cause |
|---|---|
| *Pause this ad by hand* email | Meta refused. Usually the token expired or lost `ads_management`. The ad set is still spending — pause it in Ads Manager, then reissue the token |
| No email at all after a booking | No ad set id on the car, or `STAFF_ALERT_EMAIL` unset |
| No `ViewContent` in Events Manager | `META_PIXEL_ID` blank in `zappa_settings.json`, or the deploy has not gone out |
| Events arrive but the ad set never pauses | `META_ADS_TOKEN` not in SSM. The pixel and the pause are independent — one working says nothing about the other |
| Ad barely delivers, budget unspent | The objective is Conversions. See above; switch it to Traffic |

The Graph API version is a named constant, `GRAPH_VERSION` in
`backend/cars/meta_ads.py`. Meta retires versions roughly every two years, and a retired
one starts answering errors — that is the line to change.

---

## Privacy

The site has a privacy page at `/privacy`, linked from the footer of every page, in
English and Japanese. It has to be there before the pixel runs: Meta's business tools
terms require it, and it is the APPI disclosure. It says which pages Meta is told about
and that no name, address or phone number is ever sent. If what the site collects
changes, that page changes with it — `frontend/src/i18n/{en,ja}.json`, under `privacy`,
and the crawler-facing summary in `pages.privacy_page`.
