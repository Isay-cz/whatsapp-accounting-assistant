from fastapi import FastAPI
from api.routes import webhook
from api.config import settings

app = FastAPI(
    title="WhatsApp Accounting Assistant",
    description="Assistant to manage accounting via WhatsApp",
    version="0.1.0",
    debug=settings.DEBUG
)

# Include routers
app.include_router(webhook.router, prefix="/webhook", tags=["whatsapp"])

@app.get("/")
async def root():
    return {"message": "WhatsApp Accounting Assistant API is running"}
