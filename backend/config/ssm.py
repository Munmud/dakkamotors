"""Secrets, read from SSM Parameter Store at settings import.

Replaces Zappa's `remote_env`: a JSON file at
`s3://dakkamotors-backend-media/config/env.json` that the handler fetched on every cold
start. That existed because the Lambda was in a VPC with no NAT and could not reach the
SSM API at all -- S3 was reachable only through a free gateway endpoint, so a private S3
object was the one place a secret could live. The function left the VPC with the DynamoDB
cutover, and SSM has been reachable ever since.

What that dance cost, and why it is worth deleting: a secret had to be put in SSM, *then
also* hand-copied into the S3 object, then the copy remembered and removed when it
changed. `docs/INFRA.md` called it the most error-prone procedure in this repo. Two
copies of a secret drift, and the one that drifts is the one nothing tests.

**Only secrets belong here.** Everything else -- bucket names, mail addresses, allowed
hosts -- is not sensitive and lives in `zappa_settings.json`, where it is visible, in git,
and reviewable. A parameter store is not a config file.
"""

import logging
import os

logger = logging.getLogger(__name__)

#: Everything under this prefix is fetched in one call.
PREFIX = "/dakkamotors/"

#: SSM name -> the environment variable settings.py actually reads. Anything not listed
#: is exported under its own name.
ALIASES = {
    "DJANGO_SECRET_KEY": "SECRET_KEY",
}

#: Read by the mailer Lambda straight from SSM, never by this function.
IGNORED = {"BREVO_API_KEY"}


def on_lambda():
    """Whether this process is a Lambda invocation.

    `AWS_LAMBDA_FUNCTION_NAME` is set by the runtime and by nothing else, so a laptop and
    a CI runner both answer no -- which is what keeps `manage.py test` from making a
    network call, and what lets a developer override any of this with a real environment
    variable.
    """
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def load(force=False):
    """Copy the parameters under `PREFIX` into `os.environ`.

    Called once, at settings import, which on Lambda means once per container rather than
    once per request.

    **A real environment variable always wins.** `setdefault` rather than assignment, so
    `zappa_settings.json` and a local `.env` can override a parameter without anybody
    having to know this module exists.

    Raises rather than continuing if SSM cannot be reached on Lambda. The alternative is
    Django falling back to its insecure default `SECRET_KEY` and serving traffic with it,
    which is worse than a cold start that fails loudly.
    """
    if not (force or on_lambda()):
        return {}

    import boto3

    client = boto3.client("ssm", region_name=os.environ.get(
        "AWS_S3_REGION_NAME", "ap-northeast-1"))

    loaded = {}
    token = None
    while True:
        kwargs = {"Path": PREFIX, "Recursive": True, "WithDecryption": True}
        if token:
            kwargs["NextToken"] = token
        page = client.get_parameters_by_path(**kwargs)
        for param in page.get("Parameters", []):
            name = param["Name"].rsplit("/", 1)[-1]
            if name in IGNORED:
                continue
            key = ALIASES.get(name, name)
            os.environ.setdefault(key, param["Value"])
            loaded[key] = name
        token = page.get("NextToken")
        if not token:
            break

    # The names only -- never the values, which would put a secret in CloudWatch.
    logger.info("Loaded %d parameters from %s: %s",
                len(loaded), PREFIX, ", ".join(sorted(loaded)))
    return loaded
