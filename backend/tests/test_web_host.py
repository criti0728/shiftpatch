from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.web_host import mount_web


def test_built_ui_and_api_share_one_origin_without_hiding_api_errors(tmp_path):
    (tmp_path / "index.html").write_text("<html>ShiftPatch</html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log('sample')")
    app = FastAPI()
    @app.get("/api/health")
    def health():
        return {"status": "ok"}
    mount_web(app, tmp_path)
    with TestClient(app) as client:
        assert client.get("/").text == "<html>ShiftPatch</html>"
        assert client.get("/demo/order").text == "<html>ShiftPatch</html>"
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/api/health").json() == {"status": "ok"}
        assert client.get("/api/missing").status_code == 404
        assert client.get("/assets/missing.js").status_code == 404


def test_web_root_does_not_expose_neighboring_files(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "index.html").write_text("sample")
    (tmp_path / "private.txt").write_text("not a web asset")
    app = FastAPI()
    mount_web(app, root)
    with TestClient(app) as client:
        response = client.get("/%2e%2e/private.txt")
        assert "not a web asset" not in response.text


def test_missing_web_build_fails_clearly(tmp_path):
    with pytest.raises(RuntimeError, match="index.html"):
        mount_web(FastAPI(), tmp_path)
