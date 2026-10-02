#!/usr/bin/env bash
# Validates the account, region and tooling before anything is created. Read-only. Masks the account id.
source "$(dirname "$0")/_common.sh"

need aws; need docker; need sam; need jq
echo "Region:      $AWS_REGION"
[ "$AWS_REGION" = "eu-west-2" ] || echo "warning: the default (and tested) region is eu-west-2."
ACCOUNT="$(account_id)" || die "no usable AWS credentials (use IAM Identity Center / 'aws login' / CloudShell; never long-lived keys)."
echo "Account:     $(masked "$ACCOUNT")"
echo "Environment: $ENVIRONMENT   Stacks: $ECR_STACK, $APP_STACK"

LIMIT="$(aws lambda get-account-settings --query 'AccountLimit.ConcurrentExecutions' --output text)"
echo "Lambda concurrent-executions quota: $LIMIT"
if [ "$LIMIT" -lt 11 ]; then
  echo "BLOCKER: the worker reserves 1 execution and Lambda requires 10 to stay unreserved, so the quota must be at least 11."
  echo "  Request an increase (Service Quotas -> AWS Lambda -> Concurrent executions, code L-B99A9384), e.g. to 100, then re-run."
  exit 2
fi
if [ "$LIMIT" -lt 31 ]; then
  echo "note: quota < 31, so deploy with WEB_API_RESERVED_CONCURRENCY=-1 (frontend/API unreserved; the worker is still limited to 1)."
fi

for name in postgres-runtime-dsn postgres-migration-dsn clerk-secret-key frontend-csrf-secret; do
  if aws ssm get-parameter --name "$SSM_PREFIX/$name" --query 'Parameter.Type' --output text >/dev/null 2>&1; then
    echo "SSM $name: present"
  else
    echo "SSM $name: MISSING (run deploy/aws/scripts/create-secrets.sh)"
  fi
done
echo "Preflight finished."
