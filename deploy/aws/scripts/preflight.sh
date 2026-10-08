#!/usr/bin/env bash
# Read-only readiness check, run BEFORE any stack is created or changed. Prints no secret and masks the account id.
# Exit status: non-zero if any BLOCKING check fails; informational notes never fail it.
#   BLOCK  a condition that would make the deployment fail or be unsafe
#   info   something worth knowing (quotas you may want to raise, optional settings)
source "$(dirname "$0")/_common.sh"

need aws; need docker; need sam; need jq
blocked=0
block() { echo "BLOCK $*"; blocked=1; }
ok()    { echo "ok    $*"; }
info()  { echo "info  $*"; }

echo "Region:      $AWS_REGION"
[ "$AWS_REGION" = "eu-west-2" ] || info "the default (and tested) region is eu-west-2."
ACCOUNT="$(account_id)" || die "no usable AWS credentials (use IAM Identity Center / 'aws login' / CloudShell; never long-lived keys)."
echo "Account:     $(masked "$ACCOUNT")"
echo "Environment: $ENVIRONMENT   Stacks: $ECR_STACK, $APP_STACK"

# -- Lambda: this account caps memory at 3,008 MB (BLOCKING: the template must never ask for more) -----------------------
LIMIT_MB="${LAMBDA_ACCOUNT_MEMORY_LIMIT_MB:-3008}"
if python3 "$REPO_ROOT/scripts/verify_aws_templates.py" --lambda-memory-limit >/dev/null 2>&1; then
  ok "every Lambda in the template asks for at most ${LIMIT_MB} MB (heavy OCR runs on Fargate, not in Lambda)"
else
  block "a Lambda in deploy/aws/template.yaml asks for more than ${LIMIT_MB} MB; this account rejects that (see docs/m11e1_aws_deployment_runbook.md)."
fi
info "Lambda memory is limited to ${LIMIT_MB} MB in this account; the OCR worker keeps its 8 GB by running as an ECS Fargate task."

LAMBDA_CONCURRENCY="$(aws lambda get-account-settings --query 'AccountLimit.ConcurrentExecutions' --output text 2>/dev/null || echo unknown)"
info "Lambda concurrent-executions quota: $LAMBDA_CONCURRENCY. Nothing is reserved by default; the single OCR task comes from the FIFO message group + batch size 1."
info "The OPTIONAL WEB_API_RESERVED_CONCURRENCY=N (default -1, off) needs a quota of at least 100 + 2N."

# -- Fargate: the OCR task needs 4 vCPU (BLOCKING when readable and below 4) -----------------------------------------------
FARGATE_VCPU="$(aws service-quotas get-service-quota --service-code fargate --quota-code L-3032A538 --query 'Quota.Value' --output text 2>/dev/null || true)"
case "$FARGATE_VCPU" in
  ''|None|unknown) info "could not read the Fargate On-Demand vCPU quota; make sure it is at least 4 (Service Quotas -> AWS Fargate)." ;;
  *)
    if awk -v v="$FARGATE_VCPU" 'BEGIN { exit !(v + 0 >= 4) }'; then ok "Fargate On-Demand vCPU quota: $FARGATE_VCPU (needs 4 for one OCR task)"
    else block "Fargate On-Demand vCPU quota is $FARGATE_VCPU; one OCR task needs 4. Request an increase in Service Quotas (AWS Fargate)."; fi ;;
esac

VPCS="$(aws ec2 describe-vpcs --query 'length(Vpcs)' --output text 2>/dev/null || echo unknown)"
case "$VPCS" in ''|unknown|None) ;; *) [ "$VPCS" -lt 5 ] 2>/dev/null && ok "VPCs in region: $VPCS (the stack adds one; the default limit is 5)" || info "VPCs in region: $VPCS; the stack adds one and the default limit is 5.";; esac

# -- existing application stack ---------------------------------------------------------------------------------------------
STACK_STATUS="$(aws cloudformation describe-stacks --stack-name "$APP_STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"
case "$STACK_STATUS" in
  ''|None) ok "no application stack yet (a first deployment)" ;;
  ROLLBACK_COMPLETE|CREATE_FAILED|DELETE_FAILED) block "stack $APP_STACK is in $STACK_STATUS and cannot be updated; delete the STACK ONLY first: aws cloudformation delete-stack --stack-name $APP_STACK && aws cloudformation wait stack-delete-complete --stack-name $APP_STACK (keeps the ECR stack and the SSM secrets)." ;;
  *_IN_PROGRESS) block "stack $APP_STACK is $STACK_STATUS; wait for it to finish." ;;
  *) ok "application stack is $STACK_STATUS (this will be an update)" ;;
esac

# -- container images (BLOCKING: they must exist before the stack references them) --------------------------------------------
if [ -f "$STATE_DIR/images.env" ]; then
  # shellcheck disable=SC1091
  source "$STATE_DIR/images.env"
  for pair in web:WEB_IMAGE_URI api:API_IMAGE_URI worker:WORKER_IMAGE_URI migrate:MIGRATE_IMAGE_URI; do
    name="${pair%%:*}"; variable="${pair##*:}"; uri="${!variable:-}"
    if [ -z "$uri" ]; then block "image $name: $variable is missing from $STATE_DIR/images.env (run build-and-push.sh $name)."; continue; fi
    reference="${uri##*/}"
    case "$reference" in *@sha256:*) selector="imageDigest=${reference#*@}" ;; *:*) selector="imageTag=${reference#*:}" ;; *) selector="" ;; esac
    if [ -z "$selector" ]; then block "image $name: '$uri' has neither a tag nor a digest."; continue; fi
    if aws ecr describe-images --repository-name "ap-agent-${ENVIRONMENT}/${name}" --image-ids "$selector" --query 'imageDetails[0].imageDigest' --output text >/dev/null 2>&1; then
      ok "image $name exists in ECR ($selector)"
    else
      block "image $name ($selector) was not found in repository ap-agent-${ENVIRONMENT}/${name}; run build-and-push.sh $name."
    fi
  done
else
  block "no $STATE_DIR/images.env: run build-and-push.sh first."
fi

# -- secrets (BLOCKING; existence only -- the values are never read or printed) -----------------------------------------------
for name in postgres-runtime-dsn postgres-migration-dsn clerk-secret-key frontend-csrf-secret; do
  if aws ssm get-parameter --name "$SSM_PREFIX/$name" --query 'Parameter.Type' --output text >/dev/null 2>&1; then
    ok "SSM $name present"
  else
    block "SSM $name is missing (run deploy/aws/scripts/create-secrets.sh)."
  fi
done

echo
if [ "$blocked" -eq 0 ]; then echo "Preflight passed: no blocking problem."; else echo "Preflight FAILED: fix every BLOCK line above before deploying."; fi
exit "$blocked"
