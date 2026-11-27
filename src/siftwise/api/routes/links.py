"""POST /links: per changed file, the tests selection links to it (``selector.file_links``:
the same code as ``/select``). For the MCP tool ``untested_changes``. "Linked" means imports,
paths, declared rules and fixture references, not line coverage. Nothing is stored."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from siftwise.api.deps import Caller, authorize
from siftwise.api.routes.select import js_imports, selector_config
from siftwise.core.schemas import FileLinkOut, LinksRequest, LinksResponse
from siftwise.core.selector import JavaBuildInfo, LinkStatus, file_links, load_history
from siftwise.db import get_session

router = APIRouter()

# The order files are listed in: what an agent should act on first.
_STATUS_ORDER = {s: n for n, s in enumerate(
    (LinkStatus.NO_LINKED_TEST, LinkStatus.LINKED, LinkStatus.TEST_FILE, LinkStatus.NOT_CHECKED)
)}  # fmt: skip


@router.post("/links", responses={404: {"description": "Unknown repo"}})
def post_links(
    body: LinksRequest, session: Annotated[Session, Depends(get_session)], caller: Caller
) -> LinksResponse:
    """For each changed file: the number of tests selection links to it, or that it has no
    linked test (a source file selection would run the full suite for), or why it isn't
    checked (docs, build files, fixtures, a language selection doesn't support). A read
    token is enough."""
    authorize(caller, body.repo)
    config = selector_config(body)
    history = load_history(session, body.repo, config)
    if history is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown repo {body.repo!r}")
    found = file_links(
        history,
        body.changed_files,
        body.changed_files_known,
        config,
        affected_packages=body.affected_packages,
        affected_files=body.affected_files,
        deleted_files=body.deleted_files,
        js_imports=js_imports(body),
        java=None if body.java is None else JavaBuildInfo.from_json(body.java.model_dump()),
        fixture_refs=body.fixture_refs,
    )
    found.sort(key=lambda f: (_STATUS_ORDER[f.status], f.path))
    counts = {s.value: 0 for s in _STATUS_ORDER}
    for f in found:
        counts[f.status.value] += 1
    return LinksResponse(
        repo=body.repo,
        files=[
            FileLinkOut(
                path=f.path,
                status=f.status.value,
                linked_tests=len(f.tests),
                reason_code=None if f.reason_code is None else f.reason_code.value,
                reason=f.reason,
            )
            for f in found
        ],
        counts=counts,
    )
