"""Northern Gym: Linnworks order lookup for Zendesk actions."""
import asyncio
import hmac
import logging
import os
import re
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


# Approved, generic courier tracking pages. These are not individual parcel URLs.
# Only ShippingInfo.PostalServiceName can authorize a carrier match: never
# infer the carrier from its tracking-number pattern, Vendor, or marketplace.
APPROVED_COURIERS = {
    'Evri': 'https://www.evri.com/track-a-parcel',
    'Royal Mail': 'https://www.royalmail.com/track-your-item',
    'Parcelforce': 'https://www.parcelforce.com/track-trace',
    'DPD': 'https://tracking.dpd.co.uk/',
    'DHL Express': 'https://uk.express.dhl.com/track-a-parcel/',
    'DHL eCommerce UK': 'https://track.dhlparcel.co.uk/',
    'UPS': 'https://www.ups.com/track',
    'FedEx': 'https://www.fedex.com/en-gb/tracking.html',
}


def courier_from_postal_service(postal_service_name: Any) -> dict[str, str] | None:
    """Strictly identify a *single* approved courier from Linnworks service text.

    Partial service names such as 'Evri Next Day - Account Linked' are accepted,
    but generic DHL, Amazon Buy Shipping, and conflicting vendor strings aren't.
    """
    value = ' '.join(str(postal_service_name or '').casefold().split())
    if not value or 'amazon buy shipping' in value:
        return None

    matches: list[str] = []
    patterns = {
        'Evri': r'\bevri\b',
        'Royal Mail': r'\broyal\s+mail\b',
        'Parcelforce': r'\bparcelforce\b',
        'DPD': r'\bdpd\b',
        'UPS': r'\bups\b',
        'FedEx': r'\bfed\s*ex\b',
    }
    for courier, pattern in patterns.items():
        if re.search(pattern, value):
            matches.append(courier)

    if re.search(r'\bdhl\b', value):
        express = re.search(r'\bdhl\s+express\b', value)
        ecommerce = re.search(r'\bdhl\s+(?:e[\s-]?commerce(?:\s+uk)?|parcel\s+(?:uk|next\s+day))\b', value)
        if express and not ecommerce:
            matches.append('DHL Express')
        elif ecommerce and not express:
            matches.append('DHL eCommerce UK')
        else:
            return None

    if len(matches) != 1:
        return None
    courier = matches[0]
    return {'courier': courier, 'tracking_page_url': APPROVED_COURIERS[courier]}


def tracking_page_message(reference: str, shipments: list[dict[str, str]]) -> str:
    """Only report what Linnworks contains; never claim a live carrier scan."""
    if len(shipments) == 1:
        shipment = shipments[0]
        return (
            f'I found tracking information for order {reference}. '
            f'Your tracking number is {shipment["tracking_number"]}. '
            f'You can check the latest updates with {shipment["courier"]} at '
            f'{shipment["tracking_page_url"]}. '
            'Enter your tracking number there and your delivery postcode if requested.'
        )
    parts = [
        f'I found {len(shipments)} tracking numbers associated with order {reference}.'
    ]
    for index, shipment in enumerate(shipments, 1):
        parts.append(
            f'Parcel {index}: {shipment["courier"]}, tracking number '
            f'{shipment["tracking_number"]}. Tracking page: {shipment["tracking_page_url"]}.'
        )
    parts.append('Enter each tracking number on its courier page and your delivery postcode if requested.')
    return ' '.join(parts)


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
    """Verified-email details path; only link an approved, unambiguous courier."""
    general = order.get('GeneralInfo') or {}
    shipping = order.get('ShippingInfo') or {}
    processed = bool(order.get('Processed'))
    tracking_number = str(shipping.get('TrackingNumber') or '').strip()
    held = bool(general.get('HoldOrCancel'))
    if held or not processed or not tracking_number:
        return escalate('no_confirmed_tracking_or_dispatch', reference)
    mapping = courier_from_postal_service(shipping.get('PostalServiceName'))
    if not mapping:
        return escalate('courier_unrecognised', reference)
    shipment = {'tracking_number': tracking_number, **mapping}
    items = order.get('Items') or []
    safe_items = [
        {'sku': x.get('SKU'), 'name': x.get('Title') or x.get('ItemName'), 'quantity': x.get('Quantity')}
        for x in items if isinstance(x, dict)
    ]
    return {
        'found': True, 'action': 'answer_customer', 'reason': 'tracking_page_available',
        'channel_reference': reference,
        'order': {
            'linnworks_order_id': order.get('OrderId'),
            'linnworks_order_number': order.get('NumOrderId'),
            'channel_reference': general.get('ReferenceNum') or reference,
            'source': general.get('Source'), 'status': 'tracking_available',
            'processed': processed, 'processed_at': order.get('ProcessedDateTime'),
            'carrier': mapping['courier'], 'postal_service': shipping.get('PostalServiceName'),
            'tracking_number': tracking_number,
            'tracking_page_url': mapping['tracking_page_url'],
            'tracking_url': None,  # Never claim the generic page is a personalised tracking URL.
            'received_at': general.get('ReceivedDate'), 'items': safe_items
        },
        'customer_message': tracking_page_message(reference, [shipment]),
    }


