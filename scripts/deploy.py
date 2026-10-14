#!/usr/bin/env python3
"""Build and deploy the public sample app after the owner prepares a project.

Uses the caller's gcloud login. It never creates keys or uploads a .env file.
Firebase web configuration contains public identifiers, not admin credentials.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def run(*args, capture=False):
    result = subprocess.run(["gcloud", *args], cwd=ROOT, text=True,
                            stdout=subprocess.PIPE if capture else None, check=True)
    return result.stdout.strip() if capture else ""


def exists(*args):
    return subprocess.run(["gcloud", *args], cwd=ROOT, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def deploy_rules(project):
    # The OAuth token stays in memory and is sent only to Google's Rules API.
    token = run("auth", "print-access-token", "--project", project, capture=True)
    base = "https://firebaserules.googleapis.com/v1/projects/" + project

    def request(method, path, body):
        payload = json.dumps(body).encode()
        req = Request(base + path, data=payload, method=method,
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urlopen(req, timeout=30) as response:
            return json.load(response)

    ruleset = request("POST", "/rulesets", {"source": {"files": [
        {"name": "firestore.rules", "content": (ROOT / "firestore.rules").read_text()}
    ]}})
    release = {"name": f"projects/{project}/releases/cloud.firestore", "rulesetName": ruleset["name"]}
    try:
        request("PATCH", "/releases/cloud.firestore", {"release": release, "updateMask": "rulesetName"})
    except HTTPError as error:
        if error.code != 404:
            raise
        request("POST", "/releases", release)
    print("Firestore 브라우저 직접 접근 차단 규칙을 배포했습니다.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default="asia-northeast1")
    parser.add_argument("--service", default="shiftpatch")
    parser.add_argument("--firebase-config", type=Path,
                        help="JSON object with apiKey, authDomain, projectId and appId from Firebase Web app")
    parser.add_argument("--ai-mode", choices=["gemini", "calculation_only"], default="gemini")
    parser.add_argument("--model", default="gemini-3.8-flash")
    parser.add_argument("--plan", action="store_true", help="Print configuration without changing Cloud resources")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", args.project):
        parser.error("Use the Google Cloud project ID, not its display name or number.")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", args.service):
        parser.error("Invalid service name.")
    if not re.fullmatch(r"[a-z]+-[a-z]+[0-9]", args.region):
        parser.error("Invalid Google Cloud region.")
    firebase = {}
    if args.firebase_config:
        firebase = json.loads(args.firebase_config.read_text())
        if not all(isinstance(firebase.get(key), str) and firebase[key] for key in ["apiKey", "authDomain", "projectId", "appId"]):
            parser.error("Firebase config requires apiKey, authDomain, projectId and appId.")
        if firebase["projectId"] != args.project:
            parser.error("Firebase and Cloud Run must use the same project.")

    runtime_email = f"shiftpatch-runtime@{args.project}.iam.gserviceaccount.com"
    builder_email = f"shiftpatch-builder@{args.project}.iam.gserviceaccount.com"
    source_bucket = f"{args.project}-shiftpatch-build-source"
    image = f"{args.region}-docker.pkg.dev/{args.project}/shiftpatch/app:latest"
    environment = {
        "SHIFTPATCH_ENV": "production", "SHIFTPATCH_STORAGE": "firestore",
        "GOOGLE_CLOUD_PROJECT": args.project, "GOOGLE_CLOUD_LOCATION": "global",
        "SHIFTPATCH_AI_MODE": args.ai_mode, "SHIFTPATCH_GEMINI_MODEL": args.model,
        "SHIFTPATCH_GEMINI_PROVIDER": "vertex",
        "SHIFTPATCH_PUBLIC_ORIGIN": "https://localhost",
        "SHIFTPATCH_WEB_DIST": "/srv/web",
        "SHIFTPATCH_ALLOWED_HOSTS": "*.run.app", "SHIFTPATCH_COOKIE_SECURE": "true",
        "SHIFTPATCH_AI_STORE_DAILY_LIMIT": "10", "SHIFTPATCH_AI_GLOBAL_DAILY_LIMIT": "200",
        "SHIFTPATCH_AI_COOLDOWN_SECONDS": "60",
    }
    if firebase:
        environment.update({
            "SHIFTPATCH_FIREBASE_API_KEY": firebase["apiKey"],
            "SHIFTPATCH_FIREBASE_AUTH_DOMAIN": firebase["authDomain"],
            "SHIFTPATCH_FIREBASE_APP_ID": firebase["appId"],
        })
    if args.plan:
        print(json.dumps({"project": args.project, "region": args.region, "image": image,
                          "runtime_account": runtime_email, "builder_account": builder_email,
                          "firebase_login": bool(firebase), "environment": environment}, indent=2))
        return
    if not shutil.which("gcloud"):
        parser.error("Install the Google Cloud CLI and run gcloud auth login first.")

    run("projects", "describe", args.project, "--format=value(projectId)")
    run("services", "enable", "run.googleapis.com", "cloudbuild.googleapis.com",
        "artifactregistry.googleapis.com", "firestore.googleapis.com", "aiplatform.googleapis.com",
        "iam.googleapis.com", "identitytoolkit.googleapis.com", "storage.googleapis.com",
        "firebaserules.googleapis.com", "--project", args.project)
    if not exists("firestore", "databases", "describe", "--database=(default)", "--project", args.project):
        run("firestore", "databases", "create", "--database=(default)", "--location", args.region,
            "--type=firestore-native", "--project", args.project)
    deploy_rules(args.project)
    for name, email in [("shiftpatch-runtime", runtime_email), ("shiftpatch-builder", builder_email)]:
        if not exists("iam", "service-accounts", "describe", email, "--project", args.project):
            run("iam", "service-accounts", "create", name, "--project", args.project)
    runtime_roles = ["roles/datastore.user", "roles/aiplatform.user"]
    if firebase:
        runtime_roles.append("roles/firebaseauth.viewer")
    for role in runtime_roles:
        run("projects", "add-iam-policy-binding", args.project,
            "--member=serviceAccount:" + runtime_email, "--role=" + role, "--condition=None", "--quiet")
    run("projects", "add-iam-policy-binding", args.project,
        "--member=serviceAccount:" + builder_email, "--role=roles/logging.logWriter", "--condition=None", "--quiet")
    if not exists("artifacts", "repositories", "describe", "shiftpatch", "--location", args.region, "--project", args.project):
        run("artifacts", "repositories", "create", "shiftpatch", "--repository-format=docker",
            "--location", args.region, "--project", args.project)
    run("artifacts", "repositories", "add-iam-policy-binding", "shiftpatch", "--location", args.region,
        "--project", args.project, "--member=serviceAccount:" + builder_email, "--role=roles/artifactregistry.writer", "--quiet")
    if not exists("storage", "buckets", "describe", "gs://" + source_bucket, "--project", args.project):
        run("storage", "buckets", "create", "gs://" + source_bucket, "--location", args.region,
            "--uniform-bucket-level-access", "--project", args.project)
    run("storage", "buckets", "add-iam-policy-binding", "gs://" + source_bucket,
        "--member=serviceAccount:" + builder_email, "--role=roles/storage.objectViewer", "--quiet")
    run("builds", "submit", ".", "--project", args.project, "--region", args.region,
        "--config", "cloudbuild.yaml", "--substitutions=_IMAGE=" + image,
        "--service-account=projects/" + args.project + "/serviceAccounts/" + builder_email,
        "--gcs-source-staging-dir=gs://" + source_bucket + "/source", "--quiet")
    # Start privately, obtain Cloud Run's assigned URL, then allow that exact origin.
    with tempfile.TemporaryDirectory(prefix="shiftpatch-deploy-") as temp:
        env_path = Path(temp) / "environment.json"
        env_path.write_text(json.dumps(environment))
        run("run", "deploy", args.service, "--project", args.project, "--region", args.region,
            "--image", image, "--service-account", runtime_email, "--no-allow-unauthenticated",
            "--memory=512Mi", "--cpu=1", "--concurrency=16", "--max-instances=2", "--min-instances=0",
            "--timeout=60", "--env-vars-file", str(env_path), "--quiet")
        url = run("run", "services", "describe", args.service, "--project", args.project,
                  "--region", args.region, "--format=value(status.url)", capture=True)
        environment["SHIFTPATCH_PUBLIC_ORIGIN"] = url
        environment["SHIFTPATCH_ALLOWED_HOSTS"] = url.removeprefix("https://")
        env_path.write_text(json.dumps(environment))
        run("run", "services", "update", args.service, "--project", args.project,
            "--region", args.region, "--env-vars-file", str(env_path), "--quiet")
    run("run", "services", "add-iam-policy-binding", args.service, "--project", args.project,
        "--region", args.region, "--member=allUsers", "--role=roles/run.invoker", "--quiet")
    for collection in ["shiftpatch_sessions", "shiftpatch_quotas", "shiftpatch_stores", "shiftpatch_store_chunks"]:
        run("firestore", "fields", "ttls", "update", "expire_at", "--collection-group", collection,
            "--enable-ttl", "--project", args.project, "--quiet")
    print("\n공개 샘플 매장: " + url)
    if firebase:
        print("Firebase Authentication > Settings > Authorized domains에 추가: " + url.removeprefix("https://"))
    else:
        print("Google 로그인은 Firebase Web 설정을 전달하고 다시 배포하면 활성화됩니다.")
    print("브라우저 두 개에서 데이터 분리와 로그인, 실제 Gemini 실행 기록을 확인해 주세요.")


if __name__ == "__main__":
    main()
