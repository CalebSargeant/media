{{/*
Object labels: the object's own (`own`), then the app's (`app.labels`).
*/}}
{{- define "media.labels" -}}
{{- $labels := merge (dict) (.own | default dict) (.app.labels | default dict) -}}
{{- if $labels -}}
labels:
{{ toYaml $labels | indent 2 }}
{{- end -}}
{{- end -}}

{{/*
Pod template labels: `app: <name>` plus the app's podLabels.
*/}}
{{- define "media.podLabels" -}}
labels:
  app: {{ .name }}
{{- with .app.podLabels }}
{{ toYaml . | indent 2 }}
{{- end -}}
{{- end -}}

{{/*
PUID, PGID and TZ for the linuxserver.io images.
*/}}
{{- define "media.lsioEnv" -}}
- name: PUID
  value: {{ .root.Values.puid | quote }}
- name: PGID
  value: {{ .root.Values.pgid | quote }}
{{ include "media.tzEnv" . }}
{{- end -}}

{{- define "media.tzEnv" -}}
- name: TZ
  value: {{ .app.timezone | default .root.Values.timezone | quote }}
{{- end -}}

{{/*
nodeSelector, affinity and tolerations, when set.
*/}}
{{- define "media.placement" -}}
{{- with .nodeSelector }}
nodeSelector:
{{ toYaml . | indent 2 }}
{{- end }}
{{- with .affinity }}
affinity:
{{ toYaml . | indent 2 }}
{{- end }}
{{- with .tolerations }}
tolerations:
{{ toYaml . | indent 2 }}
{{- end }}
{{- end -}}

{{/*
A PVC, plus a static PV of the same name when the volume has a source.
Args: root, name, vol, owner (the owning app's values or the library block).
*/}}
{{- define "media.claim" -}}
{{- if .vol.source }}
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: {{ .name }}
  {{- include "media.labels" (dict "app" .owner) | nindent 2 }}
  annotations:
    helm.sh/resource-policy: keep
spec:
  accessModes:
    - {{ .vol.accessMode }}
  capacity:
    storage: {{ .vol.size }}
  {{- toYaml .vol.source | nindent 2 }}
  persistentVolumeReclaimPolicy: {{ .vol.reclaimPolicy | default "Retain" }}
  {{- with .vol.storageClass }}
  storageClassName: {{ . }}
  {{- end }}
{{- end }}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ .name }}
  namespace: {{ .root.Release.Namespace }}
  {{- include "media.labels" (dict "app" .owner) | nindent 2 }}
  annotations:
    helm.sh/resource-policy: keep
spec:
  accessModes:
    - {{ .vol.accessMode }}
  resources:
    requests:
      storage: {{ .vol.size }}
  {{- with .vol.storageClass }}
  storageClassName: {{ . }}
  {{- end }}
{{- end -}}

{{/*
The app's Ingress objects. Args: root, app, service, port.
*/}}
{{- define "media.ingresses" -}}
{{- $ctx := . -}}
{{- range .app.ingresses }}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ required "every ingress needs a name" .name }}
  namespace: {{ $ctx.root.Release.Namespace }}
  {{- with .annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- include "media.labels" (dict "own" .labels "app" $ctx.app) | nindent 2 }}
spec:
  {{- with .ingressClassName }}
  ingressClassName: {{ . }}
  {{- end }}
  rules:
    {{- range .rules }}
    - host: {{ .host }}
      http:
        paths:
          - backend:
              service:
                name: {{ .service | default $ctx.service }}
                port:
                  number: {{ .port | default $ctx.port }}
            path: {{ .path | default "/" }}
            pathType: {{ .pathType | default "Prefix" }}
    {{- end }}
  {{- with .tls }}
  tls:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}
{{- end -}}

{{/*
Init container that copies config.xml from the seed ConfigMap into /config and
writes the API key out for exportarr. Args: volume (the config volume name), name.
*/}}
{{- define "media.seedConfig" -}}
- command:
    - sh
    - -c
    - rm -f /config/config.xml && cp /seed/config.xml /config/config.xml && chmod 600 /config/config.xml && sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' /seed/config.xml > /exportarr/api-key
  image: {{ .root.Values.seedConfig.image }}
  name: seed-config
  resources:
    {{- toYaml .root.Values.seedConfig.resources | nindent 4 }}
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL
    runAsGroup: 1000
    runAsNonRoot: true
    runAsUser: 1000
    seccompProfile:
      type: RuntimeDefault
  volumeMounts:
    - mountPath: /config
      name: {{ .volume }}
    - mountPath: /seed
      name: {{ .name }}-config
      readOnly: true
    - mountPath: /exportarr
      name: exportarr-key
{{- end -}}

