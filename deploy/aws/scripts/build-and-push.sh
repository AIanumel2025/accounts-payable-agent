#!/usr/bin/env bash
# Deploys the ECR stack (idempotent), builds the four images and pushes them. Writes the image URIs (not secrets) to
# deploy/aws/.local/images.env. Needs CLERK_PUBLISHABLE_KEY (public) in the environment.
source "$(dirname "$0")/_common.sh"
need aws; need docker
: "${CLERK_PUBLISHABLE_KEY:?set CLERK_PUBLISHABLE_KEY to the Clerk PUBLISHABLE key (pk_...); it is public by design}"
case "$CLERK_PUBLISHABLE_KEY" in pk_test_*|pk_live_*) ;; *) die "CLERK_PUBLISHABLE_KEY must start with pk_test_ or pk_live_";; esac

ACCOUNT="$(account_id)"; REGISTRY="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
TAG="${IMAGE_TAG:-$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)}"

aws cloudformation deploy --stack-name "$ECR_STACK" --template-file "$DEPLOY_DIR/ecr.yaml" \
  --parameter-overrides "Environment=$ENVIRONMENT" --tags "${TAGS[@]}" --no-fail-on-empty-changeset

aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
echo "Logged in to the registry of account $(masked "$ACCOUNT")."

build_push() { # <name> <dockerfile> [extra docker build args...]
  local name="$1" dockerfile="$2"; shift 2
  local uri="$REGISTRY/ap-agent-${ENVIRONMENT}/${name}:${TAG}"
  echo "== building $name ($uri)"
  docker build -f "$REPO_ROOT/docker/$dockerfile" -t "$uri" "$@" "$REPO_ROOT"
  docker push "$uri" >/dev/null
  echo "pushed $name"
  printf '%s_IMAGE_URI=%s\n' "$(echo "$name" | tr a-z A-Z)" "$uri" >> "$STATE_DIR/images.env.new"
}

: > "$STATE_DIR/images.env.new"
build_push migrate migrate.lambda.Dockerfile
build_push api api.lambda.Dockerfile
build_push web web.lambda.Dockerfile --build-arg "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=$CLERK_PUBLISHABLE_KEY"
build_push worker worker.lambda.Dockerfile
mv "$STATE_DIR/images.env.new" "$STATE_DIR/images.env"
echo "Image URIs written to $STATE_DIR/images.env"
