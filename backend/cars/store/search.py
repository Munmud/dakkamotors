"""Staff substring search, and the reason it is not a search engine.

Django's admin gave staff `icontains` across several fields. DynamoDB has no such query.
The approach here is deliberately the dull one: read the relevant GSI1 partition in full
and filter in Python against a `search_blob` written at save time.

The numbers, so the choice is arguable rather than assumed. ~100 cars at ~2 KB each is
~200 KB -- one Query, a single 1 MB page, about 25 read units, roughly $0.000003, ~15 ms
in region. Filtering in Python rather than with a `FilterExpression` is not laziness:
`contains()` is case-sensitive, cannot express a multi-field `icontains` OR without a
clumsy disjunction, and does not reduce read capacity anyway -- only bytes on the wire.

**OpenSearch is explicitly rejected.** Serverless has a two-OCU minimum (~$700/month) and
the smallest managed domain is ~$25/month plus storage. Either is ten to fifty times the
entire running cost of this site, to index a dataset that fits in one Lambda invocation's
memory.

**The ceiling, written down so nobody has to rediscover it.** This stops working somewhere
around five thousand cars, when a status partition exceeds 1 MB and the Query starts
paginating. The answer at that point is still not a cluster: rebuild a single
`cars/index.json` blob on S3 on every car write and have the staff endpoint load and cache
it in Lambda memory. That is another order of magnitude before search is a real project.
"""

from . import cars as cars_store


def matches(item, term):
    """Case-insensitive substring over the precomputed haystack."""
    if not term:
        return True
    blob = getattr(item, "search_blob", None) or ""
    return term.casefold() in blob


def find_cars(*, term=None, status=None, fuel_type=None, brand=None):
    """The staff car list.

    `status` selects the GSI1 partition -- it is the one filter that costs nothing.
    Everything else is a Python predicate over what came back.
    """
    statuses = [status] if status else ["available", "reserved", "sold"]
    found = []
    for one in statuses:
        found.extend(cars_store.list_by_status(one))

    if fuel_type:
        found = [c for c in found if c.fuel_type == fuel_type]
    if brand:
        lowered = brand.casefold()
        found = [c for c in found if (c.brand or "").casefold() == lowered]
    found = [c for c in found if matches(c, term)]

    found.sort(key=lambda c: c.created_at, reverse=True)
    return found
