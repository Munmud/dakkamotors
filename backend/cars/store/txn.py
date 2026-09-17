"""Transactions, and the one thing about them that is easy to get wrong.

`TransactWriteItems` reports failures as a `CancellationReasons` list that is positional
-- reason *i* belongs to item *i*. The trap is that **PynamoDB does not send items in the
order you add them.** `Connection.transact_write_items` concatenates its four buckets in
a fixed order:

    ConditionCheck, Delete, Put, Update

preserving call order only *within* each bucket. So a booking that calls
`update(slot)`, `update(customer)`, `save(seat)`, `save(booking)` goes on the wire as
`[seat, booking, slot, customer]`, and reading `reasons[0]` as "the slot" attributes the
wrong message to the customer -- "that time has just been taken" when what actually
happened is "you have already booked that time".

That is not hypothetical: it is exactly what the migration spike caught, and nothing
about it is visible at the call site. So this module never lets callers index the list.
Every operation is added with a label, and failures come back as `{label: reason}`.
"""

import logging
import random
import time

from pynamodb.connection import Connection
from pynamodb.constants import ALL_OLD
from pynamodb.exceptions import TransactWriteError
from pynamodb.transactions import TransactWrite

from django.conf import settings

from .errors import Retryable, TransactionFailed

logger = logging.getLogger(__name__)

#: The order PynamoDB concatenates its buckets in. Mirrored from
#: pynamodb.connection.base.Connection.transact_write_items. A test asserts that this
#: still matches the library, so an upgrade that reordered them could not pass silently.
_CONDITION_CHECK, _DELETE, _PUT, _UPDATE = 0, 1, 2, 3

#: TransactionConflict is contention, not a rule being broken; boto3 does not retry
#: TransactionCanceledException for us.
MAX_ATTEMPTS = 3


_connection = None


def connection():
    """One Connection per warm container.

    Rebuilding it per call would re-resolve credentials and re-open TLS on every
    request, which on Lambda is most of the latency budget for a small write.
    """
    global _connection
    if _connection is None:
        _connection = Connection(
            host=getattr(settings, "DYNAMODB_ENDPOINT_URL", "") or None,
            region=settings.AWS_S3_REGION_NAME,
        )
    return _connection


def reset_connection():
    """Drop the cached connection. Used by tests that re-point the endpoint."""
    global _connection
    _connection = None


class Txn:
    """A labelled TransactWriteItems.

    Usage mirrors PynamoDB's own context manager, with a label as the first argument:

        with Txn() as tx:
            tx.update("slot", slot, actions=[...], condition=...)
            tx.save("seat", seat, condition=Seat.sk.does_not_exist())

    On cancellation it raises `TransactionFailed`, whose `.by_label` maps each label to
    its `CancellationReason` (or None when that item was not the problem).
    """

    def __init__(self, connection_=None, client_request_token=None):
        self._connection = connection_ or connection()
        self._token = client_request_token
        # (bucket, label, callable(tx))
        self._ops = []

    # -- adding operations ---------------------------------------------------------

    def save(self, label, model, condition=None, return_values=ALL_OLD):
        self._ops.append((_PUT, label,
                          lambda tx: tx.save(model, condition=condition,
                                             return_values=return_values)))

    def update(self, label, model, actions, condition=None, return_values=ALL_OLD):
        self._ops.append((_UPDATE, label,
                          lambda tx: tx.update(model, actions=actions,
                                               condition=condition,
                                               return_values=return_values)))

    def delete(self, label, model, condition=None):
        self._ops.append((_DELETE, label,
                          lambda tx: tx.delete(model, condition=condition)))

    def condition_check(self, label, model_cls, hash_key, range_key=None, condition=None):
        self._ops.append((_CONDITION_CHECK, label,
                          lambda tx: tx.condition_check(model_cls, hash_key, range_key,
                                                        condition=condition)))

    # -- committing ----------------------------------------------------------------

    def labels_in_wire_order(self):
        """The labels as DynamoDB will see them, i.e. grouped by operation type.

        Public because the test suite asserts it against a captured request rather than
        trusting this module's idea of PynamoDB's internals.
        """
        return [label for _, label, _ in
                sorted(self._ops, key=lambda op: op[0])]

    def commit(self):
        order = self.labels_in_wire_order()
        last = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with TransactWrite(connection=self._connection,
                                   client_request_token=self._token) as tx:
                    # Add in bucket order too. Not strictly required -- PynamoDB
                    # regroups regardless -- but it keeps the debugger honest.
                    for _, _, apply_ in sorted(self._ops, key=lambda op: op[0]):
                        apply_(tx)
                return
            except TransactWriteError as exc:
                # A ValidationException is a bug in the caller, not a rule being
                # enforced -- two operations against one item, a malformed expression,
                # a missing key. Letting it surface as TransactionFailed would invite
                # a caller to translate it into a polite message for the customer and
                # hide a defect. Re-raise it unchanged.
                cause = getattr(exc, "cause", None)
                code = (cause.response.get("Error", {}).get("Code")
                        if cause is not None and hasattr(cause, "response") else None)
                if code and code != "TransactionCanceledException":
                    raise

                reasons = exc.cancellation_reasons
                codes = [r.code if r else None for r in reasons]
                if "TransactionConflict" in codes and attempt < MAX_ATTEMPTS:
                    # Contention, not a broken rule. Back off with jitter and retry.
                    time.sleep(random.uniform(0.02, 0.08) * attempt)
                    last = exc
                    continue
                if "TransactionConflict" in codes:
                    raise Retryable("transaction kept conflicting") from exc
                raise TransactionFailed(reasons) from exc
        raise Retryable("transaction kept conflicting") from last

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        return False


def failed(error, label, order):
    """Did `label` fail? `order` is the list from `Txn.labels_in_wire_order()`."""
    reason = reason_for(error, label, order)
    return reason is not None and reason.code == "ConditionalCheckFailed"


def reason_for(error, label, order):
    """The CancellationReason for one label, or None if that item was fine."""
    try:
        index = order.index(label)
    except ValueError:
        raise KeyError(f"no transaction item labelled {label!r}") from None
    if index >= len(error.reasons):
        return None
    return error.reasons[index]
