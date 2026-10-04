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
echo "No quota increase is needed: nothing is reserved by default (the single worker comes from the FIFO message group + batch size 1)."
echo "note: Lambda keeps 100 units of concurrency unreserved. The OPTIONAL WEB_API_RESERVED_CONCURRENCY=N (default -1, off)"
echo "      needs a quota of at least 100 + 2N; this account's quota is $LIMIT."

for name in postgres-runtime-dsn postgres-migration-dsn clerk-secret-key frontend-csrf-secret; do
  if aws ssm get-parameter --name "$SSM_PREFIX/$name" --query 'Parameter.Type' --output text >/dev/null 2>&1; then
    echo "SSM $name: present"
  else
    echo "SSM $name: MISSING (run deploy/aws/scripts/create-secrets.sh)"
  fi
done
echo "Preflight finished."