def format_reference_only(order: dict, reference: str) -> dict:
    """Reference-only response restricted to courier, tracking and generic URL.

    The reference is *not* customer ownership verification. It remains necessary
    to restrict this endpoint to authorised Zendesk workflows and prevent
    enumeration; see the handover security guidance.
    """
    general = order.get('GeneralInfo') or {}
    shipping = order.get('ShippingInfo') or {}
    held = bool(general.get('HoldOrCancel'))
    tracking_number = str(shipping.get('TrackingNumber') or '').strip()
    processed_with_tracking = bool(order.get('Processed') and tracking_number)

    if held:
        return escalate('manual_review_required', reference)
    if not processed_with_tracking:
        return escalate('no_confirmed_tracking_or_dispatch', reference)

    mapping = courier_from_postal_service(shipping.get('PostalServiceName'))
    if not mapping:
        return escalate('courier_unrecognised', reference)
    shipment = {'tracking_number': tracking_number, **mapping}
    return {
        'found': True, 'action': 'answer_customer',
        'reason': 'reference_only_tracking_page_available',
        'channel_reference': reference,
        'order': {
            'status': 'tracking_available',
            'tracking_number': tracking_number,
            'courier': mapping['courier'],
            'tracking_page_url': mapping['tracking_page_url'],
        },
        'customer_message': tracking_page_message(reference, [shipment]),
    }


