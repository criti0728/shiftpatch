"""Serve the built React application and API from one Cloud Run origin."""
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def mount_web(app, dist_path):
    if not dist_path:
        return
    root = Path(dist_path).resolve()
    if not (root / "index.html").is_file():
        raise RuntimeError("SHIFTPATCH_WEB_DIST must contain the built index.html")
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="web-assets")

    @app.get("/{path:path}", include_in_schema=False)
    def web(path: str):
        if path == "api" or path.startswith("api/") or path.startswith("assets/"):
            raise HTTPException(404, "Not found")
        requested = (root / path).resolve()
        if requested.is_relative_to(root) and requested.is_file():
            return FileResponse(requested)
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-store"})
