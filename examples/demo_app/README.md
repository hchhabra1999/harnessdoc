# Acme support bot (demo app — deliberately wasteful)

A small customer-support assistant for a fictional electronics shop, built the way many real LLM apps are:
an intent router (Anthropic), an order-ID extractor (OpenAI), a tool-using support agent (Anthropic) and a
nightly ticket-summary job (OpenAI), with one eval file.

It contains planted harness waste — see [WASTE.md](WASTE.md) for the answer key. harnessdoc's scanner is
benchmarked against it.

It is not meant to be deployed. Running it needs `anthropic` and `openai` installed and API keys; the scanner
never runs it (static analysis only).
