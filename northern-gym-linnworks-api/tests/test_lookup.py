from main import reference_matches, format_order, extract_email, escalate

SAMPLE = {
    'OrderId': 'abc', 'NumOrderId': 4321, 'Processed': True,
    'GeneralInfo': {'ReferenceNum': 'NG-123', 'Source': 'SHOPIFY'},
    'ShippingInfo': {'TrackingNumber': 'TRACK123', 'Vendor': 'Royal Mail'},
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
    assert result['order'] == {'status': 'dispatched', 'tracking_number': 'TRACK123'}
    assert result['reason'] == 'reference_only_tracking_available'
    assert 'TRACK123' in result['customer_message']
    assert 'Royal Mail' not in str(result)
    assert 'SKU-1' not in str(result)
    assert 'client@example.com' not in str(result)


def test_reference_only_without_tracking_escalates():
    from main import format_reference_only
    order = {**SAMPLE, 'ShippingInfo': {}}
    result = format_reference_only(order, 'NG-123')
    assert result['action'] == 'escalate_to_agent'
    assert result['order'] == {'status': 'processing_or_unconfirmed', 'tracking_number': None}


def test_reference_only_on_hold_escalates():
    from main import format_reference_only
    order = {**SAMPLE, 'GeneralInfo': {**SAMPLE['GeneralInfo'], 'HoldOrCancel': True}}
    result = format_reference_only(order, 'NG-123')
    assert result['action'] == 'escalate_to_agent'
    assert result['order'] == {'status': 'requires_agent', 'tracking_number': None}
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
        assert ref_only.json()['order'] == {'status': 'dispatched', 'tracking_number': 'TRACK123'}
        assert ref_only.json()['reason'] == 'reference_only_tracking_available'

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
