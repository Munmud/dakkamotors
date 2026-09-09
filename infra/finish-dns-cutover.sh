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

echo "==> Serving car photos from the real domain instead of *.cloudfront.net"
python - "$MEDIA_BUCKET" "$LAMBDA" "$REGION" <<'PY'
import json, sys, time, boto3

bucket, function, region = sys.argv[1:4]
s3 = boto3.client("s3", region_name=region)
lam = boto3.client("lambda", region_name=region)

env = json.loads(s3.get_object(Bucket=bucket, Key="config/env.json")["Body"].read())
env["MEDIA_CUSTOM_DOMAIN"] = "dakkamotors.com"
s3.put_object(
    Bucket=bucket, Key="config/env.json",
    Body=json.dumps(env, indent=2).encode(),
    ContentType="application/json", ServerSideEncryption="AES256",
)

# remote_env is only read at cold start, so retire the warm containers.
cfg = lam.get_function_configuration(FunctionName=function)
variables = cfg.get("Environment", {}).get("Variables", {})
variables["ENV_RELOADED_AT"] = str(int(time.time()))
lam.update_function_configuration(FunctionName=function, Environment={"Variables": variables})
print("    media domain set to dakkamotors.com")
PY

echo "==> Invalidating the cache"
aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION_ID" \
  --paths "/*" --query "Invalidation.Id" --output text

cat <<'DONE'

Done. Verify with:
  curl -I https://dakkamotors.com/
  curl -s https://dakkamotors.com/api/cars/
  open https://dakkamotors.com/api/admin/
DONE
