# Northern Gym — Linnworks lookup API for Zendesk

**Purpose:** Zendesk AI agents / actions call one endpoint using a channel reference and requester email. The API obtains Linnworks session credentials, searches open and processed orders, fetches order details, confirms exact channel reference and matching email, and returns a compact JSON payload. **AfterShip is not required or integrated in v1.** No customer OTP step.

## Deploy on GitHub + Render

1. Upload **all files** (including `render.yaml`) to the **root** of a GitHub repo, maintaining the `tests/` folder. Do not upload `.env` or secrets.
2. In Render, choose **New > Blueprint**, connect this GitHub repo, and apply the `render.yaml` configuration. If you already have a Render web service, you can instead point it at the repo and use the same build/start commands defined in `render.yaml`.
3. Set environment variables in the Render dashboard (or at Blueprint creation):
   - `LINNWORKS_APP_ID` — Linnworks developer app ID.
   - `LINNWORKS_APP_SECRET` — Linnworks developer app secret.
   - `LINNWORKS_PERMANENT_TOKEN` — token issued when the app is installed in Northern Gym's Linnworks account; **not** a temporary session token.
   - `API_BEARER_TOKEN` — a strong random shared secret known to Zendesk (not a Linnworks token).
4. Verify `GET https://YOUR-RENDER-URL/health` returns `{"status":"ok"...}`.
5. Configure a Zendesk HTTP action/connection to send `POST https://YOUR-RENDER-URL/v1/orders/lookup`, with headers `Authorization: Bearer YOUR_API_BEARER_TOKEN` and `Content-Type: application/json`.
6. Test with a real order whose channel reference and requester email are known. Do not treat the health endpoint as proof that Linnworks authorization works.

For a Linnworks system integration app: obtain installation token through the developer portal's installation URL, then the API uses `Auth/AuthorizeByApplication` to generate temporary session tokens. See [Linnworks documentation](https://apidocs.linnworks.net/docs/system-integration-setup).

## Zendesk action JSON

Request:
```json
{"channel_reference":"NG-12345","requester_email":"customer@example.com"}
```

Response on a confirmed dispatched order with tracking:
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

**Zendesk branching:** If `action = answer_customer`, use `customer_message`; otherwise, escalate to an agent. The response can be mapped to existing Zendesk ticket fields for channel reference, Linnworks order number, carrier, tracking number, and dispatch status. Actual ticket-field updates require Zendesk configuration; this API does **not** write directly to Zendesk. The action must not expose raw JSON or order fields without account verification.

## Behaviour and limits

- Searches both open and processed orders with `OpenOrders/SearchOrders` (`IncludeProcessed=true`), then loads details using `Orders/GetOrdersById`.
- Returns order details **only** when exactly one order matches reference and the requester email matches the Linnworks address. No OTP. If email is not supplied or cannot match, it escalates without returning private order details. Matching a reference and email is a minimal check, **not strong identity verification**; configure the Zendesk action for authenticated conversations or trusted requester contexts rather than anonymous public requests.
- Does not invent tracking links; Linnworks' tracking number is exposed while `tracking_url` remains `null` until a reliable URL source is configured.
- Can respond automatically only to a matched dispatched order with a tracking number, otherwise escalates. If an order is on hold or cancelled, it escalates.
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
 -d '{"channel_reference":"REAL-REFERENCE","requester_email":"real.customer@example.com"}'
```

## Verify before production

Review actual Linnworks responses against the account's data, especially search coverage for processed orders and emails on imported marketplace orders. Test real dispatched, open, no-match, duplicate-reference, email-mismatch and expired-session cases. Automated tests cover local mapping logic only; **live Linnworks and Zendesk connectivity has not been tested**.
