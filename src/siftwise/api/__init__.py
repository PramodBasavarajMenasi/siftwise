"""The siftwise API server (FastAPI). Needs the server extra."""

from siftwise import extras

_missing = extras.missing(extras.SERVER_MODULES)
if _missing:
    raise ImportError(
        f"the siftwise API server needs the server dependencies (missing: {', '.join(_missing)}):"
        f" {extras.install_hint('server')}"
    )
