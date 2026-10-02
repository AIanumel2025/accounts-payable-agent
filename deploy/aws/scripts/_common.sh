#!/usr/bin/env bash
# Shared helpers for the deploy/aws scripts. Sourced, never executed. Prints no secret and masks the account id.
set -euo pipefail

export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-west-2}}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export ENVIRONMENT="${ENVIRONMENT:-production}"
export SSM_PREFIX="${SSM_PREFIX:-/ap-agent/${ENVIRONMENT}}"
export ECR_STACK="${ECR_STACK:-ap-agent-${ENVIRONMENT}-ecr}"
export APP_STACK="${APP_STACK:-ap-agent-${ENVIRONMENT}}"
export TAGS=("Project=accounts-payable-agent" "Environment=${ENVIRONMENT}" "ManagedBy=cloudformation")

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$DEPLOY_DIR/../.." && pwd)"
# Local, git-ignored working files (image URIs, deployment parameters). Never contains a secret.
STATE_DIR="${STATE_DIR:-$DEPLOY_DIR/.local}"
mkdir -p "$STATE_DIR"

die() { echo "error: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not installed."; }

# Account id is only ever shown masked.
account_id() { aws sts get-caller-identity --query Account --output text; }
masked() { local id="$1"; echo "****${id: -4}"; }

stack_output() { # <stack> <OutputKey>
  aws cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue | [0]" --output text
}

# Reads a value without echoing it. Usage: read_secret VAR "Prompt"
read_secret() {
  local __var="$1" __prompt="$2" __value
  read -r -s -p "$__prompt: " __value; echo >&2
  [ -n "$__value" ] || die "empty value for: $__prompt"
  printf -v "$__var" '%s' "$__value"
}
