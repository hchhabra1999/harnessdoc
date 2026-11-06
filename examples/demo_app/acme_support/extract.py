"""Pull the order ID out of a customer message."""

from openai import OpenAI

client = OpenAI()


def extract_order_id(message: str) -> str | None:
    completion = client.chat.completions.create(
        model="gpt-6.1-sol",
        messages=[
            {"role": "system", "content": "Extract the order ID (format ORD-123456) from the user's message. "
                                          "Reply with the ID only, or NONE."},
            {"role": "user", "content": message},
        ],
    )
    answer = (completion.choices[0].message.content or "").strip()
    return None if answer == "NONE" else answer
