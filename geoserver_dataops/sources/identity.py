"""Credentials from where dataops runs, for files sources on cloud buckets: the cloud's own identity for the
pod or machine (a role, a managed identity) instead of keys kept in a Secret. Each is plain HTTP to the
cloud's documented endpoint, with no cloud library.

What is found without being asked for (cheap: environment variables and files the platform sets):
    AWS     AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (and AWS_SESSION_TOKEN); IRSA on EKS
            (AWS_WEB_IDENTITY_TOKEN_FILE + AWS_ROLE_ARN, exchanged at STS); ECS tasks and EKS Pod Identity
            (AWS_CONTAINER_CREDENTIALS_FULL_URI or _RELATIVE_URI)
    Google  GOOGLE_APPLICATION_CREDENTIALS naming a service account's key file
    Azure   AKS workload identity (AZURE_FEDERATED_TOKEN_FILE, AZURE_CLIENT_ID, AZURE_TENANT_ID)
And only with the Source's `options.identity` (asked for, as finding out elsewhere means waiting on a timeout):
    aws     the EC2 instance's role (IMDSv2)
    google  the metadata server: GKE Workload Identity, a Compute Engine VM's service account
    azure   a managed identity (Azure's instance metadata endpoint)
"""

from __future__ import annotations

import base64
import json
import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import requests

from . import SourceError

TIMEOUT = 10
AWS_IMDS = "http://169.254.169.254"
AWS_CONTAINER = "http://169.254.170.2"
GOOGLE_METADATA = "http://metadata.google.internal"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"
AZURE_IMDS = "http://169.254.169.254"
AZURE_STORAGE = "https://storage.azure.com/"


@dataclass
class AwsKeys:
    key: str
    secret: str
    token: str | None = None
    where: str = ""  # which identity, for messages


