"""The one table, and the base every item type inherits from.

Single-table design in PynamoDB works through `DiscriminatorAttribute`: every entity is
a subclass sharing the same generic `pk` / `sk` key names, distinguished by a `_cls`
attribute written alongside the data. A Query issued from a *subclass* is filtered to
that subclass; a Query issued from `BaseItem` returns everything in the partition,
deserialised into the right classes. Both behaviours are used deliberately -- the second
is what makes the car detail page a single round trip.

One limitation worth knowing before you reach for it: a discriminator cannot be a key
attribute. That is fine here because entities differ in key *values* (`CAR#`, `SLOT#`,
`CUST#`), never in key names.
"""

from django.conf import settings
from pynamodb.attributes import DiscriminatorAttribute, UnicodeAttribute
from pynamodb.indexes import AllProjection, GlobalSecondaryIndex
from pynamodb.models import Model


def _host():
    """Endpoint override for DynamoDB Local; `None` in production.

    Must be None rather than an empty string -- PynamoDB passes it straight to botocore,
    which treats "" as a malformed endpoint rather than as absent.
    """
    return getattr(settings, "DYNAMODB_ENDPOINT_URL", "") or None


class GSI1(GlobalSecondaryIndex):
    """Listing and queue reads: cars by status, bookings by status, the question queue."""

    class Meta:
        index_name = "gsi1"
        projection = AllProjection()
        billing_mode = "PAY_PER_REQUEST"

    gsi1pk = UnicodeAttribute(hash_key=True)
    gsi1sk = UnicodeAttribute(range_key=True)


class GSI2(GlobalSecondaryIndex):
    """A customer's own cross-partition reads -- currently just their questions."""

    class Meta:
        index_name = "gsi2"
        projection = AllProjection()
        billing_mode = "PAY_PER_REQUEST"

    gsi2pk = UnicodeAttribute(hash_key=True)
    gsi2sk = UnicodeAttribute(range_key=True)


class BaseItem(Model):
    class Meta:
        table_name = settings.DDB_TABLE
        region = settings.AWS_S3_REGION_NAME
        host = _host()
        billing_mode = "PAY_PER_REQUEST"

    pk = UnicodeAttribute(hash_key=True)
    sk = UnicodeAttribute(range_key=True)

    # Sparse: only set on items that need to appear in an index. An item with no
    # gsi1pk simply is not in gsi1, which is what keeps guard items out of the
    # listing queries for free.
    gsi1pk = UnicodeAttribute(null=True)
    gsi1sk = UnicodeAttribute(null=True)
    gsi2pk = UnicodeAttribute(null=True)
    gsi2sk = UnicodeAttribute(null=True)

    gsi1 = GSI1()
    gsi2 = GSI2()

    entity = DiscriminatorAttribute(attr_name="_cls")
