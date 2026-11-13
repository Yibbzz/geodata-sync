# geodata in Kubernetes

[`kubernetes/geodata.yaml`](kubernetes/geodata.yaml) is a CronJob that runs `geodata sync` nightly: each Dataset whose `update` says it's due is checked, and loaded when there's a new version. Its second container runs [`configops refresh`](https://github.com/Yibbzz/geoserver-configops) on what was loaded, so GeoServer's layers on new data get their new bounds and lose their old cached tiles; leave it out if nothing publishes the tables.

## 1. An image with your project in it

[`kubernetes/Dockerfile`](kubernetes/Dockerfile) copies a project folder into the geodata image, so every run uses the same files and a new version is a new tag:

```bash
docker build -f docs/kubernetes/Dockerfile --build-arg PROJECT=my-data -t registry.example.org/my-data-geodata:1 .
docker push registry.example.org/my-data-geodata:1
```

Leave `secrets.yaml` out of the image: logins come from Secrets. The refresh container uses your configops project's image, built the same way (configops's `docs/kubernetes.md`).

## 2. Secrets

Each Secret the project names is mounted as a folder under `/etc/geodata/secrets/<secret>`, one file per key:

```bash
kubectl create secret generic database --from-literal=host=... --from-literal=port=5432 \
  --from-literal=database=... --from-literal=username=... --from-literal=password=...
```

The hosts in them are as the pod reaches them: `<service>.<namespace>` for a database in another namespace. An OS Data Hub key or a CKAN token (`credentials: { secret: os-api }`) is another Secret, mounted the same way.

## 3. Run it

```bash
kubectl apply -f geodata.yaml
kubectl create job sync-now --from=cronjob/geodata-sync     # now, rather than tonight
kubectl logs job/sync-now -c sync                            # what was checked and loaded
kubectl logs job/sync-now -c refresh                         # what GeoServer refreshed
```

A load of `--force <dataset>` (the same version again) is a Job with the sync container's command changed.

## Rasters

A raster Dataset writes its GeoTIFF under `/data`: mount the volume GeoServer's services read their files from there, in the sync container (`ReadWriteMany` if the pods can run on different nodes).
