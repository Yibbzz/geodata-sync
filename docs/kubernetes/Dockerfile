# geodata with your data project in it, for docs/kubernetes/geodata.yaml:
#   docker build -f docs/kubernetes/Dockerfile --build-arg PROJECT=my-data -t registry.example.org/my-data-geodata:1 .
FROM ghcr.io/yibbzz/geodata-sync:latest
ARG PROJECT
COPY --chown=65534 ${PROJECT}/ /project/
