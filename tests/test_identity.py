"""Cloud identities for files sources on buckets (geoserver_dataops/sources/identity.py), against a fake of each
cloud's endpoint that answers as the real one is documented to. The buckets' listings come from fake_buckets."""

import base64
import http.server
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from geoserver_dataops.sources import Download, SourceError
from geoserver_dataops.sources import identity
from geoserver_dataops.sources.files import Files
from fake_buckets import AzureServer, S3Server

ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN",
       "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN",
       "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ENDPOINT_URL_STS",
       "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_FEDERATED_TOKEN_FILE", "AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_AUTHORITY_HOST")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """None of the machine's own cloud settings, nor a gcloud login: each test sets what it means to."""
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "no-gcloud"))


class Clouds:
    """The identity endpoints: AWS STS, the container and instance endpoints, Google's token endpoint and
    metadata server, Azure's token endpoint and instance metadata. Each records what it was asked."""

    def __init__(self):
        self.asked, self.public_key = [], None
        clouds = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self, status, body, kind="application/json"):
                body = body if isinstance(body, bytes) else (json.dumps(body) if isinstance(body, dict) else body).encode()
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def route(self, method):
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                form = {}
                if method == "POST":
                    form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
                clouds.asked.append((method, url.path, dict(self.headers), {**q, **form}))
                if url.path == "/sts" and q.get("Action") == "AssumeRoleWithWebIdentity":
                    if q.get("WebIdentityToken") != "pod-jwt":
                        return self.answer(403, "<Error/>", "text/xml")
                    return self.answer(200, '<AssumeRoleWithWebIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
                                            "<AssumeRoleWithWebIdentityResult><Credentials><AccessKeyId>key1</AccessKeyId>"
                                            "<SecretAccessKey>secret1</SecretAccessKey><SessionToken>session1</SessionToken>"
                                            "</Credentials></AssumeRoleWithWebIdentityResult></AssumeRoleWithWebIdentityResponse>", "text/xml")
                if url.path == "/container":
                    if self.headers.get("Authorization") != "pod-identity-token":
                        return self.answer(401, {})
                    return self.answer(200, {"AccessKeyId": "key1", "SecretAccessKey": "secret1", "Token": "session1"})
                if url.path == "/latest/api/token" and method == "PUT":
                    return self.answer(200, "imds-token", "text/plain")
                if url.path.startswith("/latest/meta-data/iam/security-credentials/"):
                    if self.headers.get("X-aws-ec2-metadata-token") != "imds-token":
                        return self.answer(401, "", "text/plain")
                    if url.path.endswith("/"):
                        return self.answer(200, "the-role\n", "text/plain")
                    return self.answer(200, {"AccessKeyId": "key1", "SecretAccessKey": "secret1", "Token": "session1"})
                if url.path == "/google/token":
                    return self.answer(200 if clouds.jwt_ok(form.get("assertion", "")) else 400, {"access_token": "google-token"})
                if url.path == "/google/refresh" and form.get("grant_type") == "refresh_token":
                    ok = (form.get("refresh_token"), form.get("client_id")) == ("refresh1", "gcloud-client")
                    return self.answer(200 if ok else 400, {"access_token": "user-token"})
                if url.path == "/oidc":  # as GitHub Actions hands out its OIDC token
                    ok = self.headers.get("Authorization") == "Bearer actions-request-token"
                    return self.answer(200 if ok else 401, {"value": "github-oidc-jwt"})
                if url.path == "/v1/token" and form.get("grant_type") == "urn:ietf:params:oauth:grant-type:token-exchange":
                    ok = form.get("subject_token") in ("github-oidc-jwt", "file-oidc-jwt") and form.get("audience", "").startswith("//iam.googleapis.com/")
                    return self.answer(200 if ok else 400, {"access_token": f"federated-{form.get('subject_token')}"})
                if url.path.endswith(":generateAccessToken"):
                    ok = self.headers.get("Authorization", "").startswith("Bearer federated-")
                    return self.answer(200 if ok else 403, {"accessToken": "impersonated-token"})
                if url.path == "/computeMetadata/v1/instance/service-accounts/default/token":
                    ok = self.headers.get("Metadata-Flavor") == "Google"
                    return self.answer(200 if ok else 403, {"access_token": "gke-token"})
                if url.path == "/tenant1/oauth2/v2.0/token":
                    ok = form.get("client_assertion") == "aks-jwt" and form.get("scope") == "https://storage.azure.com/.default"
                    return self.answer(200 if ok else 400, {"access_token": "aks-token"})
                if url.path == "/metadata/identity/oauth2/token":
                    ok = self.headers.get("Metadata") == "true" and q.get("resource") == "https://storage.azure.com/"
                    return self.answer(200 if ok else 400, {"access_token": "managed-token"})
                self.answer(404, {})

            def do_GET(self):
                self.route("GET")

            def do_POST(self):
                if self.headers.get("Content-Type", "").startswith("application/json"):  # impersonation's body
                    self.rfile.read(int(self.headers["Content-Length"]))
                    self.headers.replace_header("Content-Length", "0")
                self.route("POST")

            def do_PUT(self):
                self.route("PUT")

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def jwt_ok(self, assertion: str) -> bool:
        """The assertion is a JWT signed with the service account's private key (checked with its public one)."""
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        try:
            head, claims, signature = assertion.split(".")
            self.public_key.verify(base64.urlsafe_b64decode(signature + "=="), f"{head}.{claims}".encode(),
                                   padding.PKCS1v15(), hashes.SHA256())
        except (ValueError, InvalidSignature):
            return False
        body = json.loads(base64.urlsafe_b64decode(claims + "=="))
        return body["iss"] == "dataops@project.iam.gserviceaccount.com" and body["aud"].endswith("/google/token")

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def clouds(monkeypatch):
    c = Clouds()
    monkeypatch.setattr(identity, "AWS_IMDS", c.url)
    monkeypatch.setattr(identity, "AWS_CONTAINER", c.url)
    monkeypatch.setattr(identity, "GOOGLE_METADATA", c.url)
    monkeypatch.setattr(identity, "AZURE_IMDS", c.url)
    yield c
    c.close()


