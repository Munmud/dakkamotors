# Screenshots

Every public page at 390px and 1440px, Japanese and English, dark. Captured from
`tools/screenshot.py` against a local server seeded by `tools/seed_lot.py`, at 2x and
halved — so they are the size they are read at, and the set is 5MB rather than 27MB.

The cars are fixtures, not stock. The photographs are the shop's own.

| | |
|---|---|
| `*-home` | the lot |
| `*-car` | a car with a full spec sheet and eight photographs |
| `*-car-reserved` | 商談中, the state that greys the plate |
| `*-car-no-price` | "price on application", which is a different plate |
| `*-request` | the find-me-a-car form |
| `*-privacy` | the pixel disclosure Meta's ad review looks for |
| `*-car-form-filled` | the form with a name and a number in it |
| `*-car-form-sent` | what replaces the form once the server has the lead |

`-en` is the same page at `?lang=en`. Without it the page is Japanese, which is what a
visitor gets: everybody who walks into this shop is in Hamura.

**Check the `-en` files actually differ from their twins before committing a new set.**
The client ignored `?lang=` for a while, so the whole English half of this folder was
fourteen byte-identical copies of the Japanese pages, and nobody noticed because each
one looked like a perfectly good screenshot. The re-encode step now compares hashes
and says so.

The last two are the point of the set. The site has one job — get a name and a phone
number — and a page nobody has interacted with cannot show whether it does it.

Regenerate them whenever the look changes; they are documentation of a decision, and a
stale one argues for a design that is no longer there.
