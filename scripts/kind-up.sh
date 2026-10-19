#!/usr/bin/env bash
# Build the image and deploy it to a local kind cluster. Safe to re-run: it
# reuses the cluster and only rolls out a new pod when the image changed.
#
#   ./scripts/kind-up.sh
#   kubectl -n moshi port-forward svc/moshi 8998:80   # then open http://localhost:8998
set -euo pipefail

CLUSTER="${KIND_CLUSTER:-moshi}"
IMAGE="moshi"
TIMEOUT="${ROLLOUT_TIMEOUT:-120s}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OVERLAY="$ROOT/.kind"

for tool in docker kind kubectl; do
  command -v "$tool" >/dev/null || { echo "error: $tool is not installed" >&2; exit 1; }
done

if kind get clusters | grep -qx "$CLUSTER"; then
  echo "==> kind cluster '$CLUSTER' already exists"
else
  echo "==> creating kind cluster '$CLUSTER'"
  kind create cluster --name "$CLUSTER" --wait 60s
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

echo "==> building image"
docker build -t "$IMAGE:dev" "$ROOT"
# Tag by content so an unchanged image means an unchanged Deployment (no
# rollout), and a rebuilt one always rolls out.
TAG="dev-$(docker image inspect -f '{{.Id}}' "$IMAGE:dev" | cut -d: -f2 | cut -c1-12)"
docker tag "$IMAGE:dev" "$IMAGE:$TAG"

echo "==> loading $IMAGE:$TAG into kind"
kind load docker-image --name "$CLUSTER" "$IMAGE:$TAG"

# A throwaway overlay (gitignored) that points k8s/ at the local image, so the
# checked-in manifests never change.
mkdir -p "$OVERLAY"
cat > "$OVERLAY/kustomization.yaml" <<YAML
resources:
  - ../k8s
images:
  - name: ghcr.io/niushamjd/moshi-voice-chatbot
    newName: $IMAGE
    newTag: $TAG
YAML

echo "==> applying manifests"
kubectl apply -k "$OVERLAY"
kubectl -n moshi rollout status deployment/moshi --timeout="$TIMEOUT"

echo
echo "Deployed $IMAGE:$TAG. To open the UI:"
echo "  kubectl -n moshi port-forward svc/moshi 8998:80"
echo "  then open http://localhost:8998"