def _get(session, url: str, what: str, **kwargs) -> requests.Response:
    try:
        r = session.request(kwargs.pop("method", "GET"), url, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as e:
        raise SourceError(f"can't get {what}: {e}") from None
    if r.status_code != 200:
        raise SourceError(f"can't get {what}: HTTP {r.status_code} {' '.join(r.text.split())[:200]}")
    return r


# --- AWS

def aws_keys(session: requests.Session, region: str | None, identity: str | None) -> AwsKeys | None:
    env = os.environ
    if env.get("AWS_ACCESS_KEY_ID") and env.get("AWS_SECRET_ACCESS_KEY"):
        return AwsKeys(env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"], env.get("AWS_SESSION_TOKEN"), "environment")
    if env.get("AWS_WEB_IDENTITY_TOKEN_FILE") and env.get("AWS_ROLE_ARN"):
        return aws_web_identity(session, region)
    if env.get("AWS_CONTAINER_CREDENTIALS_FULL_URI") or env.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"):
        return aws_container(session)
    if identity == "aws":
        return aws_instance(session)
    return None


def aws_web_identity(session, region: str | None) -> AwsKeys:
    """IRSA: the pod's service account token exchanged at STS for the role's keys (no signature needed)."""
    token = Path(os.environ["AWS_WEB_IDENTITY_TOKEN_FILE"]).read_text().strip()
    region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    sts = os.environ.get("AWS_ENDPOINT_URL_STS") or (f"https://sts.{region}.amazonaws.com" if region else "https://sts.amazonaws.com")
    r = _get(session, sts, "keys for the pod's role from AWS STS", params={
        "Action": "AssumeRoleWithWebIdentity", "Version": "2011-06-15", "RoleArn": os.environ["AWS_ROLE_ARN"],
        "RoleSessionName": os.environ.get("AWS_ROLE_SESSION_NAME") or "geoserver-dataops", "WebIdentityToken": token})
    root = ET.fromstring(r.content)
    found = {e.tag.split("}")[-1]: e.text for e in root.iter() if e.tag.split("}")[-1] in ("AccessKeyId", "SecretAccessKey", "SessionToken")}
    if len(found) < 3:
        raise SourceError("AWS STS gave no keys for the pod's role")
    return AwsKeys(found["AccessKeyId"], found["SecretAccessKey"], found["SessionToken"], f"role {os.environ['AWS_ROLE_ARN']}")


def aws_container(session) -> AwsKeys:
    """ECS tasks and EKS Pod Identity: an endpoint the platform gives the container."""
    env = os.environ
    url = env.get("AWS_CONTAINER_CREDENTIALS_FULL_URI") or AWS_CONTAINER + env["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"]
    token = env.get("AWS_CONTAINER_AUTHORIZATION_TOKEN")
    if env.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"):
        token = Path(env["AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"]).read_text().strip()
    data = _get(session, url, "the container's AWS keys", headers={"Authorization": token} if token else {}).json()
    return AwsKeys(data["AccessKeyId"], data["SecretAccessKey"], data.get("Token"), "the container's role")


def aws_instance(session) -> AwsKeys:
    """An EC2 instance's role, through IMDSv2 (a session token first)."""
    token = _get(session, f"{AWS_IMDS}/latest/api/token", "an EC2 metadata token (is this an EC2 instance?)", method="PUT",
                 headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"}).text
    headers = {"X-aws-ec2-metadata-token": token}
    base = f"{AWS_IMDS}/latest/meta-data/iam/security-credentials/"
    role = _get(session, base, "the EC2 instance's role (has it one?)", headers=headers).text.split()[0]
    data = _get(session, base + role, "the EC2 instance role's keys", headers=headers).json()
    return AwsKeys(data["AccessKeyId"], data["SecretAccessKey"], data.get("Token"), f"instance role {role}")


# --- Google

def google_token(session, identity: str | None) -> str | None:
    key_file = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if key_file:
        return google_service_account(session, json.loads(Path(key_file).read_text()))
    if identity == "google":
        r = _get(session, f"{GOOGLE_METADATA}/computeMetadata/v1/instance/service-accounts/default/token",
                 "a token from Google's metadata server (is this GKE or Compute Engine?)", headers={"Metadata-Flavor": "Google"})
        return r.json()["access_token"]
    return None


def google_service_account(session, key: dict) -> str:
    """A service account's key file: a JWT signed with its private key, exchanged for an access token."""
    if key.get("type") != "service_account":
        raise SourceError(f"GOOGLE_APPLICATION_CREDENTIALS is a {key.get('type')!r} file; dataops reads a service account's key")
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        raise SourceError("a Google service account key needs cryptography: pip install 'geoserver-dataops[google]' "
                          "(the image has it)") from None

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    now = int(time.time())
    token_uri = key.get("token_uri") or GOOGLE_TOKEN
    head = b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": key.get("private_key_id", "")}).encode())
    claims = b64(json.dumps({"iss": key["client_email"], "scope": GOOGLE_SCOPE, "aud": token_uri,
                             "iat": now, "exp": now + 3600}).encode())
    private = serialization.load_pem_private_key(key["private_key"].encode(), password=None)
    signature = private.sign(f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    r = _get(session, token_uri, f"a token for {key['client_email']}", method="POST",
             data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": f"{head}.{claims}.{b64(signature)}"})
    return r.json()["access_token"]


# --- Azure

def azure_token(session, identity: str | None) -> str | None:
    env = os.environ
    if env.get("AZURE_FEDERATED_TOKEN_FILE") and env.get("AZURE_CLIENT_ID") and env.get("AZURE_TENANT_ID"):
        authority = (env.get("AZURE_AUTHORITY_HOST") or "https://login.microsoftonline.com/").rstrip("/")
        r = _get(session, f"{authority}/{env['AZURE_TENANT_ID']}/oauth2/v2.0/token", "a token for the pod's workload identity",
                 method="POST", data={
                     "grant_type": "client_credentials", "client_id": env["AZURE_CLIENT_ID"], "scope": AZURE_STORAGE + ".default",
                     "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                     "client_assertion": Path(env["AZURE_FEDERATED_TOKEN_FILE"]).read_text().strip()})
        return r.json()["access_token"]
    if identity == "azure":
        params = {"api-version": "2018-02-01", "resource": AZURE_STORAGE}
        if env.get("AZURE_CLIENT_ID"):  # a user-assigned identity
            params["client_id"] = env["AZURE_CLIENT_ID"]
        r = _get(session, f"{AZURE_IMDS}/metadata/identity/oauth2/token", "a managed identity's token (has this VM one?)",
                 params=params, headers={"Metadata": "true"})
        return r.json()["access_token"]
    return None
