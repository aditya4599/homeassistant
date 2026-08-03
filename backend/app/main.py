"""ASGI entrypoint: ``uvicorn app.main:app``."""

from app.core.config import get_settings
from app.factory import create_app

app = create_app()


if __name__ == "__main__":  # pragma: no cover - convenience runner
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_env == "development",
    )
