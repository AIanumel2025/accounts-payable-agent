#!/usr/bin/env bash
# Stores the secrets as SSM Parameter Store SecureStrings. Values are typed at hidden prompts (or generated); they are
# never echoed, logged, written to disk or passed on a command line that other users can see (the AWS CLI reads
# them from a file-descriptor-backed temporary file created with mode 0600 and removed immediately).
source "$(dirname "$0")/_common.sh"
need aws; need openssl

put() { # <name> <value>
  local file; file="$(mktemp)"; chmod 600 "$file"
  printf '%s' "$2" > "$file"
  aws ssm put-parameter --name "$SSM_PREFIX/$1" --type SecureString --overwrite --value "file://$file" \
    --tags "Key=Project,Value=accounts-payable-agent" "Key=Environment,Value=$ENVIRONMENT" "Key=ManagedBy,Value=cloudformation" >/dev/null 2>&1 \
    || aws ssm put-parameter --name "$SSM_PREFIX/$1" --type SecureString --overwrite --value "file://$file" >/dev/null
  rm -f "$file"
  echo "stored: $SSM_PREFIX/$1"
}

echo "Enter each value at the hidden prompt. Nothing is displayed. Press Ctrl-C to abort."
read_secret RUNTIME_DSN   "Neon RUNTIME role DSN (pooled endpoint, least-privilege role, sslmode=require)"
read_secret MIGRATION_DSN "Neon MIGRATION/OWNER role DSN (direct endpoint, sslmode=require)"
[ "$RUNTIME_DSN" != "$MIGRATION_DSN" ] || die "the runtime and migration DSNs must differ (different roles)."
read_secret CLERK_SECRET  "Clerk SECRET key (sk_...)"
case "$CLERK_SECRET" in sk_test_*|sk_live_*) ;; *) die "that does not look like a Clerk secret key.";; esac

put postgres-runtime-dsn   "$RUNTIME_DSN"
put postgres-migration-dsn "$MIGRATION_DSN"
put clerk-secret-key       "$CLERK_SECRET"
if aws ssm get-parameter --name "$SSM_PREFIX/frontend-csrf-secret" >/dev/null 2>&1; then
  echo "kept:   $SSM_PREFIX/frontend-csrf-secret (already exists)"
else
  put frontend-csrf-secret "$(openssl rand -base64 48 | tr -d '\n')"
fi
unset RUNTIME_DSN MIGRATION_DSN CLERK_SECRET
echo "Done. Verify with: deploy/aws/scripts/preflight.sh"
