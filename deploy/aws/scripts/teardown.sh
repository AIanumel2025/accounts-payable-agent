#!/usr/bin/env bash
# Complete teardown: empties the invoice bucket, deletes the application and ECR stacks, the SSM secrets and the
# deployment bucket SAM created, then verifies that nothing billable remains. DESTRUCTIVE: invoice objects are lost
# (database rows in Neon are not touched). Requires typing the environment name to confirm.
source "$(dirname "$0")/_common.sh"
need aws; need jq
echo "This permanently deletes the '$ENVIRONMENT' AWS deployment in $AWS_REGION (account $(masked "$(account_id)"))."
read -r -p "Type the environment name ($ENVIRONMENT) to continue: " CONFIRM
[ "$CONFIRM" = "$ENVIRONMENT" ] || die "not confirmed."

BUCKET="$(stack_output "$APP_STACK" UploadBucketName 2>/dev/null || true)"
if [ -n "$BUCKET" ] && [ "$BUCKET" != None ]; then
  echo "emptying bucket (all versions)..."
  aws s3 rm "s3://$BUCKET" --recursive >/dev/null || true
  aws s3api list-object-versions --bucket "$BUCKET" --output json \
    --query '{Objects: [Versions[],DeleteMarkers[]][].{Key:Key,VersionId:VersionId}}' 2>/dev/null \
    | jq -c 'select(.Objects != null and (.Objects|length) > 0) | {Objects: .Objects, Quiet: true}' \
    | while read -r batch; do aws s3api delete-objects --bucket "$BUCKET" --delete "$batch" >/dev/null; done || true
fi

echo "deleting application stack..."
aws cloudformation delete-stack --stack-name "$APP_STACK"; aws cloudformation wait stack-delete-complete --stack-name "$APP_STACK"

echo "deleting container images and ECR stack..."
for repo in web api worker migrate; do
  aws ecr delete-repository --repository-name "ap-agent-${ENVIRONMENT}/${repo}" --force >/dev/null 2>&1 || true
done
aws cloudformation delete-stack --stack-name "$ECR_STACK"; aws cloudformation wait stack-delete-complete --stack-name "$ECR_STACK"

echo "deleting secrets..."
for name in postgres-runtime-dsn postgres-migration-dsn clerk-secret-key frontend-csrf-secret; do
  aws ssm delete-parameter --name "$SSM_PREFIX/$name" >/dev/null 2>&1 || true
done

echo "deleting the SAM deployment bucket (aws-sam-cli-managed-default) if it is empty of other projects..."
SAM_STACK="aws-sam-cli-managed-default"
SAM_BUCKET="$(stack_output "$SAM_STACK" SourceBucket 2>/dev/null || true)"
if [ -n "$SAM_BUCKET" ] && [ "$SAM_BUCKET" != None ]; then
  read -r -p "Delete the shared SAM bucket '$SAM_BUCKET' too? Only say yes if no other SAM project uses it [y/N]: " ANS
  if [ "$ANS" = y ]; then aws s3 rm "s3://$SAM_BUCKET" --recursive >/dev/null; aws cloudformation delete-stack --stack-name "$SAM_STACK"; aws cloudformation wait stack-delete-complete --stack-name "$SAM_STACK"; fi
fi

echo; echo "== verification: expect no remaining resources =="
"$(dirname "$0")/verify-teardown.sh"
