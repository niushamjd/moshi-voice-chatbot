#!/usr/bin/env bash
# Delete the local kind cluster created by kind-up.sh.
set -euo pipefail

CLUSTER="${KIND_CLUSTER:-moshi}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if kind get clusters | grep -qx "$CLUSTER"; then
  kind delete cluster --name "$CLUSTER"
else
  echo "kind cluster '$CLUSTER' does not exist"
fi
rm -rf "$ROOT/.kind"
