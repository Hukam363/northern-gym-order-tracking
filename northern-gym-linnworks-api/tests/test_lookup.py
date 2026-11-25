from main import reference_matches, format_order, extract_email, escalate

SAMPLE = {
    'OrderId': 'abc', 'NumOrderId': 4321, 'Processed': True,
    'GeneralInfo': {'ReferenceNum': 'NG-123', 'Source': 'SHOPIFY'},
    'ShippingInfo': {'TrackingNumber': 'TRACK123', 'Vendor': 'Royal Mail', 'PostalServiceName': 'Royal Mail Tracked 48'},
    'CustomerInfo': {'Address': {'EmailAddress': 'client@example.com'}},
    'Items': [{'SKU': 'SKU-1', 'Title': 'Gym bench', 'Quantity': 1}],
}

def test_exact_match():
    assert reference_matches(SAMPLE, 'ng-123')
    assert not reference_matches(SAMPLE, 'ng-12')

def test_format_dispatch():
    result = format_order(SAMPLE, 'NG-123')
    assert result['order']['tracking_number'] == 'TRACK123'
    assert result['order']['tracking_url'] is None
    assert result['action'] == 'answer_customer'

def test_email_extraction():
    assert extract_email(SAMPLE) == 'client@example.com'

def test_not_found():
    assert escalate('order_not_found', 'NG-404')['action'] == 'escalate_to_agent'


def test_order_identifiers():
    assert reference_matches(SAMPLE, '4321')
    assert reference_matches(SAMPLE, 'abc')
    assert not reference_matches(SAMPLE, '432')


def test_reference_only_returns_tracking_but_no_customer_data():
    from main import format_reference_only
    result = format_reference_only(SAMPLE, 'NG-123')
    assert result['found'] is True
    assert result['action'] == 'answer_customer'
    assert result['order'] == {'status': 'tracking_available', 'tracking_number': 'TRACK123', 'courier': 'Royal Mail', 'tracking_page_url': 'https://www.royalmail.com/track-your-item'}
    assert result['reason'] == 'reference_only_tracking_page_available'
    assert 'TRACK123' in result['customer_message']
    assert 'Royal Mail' in result['customer_message']
    assert 'SKU-1' not in str(result)
    assert 'client@example.com' not in str(result)


def test_reference_only_without_tracking_escalates():
    from main import format_reference_only
    order = {**SAMPLE, 'ShippingInfo': {}}
    result = format_reference_only(order, 'NG-123')
    assert result['action'] == 'escalate_to_agent'
    assert result['order'] is None
    assert result['reason'] == 'no_confirmed_tracking_or_dispatch'


def test_reference_only_on_hold_escalates():
    from main import format_reference_only
    order = {**SAMPLE, 'GeneralInfo': {**SAMPLE['GeneralInfo'], 'HoldOrCancel': True}}
    result = format_reference_only(order, 'NG-123')
    assert result['action'] == 'escalate_to_agent'
    assert result['order'] is None
    assert result['reason'] == 'manual_review_required'
    assert 'TRACK123' not in str(result)


