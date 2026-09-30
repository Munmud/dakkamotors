/**
 * The Meta pixel, from the app's side.
 *
 * The app never learns the pixel id. Django injects Meta's base snippet into the
 * server-rendered shell when `META_PIXEL_ID` is set (see `cars/pages.py`), which is
 * what defines `window.fbq`; everything here calls that function when it exists and
 * does nothing when it does not. So local development, the test suite and any deploy
 * without an id carry no tracking at all, with no flag to remember to turn off.
 *
 * Why these three events and not more: the campaign is one video for one car on about
 * ¥1,000, and the point of measuring is to answer "did this car's ad produce a lead".
 * Anything finer would be noise at that volume.
 *
 * Note that the ad set is NOT optimised for these conversions. Meta needs roughly 50
 * a week to leave the learning phase and this will produce single digits, so the
 * campaign optimises for landing page views and these events exist to report, not to
 * bid. docs/ADS.md says the same thing where the person buying the ad will read it.
 *
 * In development every event below fires twice, because StrictMode runs effects twice.
 * The built bundle does not, which was checked rather than assumed. Do not "fix" it by
 * deduplicating on the car -- that would swallow a real second look at the same car,
 * which is a genuine event and not a rare one.
 */

/** Meta's own name for a car in their catalogue vocabulary. */
const PRODUCT = "product";

function track(event, params) {
  // Never let analytics break a page. A blocked or failed pixel script is the normal
  // case for a large share of visitors, not an exception worth handling loudly.
  try {
    if (typeof window === "undefined" || typeof window.fbq !== "function") return;
    window.fbq("track", event, params);
  } catch {
    /* ignore */
  }
}

/**
 * A client-side navigation.
 *
 * The base snippet fires PageView once, for the document the browser loaded. Every
 * route change after that is invisible to Meta unless it is reported, and in a single
 * page app that is most of the visit.
 */
export function pageView() {
  track("PageView");
}

/** Somebody is looking at one specific car. The slug is the car's id everywhere. */
export function viewedCar(slug) {
  if (!slug) return;
  track("ViewContent", { content_ids: [slug], content_type: PRODUCT });
}

/**
 * Somebody left their number. The only conversion this site has.
 *
 * `Lead` is Meta's standard event for exactly this, which matters: a standard event is
 * one Ads Manager can report on and optimise for without being taught what it means,
 * where a custom event is a string only we understand.
 *
 * Fired once the **server accepted** it, never on submit. A refused form is not a
 * lead, and counting it would overstate the one number the decision to keep paying
 * for an advertisement rests on.
 */
export function sentLead(slug) {
  track("Lead", slug ? { content_ids: [slug], content_type: PRODUCT } : undefined);
}
