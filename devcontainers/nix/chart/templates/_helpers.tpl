{{/*
Validate required values. Called at the top of every template.
*/}}
{{- define "devcontainers.validate" -}}
{{- if not .Values.username -}}
  {{- fail "username is required: --set username=<openshift-username>" -}}
{{- end -}}
{{- $supported := list "nix" -}}
{{- if not (has .Values.track $supported) -}}
  {{- fail (printf "unsupported track %q -- must be one of: %s" .Values.track (join ", " $supported)) -}}
{{- end -}}
{{- end }}

{{/*
Deployment name: <username>-dev
*/}}
{{- define "devcontainers.name" -}}
{{- printf "%s-dev" .Values.username -}}
{{- end }}

{{/*
Namespace: always the username
*/}}
{{- define "devcontainers.namespace" -}}
{{- .Values.username -}}
{{- end }}

{{/*
Full container image reference
*/}}
{{- define "devcontainers.image" -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end }}

{{/*
Standard Kubernetes labels applied to all resources.
Follows the app.kubernetes.io labelling convention.
*/}}
{{- define "devcontainers.labels" -}}
app: {{ include "devcontainers.name" . }}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
devcontainers/track: {{ .Values.track }}
devcontainers/username: {{ .Values.username }}
{{- end }}

{{/*
Selector labels for deployment matchLabels and pod labels.
Must be immutable after creation -- keep minimal.
*/}}
{{- define "devcontainers.selectorLabels" -}}
app: {{ include "devcontainers.name" . }}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Home PVC claim name: uses existingClaim if set, otherwise home-<username>
*/}}
{{- define "devcontainers.homeClaim" -}}
{{- if .Values.storage.home.existingClaim -}}
{{- .Values.storage.home.existingClaim -}}
{{- else -}}
{{- printf "home-%s" .Values.username -}}
{{- end -}}
{{- end }}

{{/*
Nix cache PVC claim name: uses existingClaim if set, otherwise nix-store-<username>
*/}}
{{- define "devcontainers.nixCacheClaim" -}}
{{- if .Values.storage.nixCache.existingClaim -}}
{{- .Values.storage.nixCache.existingClaim -}}
{{- else -}}
{{- printf "nix-store-%s" .Values.username -}}
{{- end -}}
{{- end }}
