#!/usr/bin/env bash
# Unauthenticated smoke checks of the deployed stack (no credentials, no Clerk session needed). Exits non-zero on any failure.
source "$(dirname "$0")/_common.sh"
need aws; need curl
FRONT="$(stack_output "$APP_STACK" FrontendUrl)"; FRONT="${FRONT%/}"
API="$(stack_output "$APP_STACK" ApiUrl)"; API="${API%/}"
BUCKET="$(stack_output "$APP_STACK" UploadBucketName)"
fail=0; check() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1 (expected $3, got $2)"; fail=1; fi; }

code="$(curl -s -o /dev/null -w '%{http_code}' -m 30 "$FRONT/icon.svg")";                       check "frontend serves static assets" "$code" 200
code="$(curl -s -o /dev/null -w '%{http_code}' -m 30 "$FRONT/dashboard")";                       check "unauthenticated page request is redirected" "$code" 307
loc="$(curl -s -o /dev/null -w '%{redirect_url}' -m 30 "$FRONT/dashboard")"
case "$loc" in */sign-in*) echo "ok   redirect targets sign-in";; *) echo "FAIL redirect target ($loc)"; fail=1;; esac
code="$(curl -s -o /dev/null -w '%{http_code}' -m 30 "$FRONT/api/backend/api/v1/dashboard")";    check "unauthenticated data call is refused" "$code" 401
code="$(curl -s -o /dev/null -w '%{http_code}' -m 30 -X POST "$FRONT/api/v1/operations/upload-intents" -H 'x-ap-agent-clerk-authorization: Bearer forged')"; check "forged server-to-server header is refused" "$code" 403
code="$(curl -s -o /dev/null -w '%{http_code}' -m 30 "$API/health")";                            check "API Function URL rejects unsigned requests" "$code" 403
code="$(curl -s -o /dev/null -w '%{http_code}' -m 30 -H 'X-Tenant-ID: 11111111-1111-1111-1111-111111111111' -H 'X-Actor-Role: TENANT_ADMIN' "$API/api/v1/operations/jobs")"; check "API rejects forged identity headers" "$code" 403
code="$(curl -s -o /dev/null -w '%{http_code}' -m 30 "https://${BUCKET}.s3.${AWS_REGION}.amazonaws.com/")"; check "bucket is not publicly listable" "$code" 403
echo "Block Public Access:"; aws s3api get-public-access-block --bucket "$BUCKET" --query 'PublicAccessBlockConfiguration' --output json
echo "Queue attributes:";    aws sqs get-queue-attributes --queue-url "$(stack_output "$APP_STACK" QueueUrl)" --attribute-names VisibilityTimeout RedrivePolicy --output json
exit "$fail"
