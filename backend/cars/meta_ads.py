"""The one thing this site asks of Meta's Marketing API: stop an ad set.

Everything else about advertising happens in Ads Manager, by hand, by the owner. This
exists because the one moment that matters is the one nobody is watching: a booking
lands at nine in the evening and the ad set keeps spending until somebody notices. At
a ¥1,000 lifetime budget the whole campaign can be gone overnight.

Deliberately `urllib` and not the Facebook SDK. The call is a single form POST with
three fields; the SDK is a large dependency in a Lambda package for that, and it has
its own opinions about versions and error types. Nothing here needs them.

The token is a system user's, with `ads_management` on the ad account, and lives in
SSM at `/dakkamotors/META_ADS_TOKEN` -- `config/ssm.py` already loads everything under
that prefix. Blank anywhere else, and then every function here is a no-op, which is
why no test needs a fake Meta. See docs/ADS.md for how the token is made.
"""

import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

#: The Graph API version in the URL. Meta retires these roughly every two years and a
#: retired one starts answering errors rather than 404s, so this is the line to change
#: when the pause silently stops working. Their changelog says which version is oldest.
GRAPH_VERSION = "v21.0"

#: Short. This runs inside a customer's booking request, which has an API Gateway
#: ceiling of 29 seconds, and the caller treats a failure as "the owner will see the
#: ad in the morning". Waiting is worse than failing.
TIMEOUT_SECONDS = 5


class MetaError(Exception):
    """Meta refused, or could not be reached. The caller decides how much it matters."""


def pause_ad_set(ad_set_id):
    """Set one ad set to PAUSED. Returns False when no token is configured.

    Pausing rather than deleting, on purpose: paused is reversible from Ads Manager
    and keeps the ad set's reporting, which is the whole point of having measured the
    campaign. A deleted ad set takes its numbers with it.
    """
    token = getattr(settings, "META_ADS_TOKEN", "")
    if not token or not ad_set_id:
        return False

    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{urllib.parse.quote(str(ad_set_id))}"
    body = urllib.parse.urlencode({
        "status": "PAUSED",
        "access_token": token,
    }).encode("utf-8")

    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        # Meta's message says which of the many reasons it was -- a wrong id, a token
        # without ads_management, an ad account on hold -- and none of them are
        # distinguishable from the status code alone, so the body goes in the log.
        detail = exc.read()[:500].decode("utf-8", "replace")
        raise MetaError(f"Meta refused the pause ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise MetaError(f"Could not reach Meta: {exc}") from exc
    return True
