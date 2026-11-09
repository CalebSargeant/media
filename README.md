# media

Helm chart for a self-hosted media stack: Plex, Overseerr, Sonarr, Radarr, Lidarr,
Bazarr, Prowlarr, NZBHydra2, FlareSolverr, qBittorrent, SABnzbd, Recyclarr and a
job that drops titles you can already stream.

## Install

The chart is published to GHCR as an OCI artifact:

```bash
helm install media oci://ghcr.io/calebsargeant/charts/media --version 1.0.0 \
  --namespace media --create-namespace -f my-values.yaml
```

Every app is off by default. Turn on what you run and supply the parts that belong
to your environment: hostnames, storage, node placement and secret names.
[values.yaml](charts/media/values.yaml) documents every key and
[ci/test-values.yaml](charts/media/ci/test-values.yaml) is a full example.

## Storage

Each app keeps its config on a PVC named by `persistence.claimName`. Set
`persistence.source` to a volume source (`hostPath`, `nfs`, ...) and the chart also
creates a static PV for it. The `library` block does the same for the shared
`movies`, `series`, `music` and `downloads` volumes.

Every PV and PVC carries `helm.sh/resource-policy: keep`, so uninstalling the
release never deletes media or app config.

## Secrets

The chart creates no Secrets. Point it at ones you manage:

| App | Value | Keys |
|---|---|---|
| radarr, sonarr, prowlarr | `postgres.existingSecret` | `password` (or `postgres.passwordKey`) |
| recyclarr | `existingSecret` | `SONARR_API_KEY`, `RADARR_API_KEY` |
| streamingSync | `existingSecret` | `TMDB_API_KEY`, `RADARR_API_KEY`, `SONARR_API_KEY`, optional `SLACK_BOT_TOKEN` |

`seedConfigMap` on radarr, sonarr and prowlarr names a ConfigMap holding a
`config.xml`, copied into `/config` on every start. The exportarr metrics sidecar
reads the app's API key from it.

## Releases

Conventional commits on `main` drive python-semantic-release, which tags a version
and creates a GitHub release. The release publishes the chart at that version.
