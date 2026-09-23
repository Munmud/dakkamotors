"""Typed failures the store raises, for the domain layer to translate -- and the one
predicate that tells a refused condition from a real fault.

The split matters: the store knows *which condition failed*, the domain modules know
*what to tell the customer*. Keeping the wording out of here is what lets every
`BookingError` string in `booking.py` survive the migration unchanged -- and those
strings are asserted verbatim throughout the test suite.
"""


def is_conditional_failure(exc):
    """Was this PynamoDB error a refused condition, rather than a real fault?

    The one thing every guarded single-item write has to be able to ask. PynamoDB
    wraps the botocore error in a `PutError`/`UpdateError`/`DeleteError` depending on
    the verb, so the verb's class is no use; the answer is in the wrapped cause, in
    the same place for all three.

    Here rather than in each caller because two modules had begun to grow their own
    copy, and a refused condition read as a fault is the difference between "that
    seat has just gone" and a 500.
    """
    cause = getattr(exc, "cause", None)
    code = (cause.response.get("Error", {}).get("Code")
            if cause is not None and hasattr(cause, "response") else None)
    return code == "ConditionalCheckFailedException"


class StoreError(Exception):
    """Base for everything in this package."""


class NotFound(StoreError):
    """No item at that key."""


class ConditionFailed(StoreError):
    """A single-item conditional write was refused.

    Carries the offending item when the write asked for it with
    `return_values=ALL_OLD`, so the caller can explain *why* without a second read.
    """

    def __init__(self, message="", raw_item=None):
        super().__init__(message or "condition check failed")
        self.raw_item = raw_item


class TransactionFailed(StoreError):
    """A TransactWriteItems was cancelled.

    `reasons` is the raw positional list from DynamoDB. Do not index it with a number
    taken from the order you called `tx.save()` / `tx.update()` -- PynamoDB regroups
    items by operation type before sending. Use the index map that `txn.py` builds.
    """

    def __init__(self, reasons, message="transaction cancelled"):
        super().__init__(message)
        self.reasons = list(reasons)


class Retryable(StoreError):
    """A transient `TransactionConflict`; the same call may simply be retried.

    boto3 does not auto-retry `TransactionCanceledException`, so this is raised only
    after `txn.transact_write` has exhausted its own attempts.
    """


class SlotUnavailable(StoreError):
    """The slot could not take the booking. `slot` is the item as it actually stood."""

    def __init__(self, slot=None):
        super().__init__("slot unavailable")
        self.slot = slot


class SeatTaken(StoreError):
    """Capacity was full at the moment of writing."""


class AlreadyBooked(StoreError):
    """This customer already holds a live booking for this slot."""


class CarAlreadyBooked(StoreError):
    """This customer already holds a live booking for this car."""


class LimitReached(StoreError):
    """The customer is at MAX_ACTIVE_BOOKINGS."""


class NotActive(StoreError):
    """The booking is no longer pending or confirmed, so it cannot be changed."""
