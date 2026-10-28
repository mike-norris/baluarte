# Baluarte Helm chart

Deploys the Baluarte API and web UI to Kubernetes 1.29+. The web pod serves the UI and proxies `/api/` to the API Service, so an Ingress only needs to route to the web Service.

This is the initial chart. PostgreSQL connection settings, the migration Job and the worker Deployment are added with the database (BAL-007) and job (BAL-009) work.

## Install

Images are not published yet; build them and push to a registry your cluster can pull from.

```sh
helm install baluarte deploy/helm/baluarte \
  --namespace baluarte --create-namespace \
  --set global.imageRegistry=registry.example.internal/baluarte \
  --set ingress.enabled=true --set ingress.host=baluarte.example.internal
helm test baluarte -n baluarte
```

## Key values

| Value | Default | Purpose |
|---|---|---|
| `global.imageRegistry` | `""` | Registry prefix for both images (private or air-gapped registries) |
| `api.image.tag`, `web.image.tag` | chart `appVersion` | Image tags |
| `api.replicaCount`, `web.replicaCount` | `1` | Replicas |
| `api.resources`, `web.resources` | small requests, memory limits | Resource requests and limits |
| `api.podDisruptionBudget.enabled`, `web.podDisruptionBudget.enabled` | `false` | PodDisruptionBudgets (enable with 2+ replicas) |
| `ingress.enabled`, `ingress.className`, `ingress.host`, `ingress.tls` | disabled | Ingress to the web Service |
| `imagePullSecrets` | `[]` | Registry credentials |

## Security defaults

Pods run as non-root numeric UIDs with the `RuntimeDefault` seccomp profile, a read-only root filesystem, no privilege escalation, all capabilities dropped and only `/tmp` writable. Service account tokens are not mounted, and the chart creates no RBAC because Baluarte does not call the Kubernetes API.

## Smoke test

`deploy/helm/smoke-test.sh` lints the chart, builds the images, installs into a throwaway kind cluster, runs `helm test` and security checks, upgrades, uninstalls and deletes the cluster. Requires Docker, kind, Helm and kubectl.
