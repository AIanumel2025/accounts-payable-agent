#!/usr/bin/env bash
# Records an image that was built ELSEWHERE (e.g. AWS CodeBuild for the large worker image) in deploy/aws/.local/images.env,
# by immutable digest, leaving every other recorded image untouched. Writes no secret and prints no account id.
#   record-image.sh worker <tag>              resolve the digest of ap-agent-<env>/worker:<tag> in ECR
#   record-image.sh worker sha256:<64 hex>    use this digest (it must exist in the repository)
# The file is replaced atomically and the previous one is kept as images.env.bak.
source "$(dirname "$0")/_common.sh"
need aws
NAME="${1:-}"; REF="${2:-}"
case "$NAME" in migrate|api|web|worker) ;; *) die "usage: record-image.sh <migrate|api|web|worker> <tag|sha256:digest>";; esac
[ -n "$REF" ] || die "usage: record-image.sh <migrate|api|web|worker> <tag|sha256:digest>"
case "$REF" in
  sha256:*) [[ "$REF" =~ ^sha256:[a-f0-9]{64}$ ]] || die "'$REF' is not a valid sha256 digest."; SELECTOR="imageDigest=$REF" ;;
  *) [[ "$REF" =~ ^[A-Za-z0-9_.-]{1,128}$ ]] || die "'$REF' is not a valid image tag."; SELECTOR="imageTag=$REF" ;;
esac

ACCOUNT="$(account_id)"; REGISTRY="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
REPOSITORY="ap-agent-${ENVIRONMENT}/${NAME}"
DIGEST="$(aws ecr describe-images --repository-name "$REPOSITORY" --image-ids "$SELECTOR" --query 'imageDetails[0].imageDigest' --output text)" \
  || die "image $SELECTOR was not found in $REPOSITORY."
[[ "$DIGEST" =~ ^sha256:[a-f0-9]{64}$ ]] || die "image $SELECTOR was not found in $REPOSITORY."

KEY="$(echo "$NAME" | tr a-z A-Z)_IMAGE_URI"
FILE="$STATE_DIR/images.env"; TMP="$STATE_DIR/images.env.tmp"
[ -f "$FILE" ] || : > "$FILE"
cp "$FILE" "$FILE.bak"
{ grep -v "^${KEY}=" "$FILE" || true; printf '%s=%s/%s@%s\n' "$KEY" "$REGISTRY" "$REPOSITORY" "$DIGEST"; } > "$TMP"
mv "$TMP" "$FILE"
echo "Recorded $NAME at $DIGEST (previous file kept as images.env.bak)."
