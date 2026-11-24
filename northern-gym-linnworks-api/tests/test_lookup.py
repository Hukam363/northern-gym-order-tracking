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
