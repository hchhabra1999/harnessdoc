"""Nightly job: summarise every closed ticket for the ops dashboard (nobody waits on this)."""

from openai import OpenAI

client = OpenAI()


def summarise_closed_tickets(tickets: list[dict]) -> list[str]:
    summaries = []
    for ticket in tickets:
        completion = client.chat.completions.create(
            model="gpt-6-astra",
            messages=[
                {"role": "system", "content": "Summarise this support ticket in two sentences for an ops dashboard."},
                {"role": "user", "content": ticket["transcript"]},
            ],
        )
        summaries.append(completion.choices[0].message.content)
    return summaries