def test_api_reference_only_and_email_verification(monkeypatch):
    from fastapi.testclient import TestClient
    import main

    monkeypatch.setenv('API_BEARER_TOKEN', 'test-bearer-token')
    monkeypatch.setenv('AUTO_REPLY_ENABLED', 'true')

    async def fake_search(reference):
        return [SAMPLE]

    monkeypatch.setattr(main.client, 'search', fake_search)
    with TestClient(main.app) as api:
        headers = {'Authorization': 'Bearer test-bearer-token'}
        ref_only = api.post('/v1/orders/lookup', json={'channel_reference': 'NG-123'}, headers=headers)
        assert ref_only.status_code == 200
        assert ref_only.json()['found'] is True
        assert ref_only.json()['order']['tracking_page_url'] == 'https://www.royalmail.com/track-your-item'
        assert ref_only.json()['order']['tracking_number'] == 'TRACK123'
        assert ref_only.json()['reason'] == 'reference_only_tracking_page_available'

        id_only = api.post('/v1/orders/lookup', json={'order_id': '4321'}, headers=headers)
        assert id_only.status_code == 200
        assert id_only.json()['found'] is True
        assert id_only.json()['order']['tracking_number'] == 'TRACK123'

        mismatch = api.post('/v1/orders/lookup', json={'order_id': '4321', 'requester_email': 'wrong@example.com'}, headers=headers)
        assert mismatch.json()['reason'] == 'requester_email_not_matched'
        assert mismatch.json()['order'] is None

        verified = api.post('/v1/orders/lookup', json={'order_id': '4321', 'requester_email': 'client@example.com'}, headers=headers)
        assert verified.json()['found'] is True
        assert verified.json()['order']['tracking_number'] == 'TRACK123'

        no_id = api.post('/v1/orders/lookup', json={}, headers=headers)
        assert no_id.status_code == 422

        no_auth = api.post('/v1/orders/lookup', json={'order_id': '4321'})
        assert no_auth.status_code == 401


# New: two distinct Linnworks records for one customer/channel reference.
def sample_split_orders():
    import copy
    one = copy.deepcopy(SAMPLE)
    two = copy.deepcopy(SAMPLE)
    for o in (one, two):
        o['GeneralInfo']['ReferenceNum'] = '#NORTHERN115347'
        o['GeneralInfo']['Source'] = 'SHOPIFY'
        o['GeneralInfo']['SubSource'] = 'NORTHERN'
        o['CustomerInfo']['Address'] = {
            'EmailAddress': 'client@example.com',
            'FullName': 'Test Customer',
            'Address1': '123 Example Street',
            'PostCode': 'NG1 1AA',
            'Country': 'United Kingdom',
        }
    one['OrderId'] = 'uuid-order-a'
    one['NumOrderId'] = 5050628
    one['ShippingInfo']['TrackingNumber'] = 'TRACK-A'
    two['OrderId'] = 'uuid-order-b'
    two['NumOrderId'] = 5050678
    two['ShippingInfo']['TrackingNumber'] = 'TRACK-B'
    return one, two


def test_two_matching_shipments_return_both_tracking_numbers():
    from main import format_matching_multi_orders
    one, two = sample_split_orders()
    result = format_matching_multi_orders([one, two], '#NORTHERN115347')
    assert result['found'] is True
    assert result['action'] == 'answer_customer'
    assert result['order']['tracking_numbers'] == ['TRACK-A', 'TRACK-B']
    assert result['order']['tracking_number'] == 'TRACK-A, TRACK-B'
    assert 'TRACK-A' in result['customer_message']
    assert 'TRACK-B' in result['customer_message']
    assert result['order']['tracking_page_url'] == 'https://www.royalmail.com/track-your-item'
    assert len(result['order']['shipments']) == 2
    assert 'client@example.com' not in str(result)
    assert '123 Example Street' not in str(result)


def test_split_orders_with_different_address_escalate():
    from main import format_matching_multi_orders
    one, two = sample_split_orders()
    two['CustomerInfo']['Address']['PostCode'] = 'NG1 2AA'
    result = format_matching_multi_orders([one, two], '#NORTHERN115347')
    assert result['action'] == 'escalate_to_agent'
    assert result['reason'] == 'multiple_orders_identity_unconfirmed'
    assert result['order'] is None


def test_split_orders_with_missing_address_escalate():
    from main import format_matching_multi_orders
    one, two = sample_split_orders()
    two['CustomerInfo']['Address'].pop('FullName')
    result = format_matching_multi_orders([one, two], '#NORTHERN115347')
    assert result['action'] == 'escalate_to_agent'


