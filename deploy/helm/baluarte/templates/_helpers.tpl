{{- define "baluarte.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "baluarte.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "baluarte.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "baluarte.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Usage: include "baluarte.selectorLabels" (dict "ctx" . "component" "api") */}}
{{- define "baluarte.selectorLabels" -}}
app.kubernetes.io/name: {{ include "baluarte.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "baluarte.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "baluarte.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Usage: include "baluarte.image" (dict "ctx" . "image" .Values.api.image) */}}
{{- define "baluarte.image" -}}
{{- $tag := default .ctx.Chart.AppVersion .image.tag }}
{{- if .ctx.Values.global.imageRegistry }}
{{- printf "%s/%s:%s" .ctx.Values.global.imageRegistry .image.repository $tag }}
{{- else }}
{{- printf "%s:%s" .image.repository $tag }}
{{- end }}
{{- end }}

{{- define "baluarte.apiName" -}}
{{- printf "%s-api" (include "baluarte.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "baluarte.webName" -}}
{{- printf "%s-web" (include "baluarte.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
