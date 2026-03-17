{{- define "mediamicroservices.templates.baseConfigMap" }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Values.name }}
  labels:
    mediamicroservices/service: {{ .Values.name }}
data:
 {{- range $configMap := .Values.configMaps }}
  {{- $filePath := printf "configs/%s" $configMap.value }}
  {{- $fileContent := $.Files.Get $filePath }}
  {{- if and (eq $fileContent "") (eq $configMap.value "jaeger-config-media") }}
    {{- $fileContent = $.Files.Get "configs/jaeger-config" }}
  {{- end }}
  {{ $configMap.name -}}: |
{{- tpl $fileContent $ | indent 4 -}}
  {{- end }}

{{- end }}
