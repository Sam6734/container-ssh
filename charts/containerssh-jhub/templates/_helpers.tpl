{{- define "containerssh-jhub.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "containerssh-jhub.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "containerssh-jhub.namespace" -}}
{{- default .Release.Namespace .Values.jupyterhub.userNamespace }}
{{- end }}

{{- define "containerssh-jhub.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: {{ include "containerssh-jhub.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "containerssh-jhub.selectorLabels" -}}
app.kubernetes.io/name: {{ include "containerssh-jhub.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