{{/*
exportarr sidecar. Args: root, name, port. TCP probes only, so a fault in the
exporter never marks the app NotReady.
*/}}
{{- define "media.exportarr" -}}
- args:
    - {{ .name }}
  env:
    - name: PORT
      value: "9707"
    - name: URL
      value: http://localhost:{{ .port }}
    - name: API_KEY_FILE
      value: /run/exportarr/api-key
  image: {{ .root.Values.exportarr.image }}
  livenessProbe:
    failureThreshold: 5
    periodSeconds: 60
    tcpSocket:
      port: metrics
  name: exportarr
  ports:
    - containerPort: 9707
      name: metrics
  readinessProbe:
    periodSeconds: 30
    tcpSocket:
      port: metrics
  resources:
    {{- toYaml .root.Values.exportarr.resources | nindent 4 }}
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL
    readOnlyRootFilesystem: true
    runAsGroup: 1000
    runAsNonRoot: true
    runAsUser: 1000
    seccompProfile:
      type: RuntimeDefault
  volumeMounts:
    - mountPath: /run/exportarr
      name: exportarr-key
      readOnly: true
{{- end -}}

{{/*
PodMonitor for the exportarr sidecar. Args: root, name, app.
*/}}
{{- define "media.podMonitor" -}}
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: {{ .name }}
  namespace: {{ .root.Release.Namespace }}
  {{- include "media.labels" (dict "app" .app) | nindent 2 }}
spec:
  podMetricsEndpoints:
    - interval: 2m
      port: metrics
      scrapeTimeout: 60s
  selector:
    matchLabels:
      app: {{ .name }}
{{- end -}}

{{/*
radarr, sonarr and prowlarr: Service, config claim, Deployment, PodMonitor and
Ingresses. Args: root, name, app, port, library (library volumes mounted at /<name>).
Liveness restarts a wedged app, which readiness never does; the startup probe
gives a database migration on upgrade 10 minutes first.
*/}}
{{- define "media.arr" -}}
{{- $ := .root -}}
{{- $app := .app -}}
{{- $name := .name -}}
{{- $port := .port -}}
{{- $seeded := $app.seedConfigMap -}}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ $name }}
  namespace: {{ $.Release.Namespace }}
  {{- include "media.labels" (dict "app" $app) | nindent 2 }}
spec:
  ports:
    - port: {{ $port }}
      protocol: TCP
      targetPort: {{ $port }}
  selector:
    app: {{ $name }}
  type: ClusterIP
{{ include "media.claim" (dict "root" $ "name" $app.persistence.claimName "vol" $app.persistence "owner" $app) }}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $name }}
  namespace: {{ $.Release.Namespace }}
  {{- include "media.labels" (dict "app" $app) | nindent 2 }}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {{ $name }}
  {{- with $app.strategy }}
  strategy:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  template:
    metadata:
      {{- include "media.podLabels" (dict "name" $name "app" $app) | nindent 6 }}
    spec:
      containers:
        - env:
            {{- include "media.lsioEnv" (dict "root" $ "app" $app) | nindent 12 }}
            {{- with $app.postgres.existingSecret }}
            - name: {{ upper $name }}__POSTGRES__PASSWORD
              valueFrom:
                secretKeyRef:
                  key: {{ $app.postgres.passwordKey }}
                  name: {{ . }}
            {{- end }}
          image: {{ $app.image }}
          {{- with $app.imagePullPolicy }}
          imagePullPolicy: {{ . }}
          {{- end }}
          livenessProbe:
            failureThreshold: 10
            httpGet:
              path: /ping
              port: {{ $port }}
            periodSeconds: 30
            timeoutSeconds: 10
          name: {{ $name }}
          ports:
            - containerPort: {{ $port }}
          readinessProbe:
            httpGet:
              path: /ping
              port: {{ $port }}
            initialDelaySeconds: 10
            periodSeconds: 10
          resources:
            {{- toYaml $app.resources | nindent 12 }}
          startupProbe:
            failureThreshold: 60
            httpGet:
              path: /ping
              port: {{ $port }}
            periodSeconds: 10
          volumeMounts:
            - mountPath: /config
              name: {{ $name }}
            {{- range .library }}
            - mountPath: /{{ . }}
              name: {{ . }}
            {{- end }}
        {{- if and $seeded $app.exportarr.enabled }}
        {{- include "media.exportarr" (dict "root" $ "name" $name "port" $port) | nindent 8 }}
        {{- end }}
      {{- if $seeded }}
      initContainers:
        {{- include "media.seedConfig" (dict "root" $ "name" $name "volume" $name) | nindent 8 }}
      {{- end }}
      {{- include "media.placement" $app | nindent 6 }}
      volumes:
        - name: {{ $name }}
          persistentVolumeClaim:
            claimName: {{ $app.persistence.claimName }}
        {{- with $seeded }}
        - configMap:
            name: {{ . }}
          name: {{ $name }}-config
        {{- end }}
        {{- range .library }}
        - name: {{ . }}
          persistentVolumeClaim:
            claimName: {{ . }}
        {{- end }}
        {{- if $seeded }}
        - emptyDir:
            medium: Memory
            sizeLimit: 1Mi
          name: exportarr-key
        {{- end }}
{{- if and $seeded $app.exportarr.enabled $app.podMonitor.enabled }}
---
{{ include "media.podMonitor" (dict "root" $ "name" $name "app" $app) }}
{{- end }}
{{ include "media.ingresses" (dict "root" $ "app" $app "service" $name "port" $port) }}
{{- end -}}
