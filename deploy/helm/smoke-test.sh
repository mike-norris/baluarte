#!/usr/bin/env bash
# Builds the images, installs the chart into a throwaway kind cluster, runs checks, then deletes the cluster.
# Requires docker, kind, helm and kubectl. Usage: deploy/helm/smoke-test.sh   (from any directory)
set -euo pipefail

cd "$(dirname "$0")/../.."
cluster=baluarte-smoke
release=baluarte
namespace=baluarte
tag=smoke
chart=deploy/helm/baluarte
kubectl=(kubectl --context "kind-$cluster" -n "$namespace")

fail() { echo "FAIL: $*" >&2; exit 1; }
cleanup() { kind delete cluster --name "$cluster" >/dev/null 2>&1 || true; }
trap cleanup EXIT

for values in "$chart"/ci/*-values.yaml; do
  helm lint --strict "$chart" -f "$values" >/dev/null
done
echo "ok  helm lint"

docker build -q -t "baluarte-app:$tag" backend >/dev/null
docker build -q -t "baluarte-web:$tag" frontend >/dev/null
echo "ok  images built"

kind create cluster --name "$cluster" --wait 120s
kind load docker-image --name "$cluster" "baluarte-app:$tag" "baluarte-web:$tag"

helm install "$release" "$chart" \
  --kube-context "kind-$cluster" \
  --namespace "$namespace" --create-namespace \
  -f "$chart/ci/dev-database-values.yaml" \
  --set api.image.tag="$tag" --set web.image.tag="$tag" \
  --set api.podDisruptionBudget.enabled=true --set web.podDisruptionBudget.enabled=true \
  --wait --timeout 300s
echo "ok  helm install"

"${kubectl[@]}" wait --for=condition=complete --timeout=120s job/"$release-migrate-1" >/dev/null \
  || fail "migration job did not complete"
echo "ok  migration job 1 completed"

helm test "$release" --kube-context "kind-$cluster" --namespace "$namespace" --logs

password=$("${kubectl[@]}" get secret "$release-postgresql" -o jsonpath='{.data.password}')

for component in api web; do
  pod=$("${kubectl[@]}" get pod -l "app.kubernetes.io/component=$component" -o jsonpath='{.items[0].metadata.name}')
  uid=$("${kubectl[@]}" exec "$pod" -- id -u)
  [[ "$uid" != "0" ]] || fail "$component runs as root"
  if "${kubectl[@]}" exec "$pod" -- touch /should-fail 2>/dev/null; then
    fail "$component root filesystem is writable"
  fi
  echo "ok  $component runs as uid $uid with a read-only root filesystem"
done

[[ $("${kubectl[@]}" get pdb -o name | wc -l) -eq 2 ]] || fail "expected 2 PodDisruptionBudgets"
echo "ok  PodDisruptionBudgets created"

helm upgrade "$release" "$chart" \
  --kube-context "kind-$cluster" --namespace "$namespace" \
  --reuse-values --set web.replicaCount=2 --wait --timeout 120s >/dev/null
[[ $("${kubectl[@]}" get deploy "$release-web" -o jsonpath='{.status.readyReplicas}') -eq 2 ]] \
  || fail "web did not scale to 2 ready replicas"
"${kubectl[@]}" wait --for=condition=complete --timeout=120s job/"$release-migrate-2" >/dev/null \
  || fail "upgrade migration job did not complete"
password_before=$password
password=$("${kubectl[@]}" get secret "$release-postgresql" -o jsonpath='{.data.password}')
[[ "$password" == "$password_before" ]] || fail "dev database password changed on upgrade"
echo "ok  helm upgrade (re-ran migrations, kept database password)"

helm uninstall "$release" --kube-context "kind-$cluster" --namespace "$namespace" --wait >/dev/null
[[ -z $("${kubectl[@]}" get deploy,sts,svc,cm,sa,secret,job -l "app.kubernetes.io/instance=$release" -o name) ]] \
  || fail "resources left after uninstall"
echo "ok  helm uninstall"

echo "Smoke test passed."
