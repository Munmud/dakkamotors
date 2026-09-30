"""The DynamoDB data layer.

Everything that reads or writes an item lives under here. The domain modules
(`requests.py`, `advertising.py`) keep the *rules*; this package keeps the *storage*,
and the boundary between them is the reason a migration off Aurora did not have to
rewrite either.

Import the operation modules, not the item classes:

    from .store import cars, requests
    car = cars.by_slug("2008-daihatsu-tanto-x")

Reaching for `store.models.Car` directly from a view is how key construction leaks out
of `store/keys.py`, which is the one thing single-table design cannot survive.
"""

from . import keys  # noqa: F401
from .errors import (  # noqa: F401
    ConditionFailed,
    NotFound,
    Retryable,
    StoreError,
    TransactionFailed,
)
