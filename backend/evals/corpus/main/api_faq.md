# Kestrel Track API FAQ

## Rate limits
The Kestrel Track API allows 600 requests per minute per API key. Requests above the limit receive HTTP 429 with a Retry-After header.

## Webhooks
Webhook deliveries that fail are retried 5 times with exponential backoff over 24 hours. Webhook payloads are signed with HMAC-SHA256.

## Authentication
API keys are created in the dashboard under Settings > Developers and can be revoked at any time.
