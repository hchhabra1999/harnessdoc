"""Classify an incoming message into a support intent."""

import anthropic

client = anthropic.Anthropic()

INTENTS = ["order_status", "return", "refund", "warranty", "product_question", "other"]


def classify_intent(message: str) -> str:
    response = client.messages.create(
        model="claude-opus-5-5",
        max_tokens=16000,
        messages=[
            {
                "role": "user",
                "content": (
                    "Classify this customer message into exactly one of these intents: "
                    f"{', '.join(INTENTS)}. Reply with the intent only.\n\nMessage: {message}"
                ),
            }
        ],
    )
    text = next((b.text for b in response.content if b.type == "text"), "other").strip()
    return text if text in INTENTS else "other"
