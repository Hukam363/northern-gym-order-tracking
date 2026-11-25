"""Northern Gym: Linnworks order lookup for Zendesk actions."""
import asyncio
import hmac
import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
log = logging.getLogger('northern-order-lookup')
app = FastAPI(title='Northern Gym Linnworks Lookup', version='1.0.0', docs_url=None, redoc_url=None)


class LookupRequest(BaseModel):
    # Customers can supply either a sales-channel reference or a Linnworks order ID.
    # Email is optional: Zendesk can supply it silently when a trusted email exists.
    channel_reference: str | None = Field(default=None, min_length=1, max_length=120)
    order_id: str | None = Field(default=None, min_length=1, max_length=120)
    requester_email: str | None = Field(default=None, max_length=320)


class LinnworksUnavailable(Exception):
    pass


def pick(data: dict, *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def normalize(value: Any) -> str:
    return str(value or '').strip().casefold()


def require_auth(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get('API_BEARER_TOKEN', '')
    if not expected:
        raise HTTPException(503, detail='API_BEARER_TOKEN is not configured')
    actual = authorization.removeprefix('Bearer ').strip() if authorization and authorization.startswith('Bearer ') else ''
    if not hmac.compare_digest(actual, expected):
        raise HTTPException(401, detail='Unauthorized')


class LinnworksClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.http = httpx.AsyncClient(timeout=18, transport=transport)
        self.session_token: str | None = None
        self.server: str | None = None
        self.session_until = 0.0
        self.lock = asyncio.Lock()

    async def close(self):
        await self.http.aclose()

    async def authorize(self, force: bool = False):
        async with self.lock:
            if not force and self.session_token and time.monotonic() < self.session_until:
                return
            env_names = ['LINNWORKS_APP_ID', 'LINNWORKS_APP_SECRET', 'LINNWORKS_PERMANENT_TOKEN']
            missing = [name for name in env_names if not os.getenv(name)]
            if missing:
                raise LinnworksUnavailable('Missing Linnworks configuration: ' + ', '.join(missing))
            try:
                response = await self.http.post('https://api.linnworks.net/api/Auth/AuthorizeByApplication', json={
                    'ApplicationId': os.environ['LINNWORKS_APP_ID'],
                    'ApplicationSecret': os.environ['LINNWORKS_APP_SECRET'],
                    'Token': os.environ['LINNWORKS_PERMANENT_TOKEN'],
                })
                response.raise_for_status()
                body = response.json()
                token = pick(body, 'Token', 'token')
                server = str(pick(body, 'Server', 'server') or '').rstrip('/')
                parsed = urlparse(server)
                if not token or parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.linnworks.net'):
                    raise LinnworksUnavailable('Linnworks returned an invalid authorization session')
                self.session_token, self.server = str(token), server
                self.session_until = time.monotonic() + 15 * 60
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                log.warning('Linnworks authorization failed: %s', type(exc).__name__)
                raise LinnworksUnavailable('Linnworks authorization failed') from exc

    async def call(self, path: str, payload: dict) -> Any:
        for retry in range(2):
            await self.authorize(force=retry == 1)
            try:
                response = await self.http.post(f'{self.server}/api/{path}', headers={
                    'Authorization': self.session_token, 'Accept': 'application/json'
                }, json=payload)
                if response.status_code == 401 and retry == 0:
                    continue
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and ('Error' in data or 'error' in data):
                    raise LinnworksUnavailable('Linnworks returned an API error')
                return data
            except (httpx.HTTPError, ValueError) as exc:
                log.warning('Linnworks API call %s failed: %s', path, type(exc).__name__)
                raise LinnworksUnavailable('Linnworks request failed') from exc
        raise LinnworksUnavailable('Linnworks session was rejected')

    async def search(self, reference: str) -> list[dict]:
        search_result = await self.call('OpenOrders/SearchOrders', {
            'SearchTerm': reference, 'IncludeProcessed': True,
            'LocationId': '00000000-0000-0000-0000-000000000000'
        })
        if not isinstance(search_result, dict):
            raise LinnworksUnavailable('Unexpected order search response')
        ids: list[str] = []
        for group in search_result.get('OpenOrders') or []:
            ids.extend(group.get('OrderIds') or [])
        ids.extend(search_result.get('ProcessedOrders') or [])
        ids = list(dict.fromkeys(str(item) for item in ids if item))
        # Prevent large search results from being turned into a broad PII fetch.
        max_candidates = int(os.getenv('MAX_CANDIDATES', '40'))
        if len(ids) > max_candidates:
            raise LinnworksUnavailable('Search returned too many candidates for a safe lookup')
        if not ids:
            return []
        result: list[dict] = []
        for start in range(0, len(ids), 20):
            details = await self.call('Orders/GetOrdersById', {'pkOrderIds': ids[start:start + 20]})
            if not isinstance(details, list):
                raise LinnworksUnavailable('Unexpected order details response')
            result.extend(item for item in details if isinstance(item, dict))
        return result


client = LinnworksClient()


@app.on_event('shutdown')
async def shutdown():
    await client.close()


@app.get('/health')
async def health():
    return {'status': 'ok', 'service': 'northern-gym-linnworks-lookup'}


def extract_email(order: dict) -> str | None:
    cust = order.get('CustomerInfo') or {}
    for address_key in ('Address', 'BillingAddress'):
        email = (cust.get(address_key) or {}).get('EmailAddress')
        if email:
            return str(email).strip().lower()
    return None


def reference_matches(order: dict, target: str) -> bool:
    # SearchOrders may return partial matches. Confirm an exact identifier match.
    if not target:
        return False
    general = order.get('GeneralInfo') or {}
    identifiers = [
        general.get('ReferenceNum'),
        general.get('SecondaryReference'),
        general.get('ExternalReferenceNum'),
        order.get('NumOrderId'),
        order.get('OrderId'),
    ]
    return any(normalize(value) == target for value in identifiers)


def escalate(reason: str, reference: str) -> dict:
    return {
        'found': False, 'action': 'escalate_to_agent', 'reason': reason,
        'channel_reference': reference, 'order': None,
        'customer_message': 'I could not confidently confirm the order details. I will pass this to our team.'
    }


def format_order(order: dict, reference: str) -> dict:
    general = order.get('GeneralInfo') or {}
    shipping = order.get('ShippingInfo') or {}
    processed = bool(order.get('Processed'))
    tracking_number = shipping.get('TrackingNumber') or None
    carrier = shipping.get('Vendor') or None
    status = 'dispatched' if processed else 'not_yet_dispatched'
    if general.get('HoldOrCancel'):
        status = 'on_hold_or_cancelled'
    elif general.get('PartShipped') and not processed:
        status = 'partially_dispatched'
    items = order.get('Items') or []
    safe_items = [{'sku': x.get('SKU'), 'name': x.get('Title') or x.get('ItemName'), 'quantity': x.get('Quantity')}
                  for x in items if isinstance(x, dict)]
    return {
        'found': True, 'action': 'answer_customer' if status == 'dispatched' and tracking_number else 'escalate_to_agent',
        'reason': None if status == 'dispatched' and tracking_number else 'no_confirmed_tracking_or_dispatch',
        'channel_reference': reference,
        'order': {
            'linnworks_order_id': order.get('OrderId'),
            'linnworks_order_number': order.get('NumOrderId'),
            'channel_reference': general.get('ReferenceNum') or reference,
            'source': general.get('Source'), 'status': status,
            'processed': processed, 'processed_at': order.get('ProcessedDateTime'),
            'carrier': carrier, 'postal_service': shipping.get('PostalServiceName'),
            'tracking_number': tracking_number, 'tracking_url': None,
            'received_at': general.get('ReceivedDate'), 'items': safe_items
        },
        'customer_message': (
            f'Your order {reference} has been dispatched. Tracking number: {tracking_number}.'
            if status == 'dispatched' and tracking_number else
            'I found your order, but cannot confirm a tracking update yet. I will pass this to our team.'
        )
    }


def format_reference_only(order: dict, reference: str) -> dict:
    """Respond to an unverified reference without releasing tracking or personal data.

    A customer knowing an order number is not proof they own the order. In
    particular, some couriers' tracking pages can expose delivery details.
    """
    general = order.get('GeneralInfo') or {}
    shipping = order.get('ShippingInfo') or {}
    held = bool(general.get('HoldOrCancel'))
    dispatched_with_tracking = bool(order.get('Processed') and shipping.get('TrackingNumber'))

    if held:
        status = 'requires_agent'
        action = 'escalate_to_agent'
        reason = 'manual_review_required'
        message = 'Our team will review the order enquiry and provide an update.'
    elif dispatched_with_tracking:
        status = 'dispatched'
        action = 'answer_customer'
        reason = 'reference_only_status'
        message = (
            'The order linked to that reference is marked as dispatched. '
            'Please check the dispatch email for your parcel tracking details.'
        )
    else:
        # Processing in Linnworks is not necessarily proof of dispatch.
        status = 'processing_or_unconfirmed'
        action = 'escalate_to_agent'
        reason = 'no_confirmed_tracking_or_dispatch'
        message = 'I located the reference, but our team needs to confirm the delivery update.'

    return {
        'found': True,
        'action': action,
        'reason': reason,
        'channel_reference': reference,
        # Deliberately no order ID, name, address, products, courier, or tracking.
        'order': {'status': status},
        'customer_message': message,
    }


@app.post('/v1/orders/lookup', dependencies=[Depends(require_auth)])
async def lookup(request: LookupRequest):
    reference = (request.channel_reference or request.order_id or '').strip()
    if not reference:
        raise HTTPException(422, detail='An order ID or channel reference is required')
    if request.channel_reference and request.order_id and normalize(request.channel_reference) != normalize(request.order_id):
        raise HTTPException(422, detail='Provide only one order identifier')
    if os.getenv('SERVICE_ENABLED', 'true').lower() != 'true':
        return escalate('service_disabled', reference)
    try:
        orders = await client.search(reference)
    except LinnworksUnavailable as exc:
        log.error('Lookup unavailable: %s', exc)
        return escalate('linnworks_unavailable', reference)
    matches = [order for order in orders if reference_matches(order, normalize(reference))]
    if not matches:
        return escalate('order_not_found', reference)
    if len(matches) != 1:
        return escalate('multiple_matching_orders', reference)

    if request.requester_email:
        # An email supplied in trusted Zendesk context can unlock detailed
        # tracking information. A mismatch never falls back to detailed data.
        stored = extract_email(matches[0])
        if not stored or normalize(stored) != normalize(request.requester_email):
            return escalate('requester_email_not_matched', reference)
        result = format_order(matches[0], reference)
    else:
        # Reference-only is allowed, but no identifying or tracking data leaves
        # the API. Basic dispatch status may be used in an automated response.
        result = format_reference_only(matches[0], reference)

    if os.getenv('AUTO_REPLY_ENABLED', 'true').lower() != 'true':
        result['action'] = 'escalate_to_agent'
        result['reason'] = 'auto_reply_disabled'
    return result
