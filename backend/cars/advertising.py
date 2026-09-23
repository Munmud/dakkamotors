"""Stop paying for a car's advertisement the moment it has done its job.

The shop advertises one car at a time on Facebook and Instagram, with a video and a
lifetime budget of about ¥1,000, and wants exactly one thing out of it: a test drive
booked for that car. The moment that happens the advertisement has nothing left to
earn, and every yen after it is wasted on a car somebody is already coming to see.

Nobody is watching at nine in the evening, so the site does it. A booking that names a
car with an ad set id pauses that ad set and tells the owner.

Three things this module is careful about, in the order they bite:

* **It can never cost a booking.** `stop_for` swallows everything. A customer's
  booking failing because Meta was slow, or because a token expired, would be the
  worst possible trade: it would lose the very thing the advertisement was bought to
  produce. Same bargain as `cdn.invalidate` and `mail.queue_email`.
* **It happens once.** The claim is a conditional write in the store, not a read
  followed by a write here, so two bookings landing in the same second cannot both
  pause the ad set and both email the owner about it.
* **It is inert without a token.** `META_ADS_TOKEN` is blank everywhere but
  production, so this does nothing locally, under test, or on a deploy made before
  the owner created the system user.

The rules live here rather than in the view, like every other rule in this codebase:
the same thing must happen whether the booking came from the API, from a staff page or
from a test.
"""

import logging

from django.utils import timezone

from . import mail
from . import meta_ads
from .store import cars as car_store

logger = logging.getLogger(__name__)


def stop_for(booking, *, now=None):
    """A test drive was booked. Pause that car's advertisement, if it has one.

    Returns True only when this call is what stopped it -- useful to a test, and to
    anything that later wants to report on it. False covers every other case: no car,
    no ad set, somebody else got the claim, or Meta could not be reached.
    """
    try:
        return _stop_for(booking, now=now or timezone.now())
    except Exception:  # noqa: BLE001 - a booking must never fail because of this
        logger.warning("Could not stop the advertisement for booking %s",
                       getattr(booking, "booking_id", "?"), exc_info=True)
        return False


def _stop_for(booking, *, now):
    if not booking.car_id:
        return False

    # The claim comes first, before Meta is called. The other order -- pause, then
    # record it -- looks safer and is not: two callers would both pause (harmless) and
    # both email the owner (not harmless, and the second message says an advertisement
    # was stopped that this booking did not stop).
    car = car_store.claim_ad_pause(booking.car_id, now)
    if car is None:
        return False

    try:
        paused = meta_ads.pause_ad_set(car.ad_set_id)
    except meta_ads.MetaError:
        # The claim stays taken, and the owner is told anyway -- with the opposite
        # message. Releasing the claim to retry on the next booking would be worse: it
        # would mean two messages about the same car, and the ad set is still spending
        # in the meantime either way. What the owner needs is one message saying so,
        # now, with the ad set named so they can pause it by hand. The log carries
        # what Meta actually said.
        logger.warning("Meta would not pause ad set %s for car %s",
                       car.ad_set_id, car.car_id, exc_info=True)
        paused = False

    # `paused` is also False when no token is configured, which is every environment
    # but production -- and then the message is right: the ad set was not paused.
    mail.notify_staff_of_paused_ad(car, booking, paused=paused)
    return True