@pytest.fixture
def s3():
    s = S3Server(needs_key="key1")
    s.put("in/sites.geojson", b"{}")
    yield s
    s.close()


def bucket(s3, **options):
    return Files(url="s3://drop/in/", options={"endpoint": s3.url, **options})


def signed_with(s3, key: str, token: str) -> bool:
    last = parse_qs(urlparse(s3.requests[-1]).query)
    return last["X-Amz-Credential"][0].startswith(f"{key}/") and last["X-Amz-Security-Token"][0] == token


# --- AWS

def test_aws_keys_from_the_environment(s3, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key1")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret1")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session1")
    assert [p.id for p in bucket(s3).products().products] == ["sites.geojson"]
    assert signed_with(s3, "key1", "session1")


def test_irsa_the_pods_token_is_exchanged_at_sts(s3, clouds, monkeypatch, tmp_path):
    (tmp_path / "token").write_text("pod-jwt\n")
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123:role/dataops")
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", f"{clouds.url}/sts")
    assert [p.id for p in bucket(s3).products().products] == ["sites.geojson"]
    assert signed_with(s3, "key1", "session1")
    sts = [a for a in clouds.asked if a[1] == "/sts"][0][3]
    assert (sts["RoleArn"], sts["RoleSessionName"]) == ("arn:aws:iam::123:role/dataops", "geoserver-dataops")


def test_ecs_and_eks_pod_identity_ask_the_container_endpoint(s3, clouds, monkeypatch, tmp_path):
    (tmp_path / "token").write_text("pod-identity-token")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", f"{clouds.url}/container")
    monkeypatch.setenv("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", str(tmp_path / "token"))
    bucket(s3).products()
    assert signed_with(s3, "key1", "session1")


def test_an_ec2_instance_role_only_when_asked_for(s3, clouds):
    with pytest.raises(SourceError, match="no credentials"):  # not asked: anonymous, and refused
        bucket(s3).products()
    assert not clouds.asked  # and nothing was probed
    bucket(s3, identity="aws").products()
    assert signed_with(s3, "key1", "session1")
    assert [a[1] for a in clouds.asked][:2] == ["/latest/api/token", "/latest/meta-data/iam/security-credentials/"]


def test_keys_in_the_secret_win_over_any_identity(s3, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "someone-else")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    Files(url="s3://drop/in/", options={"endpoint": s3.url},
          credentials=lambda k: {"accessKeyId": "key1", "secretAccessKey": "secret1"}.get(k)).products()
    assert parse_qs(urlparse(s3.requests[-1]).query)["X-Amz-Credential"][0].startswith("key1/")


# --- Google

def test_a_google_service_account_key_signs_for_a_token_sent_as_a_header(clouds, monkeypatch, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    clouds.public_key = private.public_key()
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    (tmp_path / "key.json").write_text(json.dumps({"type": "service_account", "client_email": "dataops@project.iam.gserviceaccount.com",
                                                   "private_key": pem, "private_key_id": "k1", "token_uri": f"{clouds.url}/google/token"}))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "key.json"))
    client = Files(url="gs://maps/in/")
    assert client.session.headers["Authorization"] == "Bearer google-token"
    # the token travels in the header: addresses aren't signed
    assert client.download_url(Download(format="GeoJSON", url="", id="a.geojson")) == "https://storage.googleapis.com/maps/in/a.geojson"


