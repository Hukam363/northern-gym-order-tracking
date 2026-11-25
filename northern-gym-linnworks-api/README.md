[README.md](https://github.com/user-attachments/files/35411258/README.md)
# Northern Gym — Linnworks lookup API for Zendesk

**Purpose:** Zendesk AI agents / actions call one endpoint using either a channel reference or a Linnworks order ID. The customer does **not** have to supply an email address or complete an OTP check. Reference-only lookups return limited dispatch status; tracking details require a matching email supplied by trusted Zendesk context. **AfterShip is optional and is not integrated in this version.**

## Deploy on GitHub + Render

1. For the existing Northern Gym repository, replace `northern-gym-linnworks-api/main.py` and optionally `README.md` and `tests/test_lookup.py` within that same folder. Do not upload `.env` or secrets.
2. Your existing Render Blueprint already points to `northern-gym-linnworks-api/render.yaml`, which sets `rootDir: northern-gym-linnworks-api`. No new Render service or environment variable is required. Deploy the updated GitHub commit.
3. Set environment variables in the Render dashboard (or at Blueprint creation):
   - `LINNWORKS_APP_ID` — Linnworks developer app ID.
   - `LINNWORKS_APP_SECRET` — Linnworks developer app secret.
   - `LINNWORKS_PERMANENT_TOKEN` — token issued when the app is installed in Northern Gym's Linnworks account; **not** a temporary session token.
   - `API_BEARER_TOKEN` — a strong random shared secret known to Zendesk (not a Linnworks token).
4. Verify `GET https://YOUR-RENDER-URL/health` returns `{"status":"ok"...}`.
5. Configure a Zendesk HTTP action/connection to send `POST https://YOUR-RENDER-URL/v1/orders/lookup`, with headers `Authorization: Bearer YOUR_API_BEARER_TOKEN` and `Content-Type: application/json`.
6. Test with a real channel reference **without an email**, then with a real Linnworks order number using `order_id`. Confirm that only a limited status is returned. Do not treat the health endpoint as proof that every Linnworks search type works.

For a Linnworks system integration app: obtain installation token through the developer portal's installation URL, then the API uses `Auth/AuthorizeByApplication` to generate temporary session tokens. See [Linnworks documentation](https://apidocs.linnworks.net/docs/system-integration-setup).

## Zendesk action JSON

Reference-only request (no email prompt):
```json
{"channel_reference":"NG-12345"}
```

Alternatively, if the customer knows the Linnworks order ID or order number:
```json
{"order_id":"12345"}
```

For trusted Zendesk tickets where Zendesk already knows and can supply the requester email:
```json
{"channel_reference":"NG-12345","requester_email":"customer@example.com"}
```

Reference-only response for a dispatched order (no tracking or personal details):
```json
{
  "found": true,
  "action": "answer_customer",
  "reason": "reference_only_status",
  "channel_reference": "NG-12345",
  "order": {"status": "dispatched"},
  "customer_message": "The order linked to that reference is marked as dispatched. Please check the dispatch email for your parcel tracking details."
}
```


Response on a dispatched order where Zendesk also supplied a matching trusted requester email:
```json
{
  "found": true,
  "action": "answer_customer",
  "reason": null,
  "channel_reference": "NG-12345",
  "order": {
    "linnworks_order_id": "uuid-from-linnworks",
    "linnworks_order_number": 12345,
    "channel_reference": "NG-12345",
    "source": "SHOPIFY",
    "status": "dispatched",
    "processed": true,
    "processed_at": "2026-11-23T10:00:00Z",
    "carrier": "Royal Mail",
    "postal_service": "Tracked",
    "tracking_number": "RM123",
    "tracking_url": null,
    "received_at": "2026-11-22T10:00:00Z",
    "items": [{"sku":"BENCH-1","name":"Gym bench","quantity":1}]
  },
  "customer_message": "Your order NG-12345 has been dispatched. Tracking number: RM123."
}
```

**Zendesk branching:** If `action = answer_customer`, use `customer_message`; otherwise escalate to an agent. For reference-only calls, only `order.status` is returned. Detailed fields like courier, tracking, items and Linnworks order ID are returned only for verified email matches. Ticket-field updates still require separate Zendesk configuration; this API does **not** write directly to Zendesk.

## Behaviour and limits

- Searches both open and processed orders with `OpenOrders/SearchOrders` (`IncludeProcessed=true`), then loads details using `Orders/GetOrdersById`.
- Accepts either `channel_reference` or `order_id`. Confirms exact matches against channel reference fields, Linnworks `OrderId`, and `NumOrderId` in returned search results. Live Linnworks search coverage for each identifier must be tested.
- With no email, returns only a limited status. A matched dispatched order with tracking can receive a generic automatic response, but **no tracking number, courier, order contents, or personally identifying information is returned**. Other statuses escalate.
- When Zendesk supplies an email, matching is still required before detailed tracking is returned. A mismatched email escalates; it does not fall back to disclosing detailed fields. No customer OTP step is required.
- **Security:** Knowing an order ID is not proof of identity. A status-only response still discloses that an order reference exists; add Zendesk rate limits / abuse controls and prevent anonymous bulk enquiries. Do not expose this API bearer token to browsers or customers. For complete tracking details, use trusted Zendesk customer context. Matching a reference and email is a minimal check, not strong identity verification.
- Does not invent tracking links. For reference-only lookups, no tracking number or URL is exposed. Verified lookups may return the Linnworks tracking number; `tracking_url` remains `null` until a reliable source is configured.
- Can respond automatically only to a matched dispatched order with a tracking number, but reference-only replies are status-only. Orders on hold, cancelled, or without confirmed tracking escalate.
- Missing orders, multiple matches, unexpected Linnworks responses and Linnworks outages become an `escalate_to_agent` result. Search responses capped at 40 candidate IDs. API failures are handled conservatively rather than misrepresented as 'not found'.
- `GET /health` is unauthenticated so Render can check availability. The lookup endpoint requires a server-to-server bearer token. Never commit this token or Linnworks secrets to GitHub.
- The code is stateless, with in-memory token refresh. No database, scheduled sync, Zendesk data enrichment, AfterShip tracking or account provisioning in v1.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Copy .env.example to .env, set secrets, then export env vars with your preferred shell tooling
uvicorn main:app --reload
```

```bash
curl -X POST 'http://127.0.0.1:8000/v1/orders/lookup' \
 -H 'Authorization: Bearer YOUR_API_BEARER_TOKEN' \
 -H 'Content-Type: application/json' \
 -d '{"channel_reference":"REAL-REFERENCE"}'
```

## Verify before production

Review actual Linnworks responses, especially search coverage for both reference and Linnworks order-number searches, and processed orders. Test real dispatched, open, no-match, duplicate-reference, wrong-email, and expired-session cases. **Nine local tests passed. The updated API has not yet been deployed or live-tested against Linnworks / Zendesk.**