def test_split_orders_with_different_email_escalate():
    from main import format_matching_multi_orders
    one, two = sample_split_orders()
    two['CustomerInfo']['Address']['EmailAddress'] = 'different@example.com'
    result = format_matching_multi_orders([one, two], '#NORTHERN115347')
    assert result['action'] == 'escalate_to_agent'
    assert result['order'] is None


def test_split_orders_without_tracking_escalate():
    from main import format_matching_multi_orders
    one, two = sample_split_orders()
    two['ShippingInfo']['TrackingNumber'] = ''
    result = format_matching_multi_orders([one, two], '#NORTHERN115347')
    assert result['action'] == 'escalate_to_agent'
    assert result['reason'] == 'multiple_orders_dispatch_unconfirmed'


def test_split_orders_on_hold_escalate():
    from main import format_matching_multi_orders
    one, two = sample_split_orders()
    two['GeneralInfo']['HoldOrCancel'] = True
    assert format_matching_multi_orders([one, two], '#NORTHERN115347')['action'] == 'escalate_to_agent'


def test_split_orders_dedup_same_tracking_number():
    from main import format_matching_multi_orders
    one, two = sample_split_orders()
    two['ShippingInfo']['TrackingNumber'] = 'TRACK-A'
    result = format_matching_multi_orders([one, two], '#NORTHERN115347')
    assert result['order']['tracking_numbers'] == ['TRACK-A']
    assert 'tracking number is TRACK-A' in result['customer_message']


