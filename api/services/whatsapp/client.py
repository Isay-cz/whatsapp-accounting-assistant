from twilio.rest import Client
from config import settings

def send_message(to: str, body: str):
    """
    Sends a message through WhatsApp using Twilio.
    """
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        print(f"Twilio not configured. Would send to {to}: {body}")
        return None

    client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    message = client.messages.create(
        body=body,
        from_=settings.TWILIO_PHONE_NUMBER,
        to=to
    )
    return message.sid
