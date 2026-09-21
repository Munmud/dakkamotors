"""The auth state Cognito has nowhere to put.

Cognito owns identity: the user, the password, whether the address is confirmed. What it
does not own is the shape of *our* signup and reset flows, which exist because Cognito
must never send an email here -- the messages are bilingual, branded, and go through
Brevo, and none of that survives being handed to Cognito's own mailer.

So three small things live in DynamoDB instead:

* a pending sign-up, holding the name and phone Cognito has no field for, plus where to
  send the customer once the emailed code is typed in
* a reset token, plus a per-customer epoch counter
* a carried-over Django password hash, read once by the migration trigger

All of them expire on a TTL. Under Aurora the expiry sweep had to run on the write path
because a scheduled job would have kept the database awake; that is DynamoDB's job now.
"""

import datetime as dt
import hashlib
import hmac
import secrets

from . import keys
from .models import (
    LegacyPassword, PendingRegistration, PendingToken, ResetEpoch, ResetToken,
)
from .txn import Txn

#: How long a sign-up code is good for. Matches the old PendingRegistration window.
PENDING_DAYS = 3

#: Wrong codes before the sign-up is dead and they start again. A six-digit code is a
#: million possibilities; five guesses against it, behind a 20/hour throttle, is not a
#: search anybody can run.
MAX_ATTEMPTS = 5

#: Matches the old PASSWORD_RESET_TIMEOUT of 24 hours.
RESET_HOURS = 24

#: Long enough for a quiet customer to come back and sign in once, short enough that a
#: carried-over hash is not kept indefinitely.
LEGACY_DAYS = 90


def new_token():
    """The raw value that goes in the email and nowhere else."""
    return secrets.token_urlsafe(32)


def new_code():
    """Six digits, zero-padded, for typing off a phone into a box.

    Short enough to copy by eye, which is the whole reason it replaced the link -- a
    link opened on the phone lost the page the customer had on the laptop.
    """
    return f"{secrets.randbelow(10 ** 6):06d}"


def hash_token(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ttl(now, **delta):
    return int((now + dt.timedelta(**delta)).timestamp())


# --------------------------------------------------------------------------------------
# Pending sign-ups
# --------------------------------------------------------------------------------------

def start_registration(*, email, name, phone, next_path="", language="en", now):
    """Record a sign-up waiting on its code. Returns the raw code to email.

    Overwrites any previous attempt for the same address, which is both how "resend"
    works and why a typo on the first try does not lock an address out for three days.
    The old code dies with the overwrite: one live code per address, and a fresh count
    of tries.

    No PendingToken item any more. The link's token was unique and could key its own
    partition; a code is checked only ever against an address, and the pending item
    is already keyed by that.
    """
    email = email.strip().lower()
    raw = new_code()
    PendingRegistration(
        pk=keys.pending_pk(email), sk=keys.META, email=email, name=name.strip(),
        phone=phone.strip(), next_path=next_path or "", language=language,
        token_hash=hash_token(raw), attempts=0, created_at=now,
        ttl=_ttl(now, days=PENDING_DAYS),
    ).save()
    return raw


def find_registration(email):
    try:
        return PendingRegistration.get(keys.pending_pk(email.strip().lower()),
                                       keys.META)
    except PendingRegistration.DoesNotExist:
        return None


class CodeDead(Exception):
    """Too many wrong codes. The sign-up is gone; they register again."""


def check_code(email, raw):
    """The pending sign-up if `raw` is its code, else None -- and a wrong guess counts.

    Expiry needs no check of its own: the TTL removes the item, so an expired code
    simply does not resolve. DynamoDB's sweep is best-effort within ~48 hours, which
    for a sign-up code is fine: worst case somebody confirms an address they proved
    three days and a bit ago.

    The miss is recorded with a guarded update -- an unguarded one would upsert a stub
    with no discriminator for an address that has no pending sign-up, which is the
    trap the store rules warn about. On the last allowed miss the item is deleted, so
    the code cannot be worn down by patience.
    """
    pending = find_registration(email)
    if pending is None:
        return None
    if hmac.compare_digest(pending.token_hash or "", hash_token((raw or "").strip())):
        return pending

    if (pending.attempts or 0) + 1 >= MAX_ATTEMPTS:
        pending.delete()
        raise CodeDead(email)
    pending.update(actions=[PendingRegistration.attempts.add(1)],
                   condition=PendingRegistration.pk.exists())
    return None


def finish_registration(pending):
    """Clear the pending item once the account exists.

    Still deletes the PendingToken by the same hash: a delete of a missing item in a
    transaction succeeds, and it is what cleans up a link-era sign-up that finishes
    inside its three-day window."""
    tx = Txn()
    tx.delete("pending", PendingRegistration(pk=pending.pk, sk=keys.META))
    tx.delete("token", PendingToken(
        pk=keys.pending_token_pk(pending.token_hash), sk=keys.META))
    tx.commit()


def discard_registration(email):
    pending = find_registration(email)
    if pending is not None:
        finish_registration(pending)


# --------------------------------------------------------------------------------------
# Password resets
# --------------------------------------------------------------------------------------

def epoch_for(sub):
    try:
        return int(ResetEpoch.get(keys.reset_epoch_pk(sub), keys.META).n)
    except ResetEpoch.DoesNotExist:
        return 0


def start_reset(*, sub, email, now):
    """Mint a reset link. Returns the raw token.

    The token carries the epoch it was minted under. `PasswordResetTokenGenerator`
    derived its token from the password hash, so a completed reset killed every
    outstanding link; Cognito never hands us the hash, so the counter buys the same
    property instead.
    """
    raw = new_token()
    ResetToken(
        pk=keys.reset_token_pk(hash_token(raw)), sk=keys.META,
        sub=sub, email=email, epoch=epoch_for(sub), ttl=_ttl(now, hours=RESET_HOURS),
    ).save()
    return raw


def reset_for_token(raw):
    """The reset a link refers to, or None if it is unknown, expired or superseded."""
    try:
        token = ResetToken.get(keys.reset_token_pk(hash_token(raw)), keys.META)
    except ResetToken.DoesNotExist:
        return None
    if int(token.epoch) != epoch_for(token.sub):
        # A later reset has already happened. Every sibling link dies with it.
        return None
    return token


def complete_reset(token):
    """Burn this link and every other one outstanding for the same account."""
    tx = Txn()
    tx.delete("token", ResetToken(pk=token.pk, sk=keys.META))
    tx.update("epoch", ResetEpoch(pk=keys.reset_epoch_pk(token.sub), sk=keys.META),
              actions=[ResetEpoch.n.add(1)])
    tx.commit()


# --------------------------------------------------------------------------------------
# Carried-over passwords
# --------------------------------------------------------------------------------------

def remember_legacy_password(*, email, password_hash, name="", phone="", now):
    LegacyPassword(
        pk=keys.legacy_password_pk(email), sk=keys.META,
        email=email.strip().lower(), password_hash=password_hash,
        name=name, phone=phone, ttl=_ttl(now, days=LEGACY_DAYS),
    ).save()


def legacy_password(email):
    try:
        return LegacyPassword.get(keys.legacy_password_pk(email), keys.META)
    except LegacyPassword.DoesNotExist:
        return None


def forget_legacy_password(email):
    record = legacy_password(email)
    if record is not None:
        record.delete()