def test_api_split_reference(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    one, two = sample_split_orders()
    monkeypatch.setenv('API_BEARER_TOKEN', 'test-bearer-token')
    monkeypatch.setenv('AUTO_REPLY_ENABLED', 'true')

    async def fake_search(_):
        return [one, two]

    monkeypatch.setattr(main.client, 'search', fake_search)
    with TestClient(main.app) as api:
        result = api.post('/v1/orders/lookup', json={'channel_reference': '#NORTHERN115347'}, headers={'Authorization': 'Bearer test-bearer-token'})
        assert result.status_code == 200
        assert result.json()['action'] == 'answer_customer'
        assert result.json()['order']['tracking_number'] == 'TRACK-A, TRACK-B'


def test_multi_order_does_not_conflate_numeric_order_id():
    from main import format_matching_multi_orders
    one, two = sample_split_orders()
    # Identical customer/destination is not enough when one record lacks the
    # same marketplace/channel identifier.
    two['GeneralInfo']['ReferenceNum'] = 'DIFFERENT'
    result = format_matching_multi_orders([one, two], '#NORTHERN115347')
    assert result['action'] == 'escalate_to_agent'
    assert result['reason'] == 'multiple_orders_reference_unconfirmed'


def test_api_split_order_rejects_supplied_wrong_email(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    one, two = sample_split_orders()
    monkeypatch.setenv('API_BEARER_TOKEN', 'test-bearer-token')

    async def fake_search(_):
        return [one, two]

    monkeypatch.setattr(main.client, 'search', fake_search)
    with TestClient(main.app) as api:
        result = api.post('/v1/orders/lookup', json={
            'channel_reference': '#NORTHERN115347',
            'requester_email': 'wrong@example.com',
        }, headers={'Authorization': 'Bearer test-bearer-token'})
        assert result.status_code == 200
        assert result.json()['reason'] == 'requester_email_not_matched'
        assert result.json()['order'] is None


import pytest

@pytest.mark.parametrize(('service','courier','url'), [
    ('Evri Next Day - Account Linked', 'Evri', 'https://www.evri.com/track-a-parcel'),
    ('Royal Mail 48 Tracked', 'Royal Mail', 'https://www.royalmail.com/track-your-item'),
    ('Parcelforce Express 24', 'Parcelforce', 'https://www.parcelforce.com/track-trace'),
    ('DPD Next Day', 'DPD', 'https://tracking.dpd.co.uk/'),
    ('DHL Express Next Day', 'DHL Express', 'https://uk.express.dhl.com/track-a-parcel/'),
    ('DHL eCommerce UK', 'DHL eCommerce UK', 'https://track.dhlparcel.co.uk/'),
    ('DHL Parcel UK - Account Linked', 'DHL eCommerce UK', 'https://track.dhlparcel.co.uk/'),
    ('UPS Saver', 'UPS', 'https://www.ups.com/track'),
    ('FedEx Priority', 'FedEx', 'https://www.fedex.com/en-gb/tracking.html'),
])
def test_courier_allowlist(service, courier, url):
    from main import courier_from_postal_service
    assert courier_from_postal_service(service) == {'courier': courier, 'tracking_page_url': url}


@pytest.mark.parametrize('service', [
    None, '', 'DHL', 'DHL UK', 'Amazon Buy Shipping', 'Unspecified',
    'Evri Royal Mail', 'DHL Express & DHL eCommerce', 'Evri Amazon Buy Shipping',
    'Amazon Logistics', 'Next Day',
])
def test_ambiguous_or_unapproved_couriers_never_guess(service):
    from main import courier_from_postal_service
    assert courier_from_postal_service(service) is None


def test_missing_service_escalates_and_vendor_is_not_used():
    from main import format_reference_only
    order = {**SAMPLE, 'ShippingInfo': {
        'TrackingNumber': 'TRACK123', 'Vendor': 'Royal Mail'
    }}
    result = format_reference_only(order, 'NG-123')
    assert result['found'] is False
    assert result['reason'] == 'courier_unrecognised'
    assert 'TRACK123' not in str(result)


def test_courier_page_is_generic_and_no_tracking_claim_of_delivery():
    from main import format_reference_only
    result = format_reference_only(SAMPLE, 'NG-123')
    assert result['order']['tracking_page_url'].endswith('/track-your-item')
    assert 'TRACK123' not in result['order']['tracking_page_url']
    assert 'dispatched' not in result['customer_message'].lower()
    assert 'delivered' not in result['customer_message'].lower()
    assert 'postcode if requested' in result['customer_message']


def test_multiple_couriers_return_correct_pairing_without_combined_url():
    from main import format_matching_multi_orders
    one, two = sample_split_orders()
    one['ShippingInfo']['PostalServiceName'] = 'Evri Standard'
    two['ShippingInfo']['PostalServiceName'] = 'Royal Mail Tracked'
    result = format_matching_multi_orders([one, two], '#NORTHERN115347')
    assert result['action'] == 'answer_customer'
    assert result['order']['tracking_page_url'] is None
    assert result['order']['tracking_numbers'] == ['TRACK-A', 'TRACK-B']
    assert result['order']['shipments'] == [
        {'tracking_number': 'TRACK-A', 'courier': 'Evri', 'tracking_page_url': 'https://www.evri.com/track-a-parcel'},
        {'tracking_number': 'TRACK-B', 'courier': 'Royal Mail', 'tracking_page_url': 'https://www.royalmail.com/track-your-item'},
    ]
    assert 'Evri' in result['customer_message']
    assert 'Royal Mail' in result['customer_message']


def test_multi_with_unapproved_courier_escalates_everything():
    from main import format_matching_multi_orders
    one, two = sample_split_orders()
    two['ShippingInfo']['PostalServiceName'] = 'DHL'
    result = format_matching_multi_orders([one, two], '#NORTHERN115347')
    assert result['action'] == 'escalate_to_agent'
    assert result['reason'] == 'courier_unrecognised'
    assert result['order'] is None
    assert 'TRACK-A' not in str(result)


def test_one_tracking_number_with_conflicting_couriers_escalates():
    from main import format_matching_multi_orders
    one, two = sample_split_orders()
    two['ShippingInfo']['TrackingNumber'] = 'TRACK-A'
    two['ShippingInfo']['PostalServiceName'] = 'Evri Standard'
    result = format_matching_multi_orders([one, two], '#NORTHERN115347')
    assert result['reason'] == 'conflicting_courier_for_tracking'
