#!/usr/bin/env bash
# Deploys the application stack with SAM/CloudFormation.
#   deploy.sh pass1   FrontendOrigin is NOT passed (creates everything; the API stays closed until pass 2)
#   deploy.sh pass2   FrontendOrigin = the deployed frontend's origin (Clerk authorized party + S3 CORS)
# Required environment: CLERK_PUBLISHABLE_KEY (public), CLERK_ISSUER (https://...). Optional: ALARM_EMAIL,
# WEB_API_RESERVED_CONCURRENCY (default -1 = unreserved; N needs a quota of at least 100 + 2N), POSTGRES_RUNTIME_ROLE,
# OCR_TASK_MAX_SECONDS (default 720). The OCR worker is an ECS Fargate task (4 vCPU / 8 GB); there is no worker memory setting.
source "$(dirname "$0")/_common.sh"
need aws; need sam
PASS="${1:-}"; [ "$PASS" = pass1 ] || [ "$PASS" = pass2 ] || die "usage: deploy.sh pass1|pass2"
: "${CLERK_PUBLISHABLE_KEY:?set CLERK_PUBLISHABLE_KEY}"; : "${CLERK_ISSUER:?set CLERK_ISSUER (https://...)}"
[ -f "$STATE_DIR/images.env" ] || die "run build-and-push.sh first."
# shellcheck disable=SC1091
source "$STATE_DIR/images.env"
for variable in WEB_IMAGE_URI API_IMAGE_URI WORKER_IMAGE_URI MIGRATE_IMAGE_URI; do
  [ -n "${!variable:-}" ] || die "$variable is missing from $STATE_DIR/images.env; run build-and-push.sh."
done

# pass1: no FrontendOrigin override at all (an empty value is rejected by some SAM versions and is the template default anyway).
ORIGIN=""
if [ "$PASS" = pass2 ]; then
  URL="$(stack_output "$APP_STACK" FrontendUrl)"; [ -n "$URL" ] && [ "$URL" != None ] || die "run pass1 first."
  ORIGIN="${URL%/}"
fi

PARAMETERS=(
  "Environment=$ENVIRONMENT" "SsmPrefix=$SSM_PREFIX"
  "WebImageUri=$WEB_IMAGE_URI" "ApiImageUri=$API_IMAGE_URI" "WorkerImageUri=$WORKER_IMAGE_URI" "MigrateImageUri=$MIGRATE_IMAGE_URI"
  "ClerkPublishableKey=$CLERK_PUBLISHABLE_KEY" "ClerkIssuer=$CLERK_ISSUER"
  "PostgresRuntimeRole=${POSTGRES_RUNTIME_ROLE:-ap_agent_app}"
  "OcrTaskMaxSeconds=${OCR_TASK_MAX_SECONDS:-720}"
  "WebAndApiReservedConcurrency=${WEB_API_RESERVED_CONCURRENCY:--1}"
)
[ -z "$ORIGIN" ] || PARAMETERS+=("FrontendOrigin=$ORIGIN")
[ -z "${ALARM_EMAIL:-}" ] || PARAMETERS+=("AlarmEmail=$ALARM_EMAIL")

# Every SAM function that uses an image needs an explicit repository (the dispatcher shares the migration image).
sam validate --lint -t "$DEPLOY_DIR/template.yaml" --region "$AWS_REGION"
sam deploy --template-file "$DEPLOY_DIR/template.yaml" --stack-name "$APP_STACK" --region "$AWS_REGION" \
  --capabilities CAPABILITY_IAM --resolve-s3 --no-confirm-changeset --no-fail-on-empty-changeset \
  --image-repositories "ApiFunction=$(image_repository "$API_IMAGE_URI")" \
  --image-repositories "WebFunction=$(image_repository "$WEB_IMAGE_URI")" \
  --image-repositories "MigrateFunction=$(image_repository "$MIGRATE_IMAGE_URI")" \
  --image-repositories "DispatcherFunction=$(image_repository "$MIGRATE_IMAGE_URI")" \
  --tags "${TAGS[@]}" \
  --parameter-overrides "${PARAMETERS[@]}"

echo
echo "FrontendUrl:  $(stack_output "$APP_STACK" FrontendUrl)"
if [ "$PASS" = pass1 ]; then
  echo "Next: add that origin (no trailing slash) to Clerk's allowed origins if required, run invoke-migration.sh migrate, then 'deploy.sh pass2'."
fi
