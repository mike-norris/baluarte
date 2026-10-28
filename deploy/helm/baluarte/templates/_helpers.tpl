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

{{- define "baluarte.devDatabaseName" -}}
{{- printf "%s-postgresql" (include "baluarte.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Database connection, validated. Fails rendering when no database is configured. */}}
{{- define "baluarte.databaseHost" -}}
{{- if .Values.devDatabase.enabled }}
{{- include "baluarte.devDatabaseName" . }}
{{- else }}
{{- required "Set database.host (external PostgreSQL) or devDatabase.enabled=true" .Values.database.host }}
{{- end }}
{{- end }}

{{- define "baluarte.databaseSecretName" -}}
{{- if .Values.devDatabase.enabled }}
{{- include "baluarte.devDatabaseName" . }}
{{- else }}
{{- required "Set database.existingSecret to a Secret holding the database password" .Values.database.existingSecret }}
{{- end }}
{{- end }}

{{- define "baluarte.databaseUrl" -}}
{{- $sslMode := ternary "disable" .Values.database.sslMode .Values.devDatabase.enabled }}
{{- printf "postgresql://%s@%s:%v/%s?sslmode=%s" .Values.database.user (include "baluarte.databaseHost" .) .Values.database.port .Values.database.name $sslMode }}
{{- end }}

{{/* Container env and volume mount for database access. */}}
{{- define "baluarte.databaseEnv" -}}
- name: BALUARTE_DATABASE_URL
  value: {{ include "baluarte.databaseUrl" . | quote }}
- name: BALUARTE_DATABASE_PASSWORD_FILE
  value: /var/run/secrets/baluarte/database/password
{{- end }}

{{- define "baluarte.databaseVolumeMount" -}}
- name: database-password
  mountPath: /var/run/secrets/baluarte/database
  readOnly: true
{{- end }}

{{- define "baluarte.databaseVolume" -}}
- name: database-password
  secret:
    secretName: {{ include "baluarte.databaseSecretName" . }}
    defaultMode: 0440
    items:
      - key: {{ ternary "password" .Values.database.passwordKey .Values.devDatabase.enabled }}
        path: password
{{- end }}
