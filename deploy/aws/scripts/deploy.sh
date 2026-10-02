#!/usr/bin/env bash
# Deploys the application stack with SAM/CloudFormation.
#   deploy.sh pass1   FrontendOrigin empty (creates everything; the API stays closed until pass 2)
#   deploy.sh pass2   FrontendOrigin = the deployed frontend's origin (Clerk authorized party + S3 CORS)
# Required environment: CLERK_PUBLISHABLE_KEY (public), CLERK_ISSUER (https://...). Optional: ALARM_EMAIL,
# WORKER_MEMORY_MB, WEB_API_RESERVED_CONCURRENCY (-1 to leave unreserved), POSTGRES_RUNTIME_ROLE.
source "$(dirname "$0")/_common.sh"
need aws; need sam
PASS="${1:-}"; [ "$PASS" = pass1 ] || [ "$PASS" = pass2 ] || die "usage: deploy.sh pass1|pass2"
: "${CLERK_PUBLISHABLE_KEY:?set CLERK_PUBLISHABLE_KEY}"; : "${CLERK_ISSUER:?set CLERK_ISSUER (https://...)}"
[ -f "$STATE_DIR/images.env" ] || die "run build-and-push.sh first."
# shellcheck disable=SC1091
source "$STATE_DIR/images.env"

ORIGIN=""
if [ "$PASS" = pass2 ]; then
  URL="$(stack_output "$APP_STACK" FrontendUrl)"; [ -n "$URL" ] && [ "$URL" != None ] || die "run pass1 first."
  ORIGIN="${URL%/}"
fi

sam validate --lint -t "$DEPLOY_DIR/template.yaml" --region "$AWS_REGION"
sam deploy --template-file "$DEPLOY_DIR/template.yaml" --stack-name "$APP_STACK" --region "$AWS_REGION" \
  --capabilities CAPABILITY_IAM --resolve-s3 --no-confirm-changeset --no-fail-on-empty-changeset \
  --tags "${TAGS[@]}" \
  --parameter-overrides \
    "Environment=$ENVIRONMENT" "SsmPrefix=$SSM_PREFIX" \
    "WebImageUri=$WEB_IMAGE_URI" "ApiImageUri=$API_IMAGE_URI" "WorkerImageUri=$WORKER_IMAGE_URI" "MigrateImageUri=$MIGRATE_IMAGE_URI" \
    "ClerkPublishableKey=$CLERK_PUBLISHABLE_KEY" "ClerkIssuer=$CLERK_ISSUER" "FrontendOrigin=$ORIGIN" \
    "PostgresRuntimeRole=${POSTGRES_RUNTIME_ROLE:-ap_agent_app}" \
    "WorkerMemoryMb=${WORKER_MEMORY_MB:-4096}" \
    "WebAndApiReservedConcurrency=${WEB_API_RESERVED_CONCURRENCY:-10}" \
    "AlarmEmail=${ALARM_EMAIL:-}"

echo
echo "FrontendUrl:  $(stack_output "$APP_STACK" FrontendUrl)"
if [ "$PASS" = pass1 ]; then
  echo "Next: add that origin (no trailing slash) to Clerk's allowed origins if required, run migrate.sh, then 'deploy.sh pass2'."
fi
