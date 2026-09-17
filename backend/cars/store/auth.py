"""The auth state Cognito has nowhere to put.

Cognito owns identity: the user, the password, whether the address is confirmed. What it
does not own is the shape of *our* signup and reset flows, which exist because Cognito
must never send an email here -- the messages are bilingual, branded, and go through
Brevo, and none of that survives being handed to Cognito's own mailer.

So three small things live in DynamoDB instead:

* a pending sign-up, holding the name and phone Cognito has no field for, plus where to
  send the customer once the link is clicked
* a reset token, plus a per-customer epoch counter
* a carried-over Django password hash, read once by the migration trigger

All of them expire on a TTL. Under Aurora the expiry sweep had to run on the write path
because a scheduled job would have kept the database awake; that is DynamoDB's job now.
"""

import datetime as dt
import hashlib
import secrets

from . import keys
from .models import (
    LegacyPassword, PendingRegistration, PendingToken, ResetEpoch, ResetToken,
)
from .txn import Txn

#: How long a sign-up link is good for. Matches the old PendingRegistration window.
PENDING_DAYS = 3

#: Matches the old PASSWORD_RESET_TIMEOUT of 24 hours.
RESET_HOURS = 24

#: Long enough for a quiet customer to come back and sign in once, short enough that a
#: carried-over hash is not kept indefinitely.
LEGACY_DAYS = 90


def new_token():
    """The raw value that goes in the email and nowhere else."""
    return secrets.token_urlsafe(32)


def hash_token(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ttl(now, **delta):
    return int((now + dt.timedelta(**delta)).timestamp())


# --------------------------------------------------------------------------------------
# Pending sign-ups
# --------------------------------------------------------------------------------------

def start_registration(*, email, name, phone, next_path="", language="en", now):
    """Record a sign-up waiting on its link. Returns the raw token to email.

    Overwrites any previous attempt for the same address, which is both how "resend"
    works and why a typo on the first try does not lock an address out for three days.
    """
    email = email.strip().lower()
    raw = new_token()
    token_hash = hash_token(raw)
    ttl = _ttl(now, days=PENDING_DAYS)

    previous = find_registration(email)

    tx = Txn()
    tx.save("pending", PendingRegistration(
        pk=keys.pending_pk(email), sk=keys.META, email=email, name=name.strip(),
        phone=phone.strip(), next_path=next_path or "", language=language,
        token_hash=token_hash, created_at=now, ttl=ttl))
    tx.save("token", PendingToken(
        pk=keys.pending_token_pk(token_hash), sk=keys.META, email=email, ttl=ttl))
    if previous is not None and previous.token_hash != token_hash:
        # The old link stops working the moment a new one is sent. Otherwise a resend
        # would leave two live activation links for one address.
        tx.delete("old_token",
                  PendingToken(pk=keys.pending_token_pk(previous.token_hash),
                               sk=keys.META))
    tx.commit()
    return raw


def find_registration(email):
    try:
        return PendingRegistration.get(keys.pending_pk(email.strip().lower()),
                                       keys.META)
    except PendingRegistration.DoesNotExist:
        return None


def registration_for_token(raw):
    """The pending sign-up a link belongs to, or None.

    Expiry needs no check of its own: the TTL removes both items, so an expired link
    simply does not resolve. That is one fewer branch than the `has_expired` property it
    replaces -- though DynamoDB's TTL sweep is best-effort within ~48 hours, so a
    belt-and-braces check on `created_at` is still worth having for anything security
    sensitive. A sign-up link is not: worst case somebody confirms an address they
    proved three days and a bit ago.
    """
    try:
        token = PendingToken.get(keys.pending_token_pk(hash_token(raw)), keys.META)
    except PendingToken.DoesNotExist:
        return None
    return find_registration(token.email)


def finish_registration(pending):
    """Clear both items once the account exists."""
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
