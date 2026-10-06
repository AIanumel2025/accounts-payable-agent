#!/usr/bin/env bash
# Deploys the ECR stack (idempotent), builds the images and pushes them. Writes the image URIs -- by immutable DIGEST -- (not
# secrets) to deploy/aws/.local/images.env. Needs CLERK_PUBLISHABLE_KEY (public) for the web image.
#   build-and-push.sh                 all four images (migrate also serves the dispatcher Lambda)
#   build-and-push.sh worker          only the OCR worker image (after an OCR/worker change)
#   build-and-push.sh migrate api     any subset of: migrate api web worker; the others keep their recorded URIs
source "$(dirname "$0")/_common.sh"
need aws; need docker
IMAGES=("$@"); [ "${#IMAGES[@]}" -gt 0 ] || IMAGES=(migrate api web worker)
for image in "${IMAGES[@]}"; do case "$image" in migrate|api|web|worker) ;; *) die "unknown image '$image' (migrate|api|web|worker)";; esac; done
case " ${IMAGES[*]} " in *" web "*)
  : "${CLERK_PUBLISHABLE_KEY:?set CLERK_PUBLISHABLE_KEY to the Clerk PUBLISHABLE key (pk_...); it is public by design}"
  case "$CLERK_PUBLISHABLE_KEY" in pk_test_*|pk_live_*) ;; *) die "CLERK_PUBLISHABLE_KEY must start with pk_test_ or pk_live_";; esac;;
esac

ACCOUNT="$(account_id)"; REGISTRY="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
TAG="${IMAGE_TAG:-$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)}"

aws cloudformation deploy --stack-name "$ECR_STACK" --template-file "$DEPLOY_DIR/ecr.yaml" \
  --parameter-overrides "Environment=$ENVIRONMENT" --tags "${TAGS[@]}" --no-fail-on-empty-changeset

aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
echo "Logged in to the registry of account $(masked "$ACCOUNT")."

[ -f "$STATE_DIR/images.env" ] && cp "$STATE_DIR/images.env" "$STATE_DIR/images.env.new" || : > "$STATE_DIR/images.env.new"

build_push() { # <name> <dockerfile> [extra docker build args...]
  local name="$1" dockerfile="$2"; shift 2
  local repository="$REGISTRY/ap-agent-${ENVIRONMENT}/${name}" key digest
  key="$(echo "$name" | tr a-z A-Z)_IMAGE_URI"
  echo "== building $name (${repository}:${TAG})"
  docker build -f "$REPO_ROOT/docker/$dockerfile" -t "${repository}:${TAG}" "$@" "$REPO_ROOT"
  docker push "${repository}:${TAG}" >/dev/null
  digest="$(aws ecr describe-images --repository-name "ap-agent-${ENVIRONMENT}/${name}" --image-ids "imageTag=${TAG}" --query 'imageDetails[0].imageDigest' --output text)"
  case "$digest" in sha256:*) ;; *) die "could not read the pushed digest of $name";; esac
  grep -v "^${key}=" "$STATE_DIR/images.env.new" > "$STATE_DIR/images.env.tmp" || true
  printf '%s=%s@%s\n' "$key" "$repository" "$digest" >> "$STATE_DIR/images.env.tmp"
  mv "$STATE_DIR/images.env.tmp" "$STATE_DIR/images.env.new"
  echo "pushed $name ($digest)"
}

for image in "${IMAGES[@]}"; do
  case "$image" in
    migrate) build_push migrate migrate.lambda.Dockerfile ;;
    api) build_push api api.lambda.Dockerfile ;;
    web) build_push web web.lambda.Dockerfile --build-arg "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=$CLERK_PUBLISHABLE_KEY" ;;
    worker) build_push worker worker.lambda.Dockerfile ;;
  esac
done
mv "$STATE_DIR/images.env.new" "$STATE_DIR/images.env"
echo "Image URIs (by digest) written to $STATE_DIR/images.env"
