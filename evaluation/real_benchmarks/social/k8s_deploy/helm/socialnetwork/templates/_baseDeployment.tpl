{{- define "socialnetwork.templates.baseDeployment" }}
apiVersion: apps/v1
kind: Deployment
metadata:
  labels:
    service: {{ .Values.name }}
  name: {{ .Values.name }}
spec: 
  replicas: {{ .Values.replicas | default .Values.global.replicas }}
  selector:
    matchLabels:
      service: {{ .Values.name }}
  template:
    metadata:
      labels:
        service: {{ .Values.name }}
        app: {{ .Values.name }}
        coz: "test"
    spec:
      {{- if hasKey .Values "nodeSelector" }}
      nodeSelector:
        {{ toYaml .Values.nodeSelector | nindent 8 | trim }}
      {{- else if hasKey $.Values.global "nodeSelector" }}
      nodeSelector:
        {{ toYaml $.Values.global.nodeSelector | nindent 8 | trim }}
      {{- end }}
      {{- if .Values.nodeName}}
      nodeName: {{ .Values.nodeName }}
      {{ end }}
      containers:
      {{- with .Values.container }}
      - name: "{{ .name }}"
        image: {{ .dockerRegistry | default $.Values.global.dockerRegistry }}/{{ .image }}:{{ .imageVersion | default $.Values.global.defaultImageVersion }}
        imagePullPolicy: {{ .imagePullPolicy | default $.Values.global.imagePullPolicy }}
        ports:
        {{- range $cport := .ports }}
        - containerPort: {{ $cport.containerPort -}}
        {{ end }} 
        {{- if .env }}
        env:
        {{- range $e := .env}}
        - name: {{ $e.name }}
          value: "{{ (tpl ($e.value | toString) $) }}"
        {{ end -}}
        {{ end -}}
        {{- if .command}}
        command: 
        - {{ .command }}
        {{- end -}}
        {{- if .args}}
        args:
        {{- range $arg := .args}}
        - {{ $arg }}
        {{- end -}}
        {{- end }}
        {{- if hasKey . "resources" }}  
        resources:
          {{ toYaml .resources | nindent 10 | trim }}
        {{- else if hasKey $.Values.global "resources" }}           
        resources:
          {{ toYaml $.Values.global.resources | nindent 10 | trim }}
        {{- end }}  
        {{- if or $.Values.configMaps .volumeMounts }}
        volumeMounts: 
        {{- if $.Values.configMaps }}
        {{- range $configMap := $.Values.configMaps }}
        - name: {{ $.Values.name }}-config
          mountPath: {{ $configMap.mountPath }}
          subPath: {{ $configMap.name }}
        {{- end }}
        {{- end }}
        {{- if .volumeMounts }}
        {{- range .volumeMounts }}
        - name: {{ .name }}
          mountPath: {{ .mountPath }}
          {{- if .subPath }}
          subPath: {{ .subPath }}
          {{- end }}
        {{- end }}
        {{- end }}
        {{- end }}
      {{- end -}}
      {{- if or $.Values.configMaps $.Values.volumes }}
      volumes:
      {{- if $.Values.configMaps }}
      - name: {{ $.Values.name }}-config
        configMap:
          name: {{ $.Values.name }}
      {{- end }}
      {{- if $.Values.volumes }}
      {{- range $.Values.volumes }}
      - name: {{ .name }}
        {{- if .emptyDir }}
        emptyDir:
          {{- if hasKey .emptyDir "medium" }}
          medium: {{ .emptyDir.medium }}
          {{- end }}
          {{- if hasKey .emptyDir "sizeLimit" }}
          sizeLimit: {{ .emptyDir.sizeLimit }}
          {{- end }}
        {{- else }}
        emptyDir: {}
        {{- end }}
      {{- end }}
      {{- end }}
      {{- end }}
      {{- if hasKey .Values "topologySpreadConstraints" }}
      topologySpreadConstraints:
        {{ tpl .Values.topologySpreadConstraints . | nindent 6 | trim }}
      {{- else if hasKey $.Values.global  "topologySpreadConstraints" }}
      topologySpreadConstraints:
        {{ tpl $.Values.global.topologySpreadConstraints . | nindent 6 | trim }}
      {{- end }}
      hostname: {{ $.Values.name }}
      restartPolicy: {{ .Values.restartPolicy | default .Values.global.restartPolicy}}

{{ include "socialnetwork.templates.baseHPA" . }}
{{- end}}
