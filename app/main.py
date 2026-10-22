import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.db import SessionLocal
from app.engine.eta import DeliverySample, TimeAwareEtaModel
from app.models import DeliveryRecord, Warehouse
from app.routes import router
from app.schemas import ErrorOut, PlanOut
from app.services.errors import DomainError
from app.services.orders import expire_stale_orders, plan_snapshot

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60
STATIC_DIR = Path(__file__).parent / "static"


def load_eta_model() -> TimeAwareEtaModel:
    with SessionLocal() as session:
        rows = session.execute(
            select(
                DeliveryRecord.warehouse_id,
                Warehouse.distance_km,
                DeliveryRecord.day_of_week,
                DeliveryRecord.dispatch_time,
                DeliveryRecord.duration_minutes,
            ).join(Warehouse, DeliveryRecord.warehouse_id == Warehouse.id)
        ).all()
    return TimeAwareEtaModel.fit([DeliverySample(*row) for row in rows])


def sweep_expired() -> None:
    with SessionLocal() as session:
        if expired := expire_stale_orders(session, datetime.now(UTC)):
            logger.info("Marked %d order(s) as expired", expired)


async def sweep_forever() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await run_in_threadpool(sweep_expired)
        except Exception:
            # Correctness never depends on the sweeper; log and keep going.
            logger.exception("Expiry sweep failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.eta_model = await run_in_threadpool(load_eta_model)
    sweeper = asyncio.create_task(sweep_forever())
    yield
    sweeper.cancel()
    with suppress(asyncio.CancelledError):
        await sweeper


app = FastAPI(
    title="Medic QuickComm",
    version="0.1.0",
    description="Warehouse selection and order fulfilment for medical supply distribution.",
    lifespan=lifespan,
)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def ui() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.exception_handler(DomainError)
def domain_error(_: Request, exc: DomainError) -> JSONResponse:
    plan = PlanOut.model_validate(plan_snapshot(exc.plan)) if exc.plan else None
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorOut(detail=exc.message, plan=plan).model_dump(mode="json"),
    )
