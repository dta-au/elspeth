"""LLM provider implementations.

Each provider wraps a specific transport (Azure SDK, OpenRouter HTTP) and
normalizes responses into LLMQueryResult. Providers own client lifecycle,
Tier 3 validation, and audit recording via their Audited*Client.
"""
