from fastapi import FastAPI
from routes.webhook import router as webhook_router
from config import get_settings
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
import logging

logging.basicConfig(level=logging.INFO)
settings = get_settings()

app = FastAPI(
    title="WhatsApp Accounting Assistant",
    debug=settings.debug,
)

app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

app.include_router(webhook_router, prefix="/api/v1")

@app.get("/health")
async def health():
    return {"status": "ok"}