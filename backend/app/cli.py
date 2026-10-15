"""CLI-only text output setup; importing the API never alters process streams."""
import sys


def configure_console():
    # Windows redirected output commonly defaults to cp949, which cannot print
    # all Japanese/Korean diagnostics. Keep reports and console output in UTF-8.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="backslashreplace")