def test_gke_workload_identity_from_the_metadata_server_when_asked_for(clouds):
    assert "Authorization" not in Files(url="gs://maps/in/").session.headers
    assert Files(url="gs://maps/in/", options={"identity": "google"}).session.headers["Authorization"] == "Bearer gke-token"


def test_a_gcloud_login_from_its_default_file(clouds, monkeypatch, tmp_path):
    """`gcloud auth application-default login` writes an authorized_user file where gcloud keeps its config."""
    config = tmp_path / "gcloud"
    config.mkdir()
    (config / "application_default_credentials.json").write_text(json.dumps({
        "type": "authorized_user", "client_id": "gcloud-client", "client_secret": "s", "refresh_token": "refresh1",
        "token_uri": f"{clouds.url}/google/refresh"}))
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(config))
    assert Files(url="gs://maps/in/").session.headers["Authorization"] == "Bearer user-token"


def federation(clouds, tmp_path, source: dict, impersonate: bool) -> str:
    info = {"type": "external_account", "audience": "//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/gh",
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt", "token_url": f"{clouds.url}/v1/token",
            "credential_source": source}
    if impersonate:
        info["service_account_impersonation_url"] = (f"{clouds.url}/v1/projects/-/serviceAccounts/"
                                                     "dataops@p.iam.gserviceaccount.com:generateAccessToken")
    (tmp_path / "federation.json").write_text(json.dumps(info))
    return str(tmp_path / "federation.json")


def test_workload_identity_federation_from_a_file_and_impersonation(clouds, monkeypatch, tmp_path):
    (tmp_path / "token").write_text("file-oidc-jwt\n")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", federation(clouds, tmp_path, {"file": str(tmp_path / "token")}, True))
    assert Files(url="gs://maps/in/").session.headers["Authorization"] == "Bearer impersonated-token"
    exchange = [a for a in clouds.asked if a[1] == "/v1/token"][0][3]
    assert exchange["scope"] == "https://www.googleapis.com/auth/cloud-platform"  # impersonation needs it


def test_workload_identity_federation_from_an_address_as_github_actions_gives_it(clouds, monkeypatch, tmp_path):
    source = {"url": f"{clouds.url}/oidc", "headers": {"Authorization": "Bearer actions-request-token"},
              "format": {"type": "json", "subject_token_field_name": "value"}}
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", federation(clouds, tmp_path, source, False))
    assert Files(url="gs://maps/in/").session.headers["Authorization"] == "Bearer federated-github-oidc-jwt"


def test_federation_from_aws_says_it_isnt_read(monkeypatch, tmp_path):
    (tmp_path / "f.json").write_text(json.dumps({"type": "external_account", "audience": "a", "subject_token_type": "t",
                                                 "credential_source": {"environment_id": "aws1", "region_url": "x"}}))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(tmp_path / "f.json"))
    with pytest.raises(SourceError, match="gets its token from AWS or a program"):
        Files(url="gs://maps/in/")


# --- Azure

@pytest.fixture
def azure():
    a = AzureServer()
    a.put("in/sites.geojson", b"{}")
    yield a
    a.close()


def test_aks_workload_identity(clouds, monkeypatch, tmp_path):
    (tmp_path / "token").write_text("aks-jwt")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("AZURE_CLIENT_ID", "client1")
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant1")
    monkeypatch.setenv("AZURE_AUTHORITY_HOST", clouds.url)
    client = Files(url="az://acct/maps/in/")
    assert client.session.headers["Authorization"] == "Bearer aks-token" and client.session.headers["x-ms-version"]


def test_an_azure_managed_identity_when_asked_for(clouds):
    assert "Authorization" not in Files(url="az://acct/maps/in/").session.headers
    assert Files(url="az://acct/maps/in/", options={"identity": "azure"}).session.headers["Authorization"] == "Bearer managed-token"


def test_an_identity_that_isnt_there_says_so(monkeypatch):
    monkeypatch.setattr(identity, "AWS_IMDS", "http://127.0.0.1:9")  # nothing listens: as off EC2
    monkeypatch.setattr(identity, "TIMEOUT", 2)
    with pytest.raises(SourceError, match="is this an EC2 instance"):
        Files(url="s3://drop/in/", options={"identity": "aws"})
