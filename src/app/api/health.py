from fastapi import APIRouter, Request
from sqlalchemy import text

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def readiness(request: Request) -> dict[str, str]:
    async with request.app.state.db_engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return {"status": "ready"}
