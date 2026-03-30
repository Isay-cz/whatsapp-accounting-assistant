from fastapi import APIRouter, Request, Depends, Form
from sqlalchemy.orm import Session
from api.database import get_db
from api.services.whatsapp.client import send_message
from api.services.nlp.extractor import extract_transaction_data

router = APIRouter()

@router.post("/")
async def whatsapp_webhook(
    Body: str = Form(...),
    From: str = Form(...),
    db: Session = Depends(get_db)
):
    """
    Endpoint for Twilio WhatsApp Webhook.
    Analyzes the incoming message and responds accordingly.
    """
    # 1. Extract data using NLP (Placeholder for Phase 2)
    transaction_data = extract_transaction_data(Body)
    
    # 2. Logic to save in DB or reply something
    response_msg = f"Recibido: {Body}. Datos extraídos: {transaction_data}"
    
    # 3. Send response back via WhatsApp
    send_message(From, response_msg)
    
    return {"status": "ok"}
