[README.md](https://github.com/user-attachments/files/35426068/README.md)
# Northern Gym — Linnworks lookup API for Zendesk

**Update (25 November 2026):** Multiple Linnworks order records that share an exact channel reference can now return multiple tracking numbers **only when** shipping email, recipient, street address, postcode, country, Source and SubSource all agree, every order is processed with tracking, and the group is no larger than five records. Otherwise the API escalates. This preserves the existing Zendesk `order.tracking_number` output by joining tracking numbers into a string and adds an optional `order.tracking_numbers` array. This change is not deployed until you replace `main.py` in GitHub and Render redeploys.

**Purpose:** Zendesk AI agents / actions call one endpoint using either a channel reference or a Linnworks order ID. The customer does **not** have to supply an email address or complete an OTP check. Reference-only lookups can return dispatch status **and tracking number** if Linnworks has confirmed the order as dispatched with tracking. A matching email unlocks additional order fields in trusted Zendesk context. **AfterShip is optional and is not integrated in this version.**

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
6. Test with a real channel reference **without an email**, then with a real Linnworks order number using `order_id`. Confirm that only status and tracking number (when available) are returned without email. Do not treat the health endpoint as proof that every Linnworks search type works.

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

Reference-only response for a dispatched order (tracking available, without personal details):
```json
{
  "found": true,
  "action": "answer_customer",
  "reason": "reference_only_tracking_available",
  "channel_reference": "NG-12345",
  "order": {"status": "dispatched", "tracking_number": "RM123"},
  "customer_message": "The order linked to reference NG-12345 has been dispatched. Your tracking number is RM123."
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

**Zendesk branching:** If `action = answer_customer`, use `customer_message`; otherwise escalate to an agent. For reference-only calls, `order.status` and `order.tracking_number` (if available) are returned. Other details such as email, address, items, courier, and Linnworks order ID are returned only for verified email matches. Ticket-field updates still require separate Zendesk configuration; this API does **not** write directly to Zendesk.

## Behaviour and limits

- Searches both open and processed orders with `OpenOrders/SearchOrders` (`IncludeProcessed=true`), then loads details using `Orders/GetOrdersById`.
- Accepts either `channel_reference` or `order_id`. Confirms exact matches against channel reference fields, Linnworks `OrderId`, and `NumOrderId` in returned search results. Live Linnworks search coverage for each identifier must be tested.
- With no email, a uniquely matched dispatched order with tracking returns **tracking number and dispatch status**, but no customer name, address, email, order contents, or other order metadata. Other statuses escalate.
- When Zendesk supplies an email, matching is still required before detailed tracking is returned. A mismatched email escalates; it does not fall back to disclosing detailed fields. No customer OTP step is required.
- **Security:** Knowing an order ID is not proof of identity. **Tracking numbers may reveal delivery information on carrier websites**, so this design trades some privacy for a faster customer experience. Restrict use to a private server-side Zendesk action, rate-limit lookups, prevent anonymous bulk/enumeration queries, and never expose the API bearer token in a browser or customer channel. An email match alone is only minimal verification, not strong identity verification.
- Does not invent tracking links. For reference-only lookups, the Linnworks tracking **number** is returned when dispatched with tracking, but **no tracking URL** is generated. Verified lookups can return additional details; `tracking_url` remains `null` until a reliable source is configured.
- Can respond automatically only to a uniquely matched dispatched order with a tracking number; reference-only replies include the number. Orders on hold, cancelled, or without confirmed tracking escalate.
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

Review actual Linnworks responses, especially search coverage for both reference and Linnworks order-number searches, and processed orders. Test real dispatched, open, no-match, duplicate-reference, wrong-email, and expired-session cases. **Local tests cover response mapping; the tracking-number update must still be deployed and live-tested against Linnworks / Zendesk.**


## Multi-shipment reference support

**Example:** `#NORTHERN115347` returned two exact Linnworks records in live diagnostics, with different tracking numbers and the same customer email. That establishes a potential split shipment, not proof of the same delivery destination. Linnworks supports split shipments and multiple tracking numbers; we require further consistency checks before enabling a reference-only response.

To test after deployment, run the existing Render Shell API test with `channel_reference` set to `#NORTHERN115347`. Success returns `order.tracking_number` containing both values separated by commas and `customer_message` mentioning both. No customer's email, name or address appears in the API response. If identity/destination fields are missing or different, expect `multiple_orders_identity_unconfirmed` and an agent escalation. If a shipment is not dispatched or has no tracking, expect `multiple_orders_dispatch_unconfirmed`.

**Zendesk mapping:** The existing `order.tracking_number` output remains a string for compatibility; `customer_message` can be used as before. The new `order.tracking_numbers` array is optional. Do not publish order details until you have reviewed customer authentication/identification risk, since order-reference knowledge alone is not proof of identity.

## Courier tracking pages enhancement — 25 November 2026

This update adds **strict mapping from `ShippingInfo.PostalServiceName` to an approved, generic courier tracking page**, preserving the existing action name, bearer token, endpoint, and `customer_message` output. See `ZENDESK_PROCEDURE.md` for paste-ready AI Agent instructions and ownership-check considerations.

### Supported couriers and official generic URLs

| Service detected in PostalServiceName | Customer-facing tracking page |
| --- | --- |
| Evri | https://www.evri.com/track-a-parcel |
| Royal Mail | https://www.royalmail.com/track-your-item |
| Parcelforce | https://www.parcelforce.com/track-trace |
| DPD | https://tracking.dpd.co.uk/ |
| DHL Express | https://uk.express.dhl.com/track-a-parcel/ |
| DHL eCommerce UK / DHL Parcel UK | https://track.dhlparcel.co.uk/ |
| UPS | https://www.ups.com/track |
| FedEx | https://www.fedex.com/en-gb/tracking.html |

The DHL Parcel URL redirects to DHL eCommerce UK's tracking page. Parcelforce points users to Royal Mail Track and Trace under its current network arrangement. URLs are *not* populated with tracking numbers or personal data.

**Strict matching:** Case-insensitive; extra wording like `Evri Next Day - Account Linked` is acceptable. A generic `DHL`, `Amazon Buy Shipping`, or services with contradictory carrier names cause `courier_unrecognised` escalation. We never infer a courier from `Vendor` or tracking-number format.

**Success JSON (illustrative):**

```json
{
  "found": true,
  "action": "answer_customer",
  "reason": "reference_only_tracking_page_available",
  "channel_reference": "EXAMPLE-ORDER",
  "order": {
    "status": "tracking_available",
    "tracking_number": "EXAMPLE123",
    "courier": "Evri",
    "tracking_page_url": "https://www.evri.com/track-a-parcel"
  },
  "customer_message": "I found tracking information for order EXAMPLE-ORDER. Your tracking number is EXAMPLE123. You can check the latest updates with Evri at https://www.evri.com/track-a-parcel. Enter your tracking number there and your delivery postcode if requested."
}
```

For compatible split shipments, `order.tracking_number` remains a comma-separated string, `order.tracking_numbers` remains an array, and `order.shipments[]` now maps each tracking number to its own approved courier and generic page. If every shipment has the same courier, `order.tracking_page_url` is set; otherwise it is `null` and the per-shipment URLs are provided. Unsupported courier mappings cause escalation rather than partial disclosure.

**Important semantic correction:** Linnworks `Processed = true` alone does not prove courier collection or delivery. Customer messages say tracking information is available and direct customers to the courier's tracking site for live updates. The API now uses `order.status = tracking_available` in successful tracking responses rather than claiming `dispatched`; if any Zendesk branch hardcodes `dispatched`, update it to branch on `action = answer_customer` instead.

**Limitations:** This update does not verify the caller customer's ownership, perform live courier scans, or implement rate limiting. The existing reference-only workflow still discloses tracking numbers after a unique, exact lookup or safely grouped split shipment. Restrict this action to trusted customer contexts, enforce ownership verification in Zendesk, and implement server-side verification and anti-enumeration safeguards before public rollout.

### Installation

Replace `main.py`, `tests/test_lookup.py` and `README.md` in the existing `northern-gym-linnworks-api/` GitHub folder; add `ZENDESK_PROCEDURE.md` there. Keep `render.yaml`, `requirements.txt`, environment variables, and all stored secrets unchanged. Let Render deploy the new commit, then run the new test in its Shell and check Zendesk's `customer_message` output.

**Test caution:** If a Linnworks record has tracking but no courier-specific `PostalServiceName`, the new strict mapping returns `courier_unrecognised` and escalates. This is intentional: it does not guess from `ShippingInfo.Vendor` or from a tracking-number pattern. Check representative real postal-service names before a broad deployment.
