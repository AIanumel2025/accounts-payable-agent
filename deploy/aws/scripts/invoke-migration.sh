#!/usr/bin/env bash
# Invokes the non-public migration/administration Lambda explicitly. Prints only its JSON result.
#   invoke-migration.sh migrate
#   invoke-migration.sh identity register-tenant --tenant-key acme --display-name "Acme Ltd"
#   invoke-migration.sh identity register --tenant-id <uuid> --org-id org_... --user-id user_... --role AP_OPERATOR
source "$(dirname "$0")/_common.sh"
need aws; need jq
ACTION="${1:-}"; shift || true
FUNCTION="ap-agent-${ENVIRONMENT}-migrate"
case "$ACTION" in
  migrate) PAYLOAD='{"action":"migrate"}' ;;
  identity) [ "$#" -ge 1 ] || die "identity needs a CLI sub-command"; # `--` ends jq's own option parsing, so values such as --tenant-key are data, never jq options.
    PAYLOAD="$(jq -cn --args '{action:"identity",args:$ARGS.positional}' -- "$@")" ;;
  *) die "usage: invoke-migration.sh migrate | identity <cli args...>" ;;
esac
OUT="$(mktemp)"; trap 'rm -f "$OUT"' EXIT
aws lambda invoke --function-name "$FUNCTION" --cli-binary-format raw-in-base64-out --payload "$PAYLOAD" "$OUT" \
  --query '{StatusCode:StatusCode,FunctionError:FunctionError}' --output json
jq . "$OUT"
jq -e '.ok == true' "$OUT" >/dev/null || die "the task reported a failure (see above; nothing further was changed)."
