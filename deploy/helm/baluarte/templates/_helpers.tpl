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
{{- $sslMode := ternary "disable" .ctx.Values.database.sslMode .ctx.Values.devDatabase.enabled }}
{{- printf "postgresql://%s@%s:%v/%s?sslmode=%s" .user (include "baluarte.databaseHost" .ctx) .ctx.Values.database.port .ctx.Values.database.name $sslMode }}
{{- end }}

{{/* The API and worker connect as database.app.user when set (the default), not the owner. */}}
{{- define "baluarte.appRoleEnabled" -}}
{{- if .Values.database.app.user }}true{{ end }}
{{- end }}

{{- define "baluarte.appDatabaseSecretName" -}}
{{- if .Values.devDatabase.enabled }}
{{- include "baluarte.devDatabaseName" . }}
{{- else }}
{{- required "Set database.app.existingSecret to a Secret holding the app role's password (or database.app.user to \"\" to connect as the owner)" .Values.database.app.existingSecret }}
{{- end }}
{{- end }}

{{/* API and worker: env, volume mount and volume for database access. */}}
{{- define "baluarte.databaseEnv" -}}
- name: BALUARTE_DATABASE_URL
  value: {{ include "baluarte.databaseUrl" (dict "ctx" . "user" (ternary .Values.database.app.user .Values.database.user (eq (include "baluarte.appRoleEnabled" .) "true"))) | quote }}
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
{{- if include "baluarte.appRoleEnabled" . }}
    secretName: {{ include "baluarte.appDatabaseSecretName" . }}
    defaultMode: 0440
    items:
      - key: {{ ternary "app-password" .Values.database.app.passwordKey .Values.devDatabase.enabled }}
        path: password
{{- else }}
    secretName: {{ include "baluarte.databaseSecretName" . }}
    defaultMode: 0440
    items:
      - key: {{ ternary "password" .Values.database.passwordKey .Values.devDatabase.enabled }}
        path: password
{{- end }}
{{- end }}

{{/* Migrations: run as the schema owner, then set up the app role. */}}
{{- define "baluarte.migrateDatabaseEnv" -}}
- name: BALUARTE_DATABASE_URL
  value: {{ include "baluarte.databaseUrl" (dict "ctx" . "user" .Values.database.user) | quote }}
- name: BALUARTE_DATABASE_PASSWORD_FILE
  value: /var/run/secrets/baluarte/database-owner/password
{{- if include "baluarte.appRoleEnabled" . }}
- name: BALUARTE_APP_DATABASE_USER
  value: {{ .Values.database.app.user | quote }}
- name: BALUARTE_APP_DATABASE_PASSWORD_FILE
  value: /var/run/secrets/baluarte/database/password
{{- end }}
{{- end }}

{{- define "baluarte.migrateDatabaseVolumeMount" -}}
- name: database-owner-password
  mountPath: /var/run/secrets/baluarte/database-owner
  readOnly: true
{{- if include "baluarte.appRoleEnabled" . }}
{{ include "baluarte.databaseVolumeMount" . }}
{{- end }}
{{- end }}

{{- define "baluarte.migrateDatabaseVolume" -}}
- name: database-owner-password
  secret:
    secretName: {{ include "baluarte.databaseSecretName" . }}
    defaultMode: 0440
    items:
      - key: {{ ternary "password" .Values.database.passwordKey .Values.devDatabase.enabled }}
        path: password
{{- if include "baluarte.appRoleEnabled" . }}
{{ include "baluarte.databaseVolume" . }}
{{- end }}
{{- end }}

{{- define "baluarte.publicUrl" -}}
{{- if .Values.auth.publicUrl -}}
{{- .Values.auth.publicUrl -}}
{{- else if .Values.ingress.enabled -}}
https://{{ .Values.ingress.host }}
{{- end -}}
{{- end }}

{{- define "baluarte.authEnabled" -}}
{{- if .Values.auth.oidc.issuer }}true{{ end -}}
{{- end }}

{{- define "baluarte.authEnv" -}}
{{- if include "baluarte.authEnabled" . }}
{{- $oidc := .Values.auth.oidc }}
{{- $publicUrl := include "baluarte.publicUrl" . }}
{{- if not (hasPrefix "https://" $publicUrl) }}
{{- fail "auth.publicUrl must be an https origin (or enable ingress so it can be derived)" }}
{{- end }}
{{- if not (or $oidc.adminGroups $oidc.analystGroups $oidc.auditorGroups) }}
{{- fail "auth.oidc needs at least one of adminGroups, analystGroups or auditorGroups" }}
{{- end }}
- name: BALUARTE_PUBLIC_URL
  value: {{ $publicUrl | quote }}
- name: BALUARTE_OIDC_ISSUER
  value: {{ $oidc.issuer | quote }}