def format_matching_multi_orders(orders: list[dict], reference: str) -> dict:
    """Return tracking for a provably consistent group of split order records.

    Only an exact shared channel reference, the same non-empty customer email,
    delivery address and source, and dispatched tracking for every record
    allow reference-only tracking disclosure. Otherwise fail closed.
    """
    if not (2 <= len(orders) <= 5):
        return escalate('multiple_matching_orders', reference)

    # Multiple internal Linnworks IDs must never be merged merely because one
    # also resembles another record's numeric ID. Every record must carry the
    # same literal channel reference in a channel-reference field.
    for order in orders:
        general = order.get('GeneralInfo') or {}
        if not any(normalize(general.get(key)) == normalize(reference) for key in (
            'ReferenceNum', 'SecondaryReference', 'ExternalReferenceNum'
        )):
            return escalate('multiple_orders_reference_unconfirmed', reference)

    # Internal matching only. Customer addresses/emails are never returned.
    def shipment_identity(order: dict) -> tuple[str, ...] | None:
        customer = order.get('CustomerInfo') or {}
        address = customer.get('Address') or {}
        general = order.get('GeneralInfo') or {}
        email = normalize(address.get('EmailAddress'))
        name = normalize(address.get('FullName'))
        address1 = normalize(address.get('Address1'))
        postcode = normalize(address.get('PostCode')).replace(' ', '')
        country = normalize(address.get('Country'))
        source = normalize(general.get('Source'))
        subsource = normalize(general.get('SubSource'))
        # Missing identity fields mean we cannot establish these records belong
        # to the same customer shipment. Deliberately escalate instead.
        if not all((email, name, address1, postcode, country, source)):
            return None
        return (email, name, address1, postcode, country, source, subsource)

    identities = [shipment_identity(order) for order in orders]
    if not identities[0] or any(identity != identities[0] for identity in identities):
        return escalate('multiple_orders_identity_unconfirmed', reference)

    tracking_numbers: list[str] = []
    shipments: list[dict[str, str]] = []
    for order in orders:
        general = order.get('GeneralInfo') or {}
        shipping = order.get('ShippingInfo') or {}
        tracking = str(shipping.get('TrackingNumber') or '').strip()
        if general.get('HoldOrCancel') or not order.get('Processed') or not tracking:
            return escalate('multiple_orders_dispatch_unconfirmed', reference)
        mapping = courier_from_postal_service(shipping.get('PostalServiceName'))
        if not mapping:
            return escalate('courier_unrecognised', reference)
        shipment = {'tracking_number': tracking, **mapping}
        if tracking in tracking_numbers:
            prior = shipments[tracking_numbers.index(tracking)]
            if prior['courier'] != shipment['courier']:
                return escalate('conflicting_courier_for_tracking', reference)
        else:
            tracking_numbers.append(tracking)
            shipments.append(shipment)

    if not shipments:
        return escalate('multiple_orders_dispatch_unconfirmed', reference)

    # Preserve the existing Zendesk tracking_number string mapping.
    joined = ', '.join(tracking_numbers)
    courier_urls = {item['tracking_page_url'] for item in shipments}
    return {
        'found': True,
        'action': 'answer_customer',
        'reason': 'multiple_shipment_tracking_available',
        'channel_reference': reference,
        'order': {
            'status': 'tracking_available',
            'tracking_number': joined,
            'tracking_numbers': tracking_numbers,
            'shipments': shipments,
            # Only a top-level URL if every parcel uses the same courier.
            'tracking_page_url': next(iter(courier_urls)) if len(courier_urls) == 1 else None,
        },
        'customer_message': tracking_page_message(reference, shipments),
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
        # If Zendesk supplied an email, enforce it for *every* record even in
        # a multi-shipment group. A mismatched email never unlocks tracking.
        if request.requester_email and any(
            normalize(extract_email(o)) != normalize(request.requester_email)
            for o in matches
        ):
            return escalate('requester_email_not_matched', reference)
        # Never pick an arbitrary order. Only group genuinely consistent
        # same-customer, same-destination, same-channel shipments.
        result = format_matching_multi_orders(matches, reference)
        if os.getenv('AUTO_REPLY_ENABLED', 'true').lower() != 'true':
            result['action'] = 'escalate_to_agent'
            result['reason'] = 'auto_reply_disabled'
        return result

    if request.requester_email:
        # An email supplied in trusted Zendesk context can unlock detailed
        # tracking information. A mismatch never falls back to detailed data.
        stored = extract_email(matches[0])
        if not stored or normalize(stored) != normalize(request.requester_email):
            return escalate('requester_email_not_matched', reference)
        result = format_order(matches[0], reference)
    else:
        # Reference-only may reveal tracking but never address, email or items.
        # Protect this endpoint behind a trusted Zendesk server-side action.
        result = format_reference_only(matches[0], reference)

    if os.getenv('AUTO_REPLY_ENABLED', 'true').lower() != 'true':
        result['action'] = 'escalate_to_agent'
        result['reason'] = 'auto_reply_disabled'
    return result
