# dataops with your data project in it, for docs/kubernetes/dataops.yaml:
#   docker build -f docs/kubernetes/Dockerfile --build-arg PROJECT=my-data -t registry.example.org/my-data-dataops:1 .
FROM ghcr.io/yibbzz/geoserver-dataops:latest
ARG PROJECT
COPY --chown=65534 ${PROJECT}/ /project/
