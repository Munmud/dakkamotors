#!/usr/bin/env bash
#
# Final go-live step. Run this AFTER GoDaddy's nameservers have been changed to the
# four this project's Route53 hosted zone was assigned.
#
#   bash infra/finish-dns-cutover.sh
#
# It waits for the certificate to validate, attaches it to CloudFront along with the
# dakkamotors.com aliases, and points image URLs at the real domain. Safe to re-run.

set -euo pipefail

# The leading "/" in SSM parameter names is rewritten into a Windows path by Git Bash
# unless path conversion is off.
export MSYS_NO_PATHCONV=1

ZONE_ID="Z051521126KCXW6R2RVF8"
CERT_ARN="arn:aws:acm:us-east-1:484907516843:certificate/3eed3285-d186-4ea1-abf5-a980ffab5647"
DISTRIBUTION_ID="E2IW2C27CUGP1D"
MEDIA_BUCKET="dakkamotors-backend-media"
LAMBDA="dakkamotors-production"
REGION="ap-northeast-1"

echo "==> Checking delegation at the registrar"
EXPECTED=$(aws route53 get-hosted-zone --id "$ZONE_ID" \
  --query "DelegationSet.NameServers" --output text | tr '\t' '\n' | sort)
echo "$EXPECTED" | sed 's/^/    expected: /'

if ! nslookup -type=NS dakkamotors.com a.gtld-servers.net 2>/dev/null \
     | grep -q "$(echo "$EXPECTED" | head -1)"; then
  echo
  echo "    The registrar is not yet publishing these nameservers."
  echo "    Update them at GoDaddy, then re-run. Propagation can take up to 48h,"
  echo "    though it is usually minutes."
  exit 1
fi
echo "    Delegation looks correct."

echo "==> Waiting for the certificate to validate (this is usually quick once DNS is live)"
aws acm wait certificate-validated --certificate-arn "$CERT_ARN" --region us-east-1
echo "    Certificate issued."

echo "==> Attaching the certificate and aliases to CloudFront"
aws cloudformation deploy \
  --stack-name dakkamotors-edge \
  --region us-east-1 \
  --template-file "$(dirname "$0")/edge.yaml" \
  --parameter-overrides \
    FrontendBucketDomain=dakkamotors-frontend.s3.ap-northeast-1.amazonaws.com \
    MediaBucketDomain=dakkamotors-backend-media.s3.ap-northeast-1.amazonaws.com \
    ApiGatewayDomain=q0zvyay9pa.execute-api.ap-northeast-1.amazonaws.com \
    CertificateArn="$CERT_ARN" \
    DomainNames=dakkamotors.com,www.dakkamotors.com \
  --no-fail-on-empty-changeset

echo "==> Checking car photos are served from the real domain"
#
# This step used to rewrite MEDIA_CUSTOM_DOMAIN inside s3://<media>/config/env.json and
# then bump ENV_RELOADED_AT to cycle the warm containers, because Zappa's remote_env was
# the only way to get a value into the function -- the Lambda was in a VPC with no NAT
# and could not reach SSM at all.
#
# remote_env is gone (see backend/config/ssm.py). MEDIA_CUSTOM_DOMAIN is an ordinary
# entry in zappa_settings.json now, so changing it is a code change and a deploy, not a
# script rewriting a JSON object under the running function. Left as a check rather than
# an action: a script that silently edits deploy configuration is how the two copies of
# a setting drift apart, which is the thing remote_env was retired for.
CURRENT=$(grep -o '"MEDIA_CUSTOM_DOMAIN": *"[^"]*"' "$(dirname "$0")/../backend/zappa_settings.json"   | sed 's/.*: *"//; s/"$//')
if [ "$CURRENT" = "dakkamotors.com" ]; then
  echo "    MEDIA_CUSTOM_DOMAIN is already dakkamotors.com."
else
  echo "    MEDIA_CUSTOM_DOMAIN is '${CURRENT:-unset}'."
  echo "    Set it to dakkamotors.com in backend/zappa_settings.json and redeploy,"
  echo "    or photo URLs keep pointing at *.cloudfront.net."
fi

echo "==> Invalidating the cache"
aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" \
  --paths "/*" --query "Invalidation.Id" --output text

cat <<'DONE'

Done. Verify with:
  curl -I https://dakkamotors.com/
  curl -s https://dakkamotors.com/api/cars/
  curl -I https://dakkamotors.com/api/staff/cars/   # expect 302 to the Cognito hosted UI
DONE