- name: BALUARTE_OIDC_CLIENT_ID
  value: {{ required "auth.oidc.clientId is required when auth.oidc.issuer is set" $oidc.clientId | quote }}
- name: BALUARTE_OIDC_SCOPES
  value: {{ $oidc.scopes | quote }}
- name: BALUARTE_OIDC_ROLES_CLAIM
  value: {{ $oidc.rolesClaim | quote }}
- name: BALUARTE_OIDC_ADMIN_GROUPS
  value: {{ join "," $oidc.adminGroups | quote }}
- name: BALUARTE_OIDC_ANALYST_GROUPS
  value: {{ join "," $oidc.analystGroups | quote }}
- name: BALUARTE_OIDC_AUDITOR_GROUPS
  value: {{ join "," $oidc.auditorGroups | quote }}
{{- with .Values.auth.session.idleMinutes }}
- name: BALUARTE_SESSION_IDLE_MINUTES
  value: {{ . | quote }}
{{- end }}
{{- with .Values.auth.session.lifetimeMinutes }}
- name: BALUARTE_SESSION_LIFETIME_MINUTES
  value: {{ . | quote }}
{{- end }}
{{- if $oidc.existingSecret }}
- name: BALUARTE_OIDC_CLIENT_SECRET_FILE
  value: /var/run/secrets/baluarte/oidc/client-secret
{{- end }}
{{- end }}
{{- end }}

{{- define "baluarte.authVolumeMount" -}}
{{- if and (include "baluarte.authEnabled" .) .Values.auth.oidc.existingSecret }}
- name: oidc-client-secret
  mountPath: /var/run/secrets/baluarte/oidc
  readOnly: true
{{- end }}
{{- end }}

{{- define "baluarte.authVolume" -}}
{{- if and (include "baluarte.authEnabled" .) .Values.auth.oidc.existingSecret }}
- name: oidc-client-secret
  secret:
    secretName: {{ .Values.auth.oidc.existingSecret }}
    defaultMode: 0440
    items:
      - key: {{ .Values.auth.oidc.clientSecretKey }}
        path: client-secret
{{- end }}
{{- end }}

{{- define "baluarte.loggingEnv" -}}
- name: BALUARTE_LOG_LEVEL
  value: {{ .Values.logging.level | quote }}
- name: BALUARTE_LOG_FORMAT
  value: {{ .Values.logging.format | quote }}
{{- end }}

{{- define "baluarte.gitlabEnv" -}}
{{- with .Values.gitlab }}
{{- if .url }}
{{- if not (hasPrefix "https://" .url) }}
{{- fail "gitlab.url must be an https URL" }}
{{- end }}
{{- if not .existingSecret }}
{{- fail "gitlab.existingSecret is required when gitlab.url is set" }}
{{- end }}
- name: BALUARTE_GITLAB_URL
  value: {{ .url | quote }}
- name: BALUARTE_GITLAB_TOKEN_FILE
  value: /var/run/secrets/baluarte/gitlab/token
- name: BALUARTE_GITLAB_TIMEOUT_SECONDS
  value: {{ .timeoutSeconds | quote }}
- name: BALUARTE_GITLAB_SYNC_INTERVAL_MINUTES
  value: {{ .syncIntervalMinutes | quote }}
{{- end }}
{{- end }}
{{- end }}

{{- define "baluarte.gitlabVolumeMount" -}}
{{- if .Values.gitlab.url }}
- name: gitlab-token
  mountPath: /var/run/secrets/baluarte/gitlab
  readOnly: true
{{- end }}
{{- end }}

{{- define "baluarte.gitlabVolume" -}}
{{- if .Values.gitlab.url }}
- name: gitlab-token
  secret:
    secretName: {{ .Values.gitlab.existingSecret }}
    defaultMode: 0440
    items:
      - key: {{ .Values.gitlab.tokenKey }}
        path: token
{{- end }}
{{- end }}

{{- define "baluarte.secretKeyName" -}}
{{- printf "%s-secret-key" (include "baluarte.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "baluarte.secretKeyEnv" -}}
- name: BALUARTE_SECRET_KEY_FILE
  value: /var/run/secrets/baluarte/secret-key/key
{{- end }}

{{- define "baluarte.secretKeyVolumeMount" -}}
- name: secret-key
  mountPath: /var/run/secrets/baluarte/secret-key
  readOnly: true
{{- end }}

{{- define "baluarte.secretKeyVolume" -}}
- name: secret-key
  secret:
    secretName: {{ .Values.secretKey.existingSecret | default (include "baluarte.secretKeyName" .) }}
    defaultMode: 0440
    items:
      - key: {{ .Values.secretKey.key }}
        path: key
{{- end }}
