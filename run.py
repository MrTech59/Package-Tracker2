"""Package Tracker entry point.

Run locally:  python run.py
Production:   uvicorn app.main:app --host 0.0.0.0 --port 8080
"""
import uvicorn

from app.config import settings

if __name__ == "__main__":
    settings.ensure_dirs()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )
