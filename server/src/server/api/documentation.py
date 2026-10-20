from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from starlette.responses import FileResponse

if TYPE_CHECKING:
    from pathlib import Path

DOCUMENTS = frozenset(
    {
        "README.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "docs/README.md",
        "docs/installation.md",
        "docs/authentication.md",
        "docs/usage.md",
        "docs/recipes.md",
        "docs/recovery.md",
        "docs/webhooks.md",
        "docs/plugins.md",
        "docs/events.md",
        "docs/development.md",
    }
)


def documentation_router(root: Path) -> APIRouter:
    router = APIRouter(prefix="/api/docs", include_in_schema=False)

    @router.get("/{file:path}")
    async def document(file: str) -> FileResponse:
        # Only published documentation is available, never arbitrary repository files.
        if file not in DOCUMENTS or not (path := root / file).is_file():
            raise HTTPException(404, "Document not found.")
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    return router
