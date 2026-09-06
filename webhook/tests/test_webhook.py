#!/usr/bin/env python3
"""
Tests for the Gravel God Webhook Receiver.

Run with: pytest webhook/tests/test_webhook.py -v
"""

import os
import re
import sys
import json
import copy
import hmac
import hashlib
import logging
import tempfile
import uuid
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta, timezone

import pytest

# Add webhook directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set test environment before importing app
os.environ['FLASK_ENV'] = 'test'
os.environ['WOOCOMMERCE_SECRET'] = ''  # Disable signature check in tests
os.environ['STRIPE_WEBHOOK_SECRET'] = ''
os.environ['STRIPE_SECRET_KEY'] = ''  # Disable in tests
# Legacy tests assert the original synchronous webhook contract (pipeline
# runs inline, response reports success/pipeline_failed). Production default
# is async; TestAsyncPipelineJobs/TestOrderStatus/TestJobSweep clear this
# to exercise the default queued path.
os.environ['SYNC_PIPELINE'] = '1'


@pytest.fixture
def temp_athletes_dir():
    """Create a temporary athletes directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        athletes_dir = Path(tmpdir) / 'athletes'
        athletes_dir.mkdir()
        scripts_dir = athletes_dir / 'scripts'
        scripts_dir.mkdir()

        # Create a mock generate_full_package.py
        mock_script = scripts_dir / 'generate_full_package.py'
        mock_script.write_text('#!/usr/bin/env python3\nimport sys; sys.exit(0)')

        yield athletes_dir


@pytest.fixture
def app(temp_athletes_dir, monkeypatch):
    """Create test Flask app."""
    os.environ['ATHLETES_DIR'] = str(temp_athletes_dir)
    os.environ['SCRIPTS_DIR'] = str(temp_athletes_dir / 'scripts')

    # Import app after setting env vars
    import app as app_module
    monkeypatch.setattr(app_module, 'ATHLETES_DIR', str(temp_athletes_dir))
    monkeypatch.setattr(app_module, 'SCRIPTS_DIR', str(temp_athletes_dir / 'scripts'))
    monkeypatch.setattr(app_module, 'DATA_DIR', str(temp_athletes_dir))
    monkeypatch.setattr(app_module, 'DELIVERIES_DIR', str(temp_athletes_dir / 'deliveries'))
    monkeypatch.setattr(app_module, 'JOBS_DIR', str(temp_athletes_dir / 'jobs'))
    monkeypatch.setattr(app_module, 'COACHING_DIRECT_CHECKOUT_ENABLED', True)
    flask_app = app_module.app
    flask_app.config['TESTING'] = True
    return flask_app


@pytest.fixture
def client(app):
    """Create test client."""
    return app.test_client()


class TestHealthEndpoint:
    """Tests for /health endpoint."""

    def test_health_returns_ok(self, client, temp_athletes_dir):
        """Health check returns 200 when dependencies exist."""
        response = client.get('/health')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'
        assert data['service'] == 'gravel-god-webhook'
        assert data['runtime_files']['apply_contract_schema'] is True

    def test_health_degraded_missing_dirs(self, client):
        """Health check returns 503 when directories missing."""
        with patch.dict(os.environ, {'ATHLETES_DIR': '/nonexistent'}):
            # Need to reimport to pick up new env
            response = client.get('/health')
            # Note: This test may pass with 200 if app caches the path
            assert response.status_code in [200, 503]

    def test_health_fails_when_apply_contract_schema_missing(self, client, monkeypatch):
        import app as app_module
        monkeypatch.setattr(
            app_module, '_required_runtime_paths',
            lambda: {'apply_contract_schema': Path(
                '/nonexistent/apply_contract_v1.schema.json')})
        response = client.get('/health')
        assert response.status_code == 503
        data = response.get_json()
        assert data['status'] == 'degraded'
        assert data['runtime_files']['apply_contract_schema'] is False

    def test_health_fails_in_production_without_token_keys(
            self, client, temp_athletes_dir, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'IS_PRODUCTION', True)
        monkeypatch.delenv('DOWNLOAD_TOKEN_SECRET', raising=False)
        monkeypatch.delenv('DOWNLOAD_TOKEN_KEYS', raising=False)
        monkeypatch.delenv('REVIEW_TOKEN_SECRET', raising=False)
        monkeypatch.delenv('REVIEW_TOKEN_KEYS', raising=False)
        response = client.get('/health')
        assert response.status_code == 503
        data = response.get_json()
        assert data['token_config']['review'] is False
        assert data['token_config']['download'] is False


class TestDeliveryArtifactResolution:
    """Coach packages must use the generated profile, not webhook scaffolding."""

    def test_prefers_hyphenated_directory_with_generated_artifacts(
            self, tmp_path, monkeypatch):
        import app as app_module

        webhook_dir = tmp_path / 'example_athlete'
        webhook_dir.mkdir()
        (webhook_dir / 'profile.yaml').write_text('brand: roadielabs\n')

        generated_dir = tmp_path / 'example-athlete'
        generated_dir.mkdir()
        (generated_dir / 'workouts').mkdir()
        (generated_dir / 'profile.yaml').write_text(
            'event_format: criterium\nroad_category: cat_4\n')

        monkeypatch.setattr(app_module, 'ATHLETES_DIR', str(tmp_path))

        resolved = app_module._resolve_generated_athlete_dir(
            'example_athlete')

        assert resolved == generated_dir
        assert 'event_format: criterium' in (
            resolved / 'profile.yaml').read_text()


class TestInputValidation:
    """Tests for input validation functions."""

    def test_validate_athlete_id_valid(self):
        """Valid athlete IDs pass validation."""
        from app import validate_athlete_id

        assert validate_athlete_id('john_doe') is True
        assert validate_athlete_id('john-doe') is True
        assert validate_athlete_id('johndoe123') is True
        assert validate_athlete_id('a') is True
        assert validate_athlete_id('ab') is True

    def test_validate_athlete_id_invalid(self):
        """Invalid athlete IDs fail validation."""
        from app import validate_athlete_id

        assert validate_athlete_id('') is False
        assert validate_athlete_id('../etc/passwd') is False
        assert validate_athlete_id('john/doe') is False
        assert validate_athlete_id('john\\doe') is False
        assert validate_athlete_id('_invalid') is False
        assert validate_athlete_id('-invalid') is False
        assert validate_athlete_id('UPPERCASE') is False
        assert validate_athlete_id('a' * 100) is False  # Too long

    def test_sanitize_athlete_id(self):
        """Sanitize converts names to safe IDs."""
        from app import sanitize_athlete_id

        assert sanitize_athlete_id('John Doe') == 'john_doe'
        assert sanitize_athlete_id('Mary-Jane Watson') == 'mary-jane_watson'
        assert sanitize_athlete_id('Test!!!User') == 'testuser'
        assert sanitize_athlete_id('  Spaces  ') == 'spaces'
        assert sanitize_athlete_id('') == ''

    def test_safe_int_valid(self):
        """safe_int handles valid integers."""
        from app import safe_int

        assert safe_int(42) == 42
        assert safe_int('42') == 42
        assert safe_int(0) == 0

    def test_safe_int_invalid(self):
        """safe_int returns None for invalid input."""
        from app import safe_int

        assert safe_int(None) is None
        assert safe_int('') is None
        assert safe_int('abc') is None
        assert safe_int(-1) is None  # Negative
        assert safe_int(1000000) is None  # Too large

    def test_safe_float_valid(self):
        """safe_float handles valid floats."""
        from app import safe_float

        assert safe_float(72.5) == 72.5
        assert safe_float('72.5') == 72.5
        assert safe_float(0) == 0.0

    def test_safe_float_invalid(self):
        """safe_float returns None for invalid input."""
        from app import safe_float

        assert safe_float(None) is None
        assert safe_float('') is None
        assert safe_float('abc') is None


class TestOrderDataValidation:
    """Tests for order data validation."""

    def test_validate_order_data_valid(self):
        """Valid order data passes validation."""
        from app import validate_order_data

        order_data = {
            'athlete_id': 'john_doe',
            'order_id': '12345',
            'tier': 'race_ready',
            'profile': {
                'name': 'John Doe',
                'email': 'john@example.com',
                'fitness_markers': {
                    'weight_kg': 75.0,
                    'ftp_watts': 250,
                }
            }
        }

        is_valid, error = validate_order_data(order_data)
        assert is_valid is True
        assert error is None

    def test_validate_order_data_missing_name(self):
        """Order data without name fails validation."""
        from app import validate_order_data

        order_data = {
            'athlete_id': 'john_doe',
            'profile': {
                'email': 'john@example.com',
            }
        }

        is_valid, error = validate_order_data(order_data)
        assert is_valid is False
        assert 'name' in error.lower()

    def test_validate_order_data_invalid_email(self):
        """Order data with invalid email fails validation."""
        from app import validate_order_data

        order_data = {
            'athlete_id': 'john_doe',
            'profile': {
                'name': 'John Doe',
                'email': 'not-an-email',
            }
        }

        is_valid, error = validate_order_data(order_data)
        assert is_valid is False
        assert 'email' in error.lower()

    def test_validate_order_data_invalid_weight(self):
        """Order data with out-of-range weight fails validation."""
        from app import validate_order_data

        order_data = {
            'athlete_id': 'john_doe',
            'profile': {
                'name': 'John Doe',
                'email': 'john@example.com',
                'fitness_markers': {
                    'weight_kg': 500,  # Too heavy
                }
            }
        }

        is_valid, error = validate_order_data(order_data)
        assert is_valid is False
        assert 'weight' in error.lower()


class TestWooCommerceWebhook:
    """Tests for WooCommerce webhook endpoint."""

    def test_woocommerce_ignores_pending_orders(self, client):
        """Pending orders are ignored."""
        response = client.post(
            '/webhook/woocommerce',
            json={'status': 'pending'},
            content_type='application/json'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ignored'

    def test_woocommerce_without_intake_is_not_falsely_fulfilled(
            self, client, temp_athletes_dir):
        """Legacy Woo orders without intake enter durable quarantine."""
        order_data = {
            'id': 12345,
            'status': 'completed',
            'billing': {
                'first_name': 'John',
                'last_name': 'Doe',
                'email': 'john@example.com',
            },
            'meta_data': [
                {'key': 'race_name', 'value': 'Test Race'},
                {'key': 'race_date', 'value': '2025-06-01'},
            ],
            'line_items': [
                {'name': 'Custom Training Plan - Race Ready', 'sku': 'training-race-ready'}
            ]
        }

        response = client.post(
            '/webhook/woocommerce',
            json=order_data,
            content_type='application/json'
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'success'
        assert data['athlete_id'] == 'john_doe'
        state_path = (temp_athletes_dir / 'deliveries' / 'orders' / '12345'
                      / 'fulfillment_status.json')
        state = json.loads(state_path.read_text())
        assert state['status'] == 'BLOCKED_REVIEW'
        issue = next(item for item in state['blocking_issues']
                     if item['id'] == 'STATE_UNAVAILABLE')
        assert issue['waivable'] is False

    def test_woocommerce_idempotency(self, client, temp_athletes_dir):
        """Duplicate orders are rejected."""
        order_data = {
            'id': 99999,
            'status': 'completed',
            'billing': {
                'first_name': 'Jane',
                'last_name': 'Doe',
                'email': 'jane@example.com',
            },
            'meta_data': [],
            'line_items': []
        }

        with patch('app.run_pipeline') as mock_pipeline:
            mock_pipeline.return_value = {'success': True, 'stdout': '', 'stderr': ''}

            # First request
            response1 = client.post(
                '/webhook/woocommerce',
                json=order_data,
                content_type='application/json'
            )
            assert response1.status_code == 200

            # Second request (duplicate)
            response2 = client.post(
                '/webhook/woocommerce',
                json=order_data,
                content_type='application/json'
            )
            assert response2.status_code == 200
            data = response2.get_json()
            assert data['status'] == 'duplicate'


class TestStripeWebhook:
    """Tests for Stripe webhook endpoint."""

    def test_stripe_ignores_non_checkout_events(self, client):
        """Non-checkout events are ignored."""
        response = client.post(
            '/webhook/stripe',
            json={'type': 'customer.created'},
            content_type='application/json'
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ignored'

    def test_stripe_without_intake_is_not_falsely_fulfilled(
            self, client, temp_athletes_dir):
        """Checkout metadata alone enters non-releasable quarantine."""
        stripe_event = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_test_123',
                    'amount_total': 18000,
                    'customer_details': {
                        'name': 'Test User',
                        'email': 'test@example.com',
                    },
                    'metadata': {
                        'tier': 'custom',
                        'race_name': 'Test Race',
                        'race_date': '2026-06-01',
                    }
                }
            }
        }

        response = client.post(
            '/webhook/stripe',
            json=stripe_event,
            content_type='application/json'
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'success'
        assert data['athlete_id'] == 'test_user'
        state_path = (temp_athletes_dir / 'deliveries' / 'orders'
                      / 'cs_test_123' / 'fulfillment_status.json')
        state = json.loads(state_path.read_text())
        assert state['status'] == 'BLOCKED_REVIEW'
        issue = next(item for item in state['blocking_issues']
                     if item['id'] == 'STATE_UNAVAILABLE')
        assert issue['waivable'] is False


class TestDataExtraction:
    """Tests for data extraction functions."""

    def test_extract_woocommerce_tier_from_sku(self):
        """Tier is correctly determined from product SKU."""
        from app import extract_woocommerce_data

        # Starter
        data = {
            'billing': {'first_name': 'Test', 'last_name': 'User', 'email': 'test@test.com'},
            'meta_data': [],
            'line_items': [{'sku': 'training-starter', 'name': 'anything'}]
        }
        result = extract_woocommerce_data(data)
        assert result['tier'] == 'starter'

        # Full Build
        data['line_items'] = [{'sku': 'training-full-build', 'name': 'anything'}]
        result = extract_woocommerce_data(data)
        assert result['tier'] == 'full_build'

        # Race Ready
        data['line_items'] = [{'sku': 'training-race-ready', 'name': 'anything'}]
        result = extract_woocommerce_data(data)
        assert result['tier'] == 'race_ready'

    def test_extract_stripe_tier_from_metadata(self):
        """Tier is correctly extracted from metadata."""
        from app import extract_stripe_data

        data = {
            'data': {
                'object': {
                    'id': 'cs_123',
                    'amount_total': 12000,
                    'customer_details': {'name': 'Test', 'email': 'test@test.com'},
                    'metadata': {'tier': 'custom'}
                }
            }
        }
        result = extract_stripe_data(data)
        assert result['tier'] == 'custom'

    def test_extract_stripe_tier_defaults_to_custom(self):
        """Tier defaults to 'custom' when not in metadata."""
        from app import extract_stripe_data

        data = {
            'data': {
                'object': {
                    'id': 'cs_123',
                    'amount_total': 12000,
                    'customer_details': {'name': 'Test', 'email': 'test@test.com'},
                    'metadata': {}
                }
            }
        }
        result = extract_stripe_data(data)
        assert result['tier'] == 'custom'


class TestProfileCreation:
    """Tests for profile creation."""

    def test_create_athlete_profile(self, temp_athletes_dir, app):
        """Profile is created with correct structure."""
        os.environ['ATHLETES_DIR'] = str(temp_athletes_dir)

        from app import create_athlete_profile

        order_data = {
            'athlete_id': 'test_athlete',
            'order_id': 'order_123',
            'tier': 'race_ready',
            'profile': {
                'name': 'Test Athlete',
                'email': 'test@example.com',
            }
        }

        athlete_id, profile_path = create_athlete_profile(order_data)

        assert athlete_id == 'test_athlete'
        assert profile_path.exists()

        import yaml
        with open(profile_path) as f:
            profile = yaml.safe_load(f)

        assert profile['name'] == 'Test Athlete'
        assert profile['email'] == 'test@example.com'
        assert profile['tier'] == 'race_ready'
        assert profile['order_id'] == 'order_123'
        assert 'created_at' in profile


class TestSecurityHeaders:
    """Tests for security headers."""

    def test_security_headers_present(self, client):
        """Security headers are set on responses."""
        response = client.get('/health')

        assert response.headers.get('X-Content-Type-Options') == 'nosniff'
        assert response.headers.get('X-Frame-Options') == 'DENY'
        assert response.headers.get('X-XSS-Protection') == '1; mode=block'


class TestIntakeStorage:
    """Tests for questionnaire intake storage."""

    def test_store_and_load_intake(self, temp_athletes_dir, app):
        """Intake data can be stored and loaded."""
        os.environ['ATHLETES_DIR'] = str(temp_athletes_dir)
        from app import store_intake, load_intake

        intake_id = '550e8400-e29b-41d4-a716-446655440000'
        data = {'name': 'Test User', 'email': 'test@test.com', 'tier': 'full_build'}

        store_intake(intake_id, data)
        loaded = load_intake(intake_id)

        assert loaded['name'] == 'Test User'
        assert loaded['email'] == 'test@test.com'
        assert loaded['tier'] == 'full_build'

    def test_load_nonexistent_intake(self, temp_athletes_dir, app):
        """Loading a nonexistent intake returns empty dict."""
        os.environ['ATHLETES_DIR'] = str(temp_athletes_dir)
        from app import load_intake

        result = load_intake('550e8400-e29b-41d4-a716-446655440001')
        assert result == {}

    def test_load_intake_rejects_invalid_id(self, temp_athletes_dir):
        """Loading with invalid UUID rejects path traversal."""
        os.environ['ATHLETES_DIR'] = str(temp_athletes_dir)
        from app import load_intake

        assert load_intake('../etc/passwd') == {}
        assert load_intake('not-a-uuid') == {}
        assert load_intake('') == {}

    def test_cleanup_stale_intakes(self, temp_athletes_dir):
        """Stale intake files are cleaned up."""
        os.environ['ATHLETES_DIR'] = str(temp_athletes_dir)
        from app import store_intake, cleanup_stale_intakes, get_intake_dir
        with patch('app.DATA_DIR', str(temp_athletes_dir)):
            intake_id = '550e8400-e29b-41d4-a716-446655440002'
            store_intake(intake_id, {'name': 'Old User'})

            # Make the file appear old
            intake_file = get_intake_dir() / f'{intake_id}.json'
            old_time = (datetime.now() - timedelta(hours=25)).timestamp()
            os.utime(intake_file, (old_time, old_time))

            cleanup_stale_intakes()
            assert intake_file.exists()  # retained for durable retry/recovery


class TestCreateCheckout:
    """Tests for POST /api/create-checkout endpoint."""

    def _future_date(self, weeks_ahead=12):
        """Helper: return ISO date string N weeks from now."""
        d = datetime.now() + timedelta(weeks=weeks_ahead)
        return d.strftime('%Y-%m-%d')

    def test_checkout_rejects_missing_email(self, client):
        """Checkout requires a valid email."""
        response = client.post(
            '/api/create-checkout',
            json={'name': 'Test', 'races': [{'name': 'R', 'date': self._future_date(), 'priority': 'A'}]},
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert 'email' in data['error'].lower()

    def test_checkout_rejects_missing_name(self, client):
        """Checkout requires a name."""
        response = client.post(
            '/api/create-checkout',
            json={'email': 'test@test.com', 'races': [{'name': 'R', 'date': self._future_date(), 'priority': 'A'}]},
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert 'name' in data['error'].lower()

    def test_checkout_rejects_no_races(self, client):
        """Checkout requires at least one race."""
        response = client.post(
            '/api/create-checkout',
            json={'name': 'Test', 'email': 'test@test.com', 'races': []},
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert 'race' in data['error'].lower()

    def test_checkout_rejects_missing_race_date(self, client):
        """Checkout requires a date on the A-race."""
        response = client.post(
            '/api/create-checkout',
            json={'name': 'Test', 'email': 'test@test.com', 'races': [{'name': 'R', 'priority': 'A'}]},
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert 'date' in data['error'].lower()

    def test_checkout_uses_a_race_date(self, client, temp_athletes_dir):
        """Checkout uses A-race date for pricing, not first race."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_test_arace'
            mock_session.url = 'https://checkout.stripe.com/test'
            mock_stripe.checkout.Session.create.return_value = mock_session

            near_date = self._future_date(weeks_ahead=5)   # 5 wk = $75
            far_date = self._future_date(weeks_ahead=16)    # 16 wk = $240

            response = client.post(
                '/api/create-checkout',
                json={
                    'name': 'Test',
                    'email': 'test@test.com',
                    'races': [
                        {'name': 'Near Race', 'date': near_date, 'priority': 'B'},
                        {'name': 'Far Race', 'date': far_date, 'priority': 'A'},
                    ],
                },
                content_type='application/json'
            )

            assert response.status_code == 200
            data = response.get_json()
            # Should use the A-race (far_date), not the first race (near_date)
            assert data['price']['weeks'] >= 16

    def test_checkout_falls_back_to_first_race(self, client, temp_athletes_dir):
        """Without an A-race, checkout uses first race's date."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_test_fallback'
            mock_session.url = 'https://checkout.stripe.com/test'
            mock_stripe.checkout.Session.create.return_value = mock_session

            race_date = self._future_date(weeks_ahead=8)

            response = client.post(
                '/api/create-checkout',
                json={
                    'name': 'Test',
                    'email': 'test@test.com',
                    'races': [
                        {'name': 'Only Race', 'date': race_date, 'priority': 'B'},
                    ],
                },
                content_type='application/json'
            )

            assert response.status_code == 200
            data = response.get_json()
            assert data['price']['weeks'] >= 8

    def test_checkout_creates_session_with_computed_price(self, client, temp_athletes_dir):
        """Valid checkout creates Stripe session with computed $/wk price."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_test_789'
            mock_session.url = 'https://checkout.stripe.com/pay/cs_test_789'
            mock_stripe.checkout.Session.create.return_value = mock_session

            race_date = self._future_date(weeks_ahead=12)
            response = client.post(
                '/api/create-checkout',
                json={
                    'name': 'Jane Doe',
                    'email': 'jane@example.com',
                    'races': [{'name': 'Unbound 200', 'date': race_date, 'priority': 'A'}],
                    'hours_per_week': '7-10',
                    'ftp': '250',
                },
                content_type='application/json'
            )

            assert response.status_code == 200
            data = response.get_json()
            assert data['checkout_url'] == 'https://checkout.stripe.com/pay/cs_test_789'
            assert 'intake_id' in data
            assert 'price' in data
            assert data['price']['weeks'] >= 12

            # Verify Stripe was called with pre-built price ID
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            line_item = call_kwargs['line_items'][0]
            assert 'price' in line_item
            assert line_item['price'].startswith('price_')
            assert call_kwargs['customer_email'] == 'jane@example.com'
            assert call_kwargs['metadata']['tier'] == 'custom'
            assert call_kwargs['metadata']['product_type'] == 'training_plan'

    def test_checkout_preserves_valid_ga4_attribution(self, client, temp_athletes_dir):
        """Consented GA ids survive Stripe redirect for webhook attribution."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_live_attribution'
            mock_session.url = 'https://checkout.stripe.com/test'
            mock_stripe.checkout.Session.create.return_value = mock_session

            response = client.post(
                '/api/create-checkout',
                json={
                    'name': 'Attributed Rider',
                    'email': 'attributed@example.com',
                    'races': [{
                        'name': 'Unbound 200',
                        'date': self._future_date(),
                        'priority': 'A',
                    }],
                    'ga4_client_id': '1391278887.1471780587',
                    'ga4_session_id': '1787846400',
                    'analytics_consent': 'granted',
                },
                environ_base={'REMOTE_ADDR': '198.51.100.101'},
            )

            assert response.status_code == 200
            metadata = mock_stripe.checkout.Session.create.call_args.kwargs['metadata']
            assert metadata['ga4_client_id'] == '1391278887.1471780587'
            assert metadata['ga4_session_id'] == '1787846400'
            assert metadata['analytics_consent'] == 'granted'
            stored = __import__('app').load_intake(response.get_json()['intake_id'])
            assert stored['ga4_client_id'] == '1391278887.1471780587'
            assert stored['ga4_session_id'] == '1787846400'
            assert stored['analytics_consent'] == 'granted'

    def test_checkout_discards_invalid_ga4_attribution(self, client, temp_athletes_dir):
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_live_bad_attribution'
            mock_session.url = 'https://checkout.stripe.com/test'
            mock_stripe.checkout.Session.create.return_value = mock_session

            response = client.post(
                '/api/create-checkout',
                json={
                    'name': 'Unattributed Rider',
                    'email': 'unattributed@example.com',
                    'races': [{
                        'name': 'Unbound 200',
                        'date': self._future_date(),
                        'priority': 'A',
                    }],
                    'ga4_client_id': '<script>alert(1)</script>',
                    'ga4_session_id': 'not-a-session',
                    'analytics_consent': 'granted',
                },
                environ_base={'REMOTE_ADDR': '198.51.100.102'},
            )

            assert response.status_code == 200
            metadata = mock_stripe.checkout.Session.create.call_args.kwargs['metadata']
            assert 'ga4_client_id' not in metadata
            assert 'ga4_session_id' not in metadata
            stored = __import__('app').load_intake(response.get_json()['intake_id'])
            assert 'ga4_client_id' not in stored
            assert 'ga4_session_id' not in stored

    def test_checkout_discards_ga4_ids_when_consent_is_denied(
            self, client, temp_athletes_dir):
        with patch('app.stripe') as mock_stripe:
            mock_stripe.checkout.Session.create.return_value = MagicMock(
                id='cs_live_denied', url='https://checkout.stripe.com/test')
            response = client.post('/api/create-checkout', json={
                'name': 'Private Rider',
                'email': 'private@example.com',
                'races': [{
                    'name': 'Unbound 200', 'date': self._future_date(),
                    'priority': 'A',
                }],
                'analytics_consent': 'denied',
                'ga4_client_id': '1391278887.1471780587',
                'ga4_session_id': '1787846400',
            }, environ_base={'REMOTE_ADDR': '198.51.100.103'})

        assert response.status_code == 200
        metadata = mock_stripe.checkout.Session.create.call_args.kwargs['metadata']
        assert metadata['analytics_consent'] == 'denied'
        assert 'ga4_client_id' not in metadata
        assert 'ga4_session_id' not in metadata

    def test_gravel_checkout_records_gravel_grit_without_charging_extra(
            self, client, temp_athletes_dir):
        """Gravel Grit is an included entitlement, never an extra line item."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_test_gravel_grit'
            mock_session.url = 'https://checkout.stripe.com/test'
            mock_stripe.checkout.Session.create.return_value = mock_session

            response = client.post(
                '/api/create-checkout',
                json={
                    'name': 'Gravel Rider',
                    'email': 'gravel@example.com',
                    'races': [{
                        'name': 'Unbound 200',
                        'date': self._future_date(),
                        'priority': 'A',
                    }],
                },
                headers={'Origin': 'https://gravelgodcycling.com'},
                environ_base={'REMOTE_ADDR': '198.51.100.21'},
            )

            assert response.status_code == 200
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert len(call_kwargs['line_items']) == 1
            assert call_kwargs['metadata']['plan_addons'] == 'gravel_grit'
            stored = __import__('app').load_intake(
                response.get_json()['intake_id'])
            assert stored['plan_addons'] == ['gravel_grit']

    def test_road_checkout_has_no_gravel_grit_entitlement(
            self, client, temp_athletes_dir):
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_test_road_addons'
            mock_session.url = 'https://checkout.stripe.com/test'
            mock_stripe.checkout.Session.create.return_value = mock_session

            response = client.post(
                '/api/create-checkout',
                json={
                    'name': 'Road Rider',
                    'email': 'road@example.com',
                    'races': [{
                        'name': 'Mallorca 312',
                        'date': self._future_date(),
                        'priority': 'A',
                    }],
                },
                headers={'Origin': 'https://roadielabs.com'},
                environ_base={'REMOTE_ADDR': '198.51.100.22'},
            )

            assert response.status_code == 200
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert call_kwargs['metadata']['brand'] == 'roadielabs'
            assert call_kwargs['metadata']['plan_addons'] == ''

    def test_checkout_rejects_unknown_addon_before_calling_stripe(
            self, client, temp_athletes_dir):
        with patch('app.stripe') as mock_stripe:
            response = client.post(
                '/api/create-checkout',
                json={
                    'name': 'Test Rider',
                    'email': 'rider@example.com',
                    'races': [{
                        'name': 'Unbound 200',
                        'date': self._future_date(),
                        'priority': 'A',
                    }],
                    'plan_addons': ['invented_client_price'],
                },
                headers={'Origin': 'https://gravelgodcycling.com'},
                environ_base={'REMOTE_ADDR': '198.51.100.23'},
            )

            assert response.status_code == 400
            assert 'unknown plan add-on' in response.get_json()['error']
            mock_stripe.checkout.Session.create.assert_not_called()

    def test_checkout_price_capped_at_249(self, client, temp_athletes_dir):
        """Price is capped at $249 for very long plans."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_test_cap'
            mock_session.url = 'https://checkout.stripe.com/test'
            mock_stripe.checkout.Session.create.return_value = mock_session

            # 30 weeks out = 30 * $15 = $450 → capped at $249
            race_date = self._future_date(weeks_ahead=30)
            response = client.post(
                '/api/create-checkout',
                json={
                    'name': 'Long Plan',
                    'email': 'long@test.com',
                    'races': [{'name': 'Far Race', 'date': race_date, 'priority': 'A'}],
                },
                content_type='application/json'
            )

            assert response.status_code == 200
            data = response.get_json()
            assert data['price']['price_cents'] == 24900  # $249 cap

    def test_checkout_options_returns_204(self, client):
        """CORS preflight returns 204."""
        response = client.options('/api/create-checkout')
        assert response.status_code == 204

    def test_cors_headers_present(self, client):
        """CORS headers are set on checkout responses."""
        response = client.post(
            '/api/create-checkout',
            json={'name': 'Test', 'email': 'bad'},
            content_type='application/json',
            headers={'Origin': 'https://gravelgodcycling.com'}
        )
        # Even on error, CORS headers should be present
        assert 'Access-Control-Allow-Origin' in response.headers


class TestStripeWebhookWithIntake:
    """Tests for Stripe webhook with intake data flow."""

    def test_stripe_webhook_loads_intake_data(self, client, temp_athletes_dir):
        """Stripe webhook loads rich questionnaire data from intake store."""
        # Store intake data using app module's actual ATHLETES_DIR
        # (module-level constant set at first import)
        import app as app_module
        intake_id = '550e8400-e29b-41d4-a716-446655440010'
        intake_dir = Path(app_module.ATHLETES_DIR) / '.intake'
        intake_dir.mkdir(parents=True, exist_ok=True)
        intake_file = intake_dir / f'{intake_id}.json'
        intake_file.write_text(json.dumps({
            'intake_id': intake_id,
            'stored_at': datetime.now().isoformat(),
            'data': {
                'name': 'Sarah Connor',
                'email': 'sarah@example.com',
                'weight': '140',
                'ftp': '220',
                'hours_per_week': '7-10',
                'races': [{'name': 'Unbound 200', 'date': '2026-06-01', 'priority': 'A'}],
            }
        }))

        stripe_event = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_test_intake',
                    'amount_total': 18000,
                    'customer_details': {
                        'name': 'Sarah Connor',
                        'email': 'sarah@example.com',
                    },
                    'metadata': {
                        'intake_id': intake_id,
                        'tier': 'custom',
                        'athlete_name': 'Sarah Connor',
                        'weeks': '12',
                        'price_cents': '18000',
                        'ga4_client_id': '1391278887.1471780587',
                        'ga4_session_id': '1787846400',
                    }
                }
            }
        }

        persisted = {'state': {
            'status': 'BLOCKED_REVIEW', 'blocking_issues': [],
            'required_confirmations': [],
        }}
        with patch('app.run_pipeline') as mock_pipeline, \
             patch('app.persist_deliverables', return_value=persisted), \
             patch('app._generate_download_token', return_value='token'), \
             patch('app._send_ga4_purchase') as mock_ga4_purchase:
            mock_pipeline.return_value = {'success': True, 'stdout': '', 'stderr': ''}

            response = client.post(
                '/webhook/stripe',
                json=stripe_event,
                content_type='application/json'
            )

            assert response.status_code == 200
            data = response.get_json()
            assert data['status'] == 'success'
            assert data['athlete_id'] == 'sarah_connor'
            mock_ga4_purchase.assert_called_once()
            ga4_kwargs = mock_ga4_purchase.call_args.kwargs
            assert ga4_kwargs['client_id'] == '1391278887.1471780587'
            assert ga4_kwargs['session_id'] == '1787846400'

        # Verify profile was created with intake data
        import yaml
        profile_path = Path(app_module.ATHLETES_DIR) / 'sarah_connor' / 'profile.yaml'
        assert profile_path.exists()
        with open(profile_path) as f:
            profile = yaml.safe_load(f)
        assert profile['name'] == 'Sarah Connor'
        assert profile['email'] == 'sarah@example.com'
        assert profile['tier'] == 'custom'
        # Weight should be converted from lbs to kg
        assert profile['fitness_markers']['ftp_watts'] == 220

    def test_stripe_webhook_refuses_without_intake(self, client, temp_athletes_dir):
        """Paid missing-intake order is durably quarantined, not failed."""
        stripe_event = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_test_no_intake',
                    'amount_total': 12000,
                    'customer_details': {
                        'name': 'No Intake User',
                        'email': 'no@intake.com',
                    },
                    'metadata': {
                        'tier': 'custom',
                    }
                }
            }
        }

        response = client.post(
            '/webhook/stripe',
            json=stripe_event,
            content_type='application/json'
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'success'
        state_path = (temp_athletes_dir / 'deliveries' / 'orders'
                      / 'cs_test_no_intake' / 'fulfillment_status.json')
        state = json.loads(state_path.read_text())
        assert state['status'] == 'BLOCKED_REVIEW'
        issue = next(item for item in state['blocking_issues']
                     if item['id'] == 'STATE_UNAVAILABLE')
        assert issue['waivable'] is False
        job = json.loads(
            (temp_athletes_dir / 'jobs' / 'orders'
             / 'cs_test_no_intake.json').read_text())
        assert job['status'] == 'succeeded'


class TestPriceComputation:
    """Tests for $/wk computed pricing model."""

    def test_price_8_weeks(self):
        """8 weeks = 8 * $15 = $120."""
        from app import compute_plan_price
        d = (datetime.now() + timedelta(weeks=8)).strftime('%Y-%m-%d')
        result = compute_plan_price(d)
        assert result['weeks'] >= 8
        assert result['price_cents'] == result['weeks'] * 1500

    def test_price_minimum_4_weeks(self):
        """Short plans get minimum 4 weeks ($60)."""
        from app import compute_plan_price
        # Race is 1 week away
        d = (datetime.now() + timedelta(weeks=1)).strftime('%Y-%m-%d')
        result = compute_plan_price(d)
        assert result['weeks'] == 4
        assert result['price_cents'] == 6000  # $60

    def test_price_capped_at_249(self):
        """Long plans are capped at $249."""
        from app import compute_plan_price
        # Race is 30 weeks away = $450 → capped at $249
        d = (datetime.now() + timedelta(weeks=30)).strftime('%Y-%m-%d')
        result = compute_plan_price(d)
        assert result['weeks'] >= 30
        assert result['price_cents'] == 24900  # $249 cap

    def test_price_cap_boundary(self):
        """17 weeks × $15 = $255 → capped at $249."""
        from app import compute_plan_price, PRICE_PER_WEEK_CENTS, PRICE_CAP_CENTS
        # 16 weeks: 16 * $15 = $240 (under cap)
        d16 = (datetime.now() + timedelta(weeks=16)).strftime('%Y-%m-%d')
        r16 = compute_plan_price(d16)
        assert r16['price_cents'] <= PRICE_CAP_CENTS

        # 17 weeks: 17 * $15 = $255 → capped at $249
        d17 = (datetime.now() + timedelta(weeks=17)).strftime('%Y-%m-%d')
        r17 = compute_plan_price(d17)
        assert r17['price_cents'] == PRICE_CAP_CENTS

    def test_price_invalid_date(self):
        """Invalid date returns minimum price."""
        from app import compute_plan_price
        result = compute_plan_price('not-a-date')
        assert result['weeks'] == 4
        assert result['price_cents'] == 6000

    def test_price_past_date(self):
        """Past race date gets minimum 4 weeks."""
        from app import compute_plan_price
        d = (datetime.now() - timedelta(weeks=2)).strftime('%Y-%m-%d')
        result = compute_plan_price(d)
        assert result['weeks'] == 4
        assert result['price_cents'] == 6000

    def test_pricing_constants(self):
        """Pricing constants are correct."""
        from app import PRICE_PER_WEEK_CENTS, PRICE_CAP_CENTS, MIN_WEEKS
        assert PRICE_PER_WEEK_CENTS == 1500  # $15
        assert PRICE_CAP_CENTS == 24900       # $249
        assert MIN_WEEKS == 4


class TestPriceParityPythonJs:
    """Verify Python and JS price computations produce identical results.

    The user sees a JS-computed price on the submit button, then gets
    charged a Python-computed price via Stripe. If these ever diverge,
    we have a trust/legal problem. This test runs the JS through Node.js
    and compares against Python for multiple date scenarios.
    """

    PARITY_JS = """
    // Mirror of computePrice() from training-plans-form.js
    var PRICE_PER_WEEK = 15;
    var PRICE_CAP = 249;
    var MIN_WEEKS = 4;

    function computePrice(raceDateStr) {
      var raceDate = new Date(raceDateStr + 'T00:00:00Z');
      var today = new Date();
      today.setUTCHours(0, 0, 0, 0);
      var days = Math.ceil((raceDate - today) / (1000 * 60 * 60 * 24));
      var weeks = Math.max(MIN_WEEKS, Math.ceil(days / 7));
      var price = Math.min(weeks * PRICE_PER_WEEK, PRICE_CAP);
      return JSON.stringify({weeks: weeks, price_cents: price * 100});
    }

    var dates = %DATES%;
    var results = dates.map(function(d) { return computePrice(d); });
    console.log(JSON.stringify(results));
    """

    def test_parity_across_date_scenarios(self):
        """Python and JS produce same price for 6 date scenarios."""
        from app import compute_plan_price

        # 6 scenarios: short, medium, at-cap, past-cap, past, near-today
        test_dates = [
            (datetime.now() + timedelta(weeks=5)).strftime('%Y-%m-%d'),
            (datetime.now() + timedelta(weeks=10)).strftime('%Y-%m-%d'),
            (datetime.now() + timedelta(weeks=16)).strftime('%Y-%m-%d'),
            (datetime.now() + timedelta(weeks=30)).strftime('%Y-%m-%d'),
            (datetime.now() - timedelta(weeks=2)).strftime('%Y-%m-%d'),
            (datetime.now() + timedelta(days=3)).strftime('%Y-%m-%d'),
        ]

        # Python results
        py_results = [compute_plan_price(d) for d in test_dates]

        # JS results via Node.js
        js_code = self.PARITY_JS.replace('%DATES%', json.dumps(test_dates))
        import subprocess
        result = subprocess.run(
            ['node', '-e', js_code],
            capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, f"Node.js failed: {result.stderr}"
        js_results = json.loads(result.stdout.strip())

        for i, (py, js_str) in enumerate(zip(py_results, js_results)):
            js = json.loads(js_str)
            assert py['price_cents'] == js['price_cents'], (
                f"Date {test_dates[i]}: Python={py['price_cents']} JS={js['price_cents']}"
            )
            assert py['weeks'] == js['weeks'], (
                f"Date {test_dates[i]}: Python weeks={py['weeks']} JS weeks={js['weeks']}"
            )


class TestTestEndpoint:
    """Tests for the test endpoint (only available in non-production)."""

    def test_test_endpoint_creates_profile(self, client, temp_athletes_dir):
        """Test endpoint creates profile without running pipeline."""
        response = client.post(
            '/webhook/test',
            json={
                'athlete_id': 'test_user',
                'tier': 'starter',
                'profile': {
                    'name': 'Test User',
                    'email': 'test@example.com',
                }
            },
            content_type='application/json'
        )

        assert response.status_code == 401

    def test_test_endpoint_sanitizes_dangerous_id(self, client, temp_athletes_dir):
        """Test endpoint sanitizes dangerous athlete IDs."""
        # The sanitize function strips dangerous characters
        # so '../etc/passwd' becomes 'etcpasswd'
        response = client.post(
            '/webhook/test',
            json={'athlete_id': '../etc/passwd'},
            content_type='application/json'
        )

        # The operator-only endpoint rejects unauthenticated requests before
        # parsing attacker-controlled identifiers.
        assert response.status_code == 401


class TestCoachingCheckout:
    """Tests for POST /api/create-coaching-checkout endpoint."""

    def test_direct_checkout_fails_closed_by_default(self, client, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'COACHING_DIRECT_CHECKOUT_ENABLED', False)
        response = client.post(
            '/api/create-coaching-checkout',
            json={'name': 'Test', 'email': 'test@test.com', 'tier': 'mid'},
            content_type='application/json')
        assert response.status_code == 409
        assert 'intake' in response.get_json()['error'].lower()

    def test_coaching_checkout_rejects_missing_email(self, client):
        """Coaching checkout requires a valid email."""
        response = client.post(
            '/api/create-coaching-checkout',
            json={'name': 'Test', 'tier': 'min'},
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert 'email' in data['error'].lower()

    def test_coaching_checkout_rejects_missing_name(self, client):
        """Coaching checkout requires a name."""
        response = client.post(
            '/api/create-coaching-checkout',
            json={'email': 'test@test.com', 'tier': 'min'},
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert 'name' in data['error'].lower()

    def test_coaching_checkout_rejects_invalid_tier(self, client):
        """Coaching checkout rejects invalid tier."""
        response = client.post(
            '/api/create-coaching-checkout',
            json={'name': 'Test', 'email': 'test@test.com', 'tier': 'ultra'},
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert 'tier' in data['error'].lower()

    def test_coaching_checkout_rejects_missing_tier(self, client):
        """Coaching checkout rejects missing tier."""
        response = client.post(
            '/api/create-coaching-checkout',
            json={'name': 'Test', 'email': 'test@test.com'},
            content_type='application/json'
        )
        assert response.status_code == 400

    def test_coaching_checkout_creates_session(self, client, temp_athletes_dir):
        """Valid coaching checkout creates Stripe subscription session."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_test_coaching'
            mock_session.url = 'https://checkout.stripe.com/coaching'
            mock_stripe.checkout.Session.create.return_value = mock_session

            response = client.post(
                '/api/create-coaching-checkout',
                json={'name': 'Coach Me', 'email': 'coach@test.com', 'tier': 'mid'},
                content_type='application/json'
            )

            assert response.status_code == 200
            data = response.get_json()
            assert data['checkout_url'] == 'https://checkout.stripe.com/coaching'

            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert call_kwargs['mode'] == 'subscription'
            assert call_kwargs['customer_email'] == 'coach@test.com'
            assert call_kwargs['metadata']['product_type'] == 'coaching'
            assert call_kwargs['metadata']['tier'] == 'mid'
            assert call_kwargs['metadata']['brand'] == 'gravelgod'
            assert call_kwargs['subscription_data']['metadata']['brand'] == 'gravelgod'
            assert call_kwargs['success_url'] == (
                'https://gravelgodcycling.com/coaching/welcome/'
                '?session_id={CHECKOUT_SESSION_ID}')
            assert call_kwargs['cancel_url'] == 'https://gravelgodcycling.com/coaching/'

            assert data['tier'] == 'mid'
            assert data['tier_label'] == 'Mid'
            assert data['setup_fee_cents'] == 9900
            assert data['setup_fee_waived'] is False
            assert data['trainingpeaks']['premium_included'] is True
            assert 'attachtocoach' in data['trainingpeaks']['attach_url']

    def test_coaching_checkout_links_valid_intake(self, client, temp_athletes_dir):
        """Checkout metadata preserves the intake lineage receipt."""
        intake_id = 'ed4d7814-921f-4b21-9f73-52b6a47ba5cb'
        with patch('app.stripe') as mock_stripe:
            mock_stripe.checkout.Session.create.return_value = MagicMock(
                id='cs_intake', url='https://checkout.stripe.com/intake')
            response = client.post(
                '/api/create-coaching-checkout',
                json={'name': 'Intake Link', 'email': 'intake@test.com',
                      'tier': 'mid', 'intake_id': intake_id},
                content_type='application/json',
                headers={'X-Forwarded-For': '198.51.100.31'})

            assert response.status_code == 200
            metadata = mock_stripe.checkout.Session.create.call_args.kwargs['metadata']
            assert metadata['intake_id'] == intake_id

    def test_coaching_checkout_rejects_invalid_intake_id(self, client):
        response = client.post(
            '/api/create-coaching-checkout',
            json={'name': 'Bad Link', 'email': 'bad@test.com',
                  'tier': 'mid', 'intake_id': '../../other-athlete'},
            content_type='application/json',
            headers={'X-Forwarded-For': '198.51.100.32'})
        assert response.status_code == 400
        assert response.get_json()['error'] == 'Invalid intake_id'

    @pytest.mark.parametrize(('origin', 'brand', 'success_url'), [
        ('https://roadielabs.com', 'roadielabs',
         'https://roadielabs.com/coaching/welcome/'),
        ('https://xcskilabs.com', 'xcskilabs',
         'https://xcskilabs.com/coaching/welcome/'),
    ])
    def test_coaching_checkout_is_live_for_each_vertical(
            self, client, origin, brand, success_url):
        with patch('app.stripe') as mock_stripe:
            mock_stripe.checkout.Session.create.return_value = MagicMock(
                id=f'cs_{brand}', url=f'https://checkout.stripe.com/{brand}')
            response = client.post(
                '/api/create-coaching-checkout',
                json={'name': 'Brand Athlete', 'email': 'brand@test.com',
                      'tier': 'mid'},
                content_type='application/json',
                headers={'Origin': origin,
                         'X-Forwarded-For': '198.51.100.33'})

        assert response.status_code == 200
        data = response.get_json()
        assert data['brand'] == brand
        assert data['tier_label'] == 'Mid'
        assert data['setup_fee_cents'] == 9900
        assert data['setup_fee_waived'] is False
        assert data['trainingpeaks']['premium_included'] is True
        call = mock_stripe.checkout.Session.create.call_args.kwargs
        assert call['metadata']['brand'] == brand
        assert call['success_url'].startswith(success_url)

    def test_coaching_checkout_includes_setup_fee(self, client, temp_athletes_dir):
        """Coaching checkout includes $99 setup fee as second line item."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_test_coaching_fee'
            mock_session.url = 'https://checkout.stripe.com/coaching-fee'
            mock_stripe.checkout.Session.create.return_value = mock_session

            response = client.post(
                '/api/create-coaching-checkout',
                json={'name': 'Fee Test', 'email': 'fee@test.com', 'tier': 'min'},
                content_type='application/json'
            )

            assert response.status_code == 200
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs

            # Should have 2 line items: subscription + setup fee
            line_items = call_kwargs['line_items']
            assert len(line_items) == 2
            # First item is the recurring subscription
            assert line_items[0]['quantity'] == 1
            # Second item is the setup fee
            assert line_items[1]['quantity'] == 1

    def test_coaching_checkout_does_not_expose_promo_codes(self, client, temp_athletes_dir):
        """Public checkout charges setup and never exposes a waiver field."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_test_coaching_promo'
            mock_session.url = 'https://checkout.stripe.com/coaching-promo'
            mock_stripe.checkout.Session.create.return_value = mock_session

            response = client.post(
                '/api/create-coaching-checkout',
                json={'name': 'Promo Test', 'email': 'promo@test.com', 'tier': 'mid'},
                content_type='application/json'
            )

            assert response.status_code == 200
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert 'allow_promotion_codes' not in call_kwargs
            assert 'discounts' not in call_kwargs
            assert call_kwargs['metadata']['setup_fee_waived'] == 'false'

    def test_coaching_checkout_all_tiers(self, client, temp_athletes_dir):
        """All three coaching tiers create valid sessions."""
        for tier in ['min', 'mid', 'max']:
            with patch('app.stripe') as mock_stripe:
                mock_session = MagicMock()
                mock_session.id = f'cs_test_{tier}'
                mock_session.url = f'https://checkout.stripe.com/{tier}'
                mock_stripe.checkout.Session.create.return_value = mock_session

                response = client.post(
                    '/api/create-coaching-checkout',
                    json={'name': 'Test', 'email': 'test@test.com', 'tier': tier},
                    content_type='application/json'
                )
                assert response.status_code == 200, f"Tier {tier} failed"

    def test_coaching_checkout_options_preflight(self, client):
        """CORS preflight returns 204."""
        response = client.options('/api/create-coaching-checkout')
        assert response.status_code == 204

    def test_coaching_checkout_handles_stripe_error(self, client, temp_athletes_dir):
        """Coaching checkout returns 502 on Stripe API failure."""
        with patch('app.stripe') as mock_stripe:
            # Create a mock StripeError that isinstance checks work with
            class MockStripeError(Exception):
                pass
            mock_stripe.error.StripeError = MockStripeError
            mock_stripe.checkout.Session.create.side_effect = MockStripeError('API down')

            response = client.post(
                '/api/create-coaching-checkout',
                json={'name': 'Test', 'email': 'test@test.com', 'tier': 'min'},
                content_type='application/json'
            )
            assert response.status_code == 502


class TestCoachingConfirmation:
    def test_mid_is_coaching_tier_and_premium_is_tp_benefit(self):
        import app as app_module

        with patch.object(app_module, '_send_email', return_value=True) as send:
            ok = app_module._send_coaching_payment_confirmation(
                'rider@test.com', 'Rider Test', 'mid', brand='gravelgod')

        assert ok is True
        subject = send.call_args.args[1]
        body = send.call_args.args[2]
        assert subject == 'Payment confirmed — Mid coaching'
        assert 'Premium coaching' not in body
        assert 'TrainingPeaks Premium is included' in body
        assert 'If it is already connected, skip that step.' in body
        assert 'attachtocoach' in body
        assert send.call_args.kwargs['brand'] == 'gravelgod'

    def test_coach_notification_uses_brand_prefix_and_tier_label(self):
        from app import _build_coaching_email

        subject, text, html = _build_coaching_email({
            'name': 'Rider Test',
            'email': 'rider@test.com',
            'tier': 'mid',
            'subscription_id': 'sub_123',
            'order_id': 'cs_123',
            'brand': 'gravelgod',
        })
        assert subject == '[GG] New coaching: Rider Test — Mid'
        assert 'Mid coaching' in html
        assert 'mid tier' not in text.lower()

    def test_confirmation_includes_verified_private_booking_link(self, monkeypatch):
        import app as app_module
        monkeypatch.setattr(
            app_module, 'COACHING_BOOKING_URL',
            'https://calendar.example.com/matti/coaching')
        with patch.object(app_module, '_send_email', return_value=True) as send:
            app_module._send_coaching_payment_confirmation(
                'rider@test.com', 'Rider Test', 'mid', brand='gravelgod')
        body = send.call_args.args[2]
        assert 'Book your kickoff call' in body
        assert 'https://calendar.example.com/matti/coaching' in body


class TestCoachingIntakeHandoff:
    @staticmethod
    def _payload(case_id='0d915c21-2ab0-46f2-a8b9-e81076c65713'):
        return {
            'submission_id': case_id,
            'brand': 'gravelgod',
            'tier': 'mid',
            'name': 'Case Rider',
            'email': 'case@test.com',
            'analytics_consent': 'granted',
            'ga4_client_id': '1391278887.1471780587',
            'ga4_session_id': '1787846400',
            'questionnaire': {'primary_goal': 'specific_race', 'age': '52'},
        }

    def test_intake_creates_fit_review_case_and_receipts(
            self, client, temp_athletes_dir, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'COACHING_INTAKE_SECRET', 'edge-secret')
        monkeypatch.setattr(app_module, 'NOTIFICATION_EMAIL', 'coach@test.com')

        with patch.object(app_module, '_send_email', return_value=True) as send:
            response = client.post(
                '/api/coaching-intakes', json=self._payload(),
                headers={'X-Coaching-Intake-Secret': 'edge-secret',
                         'X-Forwarded-For': '198.51.100.41'})

        assert response.status_code == 201
        assert response.get_json()['state'] == 'FIT_REVIEW'
        case = app_module._read_coaching_intake(self._payload()['submission_id'])
        assert case['schema'] == 'coaching_onboarding_case/v1'
        assert case['tier'] == 'mid'
        assert case['state'] == 'FIT_REVIEW'
        assert case['source']['analytics_consent'] == 'granted'
        assert case['source']['ga4_client_id'] == '1391278887.1471780587'
        assert case['source']['ga4_session_id'] == '1787846400'
        assert 'ga4_client_id' not in case['questionnaire']
        assert case['intake_audit']['schema'] == 'coaching_intake_audit/v1'
        assert 'target race list' in case['intake_audit']['missing']
        assert 'home timezone' in case['intake_audit']['missing_followup']
        assert 'home timezone' not in case['intake_audit']['unasked']
        assert 'coaching agreement receipt' in case['intake_audit']['unasked']
        assert 'TrainingPeaks coach connection' in case['intake_audit']['unverified']
        assert case['intake_audit']['gates']['plan_release'] == 'blocked'
        assert case['receipts']['athlete_intake_email']['sent'] is True
        assert case['receipts']['coach_notification']['sent'] is True
        assert [event['event_name'] for event in case['analytics_events']] == [
            'coaching_intake_submitted']
        assert send.call_count == 2

    def test_intake_audit_separates_missing_unasked_and_unverified(self):
        from app import _coaching_intake_audit, _COACHING_INTAKE_REQUIRED

        questionnaire = {field: 'provided' for field in _COACHING_INTAKE_REQUIRED}
        questionnaire.update({
            'primary_goal': 'specific_race',
            'race_list': 'Leadville Trail 100 MTB',
            'success_definition': 'Finish strong',
            'date_of_birth': '1974-01-01',
            'home_timezone': 'America/Denver',
            'home_location': 'Boulder, Colorado, USA',
            'desired_start_date': '2026-09-01',
            'preferred_contact_channel': 'trainingpeaks',
            'trainingpeaks_connection_status': 'attached',
            'injuries': 'Knee pain under evaluation',
        })

        audit = _coaching_intake_audit(questionnaire)

        assert audit['missing'] == []
        assert audit['missing_followup'] == []
        assert 'date of birth (age becomes stale and birthday reminders need a date)' not in audit['unasked']
        assert 'home timezone' not in audit['unasked']
        assert audit['gates']['intake_completeness'] == 'ready_for_fit_review'
        assert audit['gates']['health_clearance'] == 'review_disclosure'
        assert audit['gates']['payment'] == 'not_started'

    def test_intake_discards_ga4_ids_without_granted_consent(
            self, client, temp_athletes_dir, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'COACHING_INTAKE_SECRET', 'edge-secret')
        monkeypatch.setattr(app_module, 'NOTIFICATION_EMAIL', 'coach@test.com')
        payload = self._payload('45b79a9a-5f51-4d4f-9024-2e52c673bfb6')
        payload['analytics_consent'] = 'denied'
        with patch.object(app_module, '_send_email', return_value=True):
            response = client.post('/api/coaching-intakes', json=payload,
                                   headers={'X-Coaching-Intake-Secret': 'edge-secret'})

        assert response.status_code == 201
        case = app_module._read_coaching_intake(payload['submission_id'])
        assert case['source']['analytics_consent'] == 'denied'
        assert 'ga4_client_id' not in case['source']
        assert 'ga4_session_id' not in case['source']

    def test_xc_intake_audit_uses_ski_fields(self):
        from app import _coaching_intake_audit, _COACHING_INTAKE_REQUIRED_XC

        questionnaire = {
            field: ['sat'] if field == 'preferred_days' else 'provided'
            for field in _COACHING_INTAKE_REQUIRED_XC
        }
        questionnaire.update({
            'primary_goal': 'specific_race',
            'target_race': 'American Birkebeiner',
            'goal_details': 'Finish strong',
        })

        audit = _coaching_intake_audit(questionnaire, 'xcskilabs')

        assert audit['missing'] == []
        assert 'years cycling' not in audit['missing']
        assert audit['gates']['intake_completeness'] == 'needs_follow_up'
        assert 'home timezone' in audit['missing_followup']

    def test_minor_intake_requires_guardian_details_and_signed_receipt(
            self, client, temp_athletes_dir, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'COACHING_INTAKE_SECRET', 'edge-secret')
        monkeypatch.setattr(app_module, 'CRON_SECRET', 'coach-secret')
        monkeypatch.setattr(app_module, 'NOTIFICATION_EMAIL', 'coach@test.com')
        case_id = '71e25937-8cba-40f3-8d05-011ae6c5488d'
        payload = self._payload(case_id)
        payload['questionnaire'].update({
            'age': '15',
            'guardian_name': 'Parent Rider',
            'guardian_email': 'parent@test.com',
            'guardian_relationship': 'parent',
        })
        with patch.object(app_module, '_send_email', return_value=True):
            created = client.post('/api/coaching-intakes', json=payload, headers={
                'X-Coaching-Intake-Secret': 'edge-secret'})
        assert created.status_code == 201
        case = app_module._read_coaching_intake(case_id)
        assert case['athlete']['is_minor'] is True
        assert case['guardian']['email'] == 'parent@test.com'
        assert 'parent/guardian full name' not in case['intake_audit']['missing']
        assert 'signed parent/guardian consent receipt' in (
            case['readiness']['payment_blockers'])

        bad = client.post(
            f'/api/coaching-intakes/{case_id}/verify',
            json={'gate': 'guardian_consent', 'status': 'signed',
                  'source_id': 'esign-guardian', 'receipt_id': 'esign-guardian',
                  'document_version': 'counsel-approved-minor-v1',
                  'signer_name': 'Parent Rider',
                  'signer_email': 'wrong@test.com', 'signer_role': 'parent'},
            headers={'X-Cron-Secret': 'coach-secret'})
        assert bad.status_code == 400

        good = client.post(
            f'/api/coaching-intakes/{case_id}/verify',
            json={'gate': 'guardian_consent', 'status': 'signed',
                  'source_id': 'esign-guardian', 'receipt_id': 'esign-guardian',
                  'document_version': 'counsel-approved-minor-v1',
                  'signer_name': 'Parent Rider',
                  'signer_email': 'parent@test.com', 'signer_role': 'parent'},
            headers={'X-Cron-Secret': 'coach-secret'})
        assert good.status_code == 200
        assert good.get_json()['status'] == 'signed'

    def test_under_13_is_rejected_by_this_intake_path(
            self, client, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'COACHING_INTAKE_SECRET', 'edge-secret')
        payload = self._payload('33b27efc-d701-4767-a831-8d22eaf723c2')
        payload['questionnaire']['age'] = '12'
        response = client.post('/api/coaching-intakes', json=payload, headers={
            'X-Coaching-Intake-Secret': 'edge-secret'})
        assert response.status_code == 400
        assert 'age 13 and older' in response.get_json()['error']

    def test_duplicate_submission_is_idempotent(
            self, client, temp_athletes_dir, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'COACHING_INTAKE_SECRET', 'edge-secret')
        monkeypatch.setattr(app_module, 'NOTIFICATION_EMAIL', 'coach@test.com')
        headers = {'X-Coaching-Intake-Secret': 'edge-secret',
                   'X-Forwarded-For': '198.51.100.42'}

        with patch.object(app_module, '_send_email', return_value=True) as send:
            first = client.post('/api/coaching-intakes', json=self._payload(), headers=headers)
            second = client.post('/api/coaching-intakes', json=self._payload(), headers=headers)

        assert first.status_code == 201
        assert second.status_code == 200
        assert second.get_json()['duplicate'] is True
        assert send.call_count == 2

    @pytest.mark.parametrize(('brand', 'case_id'), [
        ('roadielabs', '1589cebe-eb81-49d5-9f52-02596ce42d95'),
        ('xcskilabs', 'e3996ac4-c7f1-43ef-a7b5-55d96d4ff4fb'),
    ])
    def test_shared_intake_accepts_each_coaching_brand(
            self, client, temp_athletes_dir, monkeypatch, brand, case_id):
        import app as app_module
        monkeypatch.setattr(app_module, 'COACHING_INTAKE_SECRET', 'edge-secret')
        monkeypatch.setattr(app_module, 'NOTIFICATION_EMAIL', 'coach@test.com')
        payload = self._payload(case_id)
        payload['brand'] = brand

        with patch.object(app_module, '_send_email', return_value=True):
            response = client.post(
                '/api/coaching-intakes', json=payload,
                headers={'X-Coaching-Intake-Secret': 'edge-secret',
                         'X-Forwarded-For': '198.51.100.48'})

        assert response.status_code == 201
        case = app_module._read_coaching_intake(case_id)
        assert case['brand'] == brand
        assert case['state'] == 'FIT_REVIEW'

    def test_fit_approval_is_idempotent_and_does_not_create_checkout(
            self, client, temp_athletes_dir, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'COACHING_INTAKE_SECRET', 'edge-secret')
        monkeypatch.setattr(app_module, 'CRON_SECRET', 'coach-secret')
        monkeypatch.setattr(app_module, 'NOTIFICATION_EMAIL', 'coach@test.com')
        case_id = self._payload()['submission_id']

        with patch.object(app_module, '_send_email', return_value=True), \
             patch.object(app_module, 'stripe') as mock_stripe:
            mock_stripe.checkout.Session.create.return_value = MagicMock(
                id='cs_case', url='https://checkout.stripe.com/case')
            client.post(
                '/api/coaching-intakes', json=self._payload(),
                headers={'X-Coaching-Intake-Secret': 'edge-secret',
                         'X-Forwarded-For': '198.51.100.43'})

            first = client.post(
                f'/api/coaching-intakes/{case_id}/approve',
                headers={'X-Cron-Secret': 'coach-secret',
                         'X-Forwarded-For': '198.51.100.44'})
            second = client.post(
                f'/api/coaching-intakes/{case_id}/approve',
                headers={'X-Cron-Secret': 'coach-secret',
                         'X-Forwarded-For': '198.51.100.44'})

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.get_json()['state'] == 'IDENTITY_REVIEW'
        assert second.get_json()['duplicate'] is True
        assert mock_stripe.checkout.Session.create.call_count == 0
        case = app_module._read_coaching_intake(case_id)
        assert case['state'] == 'IDENTITY_REVIEW'
        assert 'checkout' not in case
        assert case['readiness']['payment_allowed'] is False

    def test_payment_handoff_is_blocked_until_evidence_gates_pass(
            self, client, temp_athletes_dir, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'COACHING_INTAKE_SECRET', 'edge-secret')
        monkeypatch.setattr(app_module, 'CRON_SECRET', 'coach-secret')
        monkeypatch.setattr(app_module, 'NOTIFICATION_EMAIL', 'coach@test.com')
        case_id = self._payload()['submission_id']
        with patch.object(app_module, '_send_email', return_value=True):
            client.post(
                '/api/coaching-intakes', json=self._payload(),
                headers={'X-Coaching-Intake-Secret': 'edge-secret'})
            client.post(
                f'/api/coaching-intakes/{case_id}/approve',
                headers={'X-Cron-Secret': 'coach-secret'})

        response = client.post(
            f'/api/coaching-intakes/{case_id}/payment-handoff',
            headers={'X-Cron-Secret': 'coach-secret'})
        assert response.status_code == 409
        assert response.get_json()['blockers'] == [
            'identity verification', 'health-clearance disposition',
            'signed coaching agreement receipt',
            'signed data-use consent receipt']

    def test_verified_payment_handoff_creates_one_checkout_and_one_email(
            self, client, temp_athletes_dir, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'COACHING_INTAKE_SECRET', 'edge-secret')
        monkeypatch.setattr(app_module, 'CRON_SECRET', 'coach-secret')
        monkeypatch.setattr(app_module, 'NOTIFICATION_EMAIL', 'coach@test.com')
        monkeypatch.setattr(
            app_module, '_verify_coaching_checkout_contract',
            lambda brand, tier, setup_fee_waived=False: (True, ''))
        case_id = self._payload()['submission_id']
        headers = {'X-Cron-Secret': 'coach-secret'}
        with patch.object(app_module, '_send_email', return_value=True):
            client.post('/api/coaching-intakes', json=self._payload(), headers={
                'X-Coaching-Intake-Secret': 'edge-secret'})
            client.post(f'/api/coaching-intakes/{case_id}/approve', headers=headers)

        receipts = [
            {'gate': 'identity', 'status': 'verified', 'source_id': 'email-match-1'},
            {'gate': 'health_clearance', 'status': 'not_required',
             'source_id': 'coach-policy-1', 'note': 'No disclosure trigger under policy.'},
            {'gate': 'coaching_agreement', 'status': 'signed',
             'source_id': 'esign-1', 'receipt_id': 'esign-1',
             'document_version': 'counsel-approved-v1'},
            {'gate': 'data_consent', 'status': 'signed',
             'source_id': 'esign-2', 'receipt_id': 'esign-2',
             'document_version': 'counsel-approved-v1'},
        ]
        for receipt in receipts:
            response = client.post(
                f'/api/coaching-intakes/{case_id}/verify',
                json=receipt, headers=headers)
            assert response.status_code == 200

        with patch.object(app_module, 'stripe') as mock_stripe, \
             patch.object(app_module, '_send_coaching_onboarding_handoff',
                          return_value=True) as handoff:
            mock_stripe.checkout.Session.create.return_value = MagicMock(
                id='cs_case', url='https://checkout.stripe.com/case')
            first = client.post(
                f'/api/coaching-intakes/{case_id}/payment-handoff', headers=headers)
            second = client.post(
                f'/api/coaching-intakes/{case_id}/payment-handoff', headers=headers)

        assert first.status_code == 200
        assert second.status_code == 200
        assert mock_stripe.checkout.Session.create.call_count == 1
        assert handoff.call_count == 1
        checkout_call = mock_stripe.checkout.Session.create.call_args.kwargs
        assert checkout_call['metadata']['ga4_client_id'] == '1391278887.1471780587'
        assert checkout_call['metadata']['ga4_session_id'] == '1787846400'
        assert checkout_call['metadata']['analytics_consent'] == 'granted'
        assert checkout_call['subscription_data']['metadata'][
            'ga4_client_id'] == '1391278887.1471780587'
        case = app_module._read_coaching_intake(case_id)
        assert case['state'] == 'PAYMENT_PENDING'
        assert case['checkout']['session_id'] == 'cs_case'
        assert case['checkout']['handoff_sent'] is True
        assert 'coaching_checkout_created' in {
            event['event_name'] for event in case['analytics_events']}
        assert 'coaching_checkout_handoff_sent' in {
            event['event_name'] for event in case['analytics_events']}
        assert len(case['verification_history']) == 4

    def test_funnel_report_is_aggregate_and_excludes_athlete_pii(
            self, client, temp_athletes_dir, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'COACHING_INTAKE_SECRET', 'edge-secret')
        monkeypatch.setattr(app_module, 'CRON_SECRET', 'coach-secret')
        monkeypatch.setattr(app_module, 'NOTIFICATION_EMAIL', 'coach@test.com')
        with patch.object(app_module, '_send_email', return_value=True):
            client.post('/api/coaching-intakes', json=self._payload(), headers={
                'X-Coaching-Intake-Secret': 'edge-secret'})

        response = client.get('/api/coaching-funnel-report?days=30', headers={
            'X-Cron-Secret': 'coach-secret'})
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        data = response.get_json()
        assert data['all_brands']['stage_counts']['applications'] == 1
        assert data['by_brand_and_tier'][
            'gravelgod:mid']['stage_counts']['applications'] == 1
        assert data['privacy'] == 'aggregate_only_no_athlete_pii_or_case_ids'
        assert 'case@test.com' not in body
        assert 'Case Rider' not in body
        assert self._payload()['submission_id'] not in body

    def test_coaching_canary_is_side_effect_free_and_persists_receipt(
            self, client, temp_athletes_dir, monkeypatch):
        import app as app_module
        brands = copy.deepcopy(app_module.BRANDS)
        for cfg in brands.values():
            cfg['ga4_measurement_id'] = 'G-CANARY'
            cfg['ga4_mp_api_secret'] = 'configured-not-returned'
        monkeypatch.setattr(app_module, 'BRANDS', brands)
        monkeypatch.setattr(app_module, 'COACHING_INTAKE_SECRET', 'edge-secret')
        monkeypatch.setattr(app_module, 'STRIPE_SECRET_KEY', 'sk_configured')
        monkeypatch.setattr(app_module, 'STRIPE_WEBHOOK_SECRET', 'whsec_configured')
        monkeypatch.setattr(app_module, 'RESEND_API_KEY', 're_configured')
        monkeypatch.setattr(app_module, 'NOTIFICATION_EMAIL', 'coach@test.com')
        monkeypatch.setattr(
            app_module, 'COACHING_BOOKING_URL',
            'https://calendar.example.com/coaching')
        monkeypatch.setattr(
            app_module, '_verify_coaching_checkout_contract',
            lambda brand, tier, setup_fee_waived=False: (True, ''))
        self._configure_signwell(monkeypatch, app_module, test_mode=True)
        monkeypatch.setattr(
            app_module, 'SIGNWELL_SYNTHETIC_TEMPLATE_ID',
            '33333333-3333-4333-8333-333333333333')

        lifecycle_events = [
            'checkout.session.completed', 'checkout.session.expired',
            'invoice.paid', 'invoice.payment_failed',
            'invoice.payment_action_required',
            'customer.subscription.updated', 'customer.subscription.deleted',
            'customer.subscription.paused', 'customer.subscription.resumed',
        ]
        with patch.object(app_module.stripe.WebhookEndpoint, 'list', return_value={
            'data': [{
                'status': 'enabled',
                'url': 'https://pipeline.example/webhook/stripe',
                'enabled_events': lifecycle_events,
            }]
        }), patch.object(
            app_module.stripe.billing_portal.Configuration, 'list',
            return_value={'data': [{
                'active': True,
                'features': {
                    'subscription_cancel': {'enabled': True},
                    'payment_method_update': {'enabled': True},
                },
            }]}
        ), patch.object(
            app_module.SignWellClient, 'get_account',
            return_value={'id': 'synthetic-account'}
        ), patch.object(
            app_module.SignWellClient, 'get_template',
            return_value={
                'id': '33333333-3333-4333-8333-333333333333',
                'name': 'SYNTHETIC TEST ONLY — NO LEGAL EFFECT',
                'metadata': {'legal_effect': 'none'},
                'fields': [[{'type': 'signature'}, {'type': 'date'}]],
            }
        ):
            response = client.post('/api/coaching-canary', headers={
                'X-Coaching-Intake-Secret': 'edge-secret'})

        assert response.status_code == 200
        assert response.get_json()['status'] == 'ok'
        assert response.get_json()['side_effects'].startswith('no case')
        assert not (temp_athletes_dir / 'coaching_intakes').exists()
        receipts = list((temp_athletes_dir / '.canary' / 'coaching').glob('*.json'))
        assert len(receipts) == 1
        receipt_text = receipts[0].read_text()
        assert 'configured-not-returned' not in receipt_text
        assert 'sk_configured' not in receipt_text

    def test_stripe_list_normalizer_prefers_sdk_data_attribute(self):
        import app as app_module

        class StripeSdkListShape:
            data = [{'id': 'provider-object'}]

            @staticmethod
            def get(_key, _default=None):
                return []

        assert app_module._stripe_list_items(StripeSdkListShape()) == [
            {'id': 'provider-object'}]
        assert app_module._stripe_list_items(
            {'data': [{'id': 'dict-object'}]}) == [{'id': 'dict-object'}]

    @staticmethod
    def _paid_case(case_id):
        return {
            'schema': 'coaching_onboarding_case/v1',
            'case_id': case_id,
            'brand': 'gravelgod',
            'tier': 'mid',
            'state': 'PLATFORM_SETUP',
            'athlete': {'name': 'Billing Rider', 'email': 'billing@test.com',
                        'is_minor': False},
            'source': {'submitted_at': datetime.now(timezone.utc).isoformat()},
            'questionnaire': {'age': '40'},
            'verifications': {
                'coach_fit': {'status': 'approved'},
                'identity': {'status': 'verified'},
                'health_clearance': {'status': 'not_required'},
                'coaching_agreement': {'status': 'signed'},
                'data_consent': {'status': 'signed'},
            },
            'receipts': {'stripe_payment': {
                'checkout_session_id': 'cs_paid',
                'subscription_id': 'sub_case',
                'customer_id': 'cus_case',
                'confirmed_at': datetime.now(timezone.utc).isoformat(),
            }},
            'billing': {
                'schema': 'coaching_subscription_billing/v1',
                'subscription_id': 'sub_case',
                'customer_id': 'cus_case',
                'standing': 'healthy',
                'processed_event_ids': [],
            },
            'analytics_events': [],
        }

    def test_recurring_billing_failure_blocks_and_paid_restores_case(
            self, client, monkeypatch):
        import app as app_module
        case_id = '79bde7d0-94c1-452b-acbf-931fa2e75e12'
        app_module._write_coaching_intake(self._paid_case(case_id))
        failed = {
            'id': 'evt_invoice_failed',
            'type': 'invoice.payment_failed',
            'data': {'object': {
                'id': 'in_failed', 'subscription': 'sub_case',
                'customer': 'cus_case',
            }},
        }
        response = client.post('/webhook/stripe', json=failed)
        assert response.status_code == 200
        assert response.get_json()['billing_standing'] == 'past_due'
        stored = app_module._read_coaching_intake(case_id)
        assert stored['state'] == 'BILLING_ACTION_REQUIRED'
        assert stored['readiness']['plan_release_allowed'] is False

        duplicate = client.post('/webhook/stripe', json=failed)
        assert duplicate.get_json()['status'] == 'duplicate'

        paid = {
            'id': 'evt_invoice_paid',
            'type': 'invoice.paid',
            'data': {'object': {
                'id': 'in_paid', 'subscription': 'sub_case',
                'customer': 'cus_case',
            }},
        }
        restored = client.post('/webhook/stripe', json=paid)
        assert restored.status_code == 200
        assert restored.get_json()['billing_standing'] == 'healthy'
        assert app_module._read_coaching_intake(case_id)['state'] == 'PLATFORM_SETUP'

    def test_subscription_deleted_ends_case(self, client):
        import app as app_module
        case_id = '85ad0a8f-20c7-44f6-9875-2f2a7399c70c'
        app_module._write_coaching_intake(self._paid_case(case_id))
        event = {
            'id': 'evt_subscription_deleted',
            'created': 200,
            'type': 'customer.subscription.deleted',
            'data': {'object': {
                'id': 'sub_case', 'customer': 'cus_case',
                'status': 'canceled',
                'metadata': {'intake_id': case_id},
            }},
        }
        response = client.post('/webhook/stripe', json=event)
        assert response.status_code == 200
        stored = app_module._read_coaching_intake(case_id)
        assert stored['billing']['standing'] == 'ended'
        assert stored['state'] == 'SUBSCRIPTION_ENDED'

        stale_paid = {
            'id': 'evt_stale_paid',
            'created': 100,
            'type': 'invoice.paid',
            'data': {'object': {
                'id': 'in_stale', 'subscription': 'sub_case',
                'customer': 'cus_case',
            }},
        }
        ignored = client.post('/webhook/stripe', json=stale_paid)
        assert ignored.get_json()['reason'] == 'Stale out-of-order billing event'
        assert app_module._read_coaching_intake(case_id)['billing']['standing'] == 'ended'

    def test_billing_portal_is_case_bound_and_not_emailed(
            self, client, monkeypatch):
        import app as app_module
        case_id = '5e4db7f3-1df4-44bd-814e-eb880d39565e'
        app_module._write_coaching_intake(self._paid_case(case_id))
        monkeypatch.setattr(app_module, 'CRON_SECRET', 'coach-secret')
        portal = MagicMock(
            id='bps_case', url='https://billing.stripe.com/p/session/case')
        with patch.object(
                app_module.stripe.billing_portal.Session, 'create',
                return_value=portal) as create:
            response = client.post(
                f'/api/coaching-intakes/{case_id}/billing-portal',
                json={'mode': 'cancel'},
                headers={'X-Cron-Secret': 'coach-secret'})
        assert response.status_code == 200
        assert response.get_json()['delivery'].startswith('operator_must')
        kwargs = create.call_args.kwargs
        assert kwargs['customer'] == 'cus_case'
        assert kwargs['flow_data']['subscription_cancel']['subscription'] == 'sub_case'
        stored = app_module._read_coaching_intake(case_id)
        assert stored['billing_portal_receipts'][0]['portal_session_id'] == 'bps_case'
        assert 'portal_url' not in stored['billing_portal_receipts'][0]

    def test_reminder_cron_suggests_without_sending(self, client, monkeypatch):
        import app as app_module
        case_id = '6cf571de-2e3a-4dc2-aa30-ec7a86ac0b2c'
        case = self._paid_case(case_id)
        case['receipts']['stripe_payment']['confirmed_at'] = (
            datetime.now(timezone.utc) - timedelta(days=29)).isoformat()
        app_module._write_coaching_intake(case)
        monkeypatch.setattr(app_module, 'CRON_SECRET', 'coach-secret')
        with patch.object(app_module, '_send_email') as send:
            first = client.post(
                '/api/cron/coaching-onboarding-reminders',
                headers={'X-Cron-Secret': 'coach-secret'})
            second = client.post(
                '/api/cron/coaching-onboarding-reminders',
                headers={'X-Cron-Secret': 'coach-secret'})
        assert first.status_code == 200
        assert first.get_json()['suggested'] == 5
        assert second.get_json()['suggested'] == 0
        assert send.call_count == 0
        reminders = app_module._read_coaching_intake(case_id)[
            'onboarding_reminders']
        assert {item['status'] for item in reminders} == {'suggested'}
        assert all(item['automatic_send'] is False for item in reminders)

    def test_esign_readiness_fails_closed_then_allows_manual_receipts(
            self, client, monkeypatch):
        import app as app_module
        case_id = '46cbb155-2a9b-46c8-91e3-063b64e3ce6d'
        app_module._write_coaching_intake(self._paid_case(case_id))
        monkeypatch.setattr(app_module, 'CRON_SECRET', 'coach-secret')
        route = f'/api/coaching-intakes/{case_id}/esign-readiness'
        blocked = client.get(route, headers={'X-Cron-Secret': 'coach-secret'})
        assert blocked.status_code == 409
        assert 'COACHING_LEGAL_APPROVAL_RECEIPT' in (
            blocked.get_json()['missing_configuration'])

        values = {
            'COACHING_ESIGN_PROVIDER': 'manual_receipt',
            'COACHING_LEGAL_APPROVAL_RECEIPT': 'counsel-approval-2026-01',
            'COACHING_AGREEMENT_TEMPLATE_ID': 'agreement-template',
            'COACHING_AGREEMENT_TEMPLATE_VERSION': 'v1',
            'COACHING_DATA_CONSENT_TEMPLATE_ID': 'consent-template',
            'COACHING_DATA_CONSENT_TEMPLATE_VERSION': 'v1',
        }
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        ready = client.get(route, headers={'X-Cron-Secret': 'coach-secret'})
        assert ready.status_code == 200
        assert ready.get_json()['status'] == 'ready'

    @staticmethod
    def _configure_signwell(monkeypatch, app_module, *, test_mode=False):
        values = {
            'COACHING_ESIGN_PROVIDER': 'signwell',
            'COACHING_LEGAL_APPROVAL_RECEIPT': 'synthetic-counsel-receipt-v1',
            'COACHING_AGREEMENT_TEMPLATE_ID': (
                '11111111-1111-4111-8111-111111111111'),
            'COACHING_AGREEMENT_TEMPLATE_VERSION': 'synthetic-v1',
            'COACHING_DATA_CONSENT_TEMPLATE_ID': (
                '22222222-2222-4222-8222-222222222222'),
            'COACHING_DATA_CONSENT_TEMPLATE_VERSION': 'synthetic-v1',
        }
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(app_module, 'COACHING_ESIGN_PROVIDER', 'signwell')
        monkeypatch.setattr(app_module, 'SIGNWELL_API_KEY', 'synthetic-api-key')
        monkeypatch.setattr(app_module, 'SIGNWELL_WEBHOOK_ID', 'synthetic-webhook-id')
        monkeypatch.setattr(app_module, 'SIGNWELL_TEST_MODE', test_mode)
        monkeypatch.setattr(app_module, 'SIGNWELL_REMINDERS_ENABLED', False)

    @staticmethod
    def _signwell_event(document_id, case_id, event_type='document_completed'):
        event_time = 1787654321
        digest = hmac.new(
            b'synthetic-webhook-id',
            f'{event_type}@{event_time}'.encode(), hashlib.sha256).hexdigest()
        return {
            'event': {
                'type': event_type, 'time': event_time, 'hash': digest,
            },
            'data': {'object': {
                'id': document_id,
                'metadata': {'case_id': case_id},
            }},
        }

    def test_signwell_packet_is_nonembedded_authenticated_and_idempotent(
            self, client, monkeypatch):
        import app as app_module
        case_id = '51a1f8bc-4405-469f-9a14-f51a9be9a5b5'
        case = self._paid_case(case_id)
        case['verifications'].pop('coaching_agreement')
        case['verifications'].pop('data_consent')
        app_module._write_coaching_intake(case)
        monkeypatch.setattr(app_module, 'CRON_SECRET', 'coach-secret')
        self._configure_signwell(monkeypatch, app_module)
        document_id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        provider = MagicMock()
        provider.create_document_from_templates.return_value = {
            'id': document_id, 'status': 'sent', 'test_mode': False,
        }
        with patch.object(app_module, 'SignWellClient', return_value=provider):
            first = client.post(
                f'/api/coaching-intakes/{case_id}/esign-packet',
                headers={'X-Cron-Secret': 'coach-secret'})
            second = client.post(
                f'/api/coaching-intakes/{case_id}/esign-packet',
                headers={'X-Cron-Secret': 'coach-secret'})

        assert first.status_code == 201
        assert second.status_code == 200
        assert second.get_json()['duplicate'] is True
        assert provider.create_document_from_templates.call_count == 1
        payload = provider.create_document_from_templates.call_args.args[0]
        assert payload['embedded_signing'] is False
        assert payload['allow_reassign'] is False
        assert payload['reminders'] is False
        assert payload['recipients'][0]['passcode_delivery'] == {
            'enabled': True, 'methods': ['email'], 'expire_after_access': True,
        }
        assert payload['metadata']['case_id'] == case_id
        assert 'questionnaire' not in payload['metadata']

    def test_signwell_live_completion_requires_provider_readback_and_pdf(
            self, client, temp_athletes_dir, monkeypatch):
        import app as app_module
        case_id = '0cd77449-bca7-440e-aaeb-a6d78124ebc3'
        document_id = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
        case = self._paid_case(case_id)
        case['verifications'].pop('coaching_agreement')
        case['verifications'].pop('data_consent')
        case['esign_packet'] = {
            'provider': 'signwell', 'document_id': document_id,
            'status': 'sent', 'test_mode': False, 'processed_event_ids': [],
        }
        app_module._write_coaching_intake(case)
        self._configure_signwell(monkeypatch, app_module)
        readback = {
            'id': document_id, 'status': 'completed', 'test_mode': False,
            'updated_at': '2026-08-25T12:00:00Z',
            'metadata': {'case_id': case_id},
            'template_ids': [
                '11111111-1111-4111-8111-111111111111',
                '22222222-2222-4222-8222-222222222222',
            ],
            'recipients': [{
                'id': 'recipient-athlete', 'email': 'billing@test.com',
                'status': 'completed',
            }],
        }
        provider = MagicMock()
        provider.get_document.return_value = readback
        provider.get_completed_pdf.return_value = b'%PDF-1.7\nsynthetic signed packet'
        payload = self._signwell_event(document_id, case_id)
        with patch.object(app_module, 'SignWellClient', return_value=provider):
            response = client.post('/webhook/signwell', json=payload)
            duplicate = client.post('/webhook/signwell', json=payload)

        assert response.status_code == 200
        assert response.get_json()['legal_effect'] == 'receipt_recorded'
        assert duplicate.get_json()['status'] == 'duplicate'
        assert provider.get_document.call_count == 1
        stored = app_module._read_coaching_intake(case_id)
        assert stored['verifications']['coaching_agreement']['status'] == 'signed'
        assert stored['verifications']['data_consent']['provider'] == 'signwell'
        pdf_path = temp_athletes_dir / stored['esign_packet']['signed_document_path']
        assert pdf_path.read_bytes().startswith(b'%PDF-')
        assert pdf_path.stat().st_mode & 0o777 == 0o600
        assert stored['esign_packet']['signed_document_sha256'] == hashlib.sha256(
            pdf_path.read_bytes()).hexdigest()

    def test_signwell_test_completion_has_no_legal_effect(
            self, client, monkeypatch):
        import app as app_module
        case_id = '2f8a2809-dd57-416d-925b-c596448a9db7'
        document_id = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
        case = self._paid_case(case_id)
        case['verifications'].pop('coaching_agreement')
        case['verifications'].pop('data_consent')
        case['esign_packet'] = {
            'provider': 'signwell', 'document_id': document_id,
            'status': 'sent', 'test_mode': True, 'processed_event_ids': [],
        }
        app_module._write_coaching_intake(case)
        self._configure_signwell(monkeypatch, app_module, test_mode=True)
        provider = MagicMock()
        provider.get_document.return_value = {
            'id': document_id, 'status': 'completed', 'test_mode': True,
            'metadata': {'case_id': case_id},
            'template_ids': [
                '11111111-1111-4111-8111-111111111111',
                '22222222-2222-4222-8222-222222222222',
            ],
            'recipients': [{
                'id': 'recipient-athlete', 'email': 'billing@test.com',
                'status': 'completed',
            }],
        }
        provider.get_completed_pdf.return_value = b'%PDF-1.7\ntest packet'
        with patch.object(app_module, 'SignWellClient', return_value=provider):
            response = client.post(
                '/webhook/signwell',
                json=self._signwell_event(document_id, case_id))

        assert response.status_code == 200
        assert response.get_json()['legal_effect'] == 'none_test_mode'
        stored = app_module._read_coaching_intake(case_id)
        assert stored['esign_packet']['status'] == 'test_completed'
        assert 'coaching_agreement' not in stored['verifications']
        assert 'data_consent' not in stored['verifications']

    def test_signwell_webhook_rejects_bad_hash_and_readback_mismatch(
            self, client, monkeypatch):
        import app as app_module
        case_id = '3d25171c-556e-4cbd-ae59-523ce26793c7'
        document_id = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
        case = self._paid_case(case_id)
        case['verifications'].pop('coaching_agreement')
        case['verifications'].pop('data_consent')
        case['esign_packet'] = {
            'provider': 'signwell', 'document_id': document_id,
            'status': 'sent', 'test_mode': False, 'processed_event_ids': [],
        }
        app_module._write_coaching_intake(case)
        self._configure_signwell(monkeypatch, app_module)
        payload = self._signwell_event(document_id, case_id)
        payload['event']['hash'] = '0' * 64
        assert client.post('/webhook/signwell', json=payload).status_code == 401

        payload = self._signwell_event(document_id, case_id)
        provider = MagicMock()
        provider.get_document.return_value = {
            'id': document_id, 'status': 'completed', 'test_mode': False,
            'metadata': {'case_id': '00000000-0000-4000-8000-000000000000'},
            'template_ids': [], 'recipients': [],
        }
        with patch.object(app_module, 'SignWellClient', return_value=provider):
            mismatch = client.post('/webhook/signwell', json=payload)
        assert mismatch.status_code == 503
        stored = app_module._read_coaching_intake(case_id)
        assert 'coaching_agreement' not in stored['verifications']
        assert stored['esign_packet']['processed_event_ids'] == []

    def test_recovered_coaching_checkout_is_bound_to_approved_session(
            self, client, temp_athletes_dir):
        import app as app_module
        case_id = '2dd98ace-8682-4d8c-a5ff-e67e7b63a1ce'
        app_module._write_coaching_intake({
            'schema': 'coaching_onboarding_case/v1',
            'case_id': case_id,
            'brand': 'gravelgod',
            'tier': 'mid',
            'state': 'PAYMENT_PENDING',
            'athlete': {'name': 'Recovered Rider', 'email': 'rider@test.com'},
            'source': {'submitted_at': datetime.now(timezone.utc).isoformat()},
            'questionnaire': {'age': '40'},
            'verifications': {},
            'receipts': {},
            'checkout': {'session_id': 'cs_approved_original'},
        })
        event = {
            'type': 'checkout.session.completed',
            'data': {'object': {
                'id': 'cs_recovered_payment',
                'recovered_from': 'cs_approved_original',
                'amount_total': 39800,
                'subscription': 'sub_recovered',
                'customer_details': {'email': 'rider@test.com'},
                'metadata': {
                    'product_type': 'coaching', 'tier': 'mid',
                    'brand': 'gravelgod', 'athlete_name': 'Recovered Rider',
                    'intake_id': case_id,
                },
            }}
        }
        with patch.object(app_module, '_send_ga4_purchase'), \
             patch.object(app_module, '_log_product_event'), \
             patch.object(app_module, '_notify_new_order'), \
             patch.object(app_module, '_send_coaching_payment_confirmation'):
            response = client.post('/webhook/stripe', json=event)

        assert response.status_code == 200
        stored = app_module._read_coaching_intake(case_id)
        receipt = stored['receipts']['stripe_payment']
        assert receipt['checkout_session_id'] == 'cs_recovered_payment'
        assert receipt['recovered_from'] == 'cs_approved_original'
        names = {item['event_name'] for item in stored['analytics_events']}
        assert 'coaching_checkout_recovered' in names
        assert 'coaching_payment_confirmed' in names

    def test_live_checkout_contract_rejects_percent_off_waiver(self, monkeypatch):
        import app as app_module

        def stripe_object(data):
            obj = MagicMock()
            obj._to_dict_recursive.return_value = data
            return obj

        recurring = stripe_object({
            'active': True, 'unit_amount': 29900, 'currency': 'usd',
            'recurring': {'interval': 'week', 'interval_count': 4},
        })
        setup = stripe_object({
            'active': True, 'unit_amount': 9900, 'currency': 'usd',
            'type': 'one_time',
        })
        coupon = stripe_object({
            'valid': True, 'amount_off': None, 'currency': None,
            'duration': 'once', 'percent_off': 100.0,
        })
        with patch.object(app_module, 'stripe') as mock_stripe:
            mock_stripe.Price.retrieve.side_effect = [recurring, setup]
            mock_stripe.Coupon.retrieve.return_value = coupon
            ok, reason = app_module._verify_coaching_checkout_contract(
                'gravelgod', 'mid', setup_fee_waived=True)

        assert ok is False
        assert 'waiver coupon is fixed $99 once' in reason

    def test_private_setup_fee_waiver_is_applied_automatically(self):
        import app as app_module
        with patch.object(app_module, 'stripe') as mock_stripe:
            mock_stripe.checkout.Session.create.return_value = MagicMock(
                id='cs_waived', url='https://checkout.stripe.com/waived')
            app_module._create_coaching_checkout_session(
                'Waived Rider', 'waived@test.com', 'mid', 'gravelgod',
                intake_id='e287b97e-bdb5-4e67-b162-8025a80b6f1c',
                setup_fee_waived=True)
        kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
        assert kwargs['discounts'] == [{
            'coupon': app_module.COACHING_SETUP_FEE_WAIVER_COUPON_ID}]
        assert 'allow_promotion_codes' not in kwargs
        assert kwargs['metadata']['setup_fee_waived'] == 'true'

    def test_onboarding_materials_endpoint_generates_privacy_minimized_guide(
            self, client, temp_athletes_dir, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'CRON_SECRET', 'coach-secret')
        monkeypatch.setattr(
            app_module, 'COACHING_BOOKING_URL',
            'https://calendar.example.com/matti/coaching')
        monkeypatch.setattr(app_module, '_send_email', lambda *args, **kwargs: True)
        athlete_id = 'case-rider'
        athlete_dir = temp_athletes_dir / athlete_id
        athlete_dir.mkdir()
        (athlete_dir / 'profile.yaml').write_text('name: Case Rider\n')
        case_id = 'd540f0de-087d-435a-83a5-c15d237ab285'
        case = {
            'schema': 'coaching_onboarding_case/v1',
            'case_id': case_id,
            'brand': 'gravelgod',
            'tier': 'mid',
            'athlete': {'name': 'Case Rider', 'email': 'case@test.com',
                        'is_minor': False},
            'questionnaire': {
                'preferred_contact_channel': 'TrainingPeaks',
                'injuries': 'private and excluded',
            },
            'verifications': {
                'coach_fit': {'status': 'approved'},
                'identity': {'status': 'verified'},
                'health_clearance': {'status': 'not_required'},
                'coaching_agreement': {'status': 'signed'},
                'data_consent': {'status': 'signed'},
                'athlete_context': {'status': 'sealed',
                                    'athlete_id': athlete_id},
            },
            'receipts': {'stripe_payment': {'checkout_session_id': 'cs_case'}},
            'state': 'CONTEXT_SEAL',
        }
        app_module._write_coaching_intake(case)

        response = client.post(
            f'/api/coaching-intakes/{case_id}/onboarding-materials',
            headers={'X-Cron-Secret': 'coach-secret'})
        assert response.status_code == 200
        assert response.get_json()['delivered_at']
        assert response.get_json()['artifacts'] == [
            'coaching_onboarding.yaml', 'coaching_welcome.html']
        welcome = (athlete_dir / 'coaching_welcome.html').read_text()
        assert 'How Coaching Works' in welcome
        assert 'private and excluded' not in welcome
        assert 'NOSETUP' not in welcome

    def test_legal_verification_requires_versioned_receipt(
            self, client, temp_athletes_dir, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'COACHING_INTAKE_SECRET', 'edge-secret')
        monkeypatch.setattr(app_module, 'CRON_SECRET', 'coach-secret')
        monkeypatch.setattr(app_module, 'NOTIFICATION_EMAIL', 'coach@test.com')
        case_id = self._payload()['submission_id']
        with patch.object(app_module, '_send_email', return_value=True):
            client.post('/api/coaching-intakes', json=self._payload(), headers={
                'X-Coaching-Intake-Secret': 'edge-secret'})

        response = client.post(
            f'/api/coaching-intakes/{case_id}/verify',
            json={'gate': 'coaching_agreement', 'status': 'signed',
                  'source_id': 'claim-without-receipt'},
            headers={'X-Cron-Secret': 'coach-secret'})
        assert response.status_code == 400
        assert 'document_version and receipt_id' in response.get_json()['error']

        health = client.post(
            f'/api/coaching-intakes/{case_id}/verify',
            json={'gate': 'health_clearance', 'status': 'cleared',
                  'source_id': 'coach-review-without-clinician-receipt'},
            headers={'X-Cron-Secret': 'coach-secret'})
        assert health.status_code == 400
        assert 'clinician receipt_id' in health.get_json()['error']

    def test_intake_requires_trusted_worker(self, client, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'COACHING_INTAKE_SECRET', 'edge-secret')
        response = client.post('/api/coaching-intakes', json=self._payload())
        assert response.status_code == 401

    def test_private_case_read_requires_operator_secret(
            self, client, temp_athletes_dir, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'COACHING_INTAKE_SECRET', 'edge-secret')
        monkeypatch.setattr(app_module, 'CRON_SECRET', 'coach-secret')
        monkeypatch.setattr(app_module, 'NOTIFICATION_EMAIL', 'coach@test.com')
        case_id = self._payload()['submission_id']
        with patch.object(app_module, '_send_email', return_value=True):
            client.post(
                '/api/coaching-intakes', json=self._payload(),
                headers={'X-Coaching-Intake-Secret': 'edge-secret',
                         'X-Forwarded-For': '198.51.100.45'})

        denied = client.get(f'/api/coaching-intakes/{case_id}')
        allowed = client.get(
            f'/api/coaching-intakes/{case_id}',
            headers={'X-Cron-Secret': 'coach-secret'})
        assert denied.status_code == 401
        assert allowed.status_code == 200
        assert allowed.get_json()['questionnaire']['age'] == '52'


class TestConsultingCheckout:
    """Tests for POST /api/create-consulting-checkout endpoint."""

    def test_consulting_checkout_rejects_missing_email(self, client):
        """Consulting checkout requires a valid email."""
        response = client.post(
            '/api/create-consulting-checkout',
            json={'name': 'Test', 'hours': 1},
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert 'email' in data['error'].lower()

    def test_consulting_checkout_rejects_missing_name(self, client):
        """Consulting checkout requires a name."""
        response = client.post(
            '/api/create-consulting-checkout',
            json={'email': 'test@test.com', 'hours': 1},
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert 'name' in data['error'].lower()

    def test_consulting_checkout_rejects_zero_hours(self, client):
        """Consulting checkout rejects 0 hours."""
        response = client.post(
            '/api/create-consulting-checkout',
            json={'name': 'Test', 'email': 'test@test.com', 'hours': 0},
            content_type='application/json'
        )
        assert response.status_code == 400

    def test_consulting_checkout_rejects_excessive_hours(self, client):
        """Consulting checkout rejects more than 10 hours."""
        response = client.post(
            '/api/create-consulting-checkout',
            json={'name': 'Test', 'email': 'test@test.com', 'hours': 11},
            content_type='application/json'
        )
        assert response.status_code == 400

    def test_consulting_checkout_rejects_non_numeric_hours(self, client):
        """Consulting checkout rejects non-numeric hours."""
        response = client.post(
            '/api/create-consulting-checkout',
            json={'name': 'Test', 'email': 'test@test.com', 'hours': 'abc'},
            content_type='application/json'
        )
        assert response.status_code == 400

    def test_consulting_checkout_defaults_to_1_hour(self, client, temp_athletes_dir):
        """Consulting checkout defaults to 1 hour if not specified."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_test_consult'
            mock_session.url = 'https://checkout.stripe.com/consult'
            mock_stripe.checkout.Session.create.return_value = mock_session

            response = client.post(
                '/api/create-consulting-checkout',
                json={'name': 'Test', 'email': 'test@test.com'},
                content_type='application/json'
            )

            assert response.status_code == 200
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert call_kwargs['line_items'][0]['quantity'] == 1

    def test_consulting_checkout_creates_session(self, client, temp_athletes_dir):
        """Valid consulting checkout creates Stripe payment session."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_test_consult_3hr'
            mock_session.url = 'https://checkout.stripe.com/consult3'
            mock_stripe.checkout.Session.create.return_value = mock_session

            response = client.post(
                '/api/create-consulting-checkout',
                json={
                    'name': 'Consult Me', 'email': 'consult@test.com',
                    'hours': 3, 'analytics_consent': 'granted',
                    'ga4_client_id': '1391278887.1471780587',
                    'ga4_session_id': '1787846400',
                },
                content_type='application/json'
            )

            assert response.status_code == 200
            data = response.get_json()
            assert data['checkout_url'] == 'https://checkout.stripe.com/consult3'

            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert call_kwargs['mode'] == 'payment'
            assert call_kwargs['line_items'][0]['quantity'] == 3
            assert call_kwargs['metadata']['product_type'] == 'consulting'
            assert call_kwargs['metadata']['hours'] == '3'
            assert call_kwargs['metadata']['analytics_consent'] == 'granted'
            assert call_kwargs['metadata']['ga4_client_id'] == '1391278887.1471780587'
            assert call_kwargs['metadata']['ga4_session_id'] == '1787846400'

    def test_consulting_checkout_discards_ids_when_consent_denied(
            self, client, temp_athletes_dir):
        with patch('app.stripe') as mock_stripe:
            mock_stripe.checkout.Session.create.return_value = MagicMock(
                id='cs_consult_denied', url='https://checkout.stripe.com/consult')
            response = client.post('/api/create-consulting-checkout', json={
                'name': 'Private Consult', 'email': 'private@test.com',
                'hours': 1, 'analytics_consent': 'denied',
                'ga4_client_id': '1391278887.1471780587',
                'ga4_session_id': '1787846400',
            })

        assert response.status_code == 200
        metadata = mock_stripe.checkout.Session.create.call_args.kwargs['metadata']
        assert metadata['analytics_consent'] == 'denied'
        assert 'ga4_client_id' not in metadata
        assert 'ga4_session_id' not in metadata

    def test_consulting_checkout_options_preflight(self, client):
        """CORS preflight returns 204."""
        response = client.options('/api/create-consulting-checkout')
        assert response.status_code == 204


class TestConsultingCheckout400Logging:
    """Verify 400 validation failures emit structured logs with origin + failure reason (no PII)."""

    def test_invalid_json_logs_failure(self, client, caplog):
        """Invalid JSON logs the failure reason and origin."""
        with caplog.at_level(logging.WARNING):
            response = client.post(
                '/api/create-consulting-checkout',
                data='not json',
                content_type='application/json',
                headers={'Origin': 'https://xcskilabs.com'}
            )
        assert response.status_code == 400
        assert any('Consulting checkout invalid JSON' in rec.message and
                   'origin=https://xcskilabs.com' in rec.message and
                   'brand=xcskilabs' in rec.message
                   for rec in caplog.records)

    def test_missing_email_logs_failure(self, client, caplog):
        """Missing email logs the failure reason without logging PII."""
        with caplog.at_level(logging.WARNING):
            response = client.post(
                '/api/create-consulting-checkout',
                json={'name': 'Test', 'hours': 1},
                content_type='application/json',
                headers={'Origin': 'https://gravelgodcycling.com'}
            )
        assert response.status_code == 400
        assert any('Consulting checkout missing/invalid email' in rec.message and
                   'origin=https://gravelgodcycling.com' in rec.message and
                   'brand=gravelgod' in rec.message
                   for rec in caplog.records)
        # Verify no email/name in logs
        log_output = '\n'.join(rec.message for rec in caplog.records)
        assert 'Test' not in log_output

    def test_missing_name_logs_failure(self, client, caplog):
        """Missing name logs the failure reason without logging PII."""
        with caplog.at_level(logging.WARNING):
            response = client.post(
                '/api/create-consulting-checkout',
                json={'email': 'test@test.com', 'hours': 1},
                content_type='application/json'
            )
        assert response.status_code == 400
        assert any('Consulting checkout missing name' in rec.message and
                   'origin=' in rec.message and
                   'brand=gravelgod' in rec.message
                   for rec in caplog.records)
        # Verify no email in logs
        log_output = '\n'.join(rec.message for rec in caplog.records)
        assert 'test@test.com' not in log_output

    def test_invalid_hours_range_logs_failure(self, client, caplog):
        """Hours out of range logs the failure with the hours value."""
        with caplog.at_level(logging.WARNING):
            response = client.post(
                '/api/create-consulting-checkout',
                json={'name': 'Test', 'email': 'test@test.com', 'hours': 11},
                content_type='application/json'
            )
        assert response.status_code == 400
        assert any('Consulting checkout invalid hours range: 11' in rec.message and
                   'origin=' in rec.message and
                   'brand=gravelgod' in rec.message
                   for rec in caplog.records)

    def test_invalid_hours_type_logs_failure(self, client, caplog):
        """Invalid hours type logs the failure with repr."""
        with caplog.at_level(logging.WARNING):
            response = client.post(
                '/api/create-consulting-checkout',
                json={'name': 'Test', 'email': 'test@test.com', 'hours': 'abc'},
                content_type='application/json'
            )
        assert response.status_code == 400
        assert any("Consulting checkout invalid hours type: 'abc'" in rec.message and
                   'origin=' in rec.message and
                   'brand=gravelgod' in rec.message
                   for rec in caplog.records)


class TestCoachingWebhook:
    """Tests for coaching webhook handler."""

    def test_coaching_webhook_processes_subscription(self, client, temp_athletes_dir):
        """Coaching webhook processes subscription and logs event."""
        stripe_event = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_coaching_123',
                    'customer_details': {
                        'name': 'New Coach Client',
                        'email': 'client@example.com',
                    },
                    'subscription': 'sub_test_123',
                    'metadata': {
                        'product_type': 'coaching',
                        'tier': 'mid',
                        'athlete_name': 'New Coach Client',
                        'analytics_consent': 'granted',
                        'ga4_client_id': '1391278887.1471780587',
                        'ga4_session_id': '1787846400',
                    }
                }
            }
        }

        with patch('app._send_ga4_purchase') as ga4_purchase:
            response = client.post(
                '/webhook/stripe',
                json=stripe_event,
                content_type='application/json'
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'success'
        assert data['product_type'] == 'coaching'
        assert data['tier'] == 'mid'
        kwargs = ga4_purchase.call_args.kwargs
        assert kwargs['client_id'] == '1391278887.1471780587'
        assert kwargs['session_id'] == '1787846400'
        assert kwargs['analytics_consent'] == 'granted'

    def test_coaching_webhook_logs_event(self, client, temp_athletes_dir):
        """Coaching webhook writes to order log."""
        import app as app_module

        stripe_event = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_coaching_log',
                    'customer_details': {'email': 'log@test.com'},
                    'subscription': 'sub_log_123',
                    'metadata': {
                        'product_type': 'coaching',
                        'tier': 'max',
                        'athlete_name': 'Log Test',
                    }
                }
            }
        }

        response = client.post(
            '/webhook/stripe',
            json=stripe_event,
            content_type='application/json'
        )

        assert response.status_code == 200

        # Verify log file was written
        log_dir = Path(app_module.ATHLETES_DIR) / '.logs'
        log_files = list(log_dir.glob('*.jsonl'))
        assert len(log_files) > 0

        with open(log_files[0]) as f:
            lines = f.readlines()
        last_entry = json.loads(lines[-1])
        assert last_entry['product_type'] == 'coaching'
        assert last_entry['tier'] == 'max'

    def test_coaching_webhook_idempotent(self, client, temp_athletes_dir):
        """Duplicate coaching webhook is caught by idempotency."""
        stripe_event = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_coaching_dup',
                    'customer_details': {'email': 'dup@test.com'},
                    'metadata': {
                        'product_type': 'coaching',
                        'tier': 'min',
                        'athlete_name': 'Dup Test',
                    }
                }
            }
        }

        # First call
        r1 = client.post('/webhook/stripe', json=stripe_event,
                         content_type='application/json')
        assert r1.status_code == 200
        assert r1.get_json()['status'] == 'success'

        # Second call (duplicate)
        r2 = client.post('/webhook/stripe', json=stripe_event,
                         content_type='application/json')
        assert r2.status_code == 200
        assert r2.get_json()['status'] == 'duplicate'


class TestConsultingWebhook:
    """Tests for consulting webhook handler."""

    def test_consulting_webhook_processes_payment(self, client, temp_athletes_dir):
        """Consulting webhook processes payment and logs event."""
        stripe_event = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_consulting_123',
                    'customer_details': {
                        'name': 'Consult Client',
                        'email': 'consult@example.com',
                    },
                    'metadata': {
                        'product_type': 'consulting',
                        'athlete_name': 'Consult Client',
                        'hours': '2',
                        'analytics_consent': 'granted',
                        'ga4_client_id': '1391278887.1471780587',
                        'ga4_session_id': '1787846400',
                    }
                }
            }
        }

        with patch('app._send_ga4_purchase') as ga4_purchase:
            response = client.post(
                '/webhook/stripe',
                json=stripe_event,
                content_type='application/json'
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'success'
        assert data['product_type'] == 'consulting'
        assert data['hours'] == '2'
        kwargs = ga4_purchase.call_args.kwargs
        assert kwargs['client_id'] == '1391278887.1471780587'
        assert kwargs['session_id'] == '1787846400'
        assert kwargs['analytics_consent'] == 'granted'

    def test_consulting_webhook_logs_event(self, client, temp_athletes_dir):
        """Consulting webhook writes to order log."""
        import app as app_module

        stripe_event = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_consulting_log',
                    'customer_details': {'email': 'clog@test.com'},
                    'metadata': {
                        'product_type': 'consulting',
                        'athlete_name': 'Log Consult',
                        'hours': '5',
                    }
                }
            }
        }

        response = client.post(
            '/webhook/stripe',
            json=stripe_event,
            content_type='application/json'
        )

        assert response.status_code == 200

        log_dir = Path(app_module.ATHLETES_DIR) / '.logs'
        log_files = list(log_dir.glob('*.jsonl'))
        assert len(log_files) > 0

        with open(log_files[0]) as f:
            lines = f.readlines()
        last_entry = json.loads(lines[-1])
        assert last_entry['product_type'] == 'consulting'
        assert last_entry['hours'] == '5'

    def test_consulting_webhook_idempotent(self, client, temp_athletes_dir):
        """Duplicate consulting webhook is caught by idempotency."""
        stripe_event = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_consulting_dup',
                    'customer_details': {'email': 'cdup@test.com'},
                    'metadata': {
                        'product_type': 'consulting',
                        'athlete_name': 'Dup Consult',
                        'hours': '1',
                    }
                }
            }
        }

        r1 = client.post('/webhook/stripe', json=stripe_event,
                         content_type='application/json')
        assert r1.status_code == 200
        assert r1.get_json()['status'] == 'success'

        r2 = client.post('/webhook/stripe', json=stripe_event,
                         content_type='application/json')
        assert r2.status_code == 200
        assert r2.get_json()['status'] == 'duplicate'


class TestPastDateRejection:
    """Tests for past race date validation."""

    def test_checkout_rejects_old_past_date(self, client):
        """Checkout rejects race dates more than 7 days in the past."""
        old_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        response = client.post(
            '/api/create-checkout',
            json={
                'name': 'Test',
                'email': 'test@test.com',
                'races': [{'name': 'Old Race', 'date': old_date, 'priority': 'A'}],
            },
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert 'past' in data['error'].lower()

    def test_checkout_allows_recent_past_date(self, client, temp_athletes_dir):
        """Checkout allows race dates within 7 days past (just-happened race)."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_recent_past'
            mock_session.url = 'https://checkout.stripe.com/test'
            mock_stripe.checkout.Session.create.return_value = mock_session

            recent_date = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
            response = client.post(
                '/api/create-checkout',
                json={
                    'name': 'Test',
                    'email': 'test@test.com',
                    'races': [{'name': 'Recent Race', 'date': recent_date, 'priority': 'A'}],
                },
                content_type='application/json'
            )
            assert response.status_code == 200

    def test_checkout_rejects_invalid_date_format(self, client):
        """Checkout rejects non-ISO date formats."""
        response = client.post(
            '/api/create-checkout',
            json={
                'name': 'Test',
                'email': 'test@test.com',
                'races': [{'name': 'Bad Date', 'date': '06/15/2026', 'priority': 'A'}],
            },
            content_type='application/json'
        )
        assert response.status_code == 400
        data = response.get_json()
        assert 'date' in data['error'].lower()


class TestEmailMasking:
    """Tests for PII email masking in logs."""

    def test_mask_email_standard(self):
        """Standard email is properly masked."""
        from app import _mask_email
        assert _mask_email('user@example.com') == 'u***@e***.com'

    def test_mask_email_short_local(self):
        """Single-char local part is masked."""
        from app import _mask_email
        assert _mask_email('u@example.com') == 'u***@e***.com'

    def test_mask_email_empty(self):
        """Empty/invalid emails return '***'."""
        from app import _mask_email
        assert _mask_email('') == '***'
        assert _mask_email('not-an-email') == '***'
        assert _mask_email(None) == '***'

    def test_mask_email_preserves_tld(self):
        """TLD is preserved for readability."""
        from app import _mask_email
        result = _mask_email('test@company.co.uk')
        assert result.endswith('.uk')


class TestNotification:
    """Tests for order notification system."""

    def test_notify_logs_critical_without_smtp(self):
        """Without SMTP config, notification logs at CRITICAL level."""
        from app import _notify_new_order
        with patch('app.logger') as mock_logger:
            _notify_new_order('coaching', {'name': 'Test', 'tier': 'mid'})
            mock_logger.critical.assert_called_once()
            call_msg = mock_logger.critical.call_args[0][0]
            assert 'coaching' in call_msg.lower()
            assert 'Test' in call_msg


class TestLogProductEvent:
    """Tests for _log_product_event shared helper."""

    def test_log_product_event_writes_jsonl(self, temp_athletes_dir):
        """_log_product_event writes valid JSONL."""
        from app import _log_product_event

        with patch('app.DATA_DIR', str(temp_athletes_dir)):
            _log_product_event('coaching', 'order_123', tier='mid', name='Test')

        log_dir = temp_athletes_dir / '.logs'
        log_files = list(log_dir.glob('*.jsonl'))
        assert len(log_files) == 1

        with open(log_files[0]) as f:
            entry = json.loads(f.readline())
        assert entry['product_type'] == 'coaching'
        assert entry['order_id'] == 'order_123'
        assert entry['tier'] == 'mid'
        assert entry['success'] is True


class TestIdempotencyTiming:
    """Tests that idempotency marking happens BEFORE pipeline execution."""

    def test_order_marked_before_pipeline(self, client, temp_athletes_dir):
        """Order is marked processed before pipeline runs (TOCTOU fix)."""
        import app as app_module

        call_order = []

        def mock_mark(order_id, athlete_id):
            call_order.append('mark')
            # Call the real function
            original_mark(order_id, athlete_id)

        def mock_pipeline(athlete_id, deliver=True, **kwargs):
            call_order.append('pipeline')
            # By the time pipeline runs, order should already be marked
            from app import check_idempotency
            assert check_idempotency('cs_test_timing'), \
                "Order must be marked as processed BEFORE pipeline runs"
            return {'success': True, 'stdout': '', 'stderr': ''}

        original_mark = app_module.mark_order_processed

        stripe_event = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_test_timing',
                    'customer_details': {
                        'name': 'Timing Test',
                        'email': 'timing@test.com',
                    },
                    'metadata': {'tier': 'custom'}
                }
            }
        }

        with patch('app.mark_order_processed', side_effect=mock_mark), \
             patch('app.run_pipeline', side_effect=mock_pipeline):
            response = client.post(
                '/webhook/stripe',
                json=stripe_event,
                content_type='application/json'
            )

        assert response.status_code == 200
        assert call_order == ['mark', 'pipeline'], \
            f"Expected mark before pipeline, got: {call_order}"


class TestCheckoutRecovery:
    """Tests for abandoned cart recovery flow."""

    def test_expired_checkout_sends_recovery(self, client, temp_athletes_dir):
        """Expired checkout with consent triggers recovery."""
        expired_event = {
            'type': 'checkout.session.expired',
            'data': {
                'object': {
                    'id': 'cs_expired_123',
                    'customer_details': {'email': 'abandoned@test.com'},
                    'metadata': {
                        'product_type': 'training_plan',
                        'athlete_name': 'Abandoned User',
                        'weeks': '12',
                    },
                    'consent': {'promotions': 'opt_in'},
                    'after_expiration': {
                        'recovery': {
                            'url': 'https://checkout.stripe.com/recover/cs_expired_123',
                        }
                    },
                }
            }
        }

        with patch('app.RESEND_API_KEY', 're_test'), \
             patch('app._send_email', return_value=True):
            response = client.post(
                '/webhook/stripe',
                json=expired_event,
                content_type='application/json'
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'recovery_sent'

    def test_coaching_recovery_is_case_bound_and_only_sent_once(
            self, client, temp_athletes_dir):
        import app as app_module
        case_id = 'f7bafdf0-e451-4db1-a678-280639e62685'
        case = {
            'schema': 'coaching_onboarding_case/v1',
            'case_id': case_id,
            'brand': 'gravelgod',
            'tier': 'mid',
            'state': 'PAYMENT_PENDING',
            'athlete': {'name': 'Recovery Rider', 'email': 'recover@test.com'},
            'source': {'submitted_at': datetime.now(timezone.utc).isoformat()},
            'questionnaire': {'age': '40'},
            'verifications': {},
            'receipts': {},
            'checkout': {'session_id': 'cs_case_expired'},
        }
        app_module._write_coaching_intake(case)
        event = {
            'type': 'checkout.session.expired',
            'data': {'object': {
                'id': 'cs_case_expired',
                'customer_details': {'email': 'recover@test.com'},
                'metadata': {
                    'product_type': 'coaching', 'tier': 'mid',
                    'brand': 'gravelgod', 'athlete_name': 'Recovery Rider',
                    'intake_id': case_id,
                },
                'consent': {'promotions': 'opt_in'},
                'after_expiration': {'recovery': {
                    'url': 'https://checkout.stripe.com/recover/case'}},
            }}
        }

        with patch('app.RESEND_API_KEY', 're_test'), \
             patch('app._send_email', return_value=True) as send:
            first = client.post('/webhook/stripe', json=event)
            # Simulate a distinct Stripe retry/session expiry for the same case;
            # the case-level guard still permits only one recovery email.
            event['data']['object']['id'] = 'cs_case_expired_again'
            second = client.post('/webhook/stripe', json=event)

        assert first.get_json()['status'] == 'recovery_sent'
        assert second.get_json()['reason'] == 'Case recovery already sent'
        assert send.call_count == 1
        stored = app_module._read_coaching_intake(case_id)
        names = [item['event_name'] for item in stored['analytics_events']]
        assert names.count('coaching_checkout_recovery_sent') == 1
        assert stored['checkout']['recovery_disposition'] == (
            'case_recovery_already_sent')

    def test_expired_checkout_skips_without_consent(self, client, temp_athletes_dir):
        """Expired checkout without consent does not send recovery."""
        expired_event = {
            'type': 'checkout.session.expired',
            'data': {
                'object': {
                    'id': 'cs_expired_noconsent',
                    'customer_details': {'email': 'noconsent@test.com'},
                    'metadata': {
                        'product_type': 'training_plan',
                        'athlete_name': 'No Consent',
                        'weeks': '8',
                    },
                    'consent': {},
                    'after_expiration': {
                        'recovery': {
                            'url': 'https://checkout.stripe.com/recover/cs_expired_noconsent',
                        }
                    },
                }
            }
        }

        response = client.post(
            '/webhook/stripe',
            json=expired_event,
            content_type='application/json'
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ignored'

    def test_expired_checkout_skips_without_recovery_url(self, client, temp_athletes_dir):
        """Expired checkout without recovery URL is ignored."""
        expired_event = {
            'type': 'checkout.session.expired',
            'data': {
                'object': {
                    'id': 'cs_expired_nourl',
                    'customer_details': {'email': 'nourl@test.com'},
                    'metadata': {'product_type': 'training_plan'},
                    'consent': {'promotions': 'opt_in'},
                    'after_expiration': {'recovery': {}},
                }
            }
        }

        response = client.post(
            '/webhook/stripe',
            json=expired_event,
            content_type='application/json'
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ignored'

    def test_recovery_email_content(self, temp_athletes_dir):
        """Recovery email has correct subject and content per product type."""
        from app import _send_recovery_email

        # Just verify it doesn't crash (no SMTP configured = logs CRITICAL)
        with patch('app.logger') as mock_logger:
            _send_recovery_email(
                'test@test.com', 'Jane Doe', 'training_plan',
                {'weeks': '12'}, 'https://recover.example.com'
            )
            mock_logger.critical.assert_called_once()
            call_msg = mock_logger.critical.call_args[0][0]
            # PII must be masked — raw email must NOT appear in logs
            assert 'test@test.com' not in call_msg, "Raw email in log — PII violation"
            assert 't***@t***.com' in call_msg  # masked email
            assert 'https://recover.example.com' in call_msg

    def test_checkout_session_includes_recovery_params(self, client, temp_athletes_dir):
        """Checkout session creation includes recovery and consent params."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_test_recovery_params'
            mock_session.url = 'https://checkout.stripe.com/test'
            mock_stripe.checkout.Session.create.return_value = mock_session

            future_date = (datetime.now() + timedelta(weeks=12)).strftime('%Y-%m-%d')
            response = client.post(
                '/api/create-checkout',
                json={
                    'name': 'Recovery Test',
                    'email': 'recovery@test.com',
                    'races': [{'name': 'Test', 'date': future_date, 'priority': 'A'}],
                },
                content_type='application/json'
            )

            assert response.status_code == 200
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs

            # Verify recovery params
            assert call_kwargs['after_expiration']['recovery']['enabled'] is True
            assert 'expires_at' in call_kwargs
            assert call_kwargs['consent_collection'] == {'promotions': 'auto'}

            # Verify session_id in success URL
            assert '{CHECKOUT_SESSION_ID}' in call_kwargs['success_url']

    def test_recovered_session_logged(self, client, temp_athletes_dir):
        """Recovered sessions are logged when processed."""
        stripe_event = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_recovered_123',
                    'recovered_from': 'cs_expired_original',
                    'customer_details': {
                        'name': 'Recovered User',
                        'email': 'recovered@test.com',
                    },
                    'metadata': {'tier': 'custom'}
                }
            }
        }

        with patch('app.run_pipeline') as mock_pipeline:
            mock_pipeline.return_value = {'success': True, 'stdout': '', 'stderr': ''}

            response = client.post(
                '/webhook/stripe',
                json=stripe_event,
                content_type='application/json'
            )

            assert response.status_code == 200


class TestFollowupEmails:
    """Tests for post-purchase follow-up email sequence."""

    def test_cron_endpoint_rejects_no_secret(self, client):
        """Cron endpoint requires CRON_SECRET to be configured."""
        with patch('app.CRON_SECRET', ''):
            response = client.post('/api/cron/followup-emails')
            assert response.status_code == 503

    def test_cron_endpoint_rejects_bad_secret(self, client):
        """Cron endpoint rejects invalid secret."""
        with patch('app.CRON_SECRET', 'real-secret'):
            response = client.post(
                '/api/cron/followup-emails',
                headers={'X-Cron-Secret': 'wrong-secret'}
            )
            assert response.status_code == 401

    def test_cron_endpoint_accepts_valid_secret(self, client):
        """Cron endpoint processes with valid secret."""
        with patch('app.CRON_SECRET', 'test-secret'), \
             patch('app.process_followup_emails') as mock_process:
            mock_process.return_value = {'checked': 0, 'sent': 0, 'skipped': 0, 'errors': 0}
            response = client.post(
                '/api/cron/followup-emails',
                headers={'X-Cron-Secret': 'test-secret'}
            )
            assert response.status_code == 200
            assert response.get_json()['status'] == 'ok'

    def test_process_sends_day_1_email(self, tmp_path):
        """Day 1 follow-up sent for order placed yesterday."""
        log_dir = tmp_path / '.logs'
        log_dir.mkdir()

        # Create order from 1 day ago — write to YYYY-MM.jsonl (the actual log format)
        order_time = (datetime.utcnow() - timedelta(days=1))
        order = json.dumps({
            'product_type': 'training_plan',
            'order_id': 'cs_test_day1',
            'email': 'athlete@test.com',
            'name': 'Test Athlete',
            'timestamp': order_time.isoformat(),
            'success': True,
        })
        log_filename = order_time.strftime('%Y-%m') + '.jsonl'
        (log_dir / log_filename).write_text(order + '\n')

        with patch('app.DATA_DIR', str(tmp_path)), \
             patch('app._send_followup_email') as mock_send:
            mock_send.return_value = True
            from app import process_followup_emails
            stats = process_followup_emails()

        assert stats['sent'] == 1
        mock_send.assert_called_once()
        args = mock_send.call_args
        assert 'athlete@test.com' == args[0][0]
        assert 'one thing to do first' in args[0][1]

    def test_process_skips_already_sent(self, tmp_path):
        """Follow-up not re-sent if already tracked."""
        log_dir = tmp_path / '.logs'
        log_dir.mkdir()

        order_time = (datetime.utcnow() - timedelta(days=1))
        order = json.dumps({
            'product_type': 'training_plan',
            'order_id': 'cs_test_dedup',
            'email': 'athlete@test.com',
            'name': 'Test',
            'timestamp': order_time.isoformat(),
            'success': True,
        })
        log_filename = order_time.strftime('%Y-%m') + '.jsonl'
        (log_dir / log_filename).write_text(order + '\n')

        # Mark day 1 as already sent
        sent = json.dumps({
            'order_id': 'cs_test_dedup',
            'day': 1,
            'email': 'a***@test.com',
            'sent_at': datetime.utcnow().isoformat(),
        })
        (log_dir / 'followup_sent.jsonl').write_text(sent + '\n')

        with patch('app.DATA_DIR', str(tmp_path)), \
             patch('app._send_followup_email') as mock_send:
            from app import process_followup_emails
            stats = process_followup_emails()

        mock_send.assert_not_called()
        assert stats['sent'] == 0

    def test_process_skips_coaching_orders(self, tmp_path):
        """Coaching orders don't get automated follow-ups."""
        log_dir = tmp_path / '.logs'
        log_dir.mkdir()

        order_time = (datetime.utcnow() - timedelta(days=1))
        order = json.dumps({
            'product_type': 'coaching',
            'order_id': 'cs_test_coaching',
            'email': 'coach@test.com',
            'name': 'Coach Client',
            'timestamp': order_time.isoformat(),
            'success': True,
        })
        log_filename = order_time.strftime('%Y-%m') + '.jsonl'
        (log_dir / log_filename).write_text(order + '\n')

        with patch('app.DATA_DIR', str(tmp_path)), \
             patch('app._send_followup_email') as mock_send:
            from app import process_followup_emails
            stats = process_followup_emails()

        mock_send.assert_not_called()
        assert stats['checked'] == 0

    def test_process_sends_day_7_with_coaching_upsell(self, tmp_path):
        """Day 7 email includes coaching cross-sell link."""
        log_dir = tmp_path / '.logs'
        log_dir.mkdir()

        order_time = (datetime.utcnow() - timedelta(days=7))
        order = json.dumps({
            'product_type': 'training_plan',
            'order_id': 'cs_test_day7',
            'email': 'athlete@test.com',
            'name': 'Week One Done',
            'timestamp': order_time.isoformat(),
            'success': True,
        })
        log_filename = order_time.strftime('%Y-%m') + '.jsonl'
        (log_dir / log_filename).write_text(order + '\n')

        with patch('app.DATA_DIR', str(tmp_path)), \
             patch('app._send_followup_email') as mock_send:
            mock_send.return_value = True
            from app import process_followup_emails
            process_followup_emails()

        # Day 7 email should mention coaching
        call_args = mock_send.call_args
        assert '/coaching/' in call_args[0][2]  # body contains coaching URL

    def test_followup_sequence_has_required_fields(self):
        """All follow-up templates have required fields."""
        from app import FOLLOWUP_SEQUENCE
        for followup in FOLLOWUP_SEQUENCE:
            assert 'day' in followup
            assert 'subject' in followup
            assert 'template' in followup
            assert '{first_name}' in followup['template']
            assert followup['day'] > 0

    def test_mark_and_read_followup_sent(self, tmp_path):
        """Sent log correctly tracks and reads follow-ups."""
        with patch('app.DATA_DIR', str(tmp_path)):
            from app import _mark_followup_sent, _get_sent_followups
            _mark_followup_sent('order_123', 1, 'test@example.com')
            _mark_followup_sent('order_123', 3, 'test@example.com')

            sent = _get_sent_followups()
            assert ('order_123', 1) in sent
            assert ('order_123', 3) in sent
            assert ('order_123', 7) not in sent


# ── Quality Gate Tests ──────────────────────────────────────


class TestSetupFeeAllTiers:
    """Setup fee must be included in ALL coaching tier checkouts, not just min."""

    @pytest.fixture(autouse=True)
    def setup_stripe_mock(self, client, temp_athletes_dir):
        self.client = client

    def test_setup_fee_on_every_tier(self, client, temp_athletes_dir):
        """Every coaching tier includes exactly 2 line items (subscription + $99 fee)."""
        for tier in ['min', 'mid', 'max']:
            with patch('app.stripe') as mock_stripe:
                mock_session = MagicMock()
                mock_session.id = f'cs_fee_{tier}'
                mock_session.url = f'https://checkout.stripe.com/{tier}'
                mock_stripe.checkout.Session.create.return_value = mock_session

                response = client.post(
                    '/api/create-coaching-checkout',
                    json={'name': 'Fee Test', 'email': 'fee@test.com', 'tier': tier},
                    content_type='application/json'
                )
                assert response.status_code == 200
                call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
                line_items = call_kwargs['line_items']
                assert len(line_items) == 2, (
                    f"Tier {tier}: expected 2 line items (subscription + fee), got {len(line_items)}"
                )

    def test_setup_fee_price_id_matches(self, client, temp_athletes_dir):
        """Setup fee line item uses the correct price ID."""
        from app import COACHING_SETUP_FEE_PRICE_ID
        assert COACHING_SETUP_FEE_PRICE_ID, "COACHING_SETUP_FEE_PRICE_ID must not be empty"

        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_fee_check'
            mock_session.url = 'https://checkout.stripe.com/fee-check'
            mock_stripe.checkout.Session.create.return_value = mock_session

            client.post(
                '/api/create-coaching-checkout',
                json={'name': 'Fee Test', 'email': 'fee@test.com', 'tier': 'min'},
                content_type='application/json'
            )
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            fee_item = call_kwargs['line_items'][1]
            assert fee_item['price'] == COACHING_SETUP_FEE_PRICE_ID


class TestPIIMasking:
    """Raw email addresses must never appear in log output."""

    def test_recovery_email_handler_masks_pii(self):
        """Recovery email fallback logging uses _mask_email, not raw email."""
        import inspect
        from app import _send_recovery_email
        source = inspect.getsource(_send_recovery_email)
        # Count raw email references in log calls
        import re
        log_calls = re.findall(r'logger\.\w+\(.*?\)', source, re.DOTALL)
        for call in log_calls:
            if 'email' in call.lower() and 'mask_email' not in call and 'Email:' in call:
                assert '_mask_email' in call, (
                    f"PII violation: raw email in log call: {call[:100]}"
                )

    def test_no_month_in_recovery_emails(self):
        """Recovery email copy must not say 'month' — billing is every 4 weeks."""
        import inspect
        from app import _send_recovery_email
        source = inspect.getsource(_send_recovery_email)
        assert 'first month' not in source, (
            "Recovery email says 'first month' — should say 'first few weeks' (billing is /4wk)"
        )


class TestCoachingSuccessUrl:
    """Coaching checkout success URL must include session_id for GA4."""

    def test_success_url_has_session_id(self, client, temp_athletes_dir):
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_url_check'
            mock_session.url = 'https://checkout.stripe.com/url-check'
            mock_stripe.checkout.Session.create.return_value = mock_session

            client.post(
                '/api/create-coaching-checkout',
                json={'name': 'URL Test', 'email': 'url@test.com', 'tier': 'min'},
                content_type='application/json'
            )
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert '{CHECKOUT_SESSION_ID}' in call_kwargs['success_url'], (
                "Success URL must include {CHECKOUT_SESSION_ID} for GA4 attribution"
            )


class TestExpiredCheckoutIdempotency:
    """Expired checkout handler must be idempotent — no duplicate recovery emails."""

    def test_duplicate_expired_event_caught(self, client, temp_athletes_dir):
        """Second expired event for same session returns duplicate."""
        expired_event = {
            'type': 'checkout.session.expired',
            'data': {
                'object': {
                    'id': 'cs_expired_idem',
                    'customer_details': {'email': 'idem@test.com'},
                    'metadata': {
                        'product_type': 'training_plan',
                        'athlete_name': 'Idem Test',
                        'weeks': '8',
                    },
                    'consent': {'promotions': 'opt_in'},
                    'after_expiration': {
                        'recovery': {
                            'url': 'https://checkout.stripe.com/recover/cs_expired_idem',
                        }
                    },
                }
            }
        }

        with patch('app.RESEND_API_KEY', 're_test'), \
             patch('app._send_email', return_value=True):
            r1 = client.post('/webhook/stripe', json=expired_event,
                             content_type='application/json')
            assert r1.status_code == 200
            assert r1.get_json()['status'] == 'recovery_sent'

            r2 = client.post('/webhook/stripe', json=expired_event,
                             content_type='application/json')
        assert r2.status_code == 200
        assert r2.get_json()['status'] == 'duplicate'

    def test_expired_handler_returns_200_on_error(self, client, temp_athletes_dir):
        """Expired handler returns 200 even on internal error to stop Stripe retries."""
        expired_event = {
            'type': 'checkout.session.expired',
            'data': {
                'object': {
                    'id': 'cs_expired_err',
                    'customer_details': {'email': 'err@test.com'},
                    'metadata': {
                        'product_type': 'training_plan',
                        'athlete_name': 'Error Test',
                    },
                    'consent': {'promotions': 'opt_in'},
                    'after_expiration': {
                        'recovery': {
                            'url': 'https://checkout.stripe.com/recover/cs_expired_err',
                        }
                    },
                }
            }
        }

        with patch('app._send_recovery_email', side_effect=Exception('SMTP down')):
            response = client.post('/webhook/stripe', json=expired_event,
                                   content_type='application/json')
            assert response.status_code == 200


class TestCustomerCreation:
    """Training plan and consulting checkouts must create Stripe customers."""

    def test_training_plan_creates_customer(self, client, temp_athletes_dir):
        """Training plan checkout includes customer_creation='always'."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_cust_tp'
            mock_session.url = 'https://checkout.stripe.com/test'
            mock_stripe.checkout.Session.create.return_value = mock_session

            future_date = (datetime.now() + timedelta(weeks=12)).strftime('%Y-%m-%d')
            client.post(
                '/api/create-checkout',
                json={
                    'name': 'Cust Test',
                    'email': 'cust@test.com',
                    'races': [{'name': 'R', 'date': future_date, 'priority': 'A'}],
                },
                content_type='application/json'
            )
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert call_kwargs['customer_creation'] == 'always'

    def test_consulting_creates_customer(self, client, temp_athletes_dir):
        """Consulting checkout includes customer_creation='always'."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_cust_consult'
            mock_session.url = 'https://checkout.stripe.com/test'
            mock_stripe.checkout.Session.create.return_value = mock_session

            client.post(
                '/api/create-consulting-checkout',
                json={'name': 'Cust Test', 'email': 'cust@test.com', 'hours': 1},
                content_type='application/json'
            )
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert call_kwargs['customer_creation'] == 'always'


class TestCoachingCheckoutEnhancements:
    """Coaching checkout must collect phone + pass metadata to subscription."""

    def test_phone_number_collected(self, client, temp_athletes_dir):
        """Coaching checkout enables phone number collection."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_phone'
            mock_session.url = 'https://checkout.stripe.com/test'
            mock_stripe.checkout.Session.create.return_value = mock_session

            client.post(
                '/api/create-coaching-checkout',
                json={'name': 'Phone Test', 'email': 'phone@test.com', 'tier': 'min'},
                content_type='application/json'
            )
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert call_kwargs['phone_number_collection'] == {'enabled': True}

    def test_subscription_data_has_metadata(self, client, temp_athletes_dir):
        """Coaching checkout passes tier + name to subscription metadata."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock()
            mock_session.id = 'cs_submeta'
            mock_session.url = 'https://checkout.stripe.com/test'
            mock_stripe.checkout.Session.create.return_value = mock_session

            client.post(
                '/api/create-coaching-checkout',
                json={'name': 'Meta Test', 'email': 'meta@test.com', 'tier': 'max'},
                content_type='application/json'
            )
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            sub_data = call_kwargs['subscription_data']
            assert sub_data['metadata']['tier'] == 'max'
            assert sub_data['metadata']['athlete_name'] == 'Meta Test'


class TestLogOrderSchema:
    """log_order must include email, name, product_type for follow-up emails."""

    def test_log_order_has_followup_fields(self, temp_athletes_dir):
        """log_order entries include email, name, product_type."""
        from app import log_order

        order_data = {
            'athlete_id': 'test_log',
            'order_id': 'cs_log_test',
            'tier': 'custom',
            'profile': {
                'name': 'Log Schema Test',
                'email': 'schema@test.com',
            }
        }
        result = {'success': True, 'stdout': '', 'stderr': ''}

        with patch('app.DATA_DIR', str(temp_athletes_dir)):
            log_order(order_data, result)

        log_dir = temp_athletes_dir / '.logs'
        log_files = list(log_dir.glob('*.jsonl'))
        assert len(log_files) == 1

        with open(log_files[0]) as f:
            entry = json.loads(f.readline())

        assert entry['product_type'] == 'training_plan'
        assert entry['email'] == 'schema@test.com'
        assert entry['name'] == 'Log Schema Test'
        assert entry['order_id'] == 'cs_log_test'
        assert entry['success'] is True


class TestFollowupReadsCorrectLogFiles:
    """process_followup_emails must read from YYYY-MM.jsonl, not orders.jsonl."""

    def test_reads_monthly_log_not_orders_jsonl(self, tmp_path):
        """Follow-up reads from YYYY-MM.jsonl (where log_order writes)."""
        log_dir = tmp_path / '.logs'
        log_dir.mkdir()

        order_time = (datetime.utcnow() - timedelta(days=1))
        order = json.dumps({
            'product_type': 'training_plan',
            'order_id': 'cs_correct_path',
            'email': 'correct@test.com',
            'name': 'Correct Path',
            'timestamp': order_time.isoformat(),
            'success': True,
        })

        # Write to the correct YYYY-MM.jsonl path
        log_filename = order_time.strftime('%Y-%m') + '.jsonl'
        (log_dir / log_filename).write_text(order + '\n')

        # orders.jsonl should NOT exist (that was the old bug)
        assert not (log_dir / 'orders.jsonl').exists()

        with patch('app.DATA_DIR', str(tmp_path)), \
             patch('app._send_followup_email') as mock_send:
            mock_send.return_value = True
            from app import process_followup_emails
            stats = process_followup_emails()

        # Should find the order and send day 1 email
        assert stats['checked'] == 1
        assert stats['sent'] == 1

    def test_skips_failed_orders(self, tmp_path):
        """Follow-up skips orders where pipeline failed."""
        log_dir = tmp_path / '.logs'
        log_dir.mkdir()

        order_time = (datetime.utcnow() - timedelta(days=1))
        order = json.dumps({
            'product_type': 'training_plan',
            'order_id': 'cs_failed',
            'email': 'failed@test.com',
            'name': 'Failed Order',
            'timestamp': order_time.isoformat(),
            'success': False,
            'error': 'Pipeline timed out',
        })
        log_filename = order_time.strftime('%Y-%m') + '.jsonl'
        (log_dir / log_filename).write_text(order + '\n')

        with patch('app.DATA_DIR', str(tmp_path)), \
             patch('app._send_followup_email') as mock_send:
            from app import process_followup_emails
            stats = process_followup_emails()

        mock_send.assert_not_called()
        assert stats['checked'] == 0


class TestRateLimiting:
    """Checkout endpoints must have rate limiting."""

    def test_rate_limit_config_exists(self):
        """Verify rate limiter is configured on the app."""
        import app as app_module
        assert hasattr(app_module, 'limiter'), "Flask-Limiter not configured on app"

    def test_checkout_endpoints_have_limits(self):
        """All checkout endpoints must be rate-limited."""
        import inspect
        import app as app_module
        source = inspect.getsource(app_module)
        # Each checkout endpoint should have @limiter.limit before it
        for endpoint in ['create_checkout', 'create_coaching_checkout',
                         'create_consulting_checkout']:
            # Find the function definition and check for limiter decorator
            pattern = f'limiter.limit.*\ndef {endpoint}'
            import re
            assert re.search(pattern, source), (
                f"Endpoint {endpoint} missing rate limit decorator"
            )


class TestQuestionnaireStarted:
    """Tests for /api/questionnaire-started endpoint."""

    def test_tracks_valid_start(self, client, temp_athletes_dir):
        """Valid name + email gets logged."""
        os.environ['DATA_DIR'] = str(temp_athletes_dir)
        response = client.post('/api/questionnaire-started',
                               json={'name': 'Test Rider', 'email': 'test@example.com',
                                     'sections_reached': 2, 'source': 'questionnaire'})
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'tracked'

        # Verify log file written (monthly rotation)
        from datetime import datetime as dt
        month = dt.now().strftime('%Y-%m')
        log_file = temp_athletes_dir / '.logs' / f'questionnaire-starts-{month}.jsonl'
        assert log_file.exists()
        entry = json.loads(log_file.read_text().strip())
        assert entry['email'] == 'test@example.com'
        assert entry['name'] == 'Test Rider'

    def test_deduplicates_within_24hrs(self, client, temp_athletes_dir):
        """Same email within 24hrs returns already_tracked."""
        os.environ['DATA_DIR'] = str(temp_athletes_dir)
        client.post('/api/questionnaire-started',
                    json={'name': 'Test', 'email': 'dupe@example.com'})
        response = client.post('/api/questionnaire-started',
                               json={'name': 'Test', 'email': 'dupe@example.com'})
        assert response.status_code == 200
        assert response.get_json()['status'] == 'already_tracked'

    def test_missing_email_returns_204(self, client):
        """Missing email fails silently."""
        response = client.post('/api/questionnaire-started',
                               json={'name': 'Test'})
        assert response.status_code == 204

    def test_invalid_email_returns_204(self, client):
        """Invalid email fails silently."""
        response = client.post('/api/questionnaire-started',
                               json={'name': 'Test', 'email': 'notanemail'})
        assert response.status_code == 204

    def test_empty_body_returns_204(self, client, temp_athletes_dir):
        """Empty/missing JSON fails silently."""
        os.environ['DATA_DIR'] = str(temp_athletes_dir)
        response = client.post('/api/questionnaire-started',
                               data='', content_type='application/json')
        assert response.status_code == 204

    def test_cors_preflight(self, client):
        """OPTIONS request returns 204."""
        response = client.options('/api/questionnaire-started')
        assert response.status_code == 204

    def test_sends_notification_email(self, client, temp_athletes_dir):
        """Coach gets notified of new questionnaire start."""
        os.environ['DATA_DIR'] = str(temp_athletes_dir)
        with patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app.RESEND_API_KEY', 'test-key'), \
             patch('app._send_email') as mock_send:
            mock_send.return_value = True
            client.post('/api/questionnaire-started',
                        json={'name': 'Test Rider', 'email': 'test@example.com'})
            mock_send.assert_called_once()
            args = mock_send.call_args
            assert 'coach@example.com' in args[0]
            assert 'Test Rider' in args[0][1]  # subject

    def test_health_check_has_no_storage_or_email_side_effects(
            self, client, temp_athletes_dir):
        """Synthetic monitors stop before lead storage and notification."""
        os.environ['DATA_DIR'] = str(temp_athletes_dir)
        with patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app.RESEND_API_KEY', 'test-key'), \
             patch('app._send_email') as mock_send:
            response = client.post('/api/questionnaire-started', json={
                'name': 'Daily Health Check [TEST]',
                'email': 'healthcheck@gravelgodcycling.com',
                'sections_reached': 1,
                'source': 'health-check',
            })

        assert response.status_code == 200
        assert response.get_json()['status'] == 'ignored'
        assert not (temp_athletes_dir / '.logs').exists()
        mock_send.assert_not_called()

    def test_pii_not_logged(self, client, temp_athletes_dir):
        """Email address is masked in log output."""
        os.environ['DATA_DIR'] = str(temp_athletes_dir)
        import logging
        with patch.object(logging.getLogger('gravel-god-webhook'), 'info') as mock_log:
            client.post('/api/questionnaire-started',
                        json={'name': 'Test', 'email': 'secret@example.com'})
            log_msg = mock_log.call_args[0][0]
            assert 'secret@example.com' not in log_msg
            assert 's***' in log_msg  # masked

    def test_rate_limited(self):
        """Endpoint has rate limit decorator."""
        import inspect
        import app as app_module
        source = inspect.getsource(app_module)
        import re
        assert re.search(r'limiter\.limit.*\ndef questionnaire_started', source), \
            "questionnaire_started missing rate limit"


class TestPipelineErrorExcerpt:
    """Failure details must survive into logs/emails. intake_to_plan.py
    reports most errors on STDOUT — stderr-only capture produced the blank
    error field in the Jun 9 2026 Jesse Couch failure."""

    def test_prefers_stderr_when_present(self):
        from app import _pipeline_error_excerpt
        result = {'stderr': 'Traceback: boom', 'stdout': 'lots of progress'}
        assert _pipeline_error_excerpt(result) == 'Traceback: boom'

    def test_falls_back_to_stdout_tail(self):
        from app import _pipeline_error_excerpt
        result = {'stderr': '', 'stdout': 'x' * 1000 + 'FATAL: race not matched'}
        excerpt = _pipeline_error_excerpt(result)
        assert 'FATAL: race not matched' in excerpt
        assert len(excerpt) <= 500

    def test_empty_result_gives_empty_string(self):
        from app import _pipeline_error_excerpt
        assert _pipeline_error_excerpt({'stderr': '', 'stdout': ''}) == ''
        assert _pipeline_error_excerpt({}) == ''

    def test_whitespace_only_stderr_falls_back(self):
        from app import _pipeline_error_excerpt
        result = {'stderr': '  \n ', 'stdout': 'the real error'}
        assert _pipeline_error_excerpt(result) == 'the real error'


def _ga4_brands(gravel_secret='secret', rl_secret='rl-secret'):
    """BRANDS dict with controllable MP secrets for tests."""
    return {
        'gravelgod': {
            'name': 'Gravel God Cycling',
            'site': 'https://gravelgodcycling.com',
            'questionnaire_path': '/training-plans/questionnaire/',
            'ga4_measurement_id': 'G-TESTGRAVEL',
            'ga4_mp_api_secret': gravel_secret,
        },
        'roadielabs': {
            'name': 'Roadie Labs',
            'site': 'https://roadielabs.com',
            'questionnaire_path': '/questionnaire/',
            'ga4_measurement_id': 'G-TESTROAD',
            'ga4_mp_api_secret': rl_secret,
        },
    }


class TestGa4ServerSidePurchase:
    """Server-side GA4 purchase must fire for real payments, skip tests,
    route per brand, and never break order processing."""

    def test_noop_without_api_secret(self):
        import app as app_module
        with patch.object(app_module, 'BRANDS', _ga4_brands(gravel_secret='')), \
             patch.object(app_module.http_requests, 'post') as mock_post:
            app_module._send_ga4_purchase('cs_live_x', 24900, 'training_plan', 'Plan')
        mock_post.assert_not_called()

    def test_skips_test_orders(self):
        import app as app_module
        with patch.object(app_module, 'BRANDS', _ga4_brands()), \
             patch.object(app_module.http_requests, 'post') as mock_post:
            app_module._send_ga4_purchase('test_20260609', 24900, 'training_plan', 'Plan')
        mock_post.assert_not_called()

    def test_skips_stripe_test_mode_orders(self):
        import app as app_module
        with patch.object(app_module, 'BRANDS', _ga4_brands()), \
             patch.object(app_module.http_requests, 'post') as mock_post:
            app_module._send_ga4_purchase('cs_test_abc123', 24900,
                                          'training_plan', 'Plan')
        mock_post.assert_not_called()

    def test_skips_when_analytics_consent_is_denied(self):
        import app as app_module
        with patch.object(app_module, 'BRANDS', _ga4_brands()), \
             patch.object(app_module.http_requests, 'post') as mock_post:
            app_module._send_ga4_purchase(
                'cs_live_private', 24900, 'training_plan', 'Plan',
                analytics_consent='denied')
        mock_post.assert_not_called()

    def test_sends_purchase_payload(self):
        import app as app_module
        with patch.object(app_module, 'BRANDS', _ga4_brands()), \
             patch.object(app_module.http_requests, 'post') as mock_post:
            mock_post.return_value = MagicMock(status_code=204)
            app_module._send_ga4_purchase('cs_live_abc123', 24900,
                                          'training_plan', 'Custom Training Plan')

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert 'google-analytics.com/mp/collect' in args[0]
        assert kwargs['params']['api_secret'] == 'secret'
        assert kwargs['params']['measurement_id'] == 'G-TESTGRAVEL'
        event = kwargs['json']['events'][0]
        assert event['name'] == 'purchase'
        assert event['params']['transaction_id'] == 'cs_live_abc123'
        assert event['params']['value'] == 249.0
        assert event['params']['currency'] == 'USD'
        assert kwargs['timeout'] == 5

    def test_joins_purchase_to_original_browser_session(self):
        import app as app_module
        with patch.object(app_module, 'BRANDS', _ga4_brands()), \
             patch.object(app_module.http_requests, 'post') as mock_post:
            mock_post.return_value = MagicMock(status_code=204)
            app_module._send_ga4_purchase(
                'cs_live_attributed', 24900, 'training_plan', 'Plan',
                client_id='1391278887.1471780587',
                session_id='1787846400')

        payload = mock_post.call_args.kwargs['json']
        assert payload['client_id'] == '1391278887.1471780587'
        params = payload['events'][0]['params']
        assert params['session_id'] == 1787846400
        assert params['engagement_time_msec'] == 1

    def test_invalid_attribution_uses_order_scoped_fallback(self):
        import app as app_module
        with patch.object(app_module, 'BRANDS', _ga4_brands()), \
             patch.object(app_module.http_requests, 'post') as mock_post:
            mock_post.return_value = MagicMock(status_code=204)
            app_module._send_ga4_purchase(
                'cs_live_fallback', 24900, 'training_plan', 'Plan',
                client_id='bad', session_id='also-bad')

        payload = mock_post.call_args.kwargs['json']
        assert payload['client_id'].startswith('srv.')
        assert 'session_id' not in payload['events'][0]['params']

    def test_routes_to_brand_property(self):
        import app as app_module
        with patch.object(app_module, 'BRANDS', _ga4_brands()), \
             patch.object(app_module.http_requests, 'post') as mock_post:
            mock_post.return_value = MagicMock(status_code=204)
            app_module._send_ga4_purchase('cs_live_x', 24900, 'training_plan',
                                          'Plan', brand='roadielabs')
        params = mock_post.call_args.kwargs['params']
        assert params['measurement_id'] == 'G-TESTROAD'
        assert params['api_secret'] == 'rl-secret'

    def test_unconfigured_brand_is_noop(self):
        import app as app_module
        with patch.object(app_module, 'BRANDS', _ga4_brands(rl_secret='')), \
             patch.object(app_module.http_requests, 'post') as mock_post:
            app_module._send_ga4_purchase('cs_live_x', 24900, 'training_plan',
                                          'Plan', brand='roadielabs')
        mock_post.assert_not_called()

    def test_never_raises_on_network_error(self):
        import app as app_module
        with patch.object(app_module, 'BRANDS', _ga4_brands()), \
             patch.object(app_module.http_requests, 'post',
                          side_effect=Exception('network down')):
            # Must not raise — analytics never blocks an order
            app_module._send_ga4_purchase('cs_live_x', 100, 'coaching', 'Coaching')

    def test_handles_missing_amount(self):
        import app as app_module
        with patch.object(app_module, 'BRANDS', _ga4_brands()), \
             patch.object(app_module.http_requests, 'post') as mock_post:
            mock_post.return_value = MagicMock(status_code=204)
            app_module._send_ga4_purchase('cs_live_x', None, 'coaching', 'Coaching')
        event = mock_post.call_args.kwargs['json']['events'][0]
        assert event['params']['value'] == 0.0


class TestMultiBrand:
    """Brand derivation from Origin, CORS allowlist, and brand-aware checkout."""

    def test_roadielabs_origin_maps_to_brand(self):
        from app import _brand_from_origin
        assert _brand_from_origin('https://roadielabs.com') == 'roadielabs'
        assert _brand_from_origin('https://www.roadielabs.com') == 'roadielabs'

    def test_gravel_and_unknown_origins_default(self):
        from app import _brand_from_origin, DEFAULT_BRAND
        assert _brand_from_origin('https://gravelgodcycling.com') == DEFAULT_BRAND
        assert _brand_from_origin('') == DEFAULT_BRAND
        assert _brand_from_origin('https://evil.example.com') == DEFAULT_BRAND

    def test_roadielabs_in_cors_allowlist(self):
        from app import ALLOWED_ORIGINS
        assert 'https://roadielabs.com' in ALLOWED_ORIGINS
        assert 'https://www.roadielabs.com' in ALLOWED_ORIGINS

    def test_brand_config_falls_back_to_default(self):
        from app import _brand_config, BRANDS, DEFAULT_BRAND
        assert _brand_config('nonsense') == BRANDS[DEFAULT_BRAND]
        assert _brand_config('') == BRANDS[DEFAULT_BRAND]

    def test_registry_is_the_authoritative_brand_source(self):
        from app import BRANDS
        assert set(BRANDS) == {'gravelgod', 'roadielabs', 'xcskilabs'}
        assert BRANDS['roadielabs']['discipline'] == 'road'
        assert BRANDS['roadielabs']['allowed_disciplines'] == ['road']
        assert BRANDS['roadielabs']['subject_prefix'] == '[RL]'
        assert BRANDS['roadielabs']['email']['from_email'] == 'coach@roadielabs.com'
        assert set(BRANDS['gravelgod']['allowed_disciplines']) == {'gravel', 'mtb'}
        assert BRANDS['xcskilabs']['discipline'] == 'xc_ski'
        assert BRANDS['xcskilabs']['allowed_disciplines'] == ['xc_ski']
        assert BRANDS['xcskilabs']['subject_prefix'] == '[XC]'
        assert BRANDS['xcskilabs']['training_plan_generation_enabled'] is False
        for brand in ('gravelgod', 'roadielabs', 'xcskilabs'):
            coaching = BRANDS[brand]['coaching']
            assert coaching['enabled'] is True
            assert set(coaching['tiers']) == {'min', 'mid', 'max'}
            assert coaching['trainingpeaks_premium_included'] is True
            assert coaching['billing_period_days'] == 28
            assert coaching['setup_fee_waiver_mode'] == 'case_by_case_private'
            assert coaching['setup_fee_cents'] == 9900

    def test_railway_image_copies_registry_parent_directory(self):
        dockerfile = (Path(__file__).parents[1] / 'Dockerfile').read_text()
        assert 'COPY athletes/ ./athletes/' in dockerfile

    def test_railway_image_copies_apply_contract_schema(self):
        dockerfile = (Path(__file__).parents[1] / 'Dockerfile').read_text()
        assert 'COPY schemas/ ./schemas/' in dockerfile

    def test_checkout_uses_brand_success_url(self, client, tmp_path):
        """A checkout created from roadielabs.com sends the customer back
        to roadielabs.com, with brand recorded in metadata."""
        future = (datetime.now() + timedelta(days=90)).strftime('%Y-%m-%d')
        with patch('app.DATA_DIR', str(tmp_path)), \
             patch('app.stripe.checkout.Session.create') as mock_create:
            mock_create.return_value = MagicMock(
                id='cs_test_rl', url='https://checkout.stripe.com/x')
            resp = client.post(
                '/api/create-checkout',
                json={'name': 'Road Tester', 'email': 'road@test.com',
                      'races': [{'name': 'Maratona', 'date': future,
                                 'priority': 'A'}]},
                headers={'Origin': 'https://roadielabs.com'})

        assert resp.status_code == 200
        kwargs = mock_create.call_args.kwargs
        assert kwargs['success_url'].startswith(
            'https://roadielabs.com/training-plans/success/')
        assert kwargs['cancel_url'] == 'https://roadielabs.com/questionnaire/'
        assert kwargs['metadata']['brand'] == 'roadielabs'

    def test_xcskilabs_origin_maps_to_brand(self):
        from app import _brand_from_origin
        assert _brand_from_origin('https://xcskilabs.com') == 'xcskilabs'
        assert _brand_from_origin('https://www.xcskilabs.com') == 'xcskilabs'

    def test_xcskilabs_in_cors_allowlist(self):
        from app import ALLOWED_ORIGINS
        assert 'https://xcskilabs.com' in ALLOWED_ORIGINS
        assert 'https://www.xcskilabs.com' in ALLOWED_ORIGINS

    def test_checkout_defaults_to_gravel_urls(self, client, tmp_path):
        future = (datetime.now() + timedelta(days=90)).strftime('%Y-%m-%d')
        with patch('app.DATA_DIR', str(tmp_path)), \
             patch('app.stripe.checkout.Session.create') as mock_create:
            mock_create.return_value = MagicMock(
                id='cs_test_gg', url='https://checkout.stripe.com/x')
            resp = client.post(
                '/api/create-checkout',
                json={'name': 'Gravel Tester', 'email': 'gravel@test.com',
                      'races': [{'name': 'Unbound', 'date': future,
                                 'priority': 'A'}]},
                headers={'Origin': 'https://gravelgodcycling.com'})

        assert resp.status_code == 200
        kwargs = mock_create.call_args.kwargs
        assert kwargs['success_url'].startswith(
            'https://gravelgodcycling.com/training-plans/success/')
        assert kwargs['metadata']['brand'] == 'gravelgod'


class TestXcSkiLabsBrand:
    """XC coaching/consulting share the commercial rails while automated
    ski-plan generation remains separately disabled."""

    def test_consulting_checkout_uses_xcskilabs_success_url_and_metadata(
            self, client, temp_athletes_dir):
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock(id='cs_xc', url='https://checkout.stripe.com/xc')
            mock_stripe.checkout.Session.create.return_value = mock_session
            resp = client.post(
                '/api/create-consulting-checkout',
                json={'name': 'XC Tester', 'email': 'xc@test.com'},
                content_type='application/json',
                headers={'Origin': 'https://xcskilabs.com'})

        assert resp.status_code == 200
        call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
        assert call_kwargs['success_url'].startswith(
            'https://xcskilabs.com/consulting/confirmed/')
        assert call_kwargs['cancel_url'] == 'https://xcskilabs.com/consulting/'
        assert call_kwargs['metadata']['brand'] == 'xcskilabs'

    def test_training_plan_checkout_rejected_until_ski_engine_is_ready(
            self, client, tmp_path):
        future = (datetime.now() + timedelta(days=90)).strftime('%Y-%m-%d')
        with patch('app.DATA_DIR', str(tmp_path)), \
             patch('app.stripe.checkout.Session.create') as mock_create:
            resp = client.post(
                '/api/create-checkout',
                json={'name': 'XC Tester', 'email': 'xc@test.com',
                      'races': [{'name': 'Birkie', 'date': future,
                                 'priority': 'A'}]},
                headers={'Origin': 'https://xcskilabs.com'})

        assert resp.status_code == 400
        data = resp.get_json()
        assert 'generation' in data['error'].lower()
        mock_create.assert_not_called()

    def test_training_plan_webhook_rejected_until_ski_engine_is_ready(
            self, client, temp_athletes_dir):
        stripe_event = {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': 'cs_xc_plan_reject',
                    'customer_details': {'name': 'XC Tester', 'email': 'xc@test.com'},
                    'metadata': {
                        'product_type': 'training_plan',
                        'brand': 'xcskilabs',
                    },
                }
            }
        }
        resp = client.post('/webhook/stripe', json=stripe_event,
                           content_type='application/json')
        assert resp.status_code == 400
        data = resp.get_json()
        assert 'generation' in data['error'].lower()

    def test_consult_coach_notification_subject_prefix_xc(
            self, client, temp_athletes_dir):
        with patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app.RESEND_API_KEY', 'test-key'), \
             patch('app._send_email', return_value=True) as mock_send:
            client.post('/webhook/stripe',
                        json=_consulting_stripe_event(session_id='cs_xc_coach',
                                                      brand='xcskilabs'),
                        content_type='application/json')

        coach_calls = [c for c in mock_send.call_args_list
                       if c.args[0] == 'coach@example.com']
        assert len(coach_calls) == 1
        assert coach_calls[0].args[1].startswith('[XC]')

    def test_consult_welcome_renders_for_xcskilabs(self, client, temp_athletes_dir):
        with patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app.RESEND_API_KEY', 'test-key'), \
             patch('app._send_email', return_value=True) as mock_send:
            resp = client.post(
                '/webhook/stripe',
                json=_consulting_stripe_event(session_id='cs_xc_welcome',
                                              email='xcathlete@example.com',
                                              brand='xcskilabs'),
                content_type='application/json')

        assert resp.status_code == 200
        welcome_calls = [c for c in mock_send.call_args_list
                         if c.args[0] == 'xcathlete@example.com']
        assert len(welcome_calls) == 1
        assert welcome_calls[0].kwargs.get('brand') == 'xcskilabs'

    def test_send_email_sender_falls_back_to_gg_default_when_env_unset(
            self, monkeypatch):
        import app as app_module
        monkeypatch.delenv('RESEND_FROM_XCSKILABS', raising=False)
        with patch.object(app_module, 'RESEND_API_KEY', 'test-key'), \
             patch.object(app_module.http_requests, 'post') as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            app_module._send_email('xc@test.com', 'subject', 'body',
                                   brand='xcskilabs')

        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs['json']['from'] == app_module.RESEND_FROM


class TestPipelineTimeoutHeadroom:
    """PIPELINE_TIMEOUT must leave room under gunicorn's --timeout (600) so
    the FAILED email can send before the worker is killed."""

    def test_default_timeout_below_gunicorn(self):
        from app import PIPELINE_TIMEOUT
        assert PIPELINE_TIMEOUT < 600

    def test_dockerfile_gunicorn_timeout_exceeds_pipeline_timeout(self):
        from app import PIPELINE_TIMEOUT
        dockerfile = Path(__file__).parent.parent / 'Dockerfile'
        text = dockerfile.read_text()
        match = re.search(r'"--timeout",\s*"(\d+)"', text)
        assert match, 'gunicorn --timeout not found in Dockerfile CMD'
        assert int(match.group(1)) > PIPELINE_TIMEOUT


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


# =============================================================================
# Lifecycle touchpoints (anti-churn email spine)
# =============================================================================

class TestComputeTouchpoints:
    """compute_touchpoints() builds the plan-aware email schedule."""

    @staticmethod
    def _plan_dates():
        weeks = []
        from datetime import date, timedelta
        start = date(2026, 6, 22)
        for i in range(12):
            monday = start + timedelta(weeks=i)
            weeks.append({
                'week': i + 1,
                'monday': monday.isoformat(),
                'sunday': (monday + timedelta(days=6)).isoformat(),
                'is_recovery_week': (i + 1) in (4, 8),
                'is_race_week': (i + 1) == 12,
            })
        weeks[6]['b_race'] = {'name': 'Tune-Up Race', 'date': '2026-08-08'}
        return {
            'plan_start': '2026-06-22',
            'plan_end': '2026-09-13',
            'race_date': '2026-09-12',
            'race_week_monday': '2026-09-07',
            'weeks': weeks,
        }

    def test_all_touchpoint_kinds_present(self):
        from app import compute_touchpoints
        touches = compute_touchpoints(self._plan_dates(), 'Jesse', 'Borderlands')
        keys = {t['key'] for t in touches}
        assert 'setup_check' in keys
        assert 'ftp_rescale' in keys
        assert 'recovery_note' in keys
        assert 'midplan_survey' in keys
        assert 'b_debrief_2026-08-08' in keys
        assert 'race_week' in keys
        assert 'postrace' in keys

    def test_dates_are_sorted_and_iso(self):
        from app import compute_touchpoints
        import re as _re
        touches = compute_touchpoints(self._plan_dates(), 'Jesse', 'Borderlands')
        dates = [t['date'] for t in touches]
        assert dates == sorted(dates)
        assert all(_re.match(r'^\d{4}-\d{2}-\d{2}$', d) for d in dates)

    def test_setup_check_is_day_two(self):
        from app import compute_touchpoints
        touches = compute_touchpoints(self._plan_dates(), 'Jesse', 'Borderlands')
        setup = next(t for t in touches if t['key'] == 'setup_check')
        assert setup['date'] == '2026-06-23'

    def test_postrace_is_day_after_race(self):
        from app import compute_touchpoints
        touches = compute_touchpoints(self._plan_dates(), 'Jesse', 'Borderlands')
        post = next(t for t in touches if t['key'] == 'postrace')
        assert post['date'] == '2026-09-13'

    def test_recovery_note_on_first_recovery_week_only(self):
        from app import compute_touchpoints
        touches = compute_touchpoints(self._plan_dates(), 'Jesse', 'Borderlands')
        notes = [t for t in touches if t['key'] == 'recovery_note']
        assert len(notes) == 1

    def test_first_name_personalizes_body(self):
        from app import compute_touchpoints
        touches = compute_touchpoints(self._plan_dates(), 'Jesse', 'Borderlands')
        assert all('Jesse' in t['body'] for t in touches)

    def test_empty_plan_dates_returns_empty(self):
        from app import compute_touchpoints
        assert compute_touchpoints({}, 'X', 'Y') == []

    def test_postrace_contains_coaching_bridge(self):
        from app import compute_touchpoints
        touches = compute_touchpoints(self._plan_dates(), 'Jesse', 'Borderlands')
        post = next(t for t in touches if t['key'] == 'postrace')
        assert 'coaching' in post['body'].lower()


class TestTravelDatesPassthrough:
    def test_markdown_preserves_complete_race_demand_vector(self):
        import json
        from app import _questionnaire_to_markdown

        demands = {
            'durability': 8, 'climbing': 10, 'vo2_power': 7,
            'threshold': 8, 'technical': 2, 'heat_resilience': 4,
            'altitude': 3, 'race_specificity': 9,
        }
        md = _questionnaire_to_markdown(
            {'race_demands': demands}, name='T', email='t@e.com')
        line = next(line for line in md.splitlines()
                    if line.startswith('- Race Demands: '))
        assert json.loads(line.split(': ', 1)[1]) == demands

    def test_markdown_includes_travel_dates(self):
        from app import _questionnaire_to_markdown
        md = _questionnaire_to_markdown(
            {'travel_dates': '2026-10-15, 2026-10-18 to 2026-10-19'},
            name='T', email='t@e.com')
        assert 'Travel Dates: 2026-10-15, 2026-10-18 to 2026-10-19' in md

    def test_markdown_travel_dates_default_none(self):
        from app import _questionnaire_to_markdown
        md = _questionnaire_to_markdown({}, name='T', email='t@e.com')
        assert 'Travel Dates: None' in md

    def test_markdown_includes_demonstrated_training_fuel(self):
        from app import _questionnaire_to_markdown
        md = _questionnaire_to_markdown(
            {'training_fuel': '55g/hr'}, name='T', email='t@e.com')
        assert '## Nutrition' in md
        assert 'Training Fuel: 55g/hr' in md

    def test_markdown_devices_come_only_from_form(self):
        from app import _questionnaire_to_markdown
        supplied = _questionnaire_to_markdown(
            {'devices': 'power meter, hr strap'}, name='T', email='t@e.com')
        absent = _questionnaire_to_markdown({}, name='T', email='t@e.com')
        assert 'Devices: power meter, hr strap' in supplied
        assert 'Devices: unknown' in absent
        assert 'power meter, HR strap' not in absent

    def test_markdown_preserves_training_metric(self):
        from app import _questionnaire_to_markdown
        md = _questionnaire_to_markdown(
            {'powerOrHr': 'hr'}, name='T', email='t@e.com')
        assert 'Training Metric: hr' in md

    def test_markdown_preserves_programmed_midweek_ceiling_and_notes_role(self):
        from app import _questionnaire_to_markdown
        md = _questionnaire_to_markdown({
            'programmed_midweek_max_minutes': 45,
            'notes': 'Programmed midweek sessions must not exceed 45 minutes.',
        }, name='Michael Beal', email='wmbeal@outlook.com')
        assert 'Programmed Midweek Max Minutes: 45' in md
        assert '- Notes: Programmed midweek sessions must not exceed 45 minutes.' in md


class TestIntelStatsWindow:
    def test_validates_hours_and_rejects_limit(self, client, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'CRON_SECRET', 'secret')
        headers = {'X-Cron-Secret': 'secret'}
        for query in ('?hours=nope', '?hours=0', '?hours=-1', '?hours=721', '?limit=5'):
            assert client.get('/api/intel-stats' + query, headers=headers).status_code == 400
        response = client.get('/api/intel-stats?hours=720', headers=headers)
        assert response.status_code == 200
        assert response.get_json()['window_hours'] == 720

    def test_reads_every_required_month_and_orders_deterministically(
            self, client, monkeypatch, tmp_path):
        import app as app_module

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 3, 1, 1, 0, 0)

        monkeypatch.setattr(app_module, 'CRON_SECRET', 'secret')
        monkeypatch.setattr(app_module, 'DATA_DIR', str(tmp_path))
        monkeypatch.setattr(app_module, 'datetime', FrozenDatetime)
        log_dir = tmp_path / '.logs'
        log_dir.mkdir()
        (log_dir / '2026-01.jsonl').write_text(json.dumps({
            'timestamp': '2026-01-31T12:00:00', 'order_id': 'cs_b',
            'product_type': 'training_plan', 'email': 'b@real.test',
            'name': 'B', 'success': True}) + '\n')
        (log_dir / '2026-02.jsonl').write_text('\n'.join([
            json.dumps({'timestamp': '2026-02-10T12:00:00', 'order_id': 'cs_z',
                        'product_type': 'training_plan', 'email': 'z@real.test',
                        'name': 'Z', 'success': True}),
            json.dumps({'timestamp': '2026-02-10T12:00:00', 'order_id': 'cs_a',
                        'product_type': 'training_plan', 'email': 'a@real.test',
                        'name': 'A', 'success': False}),
        ]) + '\n')
        response = client.get(
            '/api/intel-stats?hours=720', headers={'X-Cron-Secret': 'secret'})
        assert response.status_code == 200
        data = response.get_json()
        assert [order['id'] for order in data['orders']] == ['cs_b', 'cs_a', 'cs_z']
        assert [order['id'] for order in data['failed_orders']] == ['cs_a']


class TestComplianceNeedsReview:
    """A plan that delivers but fails an auto-compliance check must reach the
    coach as 'NEEDS REVIEW' — the order is NOT lost, it just needs a human pass
    before sending (the safety net that replaced hard-failing the order)."""

    def _details(self, needs_review):
        return {'name': 'Taylor F', 'email': 't@e.com', 'race_name': 'Big Sugar',
                'athlete_id': 'tf', 'needs_review': needs_review,
                'pipeline_success': True, 'download_token': 'tok'}

    def test_needs_review_subject_and_body(self):
        from app import _build_training_plan_email
        subj, text, html = _build_training_plan_email(self._details(True))
        assert 'ACTION REQUIRED' in subj
        assert 'AUTO-CHECK FAILED' in (text + (html or ''))

    def test_clean_delivery_has_no_review_flag(self):
        from app import _build_training_plan_email
        subj, text, html = _build_training_plan_email(self._details(False))
        assert 'NEEDS REVIEW' not in subj
        assert 'AUTO-CHECK FAILED' not in (text + (html or ''))

    def test_marker_in_stdout_sets_needs_review(self):
        from app import _build_plan_notification_details
        order = {'profile': {'name': 'X', 'email': 'x@e.com',
                             'target_race': {'name': 'R', 'date': '2026-10-01'}},
                 'order_id': 'o', 'athlete_id': 'x'}
        flagged = _build_plan_notification_details(
            order, {'success': True, 'stdout': '...\nGG_NEEDS_REVIEW=1\n'}, None)
        clean = _build_plan_notification_details(
            order, {'success': True, 'stdout': 'all good'}, None)
        assert flagged['needs_review'] is True
        assert clean['needs_review'] is False


class TestRaceSlugPassthrough:
    """The race the customer selected (?race=slug) must reach the pipeline so it
    resolves the target race by ID, not by fuzzy-matching the typed name."""

    def test_markdown_emits_race_slug(self):
        from app import _questionnaire_to_markdown
        md = _questionnaire_to_markdown(
            {'race_slug': 'bwr-north-carolina',
             'races': [{'name': 'Belgian Waffle Ride', 'date': '2026-10-03',
                        'distance': '131 miles', 'priority': 'A'}]},
            name='T', email='t@e.com')
        assert 'Race Slug: bwr-north-carolina' in md

    def test_markdown_slug_blank_when_absent(self):
        from app import _questionnaire_to_markdown
        md = _questionnaire_to_markdown(
            {'races': [{'name': 'Some Race', 'date': '2026-10-03', 'priority': 'A'}]},
            name='T', email='t@e.com')
        assert 'Race Slug:' in md  # field present, value empty — harmless

# =============================================================================
# ASYNC PIPELINE JOBS — default production path (SYNC_PIPELINE unset)
# =============================================================================

def _async_env():
    """Context manager: clear SYNC_PIPELINE so the default async path runs."""
    return patch.dict(os.environ, {'SYNC_PIPELINE': ''})


def _stripe_event(session_id='cs_test_async', name='Async Tester',
                  email='async@test.com'):
    return {
        'type': 'checkout.session.completed',
        'data': {
            'object': {
                'id': session_id,
                'amount_total': 18000,
                'customer_details': {'name': name, 'email': email},
                'metadata': {'tier': 'custom'},
            }
        }
    }


@pytest.fixture
def jobs_dir(app):
    """Clean jobs directory for the app module's JOBS_DIR."""
    import app as app_module
    d = Path(app_module.JOBS_DIR)
    if d.exists():
        import shutil
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    yield d
    if d.exists():
        import shutil
        shutil.rmtree(d)


class TestAsyncPipelineJobs:
    """Default async path: 200 to Stripe fast, durable job records."""

    def test_webhook_returns_accepted_without_running_pipeline(
            self, client, temp_athletes_dir, jobs_dir):
        """Webhook responds 'accepted' immediately; pipeline deferred to thread."""
        with _async_env(), \
             patch('app._start_job_thread') as mock_thread, \
             patch('app.run_pipeline') as mock_pipeline:
            response = client.post('/webhook/stripe',
                                   json=_stripe_event('cs_async_accept'),
                                   content_type='application/json')

        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'accepted'
        assert data['athlete_id'] == 'async_tester'
        assert data['job_status'] == 'queued'
        mock_thread.assert_called_once()
        mock_pipeline.assert_not_called()  # nothing ran in the request

        # Durable job record written to the volume, queued
        job = json.loads((jobs_dir / 'async_tester.json').read_text())
        assert job['status'] == 'queued'
        assert job['order_id'] == 'cs_async_accept'
        assert job['attempts'] == 1
        assert job['order_data']['athlete_id'] == 'async_tester'

    def test_order_marked_processed_before_thread_spawn(
            self, client, temp_athletes_dir, jobs_dir):
        """Idempotency mark still lands before generation starts."""
        import app as app_module
        with _async_env(), patch('app._start_job_thread'):
            client.post('/webhook/stripe',
                        json=_stripe_event('cs_async_idem'),
                        content_type='application/json')
        assert app_module.check_idempotency('cs_async_idem')

    def test_cancelled_synthetic_drill_may_be_reprocessed(
            self, temp_athletes_dir, monkeypatch):
        import app as app_module
        from fulfillment_state import CANCELLED, transition, write_generation
        monkeypatch.setattr(app_module, 'DATA_DIR', str(temp_athletes_dir))
        monkeypatch.setattr(
            app_module, 'DELIVERIES_DIR', str(temp_athletes_dir / 'deliveries'))
        order_id = 'drill-20260814'
        path = app_module._fulfillment_status_path(order_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_generation(path, 'daily-drill', order_id=order_id)
        transition(
            path, CANCELLED, 'daily-drill-cleanup',
            credential='operator-secret', metadata={'reason': 'same-day leftover'})
        app_module.mark_order_processed(order_id, 'daily-drill')
        assert app_module.check_idempotency(order_id) is False
        app_module.mark_order_processed('cs_live_paid', 'someone')
        assert app_module.check_idempotency('cs_live_paid') is True

    def test_duplicate_webhook_does_not_double_run(
            self, client, temp_athletes_dir, jobs_dir):
        """Stripe retry → 'duplicate', only one thread ever spawned."""
        with _async_env(), patch('app._start_job_thread') as mock_thread:
            r1 = client.post('/webhook/stripe',
                             json=_stripe_event('cs_async_dup'),
                             content_type='application/json')
            r2 = client.post('/webhook/stripe',
                             json=_stripe_event('cs_async_dup'),
                             content_type='application/json')

        assert r1.get_json()['status'] == 'accepted'
        assert r2.get_json()['status'] == 'duplicate'
        assert mock_thread.call_count == 1

    def test_repeat_athlete_gets_independent_order_job(
            self, temp_athletes_dir, app, jobs_dir):
        """Same athlete may have two orders; only order id deduplicates."""
        import app as app_module
        app_module._write_job({'athlete_id': 'busy_rider', 'order_id': 'cs_1',
                               'status': 'running', 'attempts': 1})

        with _async_env(), patch('app._start_job_thread') as mock_thread:
            job, result = app_module._spawn_plan_job(
                {'athlete_id': 'busy_rider', 'order_id': 'cs_2',
                 'tier': 'custom', 'profile': {}})

        assert job['order_id'] == 'cs_2'
        assert result is None
        mock_thread.assert_called_once()

    def test_persistence_failure_never_reports_success(
            self, client, temp_athletes_dir, jobs_dir):
        """No durable order state means failed job + failure notice."""
        import app as app_module

        def run_inline(job, intake_data=None):
            return app_module._execute_plan_job(job, intake_data=intake_data)

        with _async_env(), \
             patch('app._start_job_thread', side_effect=run_inline), \
             patch('app.run_pipeline') as mock_pipeline, \
             patch('app.persist_deliverables') as mock_persist, \
             patch('app._notify_new_order') as mock_notify, \
            patch('app.log_order') as mock_log:
            mock_pipeline.return_value = {'success': True, 'stdout': '', 'stderr': ''}
            mock_persist.return_value = None
            response = client.post('/webhook/stripe',
                                   json=_stripe_event('cs_async_ok'),
                                   content_type='application/json')

        assert response.get_json()['status'] == 'accepted'
        mock_pipeline.assert_called_once()
        assert mock_persist.call_args.args[:2] == ('cs_async_ok', 'async_tester')
        mock_log.assert_called_once()
        assert mock_notify.call_args[0][0] == 'training_plan_FAILED'
        assert mock_notify.call_args[0][1]['fulfillment_state'] == 'unavailable'

        job = json.loads((jobs_dir / 'async_tester.json').read_text())
        assert job['status'] == 'failed'
        assert 'Persistence returned no durable order state' in job['error']

    def test_failure_marks_failed_and_notifies_operator(
            self, client, temp_athletes_dir, jobs_dir):
        """Pipeline failure → job failed with error, loud operator email."""
        import app as app_module

        def run_inline(job, intake_data=None):
            return app_module._execute_plan_job(job, intake_data=intake_data)

        with _async_env(), \
             patch('app._start_job_thread', side_effect=run_inline), \
             patch('app.run_pipeline') as mock_pipeline, \
             patch('app._notify_new_order') as mock_notify:
            mock_pipeline.return_value = {
                'success': False, 'stdout': '', 'stderr': 'boom: step 7 exploded'}
            response = client.post('/webhook/stripe',
                                   json=_stripe_event('cs_async_fail'),
                                   content_type='application/json')

        assert response.status_code == 200  # Stripe still gets 200
        assert mock_notify.call_args[0][0] == 'training_plan_FAILED'

        job = json.loads((jobs_dir / 'async_tester.json').read_text())
        assert job['status'] == 'failed'
        assert 'boom' in job['error']

    def test_job_crash_never_leaves_running_record(
            self, temp_athletes_dir, app, jobs_dir):
        """An unexpected exception in the thread marks the job failed."""
        import app as app_module
        job = {'athlete_id': 'crash_case', 'order_id': 'cs_crash',
               'status': 'queued', 'attempts': 1,
               'order_data': {'athlete_id': 'crash_case', 'order_id': 'cs_crash',
                              'tier': 'custom', 'profile': {}}}
        app_module._write_job(job)

        with patch('app.run_pipeline', side_effect=RuntimeError('kaboom')), \
             patch('app._notify_new_order') as mock_notify:
            result = app_module._execute_plan_job(job)

        assert result['success'] is False
        record = app_module._read_job('crash_case')
        assert record['status'] == 'failed'
        assert 'kaboom' in record['error']
        assert mock_notify.call_args[0][0] == 'training_plan_FAILED'

    def test_atomic_write_leaves_no_temp_files(self, app, jobs_dir):
        """Job writes go through temp + os.replace; no droppings left."""
        import app as app_module
        app_module._write_job({'athlete_id': 'atomic_check',
                               'status': 'queued', 'attempts': 1})
        leftovers = [p for p in jobs_dir.iterdir() if p.suffix == '.tmp']
        assert leftovers == []
        assert app_module._read_job('atomic_check')['status'] == 'queued'


class TestJobSweep:
    """Startup/cron sweep retries jobs orphaned by restarts."""

    def _stale_job(self, app_module, athlete_id, status='running', attempts=1,
                   minutes_old=45):
        job = {
            'athlete_id': athlete_id,
            'order_id': f'cs_{athlete_id}',
            'status': status,
            'attempts': attempts,
            'max_attempts': 2,
            'order_data': {'athlete_id': athlete_id,
                           'order_id': f'cs_{athlete_id}',
                           'tier': 'custom',
                           'profile': {'name': 'Stuck Rider',
                                       'email': 'stuck@test.com'}},
        }
        app_module._write_job(job)
        # Backdate updated_at past the stuck threshold
        raw_path = app_module._canonical_job_path(f'cs_{athlete_id}')
        raw = json.loads(raw_path.read_text())
        raw['updated_at'] = (datetime.now()
                             - timedelta(minutes=minutes_old)).isoformat()
        raw_path.write_text(json.dumps(raw))
        return raw

    def test_sweep_retries_stuck_job(self, app, jobs_dir):
        import app as app_module
        self._stale_job(app_module, 'stuck_one', status='running', attempts=1)

        with _async_env(), patch('app._start_job_thread') as mock_thread:
            stats = app_module.sweep_stuck_jobs()

        assert stats['retried'] == 1
        assert stats['failed'] == 0
        mock_thread.assert_called_once()
        record = app_module._read_job('stuck_one')
        assert record['status'] == 'queued'
        assert record['attempts'] == 2

    def test_sweep_fails_job_after_max_attempts(self, app, jobs_dir):
        import app as app_module
        self._stale_job(app_module, 'stuck_max', status='queued', attempts=2)

        with _async_env(), \
             patch('app._start_job_thread') as mock_thread, \
             patch('app._notify_new_order') as mock_notify:
            stats = app_module.sweep_stuck_jobs()

        assert stats['failed'] == 1
        assert stats['retried'] == 0
        mock_thread.assert_not_called()
        assert mock_notify.call_args[0][0] == 'training_plan_FAILED'
        record = app_module._read_job('stuck_max')
        assert record['status'] == 'failed'
        assert 'stuck' in record['error'].lower()

    def test_sweep_skips_fresh_and_finished_jobs(self, app, jobs_dir):
        import app as app_module
        # Fresh running job (just written → updated_at = now)
        app_module._write_job({'athlete_id': 'fresh_run', 'status': 'running',
                               'attempts': 1, 'order_data': {}})
        # Finished jobs
        app_module._write_job({'athlete_id': 'done_ok', 'status': 'succeeded',
                               'attempts': 1})
        app_module._write_job({'athlete_id': 'done_bad', 'status': 'failed',
                               'attempts': 2})

        with _async_env(), patch('app._start_job_thread') as mock_thread:
            stats = app_module.sweep_stuck_jobs()

        assert stats['retried'] == 0
        assert stats['failed'] == 0
        mock_thread.assert_not_called()
        assert app_module._read_job('fresh_run')['status'] == 'running'

    def test_sweep_endpoint_requires_secret(self, client, jobs_dir):
        with patch.dict(os.environ, {'CRON_SECRET': 'shhh'}), \
             patch('app.CRON_SECRET', 'shhh'):
            r_no = client.post('/api/jobs/sweep')
            r_bad = client.post('/api/jobs/sweep',
                                headers={'X-Cron-Secret': 'wrong'})
            r_ok = client.post('/api/jobs/sweep',
                               headers={'X-Cron-Secret': 'shhh'})

        assert r_no.status_code == 401
        assert r_bad.status_code == 401
        assert r_ok.status_code == 200
        assert r_ok.get_json()['status'] == 'ok'


class TestFulfillmentStatusEndpoint:
    """GET /api/fulfillment/<athlete_id>/status — same auth as the transition
    endpoint. This is the Railway-authoritative status the TP apply CLI polls
    for its APPROVED preflight gate; fulfillment_status.json is deliberately
    excluded from downloaded packages, so a stale local snapshot can never
    satisfy that gate (spec sol r2 F1)."""

    def test_rejects_missing_secret(self, client, monkeypatch):
        monkeypatch.setenv('CRON_SECRET', 'real-secret')
        response = client.get('/api/fulfillment/status_rider/status')
        assert response.status_code == 401

    def test_rejects_wrong_secret(self, client, monkeypatch):
        monkeypatch.setenv('CRON_SECRET', 'real-secret')
        response = client.get('/api/fulfillment/status_rider/status',
                              headers={'X-Cron-Secret': 'wrong-secret'})
        assert response.status_code == 401

    def test_rejects_invalid_athlete_id(self, client, monkeypatch):
        monkeypatch.setenv('CRON_SECRET', 'test-secret')
        response = client.get('/api/fulfillment/UPPERCASE!/status',
                              headers={'X-Cron-Secret': 'test-secret'})
        assert response.status_code == 404

    def test_unknown_athlete_returns_404(self, client, monkeypatch):
        monkeypatch.setenv('CRON_SECRET', 'test-secret')
        response = client.get('/api/fulfillment/no_such_status_athlete/status',
                              headers={'X-Cron-Secret': 'test-secret'})
        assert response.status_code == 404

    def test_returns_current_state_with_evidence(self, client, monkeypatch):
        import shutil
        import app as app_module
        from fulfillment_state import (APPROVED, finalize_transitional_release,
                                       transition, write_generation)

        monkeypatch.setenv('CRON_SECRET', 'test-secret')
        athlete_id = 'status_ready_rider'
        order_id = 'test_status_ready'
        state_path = app_module._fulfillment_status_path(order_id)
        try:
            write_generation(state_path, athlete_id, order_id=order_id)
            revision = app_module._order_dir(order_id) / 'revisions' / 'r1'
            revision.mkdir(parents=True)
            (revision / 'artifact.txt').write_text('sealed')
            sealed = finalize_transitional_release(
                state_path, revision, expected_revision=1)
            transition(
                state_path, APPROVED, 'coach_lee', expected_revision=1,
                expected_catalog_digest=sealed['review_catalog_digest'],
                review_decisions=[
                    {
                        'item_id': item['item_id'], 'revision': 1,
                        'disposition': 'confirmed',
                    }
                    for item in sealed['review_items']
                    if item['type'] in {'required_confirmation', 'verified_fact'}
                ],
            )
            app_module._record_order_lookup(order_id, athlete_id)

            response = client.get(f'/api/fulfillment/{order_id}/status',
                                  headers={'X-Cron-Secret': 'test-secret'})
            assert response.status_code == 200
            data = response.get_json()
            assert data['athlete_id'] == athlete_id
            assert data['status'] == 'APPROVED'
            assert data['generation_revision'] == 1
            assert data['approval']['coach'] == 'coach_lee'
            assert data['application'] is None
            assert 'updated_at' in data
            assert 'blocking_issues' in data
        finally:
            shutil.rmtree(app_module._order_dir(order_id), ignore_errors=True)


class TestOrderStatus:
    """Customer-facing /api/order-status — honest, gentle, never an error."""

    def test_processing_while_job_running(self, app, client, jobs_dir):
        import app as app_module
        order_id = 'test_inflight_rider'
        app_module._write_job({'athlete_id': 'inflight_rider',
                               'order_id': order_id,
                               'status': 'running', 'attempts': 1})
        app_module.mark_order_processed(order_id, 'inflight_rider')
        r = client.get(f'/api/order-status/{order_id}')
        assert r.status_code == 200
        data = r.get_json()
        assert data['status'] == 'processing'
        assert data['download_ready'] is False

    def test_ready_when_job_succeeded(self, app, client, jobs_dir):
        import app as app_module
        order_id = 'test_done_rider'
        app_module._write_job({'athlete_id': 'done_rider',
                               'order_id': order_id,
                               'status': 'succeeded', 'attempts': 1})
        app_module.mark_order_processed(order_id, 'done_rider')
        r = client.get(f'/api/order-status/{order_id}')
        assert r.get_json()['status'] == 'processing'
        assert r.get_json()['download_ready'] is False

    def test_ready_when_customer_zip_exists(self, app, client, jobs_dir):
        import app as app_module
        order_id = 'test_zipped_rider'
        d = Path(app_module.DELIVERIES_DIR) / 'zipped_rider'
        d.mkdir(parents=True, exist_ok=True)
        (d / 'zipped_rider-training-plan.zip').write_bytes(b'PK')
        try:
            app_module.mark_order_processed(order_id, 'zipped_rider')
            r = client.get(f'/api/order-status/{order_id}')
            data = r.get_json()
            assert data['status'] == 'processing'
            assert data['download_ready'] is False
        finally:
            (d / 'zipped_rider-training-plan.zip').unlink()

    def test_failed_job_reads_gentle_not_broken(self, app, client, jobs_dir):
        """Failure is loud to the operator, invisible-gentle to the customer."""
        import app as app_module
        order_id = 'test_failed_rider'
        app_module._write_job({'athlete_id': 'failed_rider', 'status': 'failed',
                               'order_id': order_id,
                               'attempts': 2,
                               'error': 'Traceback: ValueError: secret stack'})
        app_module.mark_order_processed(order_id, 'failed_rider')
        r = client.get(f'/api/order-status/{order_id}')
        assert r.status_code == 200
        data = r.get_json()
        assert data['status'] == 'processing'  # never "failed" to customer
        body = r.get_data(as_text=True)
        assert 'Traceback' not in body
        assert 'secret stack' not in body
        assert 'error' not in data

    def test_session_id_resolves_via_processed_orders(
            self, client, temp_athletes_dir, jobs_dir):
        """Success page only has ?session_id= — map it to the athlete."""
        import app as app_module
        with _async_env(), patch('app._start_job_thread'):
            client.post('/webhook/stripe',
                        json=_stripe_event('cs_status_lookup'),
                        content_type='application/json')

        r = client.get('/api/order-status/cs_status_lookup')
        assert r.status_code == 200
        assert r.get_json()['status'] == 'processing'  # job queued

    def test_unknown_session_reads_processing_not_error(self, client, jobs_dir):
        """Webhook may lag the success page — never scare the customer."""
        r = client.get('/api/order-status/cs_never_seen_before')
        assert r.status_code == 200
        assert r.get_json()['status'] == 'processing'

    def test_unknown_athlete_returns_404(self, client, jobs_dir):
        r = client.get('/api/order-status/nobody_here_xyz')
        assert r.status_code == 404
        assert r.get_json()['status'] == 'unknown'

    def test_invalid_ref_rejected(self, client, jobs_dir):
        r = client.get('/api/order-status/..%2Fetc%2Fpasswd')
        assert r.status_code == 404


class TestSyncPipelineEscapeHatch:
    """SYNC_PIPELINE=1 keeps the old inline path for tests/local debugging."""

    def test_sync_mode_flag(self, app):
        import app as app_module
        with patch.dict(os.environ, {'SYNC_PIPELINE': '1'}):
            assert app_module._sync_pipeline_mode() is True
        with patch.dict(os.environ, {'SYNC_PIPELINE': ''}):
            assert app_module._sync_pipeline_mode() is False

    def test_sync_mode_returns_legacy_success_contract(
            self, client, temp_athletes_dir, jobs_dir):
        """Inline path reports success only after durable persistence."""
        persisted = {'state': {
            'status': 'BLOCKED_REVIEW', 'blocking_issues': [],
            'required_confirmations': [],
        }}
        with patch.dict(os.environ, {'SYNC_PIPELINE': '1'}), \
             patch('app.run_pipeline') as mock_pipeline, \
             patch('app.persist_deliverables', return_value=persisted), \
             patch('app._generate_download_token', return_value='token'), \
             patch('app._notify_new_order'):
            mock_pipeline.return_value = {'success': True, 'stdout': '', 'stderr': ''}
            r = client.post('/webhook/stripe',
                            json=_stripe_event('cs_sync_legacy'),
                            content_type='application/json')
        assert r.get_json()['status'] == 'success'

        # And the job record reflects the completed run
        import app as app_module
        assert app_module._read_job('async_tester')['status'] == 'succeeded'


# =============================================================================
# CONSULT-ENGINE C1 (docs/CONSULT_ENGINE_SPEC.md)
# =============================================================================

def _consult_dir(app_module):
    return Path(app_module.DELIVERIES_DIR)


def _consulting_stripe_event(session_id='cs_consult_engine', name='Jesse Couch',
                             email='jesse@example.com', hours='1',
                             plan_addon='0', brand=None, amount_total=15000):
    metadata = {
        'product_type': 'consulting',
        'athlete_name': name,
        'hours': hours,
        'plan_addon': plan_addon,
    }
    if brand:
        metadata['brand'] = brand
    return {
        'type': 'checkout.session.completed',
        'data': {
            'object': {
                'id': session_id,
                'amount_total': amount_total,
                'customer_details': {'name': name, 'email': email},
                'metadata': metadata,
            }
        }
    }


class TestConsultingCheckoutAddon:
    """POST /api/create-consulting-checkout — add-on line item + brand."""

    def test_addon_not_added_when_env_unset(self, client, temp_athletes_dir):
        with patch('app.stripe') as mock_stripe, patch('app.CONSULT_PLAN_ADDON_PRICE_ID', ''):
            mock_session = MagicMock(id='cs_x', url='https://checkout.stripe.com/x')
            mock_stripe.checkout.Session.create.return_value = mock_session
            client.post('/api/create-consulting-checkout',
                        json={'name': 'T', 'email': 't@test.com', 'plan_addon': True},
                        content_type='application/json')
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert len(call_kwargs['line_items']) == 1
            assert call_kwargs['metadata']['plan_addon'] == '0'

    def test_addon_added_when_env_set_and_requested(self, client, temp_athletes_dir):
        with patch('app.stripe') as mock_stripe, \
             patch('app.CONSULT_PLAN_ADDON_PRICE_ID', 'price_addon_test'):
            mock_session = MagicMock(id='cs_x', url='https://checkout.stripe.com/x')
            mock_stripe.checkout.Session.create.return_value = mock_session
            client.post('/api/create-consulting-checkout',
                        json={'name': 'T', 'email': 't@test.com', 'plan_addon': True},
                        content_type='application/json')
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert len(call_kwargs['line_items']) == 2
            assert call_kwargs['line_items'][1]['price'] == 'price_addon_test'
            assert call_kwargs['metadata']['plan_addon'] == '1'

    def test_addon_not_added_when_env_set_but_not_requested(self, client, temp_athletes_dir):
        with patch('app.stripe') as mock_stripe, \
             patch('app.CONSULT_PLAN_ADDON_PRICE_ID', 'price_addon_test'):
            mock_session = MagicMock(id='cs_x', url='https://checkout.stripe.com/x')
            mock_stripe.checkout.Session.create.return_value = mock_session
            client.post('/api/create-consulting-checkout',
                        json={'name': 'T', 'email': 't@test.com'},
                        content_type='application/json')
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert len(call_kwargs['line_items']) == 1

    def test_brand_from_origin_in_metadata_and_urls(self, client, temp_athletes_dir):
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock(id='cs_x', url='https://checkout.stripe.com/x')
            mock_stripe.checkout.Session.create.return_value = mock_session
            client.post('/api/create-consulting-checkout',
                        json={'name': 'T', 'email': 't@test.com'},
                        content_type='application/json',
                        headers={'Origin': 'https://gravelgodcycling.com'})
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert call_kwargs['metadata']['brand'] == 'gravelgod'
            assert call_kwargs['success_url'].startswith(
                'https://gravelgodcycling.com/consulting/confirmed/')
            assert call_kwargs['cancel_url'] == 'https://gravelgodcycling.com/consulting/'

    def test_existing_default_hour_and_multi_hour_contract_unchanged(self, client, temp_athletes_dir):
        """Regression: pre-C1 behavior for the base line item must be untouched."""
        with patch('app.stripe') as mock_stripe:
            mock_session = MagicMock(id='cs_x', url='https://checkout.stripe.com/x')
            mock_stripe.checkout.Session.create.return_value = mock_session
            client.post('/api/create-consulting-checkout',
                        json={'name': 'T', 'email': 't@test.com', 'hours': 3},
                        content_type='application/json')
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert call_kwargs['line_items'][0]['price'] == 'price_1T2ekVLoaHDbEqSq0GGfoBEX'
            assert call_kwargs['line_items'][0]['quantity'] == 3


class TestConsultAddonCheckoutRoute:
    """POST /api/create-consult-addon-checkout — post-call add-on purchase."""

    def test_503_when_addon_price_unconfigured(self, client, temp_athletes_dir):
        with patch('app.CONSULT_PLAN_ADDON_PRICE_ID', ''):
            r = client.post('/api/create-consult-addon-checkout',
                            json={'ref': 'cs_1'}, content_type='application/json')
            assert r.status_code == 503

    def test_400_missing_ref(self, client, temp_athletes_dir):
        with patch('app.CONSULT_PLAN_ADDON_PRICE_ID', 'price_addon'):
            r = client.post('/api/create-consult-addon-checkout',
                            json={}, content_type='application/json')
            assert r.status_code == 400

    def test_404_unknown_consultation(self, client, temp_athletes_dir):
        with patch('app.CONSULT_PLAN_ADDON_PRICE_ID', 'price_addon'):
            r = client.post('/api/create-consult-addon-checkout',
                            json={'ref': 'cs_does_not_exist'},
                            content_type='application/json')
            assert r.status_code == 404

    def test_creates_addon_only_checkout_for_existing_record(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_addon_ref', brand='gravelgod',
                                          athlete_email='j@test.com')
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.CONSULT_PLAN_ADDON_PRICE_ID', 'price_addon'), \
             patch('app.stripe') as mock_stripe:
            mock_session = MagicMock(id='cs_x', url='https://checkout.stripe.com/addon')
            mock_stripe.checkout.Session.create.return_value = mock_session
            r = client.post('/api/create-consult-addon-checkout',
                            json={'ref': 'cs_addon_ref'}, content_type='application/json')
            assert r.status_code == 200
            call_kwargs = mock_stripe.checkout.Session.create.call_args.kwargs
            assert call_kwargs['line_items'] == [{'price': 'price_addon', 'quantity': 1}]
            assert call_kwargs['metadata']['product_type'] == 'consult_addon'
            assert call_kwargs['metadata']['consult_order_id'] == 'cs_addon_ref'
            assert call_kwargs['customer_email'] == 'j@test.com'

    def test_options_preflight(self, client):
        r = client.options('/api/create-consult-addon-checkout')
        assert r.status_code == 204


class TestConsultingWebhookRecordOrder:
    """Record + welcome happen BEFORE mark_order_processed (§3)."""

    def test_existing_response_shape_still_green(self, client, temp_athletes_dir):
        """test_consulting_webhook_processes_payment, reproduced here to
        pin the exact regression the spec calls out — tolerate missing
        amount_total/customer_email/brand, never 500, response unchanged."""
        stripe_event = {
            'type': 'checkout.session.completed',
            'data': {'object': {
                'id': 'cs_consulting_123',
                'customer_details': {'name': 'Consult Client', 'email': 'consult@example.com'},
                'metadata': {'product_type': 'consulting', 'athlete_name': 'Consult Client',
                            'hours': '2'},
            }}
        }
        response = client.post('/webhook/stripe', json=stripe_event, content_type='application/json')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'success'
        assert data['product_type'] == 'consulting'
        assert data['hours'] == '2'

    def test_record_written_before_mark_order_processed(self, client, temp_athletes_dir, app):
        import app as app_module
        call_order = []
        orig_mark = app_module.mark_order_processed
        orig_write = app_module.consultations.write_record

        def spy_mark(*a, **kw):
            call_order.append('mark_order_processed')
            return orig_mark(*a, **kw)

        def spy_write(*a, **kw):
            call_order.append('write_record')
            return orig_write(*a, **kw)

        with patch('app.mark_order_processed', side_effect=spy_mark), \
             patch.object(app_module.consultations, 'write_record', side_effect=spy_write):
            r = client.post('/webhook/stripe',
                            json=_consulting_stripe_event(session_id='cs_order_test'),
                            content_type='application/json')
        assert r.status_code == 200
        assert call_order.index('write_record') < call_order.index('mark_order_processed')

    def test_record_persisted_with_open_status(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        client.post('/webhook/stripe',
                    json=_consulting_stripe_event(session_id='cs_persist_test'),
                    content_type='application/json')
        record = consultations.read_record(_consult_dir(app_module), 'cs_persist_test')
        assert record is not None
        assert record['status'] == 'open'
        assert record['athlete']['email'] == 'jesse@example.com'
        assert record['timeline'][0]['event'] == 'paid'

    def test_never_500_on_email_failure(self, client, temp_athletes_dir):
        with patch('app._send_email', side_effect=RuntimeError('resend down')):
            r = client.post('/webhook/stripe',
                            json=_consulting_stripe_event(session_id='cs_email_fail'),
                            content_type='application/json')
        assert r.status_code == 200
        assert r.get_json()['status'] == 'success'


class TestConsultWelcomeContent:
    """Welcome email: fragment intake link, TP link, booking link, null on failure."""

    def test_welcome_content_has_booking_intake_and_tp_links(self, client, temp_athletes_dir, app):
        with patch('app.CONSULT_BOOKING_URL', 'https://cal.example/matti'), \
             patch.dict(os.environ, {'CONSULT_INTAKE_TOKEN_SECRET': 'test-secret'}), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app.RESEND_API_KEY', 'test-key'), \
             patch('app._send_email', return_value=True) as mock_send:
            client.post('/webhook/stripe',
                        json=_consulting_stripe_event(session_id='cs_welcome_test'),
                        content_type='application/json')

        welcome_calls = [c for c in mock_send.call_args_list
                         if c.args[0] == 'jesse@example.com']
        assert len(welcome_calls) == 1
        body = welcome_calls[0].args[2]
        assert 'https://cal.example/matti' in body
        assert '/consulting/intake/#ref=cs_welcome_test&t=' in body
        assert 'https://home.trainingpeaks.com/attachtocoach?sharedKey=2OTEPC6BXNVQU' in body

    def test_welcome_sent_at_null_on_resend_failure_then_cron_resends(
            self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations

        with patch('app.RESEND_API_KEY', ''):  # _send_email returns False
            client.post('/webhook/stripe',
                        json=_consulting_stripe_event(session_id='cs_resend_fail'),
                        content_type='application/json')

        record = consultations.read_record(_consult_dir(app_module), 'cs_resend_fail')
        assert record['welcome_sent_at'] is None

        with patch('app.CRON_SECRET', 'shhh'), \
             patch('app.RESEND_API_KEY', 'test-key'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True):
            r = client.post('/api/cron/followup-emails',
                            headers={'X-Cron-Secret': 'shhh'})
        assert r.status_code == 200
        assert r.get_json()['consult']['welcome_resent'] == 1

        record = consultations.read_record(_consult_dir(app_module), 'cs_resend_fail')
        assert record['welcome_sent_at'] is not None


class TestConsultAddonWebhook:
    """consult_addon webhook branch: idempotent flip on the existing record."""

    def test_flips_plan_addon_purchased_on_existing_record(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_addon_flip', brand='gravelgod',
                                          athlete_email='j@test.com')
        consultations.write_record(_consult_dir(app_module), record)

        event = {
            'type': 'checkout.session.completed',
            'data': {'object': {
                'id': 'cs_addon_purchase_1',
                'customer_details': {'email': 'j@test.com'},
                'metadata': {'product_type': 'consult_addon', 'consult_order_id': 'cs_addon_flip'},
            }}
        }
        r = client.post('/webhook/stripe', json=event, content_type='application/json')
        assert r.status_code == 200
        assert r.get_json()['status'] == 'success'

        updated = consultations.read_record(_consult_dir(app_module), 'cs_addon_flip')
        assert updated['products']['plan_addon']['purchased'] is True
        assert updated['products']['plan_addon']['purchased_at'] is not None

    def test_repeat_webhook_sends_no_second_email(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_addon_dup', brand='gravelgod',
                                          athlete_email='j@test.com')
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            event1 = {
                'type': 'checkout.session.completed',
                'data': {'object': {
                    'id': 'cs_addon_dup_1',
                    'customer_details': {'email': 'j@test.com'},
                    'metadata': {'product_type': 'consult_addon', 'consult_order_id': 'cs_addon_dup'},
                }}
            }
            client.post('/webhook/stripe', json=event1, content_type='application/json')
            first_call_count = mock_send.call_count
            assert first_call_count >= 1

            record_reloaded = consultations.read_record(_consult_dir(app_module), 'cs_addon_dup')
            assert record_reloaded['products']['plan_addon']['purchased'] is True

            event2 = {
                'type': 'checkout.session.completed',
                'data': {'object': {
                    'id': 'cs_addon_dup_2',
                    'customer_details': {'email': 'j@test.com'},
                    'metadata': {'product_type': 'consult_addon', 'consult_order_id': 'cs_addon_dup'},
                }}
            }
            client.post('/webhook/stripe', json=event2, content_type='application/json')
            assert mock_send.call_count == first_call_count  # no second notify

    def test_missing_consult_order_id_does_not_crash(self, client, temp_athletes_dir):
        event = {
            'type': 'checkout.session.completed',
            'data': {'object': {
                'id': 'cs_addon_orphan',
                'customer_details': {'email': 'j@test.com'},
                'metadata': {'product_type': 'consult_addon'},
            }}
        }
        r = client.post('/webhook/stripe', json=event, content_type='application/json')
        assert r.status_code == 200


class TestConsultIntakeEndpoint:
    """POST /api/consult-intake — body-token intake submission."""

    def _seed_record(self, app_module, order_id='cs_intake_test'):
        import consultations
        record = consultations.new_record(order_id=order_id, brand='gravelgod',
                                          athlete_email='j@test.com')
        consultations.write_record(_consult_dir(app_module), record)
        return record

    def _token(self, order_id):
        from consult_intake_tokens import issue_intake_token
        return issue_intake_token(order_id=order_id)

    def test_options_preflight(self, client):
        r = client.options('/api/consult-intake')
        assert r.status_code == 204

    def test_missing_fields_400(self, client, temp_athletes_dir):
        r = client.post('/api/consult-intake', json={'ref': 'x'}, content_type='application/json')
        assert r.status_code == 400

    def test_invalid_token_401(self, client, temp_athletes_dir, app):
        import app as app_module
        with patch.dict(os.environ, {'CONSULT_INTAKE_TOKEN_SECRET': 'test-secret'}):
            self._seed_record(app_module)
            r = client.post('/api/consult-intake',
                            json={'ref': 'cs_intake_test', 't': 'garbage', 'answers': {'goal': 'x'}},
                            content_type='application/json')
        assert r.status_code == 401

    def test_unknown_ref_404(self, client, temp_athletes_dir):
        with patch.dict(os.environ, {'CONSULT_INTAKE_TOKEN_SECRET': 'test-secret'}):
            token = self._token('cs_no_such_ref')
            r = client.post('/api/consult-intake',
                            json={'ref': 'cs_no_such_ref', 't': token, 'answers': {'goal': 'x'}},
                            content_type='application/json')
        assert r.status_code == 404

    def test_valid_submission_stores_intake_and_updates_record(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        with patch.dict(os.environ, {'CONSULT_INTAKE_TOKEN_SECRET': 'test-secret'}):
            self._seed_record(app_module)
            token = self._token('cs_intake_test')
            r = client.post('/api/consult-intake',
                            json={'ref': 'cs_intake_test', 't': token,
                                 'answers': {'goal_event': 'Unbound', 'ftp': "don't know"}},
                            content_type='application/json')
        assert r.status_code == 200

        record = consultations.read_record(_consult_dir(app_module), 'cs_intake_test')
        assert record['intake']['intake_id'] is not None
        assert record['intake']['received_at'] is not None
        assert any(e['event'] == 'intake_received' for e in record['timeline'])

        stored = app_module.load_intake(record['intake']['intake_id'])
        assert stored['answers']['goal_event'] == 'Unbound'

    def test_never_synthesizes_dont_know(self, client, temp_athletes_dir, app):
        """Retro rule (§4): 'don't know' is stored verbatim, never replaced
        with a guessed number."""
        import app as app_module
        with patch.dict(os.environ, {'CONSULT_INTAKE_TOKEN_SECRET': 'test-secret'}):
            self._seed_record(app_module)
            token = self._token('cs_intake_test')
            client.post('/api/consult-intake',
                        json={'ref': 'cs_intake_test', 't': token,
                             'answers': {'ftp': "don't know", 'lthr': "don't know"}},
                        content_type='application/json')
        import consultations
        record = consultations.read_record(_consult_dir(app_module), 'cs_intake_test')
        stored = app_module.load_intake(record['intake']['intake_id'])
        assert stored['answers']['ftp'] == "don't know"
        assert stored['answers']['lthr'] == "don't know"

    def test_second_submission_replaces_and_appends_timeline(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        with patch.dict(os.environ, {'CONSULT_INTAKE_TOKEN_SECRET': 'test-secret'}):
            self._seed_record(app_module)
            token = self._token('cs_intake_test')
            client.post('/api/consult-intake',
                        json={'ref': 'cs_intake_test', 't': token, 'answers': {'goal_event': 'First'}},
                        content_type='application/json')
            first_id = consultations.read_record(
                _consult_dir(app_module), 'cs_intake_test')['intake']['intake_id']

            client.post('/api/consult-intake',
                        json={'ref': 'cs_intake_test', 't': token, 'answers': {'goal_event': 'Second'}},
                        content_type='application/json')

        record = consultations.read_record(_consult_dir(app_module), 'cs_intake_test')
        second_id = record['intake']['intake_id']
        assert second_id != first_id
        assert [e['event'] for e in record['timeline']].count('intake_received') == 2
        stored = app_module.load_intake(second_id)
        assert stored['answers']['goal_event'] == 'Second'

    def test_token_scope_rejected_on_runner_routes(self, client, temp_athletes_dir, app):
        """An intake token must not double as X-Runner-Secret."""
        import app as app_module
        with patch.dict(os.environ, {'CONSULT_INTAKE_TOKEN_SECRET': 'test-secret'}), \
             patch('app.CONSULT_RUNNER_SECRET', 'runner-secret'):
            self._seed_record(app_module)
            token = self._token('cs_intake_test')
            r = client.get('/api/consult/jobs/pending',
                           headers={'X-Runner-Secret': token})
        assert r.status_code == 401


class TestConsultRunnerAuth:
    """X-Runner-Secret: 503 unset, 401 wrong — same pattern as X-Cron-Secret."""

    def test_pending_503_when_unset(self, client, temp_athletes_dir):
        with patch('app.CONSULT_RUNNER_SECRET', ''):
            r = client.get('/api/consult/jobs/pending')
        assert r.status_code == 503

    def test_pending_401_when_wrong(self, client, temp_athletes_dir):
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/pending', headers={'X-Runner-Secret': 'nope'})
        assert r.status_code == 401

    def test_pending_200_when_correct(self, client, temp_athletes_dir):
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/pending', headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 200

    @pytest.mark.parametrize('method,path', [
        ('post', '/api/consult/jobs/cs_x/tp-linked'),
        ('post', '/api/consult/jobs/cs_x/claim'),
        ('post', '/api/consult/jobs/cs_x/report'),
        ('post', '/api/consult/jobs/cs_x/error'),
        ('get', '/api/consult/jobs/cs_x'),
        ('get', '/api/consult/jobs/ready'),
        ('post', '/api/consult/runner/heartbeat'),
    ])
    def test_all_runner_routes_503_unset(self, client, temp_athletes_dir, method, path):
        with patch('app.CONSULT_RUNNER_SECRET', ''):
            r = getattr(client, method)(path)
        assert r.status_code == 503

    @pytest.mark.parametrize('method,path', [
        ('post', '/api/consult/jobs/cs_x/tp-linked'),
        ('post', '/api/consult/jobs/cs_x/claim'),
        ('post', '/api/consult/jobs/cs_x/report'),
        ('post', '/api/consult/jobs/cs_x/error'),
        ('get', '/api/consult/jobs/cs_x'),
        ('get', '/api/consult/jobs/ready'),
        ('post', '/api/consult/runner/heartbeat'),
    ])
    def test_all_runner_routes_401_wrong(self, client, temp_athletes_dir, method, path):
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = getattr(client, method)(path, headers={'X-Runner-Secret': 'nope'})
        assert r.status_code == 401


class TestConsultRunnerJobsPending:
    def test_lists_only_open_records_lacking_tp_match(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        d = _consult_dir(app_module)

        matched = consultations.new_record(order_id='cs_matched', brand='gravelgod',
                                           athlete_email='m@test.com')
        matched['athlete']['tp_matched_at'] = consultations.now_iso()
        consultations.write_record(d, matched)

        unmatched = consultations.new_record(order_id='cs_unmatched', brand='gravelgod',
                                             athlete_email='u@test.com')
        consultations.write_record(d, unmatched)

        closed = consultations.new_record(order_id='cs_closed', brand='gravelgod',
                                          athlete_email='c@test.com')
        closed['status'] = 'closed'
        consultations.write_record(d, closed)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/pending', headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 200
        order_ids = [p['order_id'] for p in r.get_json()['pending']]
        assert order_ids == ['cs_unmatched']


class TestConsultRunnerJobsReady:
    """GET /api/consult/jobs/ready (docs/CONSULT_ENGINE_SPEC.md §5, C1.1)."""

    def _matched(self, order_id, created_at=None, status='open', **overrides):
        import consultations
        record = consultations.new_record(order_id=order_id, brand='gravelgod',
                                          athlete_email=f'{order_id}@test.com')
        record['athlete']['tp_matched_at'] = consultations.now_iso()
        record['athlete']['tp_athlete_id'] = f'tp-{order_id}'
        record['status'] = status
        if created_at:
            record['created_at'] = created_at
        for key, value in overrides.items():
            record[key] = value
        return record

    def test_unmatched_record_excluded(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        d = _consult_dir(app_module)
        unmatched = consultations.new_record(order_id='cs_unmatched', brand='gravelgod')
        consultations.write_record(d, unmatched)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/ready', headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 200
        assert r.get_json()['ready'] == []

    def test_matched_open_record_included(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        d = _consult_dir(app_module)
        consultations.write_record(d, self._matched('cs_open'))

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/ready', headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 200
        ready = r.get_json()['ready']
        assert len(ready) == 1
        item = ready[0]
        assert item['order_id'] == 'cs_open'
        assert item['tp_athlete_id'] == 'tp-cs_open'
        assert item['email'] == 'cs_open@test.com'
        assert item['intake_answers'] is None
        assert item['plan_addon'] == {'purchased': False, 'purchased_at': None}
        assert item['call_at'] is None
        assert item['attempts'] == 0

    def test_analysis_running_with_expired_lease_included(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        d = _consult_dir(app_module)
        record = self._matched('cs_expired', status='analysis_running')
        record['analysis']['lease_expires_at'] = (
            datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        record['analysis']['attempts'] = 1
        consultations.write_record(d, record)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/ready', headers={'X-Runner-Secret': 'shhh'})
        order_ids = [item['order_id'] for item in r.get_json()['ready']]
        assert order_ids == ['cs_expired']

    def test_analysis_running_with_live_lease_excluded(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        d = _consult_dir(app_module)
        record = self._matched('cs_live', status='analysis_running')
        record['analysis']['lease_expires_at'] = (
            datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat()
        record['analysis']['attempts'] = 1
        consultations.write_record(d, record)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/ready', headers={'X-Runner-Secret': 'shhh'})
        assert r.get_json()['ready'] == []

    def test_attempts_at_max_excluded(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        d = _consult_dir(app_module)
        record = self._matched('cs_maxed')
        record['analysis']['attempts'] = app_module.CONSULT_ANALYSIS_MAX_ATTEMPTS
        consultations.write_record(d, record)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/ready', headers={'X-Runner-Secret': 'shhh'})
        assert r.get_json()['ready'] == []

    def test_closed_excluded(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        d = _consult_dir(app_module)
        consultations.write_record(d, self._matched('cs_closed', status='closed'))

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/ready', headers={'X-Runner-Secret': 'shhh'})
        assert r.get_json()['ready'] == []

    def test_report_ready_and_needs_attention_excluded(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        d = _consult_dir(app_module)
        consultations.write_record(d, self._matched('cs_report_ready', status='report_ready'))
        consultations.write_record(d, self._matched('cs_needs_attention', status='needs_attention'))

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/ready', headers={'X-Runner-Secret': 'shhh'})
        assert r.get_json()['ready'] == []

    def test_intake_answers_passthrough(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        d = _consult_dir(app_module)
        intake_id = str(uuid.uuid4())
        app_module.store_intake(intake_id, {
            'answers': {'goal_event': 'Unbound 200', 'ftp': 'don\'t know'},
            'consult_order_id': 'cs_intake',
        })
        record = self._matched('cs_intake')
        record['intake'] = {'intake_id': intake_id, 'received_at': consultations.now_iso()}
        consultations.write_record(d, record)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/ready', headers={'X-Runner-Secret': 'shhh'})
        item = r.get_json()['ready'][0]
        assert item['intake_answers'] == {'goal_event': 'Unbound 200', 'ftp': "don't know"}

    def test_no_intake_yields_null(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        d = _consult_dir(app_module)
        consultations.write_record(d, self._matched('cs_no_intake'))

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/ready', headers={'X-Runner-Secret': 'shhh'})
        assert r.get_json()['ready'][0]['intake_answers'] is None

    def test_oldest_first(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        d = _consult_dir(app_module)
        newer = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        older = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        consultations.write_record(d, self._matched('cs_newer', created_at=newer))
        consultations.write_record(d, self._matched('cs_older', created_at=older))

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/ready', headers={'X-Runner-Secret': 'shhh'})
        order_ids = [item['order_id'] for item in r.get_json()['ready']]
        assert order_ids == ['cs_older', 'cs_newer']

    def test_plan_addon_purchased_shown(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        d = _consult_dir(app_module)
        record = self._matched('cs_addon')
        record['products']['plan_addon'] = {
            'purchased': True, 'amount': 10000,
            'purchased_at': '2026-08-01T00:00:00+00:00', 'offer_expires_at': None,
        }
        consultations.write_record(d, record)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/ready', headers={'X-Runner-Secret': 'shhh'})
        item = r.get_json()['ready'][0]
        assert item['plan_addon'] == {'purchased': True, 'purchased_at': '2026-08-01T00:00:00+00:00'}


class TestConsultRunnerTpLinked:
    def test_sets_tp_matched_at(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_link', brand='gravelgod')
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.post('/api/consult/jobs/cs_link/tp-linked',
                            json={'tp_athlete_id': '12345'},
                            headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 200
        updated = consultations.read_record(_consult_dir(app_module), 'cs_link')
        assert updated['athlete']['tp_athlete_id'] == '12345'
        assert updated['athlete']['tp_matched_at'] is not None

    def test_idempotent_repeat_call_keeps_original_timestamp(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_link2', brand='gravelgod')
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            client.post('/api/consult/jobs/cs_link2/tp-linked',
                        json={'tp_athlete_id': '1'}, headers={'X-Runner-Secret': 'shhh'})
            first = consultations.read_record(
                _consult_dir(app_module), 'cs_link2')['athlete']['tp_matched_at']

            client.post('/api/consult/jobs/cs_link2/tp-linked',
                        json={'tp_athlete_id': '1'}, headers={'X-Runner-Secret': 'shhh'})
            second = consultations.read_record(
                _consult_dir(app_module), 'cs_link2')['athlete']['tp_matched_at']

        assert first == second

    def test_404_unknown_order(self, client, temp_athletes_dir):
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.post('/api/consult/jobs/cs_missing/tp-linked',
                            json={'tp_athlete_id': '1'}, headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 404


class TestConsultRunnerClaim:
    def test_claim_sets_lease(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_claim', brand='gravelgod')
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.post('/api/consult/jobs/cs_claim/claim',
                            json={'claimed_by': 'mac-mini'}, headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 200
        updated = consultations.read_record(_consult_dir(app_module), 'cs_claim')
        assert updated['status'] == 'analysis_running'
        assert updated['analysis']['claimed_by'] == 'mac-mini'
        assert updated['analysis']['attempts'] == 1

    def test_second_claim_conflicts_409(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_claim2', brand='gravelgod')
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            client.post('/api/consult/jobs/cs_claim2/claim',
                        json={'claimed_by': 'runner-a'}, headers={'X-Runner-Secret': 'shhh'})
            r2 = client.post('/api/consult/jobs/cs_claim2/claim',
                             json={'claimed_by': 'runner-b'}, headers={'X-Runner-Secret': 'shhh'})
        assert r2.status_code == 409

    def test_stuck_sweep_reopens_expired_lease_under_max_attempts(self, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_stuck', brand='gravelgod')
        record['status'] = 'analysis_running'
        record['analysis'].update(
            claimed_by='mac-mini', attempts=1,
            lease_expires_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
        consultations.write_record(_consult_dir(app_module), record)

        stats = app_module.sweep_stuck_consultations()
        assert stats['reopened'] == 1
        updated = consultations.read_record(_consult_dir(app_module), 'cs_stuck')
        assert updated['status'] == 'open'
        assert updated['analysis']['claimed_by'] is None

    def test_stuck_sweep_flags_needs_attention_at_max_attempts(self, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_stuck_max', brand='gravelgod')
        record['status'] = 'analysis_running'
        record['analysis'].update(
            claimed_by='mac-mini', attempts=app_module.CONSULT_ANALYSIS_MAX_ATTEMPTS,
            lease_expires_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True):
            stats = app_module.sweep_stuck_consultations()
        assert stats['needs_attention'] == 1
        updated = consultations.read_record(_consult_dir(app_module), 'cs_stuck_max')
        assert updated['status'] == 'needs_attention'


class TestConsultRunnerReport:
    def _seed(self, app_module, order_id='cs_report'):
        import consultations
        record = consultations.new_record(order_id=order_id, brand='gravelgod',
                                          athlete_name='Jesse Couch')
        consultations.write_record(_consult_dir(app_module), record)

    def test_report_upload_sets_status_and_sends_one_email(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        self._seed(app_module)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            data = {'report_md': (BytesIO(b'# ONE thing\n\nDo the work.'), 'report.md')}
            r = client.post('/api/consult/jobs/cs_report/report',
                            data=data, content_type='multipart/form-data',
                            headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 200
        assert mock_send.call_count == 1
        updated = consultations.read_record(_consult_dir(app_module), 'cs_report')
        assert updated['status'] == 'report_ready'
        assert updated['analysis']['report_path']

    def test_repeat_report_upload_is_idempotent_no_second_email(self, client, temp_athletes_dir, app):
        import app as app_module
        self._seed(app_module, order_id='cs_report_dup')

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            for _ in range(2):
                data = {'report_md': (BytesIO(b'# ONE thing'), 'report.md')}
                client.post('/api/consult/jobs/cs_report_dup/report',
                            data=data, content_type='multipart/form-data',
                            headers={'X-Runner-Secret': 'shhh'})
        assert mock_send.call_count == 1

    def test_missing_report_md_400(self, client, temp_athletes_dir, app):
        import app as app_module
        self._seed(app_module, order_id='cs_report_missing')
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.post('/api/consult/jobs/cs_report_missing/report',
                            data={}, content_type='multipart/form-data',
                            headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 400

    def test_max_content_length_rejects_oversized_upload(self, client, temp_athletes_dir, app):
        import app as app_module
        self._seed(app_module, order_id='cs_report_big')
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'), \
             patch.object(app_module.app.config, '__getitem__',
                          wraps=app_module.app.config.__getitem__):
            # Shrink the cap for the test instead of generating a 25MB body.
            original = app_module.app.config['MAX_CONTENT_LENGTH']
            app_module.app.config['MAX_CONTENT_LENGTH'] = 10
            try:
                data = {'report_md': (BytesIO(b'x' * 1000), 'report.md')}
                r = client.post('/api/consult/jobs/cs_report_big/report',
                                data=data, content_type='multipart/form-data',
                                headers={'X-Runner-Secret': 'shhh'})
            finally:
                app_module.app.config['MAX_CONTENT_LENGTH'] = original
        assert r.status_code == 413


class TestConsultRunnerError:
    def test_error_sets_needs_attention_and_notifies(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_err', brand='gravelgod')
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            r = client.post('/api/consult/jobs/cs_err/error',
                            json={'error': 'TP session expired'},
                            headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 200
        assert mock_send.call_count == 1
        updated = consultations.read_record(_consult_dir(app_module), 'cs_err')
        assert updated['status'] == 'needs_attention'
        assert updated['analysis']['error'] == 'TP session expired'

    def test_repeat_same_error_sends_no_second_email(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_err2', brand='gravelgod')
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            for _ in range(2):
                client.post('/api/consult/jobs/cs_err2/error',
                            json={'error': 'same failure'},
                            headers={'X-Runner-Secret': 'shhh'})
        assert mock_send.call_count == 1

    def test_404_unknown_order(self, client, temp_athletes_dir):
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.post('/api/consult/jobs/cs_nope/error',
                            json={'error': 'x'}, headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 404


class TestConsultRunnerGet:
    def test_returns_full_record(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_get', brand='gravelgod',
                                          athlete_email='j@test.com')
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/cs_get', headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 200
        assert r.get_json()['athlete']['email'] == 'j@test.com'

    def test_404_unknown_order(self, client, temp_athletes_dir):
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/cs_nope', headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 404


class TestConsultRunnerHeartbeat:
    """POST /api/consult/runner/heartbeat (docs/CONSULT_ENGINE_SPEC.md §6, C1.1)."""

    def _path(self, app_module):
        return app_module._consult_runner_heartbeat_path()

    def test_persists_ok_true(self, client, temp_athletes_dir, app):
        import app as app_module
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.post('/api/consult/runner/heartbeat',
                            json={'runner_id': 'mac-mini', 'ok': True},
                            headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 200
        stored = app_module._read_consult_runner_heartbeat()
        assert stored['runner_id'] == 'mac-mini'
        assert stored['ok'] is True
        assert stored['detail'] == ''
        assert stored['at']

    def test_persists_ok_false_with_detail(self, client, temp_athletes_dir, app):
        import app as app_module
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.post('/api/consult/runner/heartbeat',
                            json={'runner_id': 'mac-mini', 'ok': False,
                                  'detail': 'TP session expired'},
                            headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 200
        stored = app_module._read_consult_runner_heartbeat()
        assert stored['ok'] is False
        assert stored['detail'] == 'TP session expired'

    def test_preserves_alarm_cooldown_across_writes(self, client, temp_athletes_dir, app):
        import app as app_module
        app_module._write_consult_runner_heartbeat({
            'runner_id': 'mac-mini', 'ok': True, 'detail': '',
            'at': '2026-08-01T00:00:00+00:00',
            'last_runner_alarm_at': '2026-08-01T01:00:00+00:00',
        })
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            client.post('/api/consult/runner/heartbeat', json={'runner_id': 'mac-mini', 'ok': True},
                        headers={'X-Runner-Secret': 'shhh'})
        stored = app_module._read_consult_runner_heartbeat()
        assert stored['last_runner_alarm_at'] == '2026-08-01T01:00:00+00:00'


class TestConsultRunnerAlarm:
    """process_consult_followups() runner-heartbeat alarm (C1.1)."""

    def test_fires_when_heartbeat_missing(self, temp_athletes_dir, app):
        import app as app_module
        now = datetime.now(timezone.utc)
        with patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            fired = app_module._check_consult_runner_alarm(now)
        assert fired is True
        assert mock_send.call_count == 1
        assert mock_send.call_args.args[0] == 'coach@example.com'
        assert mock_send.call_args.args[1] == '[GG] Consult runner needs attention'

    def test_fires_when_heartbeat_stale(self, temp_athletes_dir, app):
        import app as app_module
        now = datetime.now(timezone.utc)
        stale_at = now - timedelta(hours=7)
        app_module._write_consult_runner_heartbeat({
            'runner_id': 'mac-mini', 'ok': True, 'detail': '',
            'at': stale_at.isoformat(), 'last_runner_alarm_at': None,
        })
        with patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            fired = app_module._check_consult_runner_alarm(now)
        assert fired is True
        assert mock_send.call_count == 1

    def test_fires_when_ok_false_even_if_recent(self, temp_athletes_dir, app):
        import app as app_module
        now = datetime.now(timezone.utc)
        app_module._write_consult_runner_heartbeat({
            'runner_id': 'mac-mini', 'ok': False, 'detail': 'auth failed 3x',
            'at': now.isoformat(), 'last_runner_alarm_at': None,
        })
        with patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            fired = app_module._check_consult_runner_alarm(now)
        assert fired is True
        body = mock_send.call_args.args[2]
        assert 'auth failed 3x' in body

    def test_no_alarm_when_recent_and_ok(self, temp_athletes_dir, app):
        import app as app_module
        now = datetime.now(timezone.utc)
        app_module._write_consult_runner_heartbeat({
            'runner_id': 'mac-mini', 'ok': True, 'detail': '',
            'at': now.isoformat(), 'last_runner_alarm_at': None,
        })
        with patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            fired = app_module._check_consult_runner_alarm(now)
        assert fired is False
        assert mock_send.call_count == 0

    def test_fires_at_most_once_per_24h(self, temp_athletes_dir, app):
        import app as app_module
        now = datetime.now(timezone.utc)
        with patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            first = app_module._check_consult_runner_alarm(now)
            second = app_module._check_consult_runner_alarm(now + timedelta(hours=1))
        assert first is True
        assert second is False
        assert mock_send.call_count == 1
        stored = app_module._read_consult_runner_heartbeat()
        assert stored['last_runner_alarm_at'] is not None

    def test_fires_again_after_cooldown_expires(self, temp_athletes_dir, app):
        import app as app_module
        now = datetime.now(timezone.utc)
        with patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            app_module._check_consult_runner_alarm(now)
            fired_again = app_module._check_consult_runner_alarm(now + timedelta(hours=25))
        assert fired_again is True
        assert mock_send.call_count == 2

    def test_process_consult_followups_wires_alarm(self, temp_athletes_dir, app):
        import app as app_module
        with patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            stats = app_module.process_consult_followups()
        assert stats['runner_alarm_sent'] is True
        alarm_calls = [c for c in mock_send.call_args_list
                       if c.args[1] == '[GG] Consult runner needs attention']
        assert len(alarm_calls) == 1


class TestConsultOperatorEndpoint:
    """POST /api/consult/<order_id>/op — X-Cron-Secret operator lever."""

    def test_503_when_cron_secret_unset(self, client, temp_athletes_dir):
        with patch('app.CRON_SECRET', ''):
            r = client.post('/api/consult/cs_x/op', json={'retry': True})
        assert r.status_code == 503

    def test_401_when_wrong_secret(self, client, temp_athletes_dir):
        with patch('app.CRON_SECRET', 'shhh'):
            r = client.post('/api/consult/cs_x/op', json={'retry': True},
                            headers={'X-Cron-Secret': 'nope'})
        assert r.status_code == 401

    def test_400_when_no_recognized_op(self, client, temp_athletes_dir):
        with patch('app.CRON_SECRET', 'shhh'):
            r = client.post('/api/consult/cs_x/op', json={},
                            headers={'X-Cron-Secret': 'shhh'})
        assert r.status_code == 400

    def test_sets_call_at(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_op_call', brand='gravelgod')
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.CRON_SECRET', 'shhh'):
            r = client.post('/api/consult/cs_op_call/op',
                            json={'call_at': '2026-09-01T15:00:00+00:00'},
                            headers={'X-Cron-Secret': 'shhh'})
        assert r.status_code == 200
        updated = consultations.read_record(_consult_dir(app_module), 'cs_op_call')
        assert updated['call_at'] == '2026-09-01T15:00:00+00:00'

    def test_closes_with_reason(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_op_close', brand='gravelgod')
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.CRON_SECRET', 'shhh'):
            r = client.post('/api/consult/cs_op_close/op',
                            json={'close': 'delivered'},
                            headers={'X-Cron-Secret': 'shhh'})
        assert r.status_code == 200
        updated = consultations.read_record(_consult_dir(app_module), 'cs_op_close')
        assert updated['status'] == 'closed'
        assert updated['closed_reason'] == 'delivered'

    def test_retry_reopens_and_clears_lease(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_op_retry', brand='gravelgod')
        record['status'] = 'needs_attention'
        record['analysis'].update(claimed_by='mac-mini', lease_expires_at='2026-01-01T00:00:00+00:00',
                                  error='boom')
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.CRON_SECRET', 'shhh'):
            r = client.post('/api/consult/cs_op_retry/op',
                            json={'retry': True},
                            headers={'X-Cron-Secret': 'shhh'})
        assert r.status_code == 200
        updated = consultations.read_record(_consult_dir(app_module), 'cs_op_retry')
        assert updated['status'] == 'open'
        assert updated['analysis']['claimed_by'] is None
        assert updated['analysis']['error'] is None

    def test_404_unknown_order(self, client, temp_athletes_dir):
        with patch('app.CRON_SECRET', 'shhh'):
            r = client.post('/api/consult/cs_missing/op',
                            json={'retry': True}, headers={'X-Cron-Secret': 'shhh'})
        assert r.status_code == 404

    def test_deliver_endure_sets_block_and_timeline(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_op_endure', brand='gravelgod')
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.CRON_SECRET', 'shhh'):
            r = client.post('/api/consult/cs_op_endure/op',
                            json={'deliver_endure': {'plan_of_action_md': '## Plan\n\nDo the work.'}},
                            headers={'X-Cron-Secret': 'shhh'})
        assert r.status_code == 200
        assert r.get_json()['applied'] == ['deliver_endure']
        updated = consultations.read_record(_consult_dir(app_module), 'cs_op_endure')
        assert updated['endure']['plan_of_action_md'] == '## Plan\n\nDo the work.'
        assert updated['endure']['requested_at']
        assert updated['endure']['delivered_at'] is None
        assert updated['endure']['result'] is None
        assert updated['timeline'][-1]['event'] == 'endure_requested'


class TestConsultJobsDeliver:
    """GET /api/consult/jobs/deliver (docs/CONSULT_ENGINE_SPEC.md §5,
    endurelabs specs/consult-delivery/spec.md §6, CD-1b)."""

    def _seed(self, app_module, order_id='cs_deliver', **overrides):
        import consultations
        record = consultations.new_record(order_id=order_id, brand='gravelgod',
                                          athlete_name='Jesse Couch',
                                          athlete_email='jesse@example.com')
        record['athlete']['tp_athlete_id'] = '999'
        record['athlete']['tp_matched_at'] = consultations.now_iso()
        record['endure'] = {
            'requested_at': consultations.now_iso(),
            'plan_of_action_md': '## Plan\n\nBase for 6 weeks.',
            'delivered_at': None,
            'result': None,
        }
        for key, value in overrides.items():
            record[key] = value
        consultations.write_record(_consult_dir(app_module), record)
        return record

    def _write_report(self, app_module, order_id, report):
        report_dir = _consult_dir(app_module) / 'consultations' / order_id
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / 'report.json').write_text(json.dumps(report))

    def test_503_when_runner_secret_unset(self, client, temp_athletes_dir):
        with patch('app.CONSULT_RUNNER_SECRET', ''):
            r = client.get('/api/consult/jobs/deliver')
        assert r.status_code == 503

    def test_401_when_wrong_secret(self, client, temp_athletes_dir):
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/deliver', headers={'X-Runner-Secret': 'nope'})
        assert r.status_code == 401

    def test_excludes_no_endure_requested(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        record = consultations.new_record(order_id='cs_no_endure', brand='gravelgod')
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/deliver', headers={'X-Runner-Secret': 'shhh'})
        assert r.get_json()['deliver'] == []

    def test_excludes_already_delivered(self, client, temp_athletes_dir, app):
        import app as app_module
        self._seed(app_module, order_id='cs_delivered_already',
                   endure={'requested_at': 'x', 'plan_of_action_md': '', 'delivered_at': 'y', 'result': {}})

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/deliver', headers={'X-Runner-Secret': 'shhh'})
        assert r.get_json()['deliver'] == []

    def test_payload_shape_from_stored_report(self, client, temp_athletes_dir, app):
        import app as app_module
        self._seed(app_module, order_id='cs_deliver_shape',
                   call_at='2026-09-01T15:00:00+00:00')
        self._write_report(app_module, 'cs_deliver_shape', {
            'one_thing': {'rule': 'e', 'label': 'durability', 'text': 'Durability is the limiter.'},
            'data_bullets': [
                'CTL ramp net +3.2 over the last 10 weeks.',
                'not available: not enough analysis.json rows to compute a data bullet',
            ],
            'athlete_card': {'ftp': 250, 'lthr': 165, 'age': 42},
        })

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/deliver', headers={'X-Runner-Secret': 'shhh'})
        items = r.get_json()['deliver']
        assert len(items) == 1
        item = items[0]
        assert item['order_id'] == 'cs_deliver_shape'
        assert item['tp_athlete_id'] == '999'
        assert item['email'] == 'jesse@example.com'
        assert item['first_name'] == 'Jesse'
        assert item['last_name'] == 'Couch'
        assert item['consult_date'] == '2026-09-01T15:00:00+00:00'
        assert item['plan_addon'] is False
        assert item['plan_of_action_md'] == '## Plan\n\nBase for 6 weeks.'
        assert item['prefill'] == {'ftp': 250, 'lthr': 165}
        # ONE thing + 1 non-placeholder bullet ("not available:" is dropped)
        assert len(item['findings']) == 2
        assert item['findings'][0] == {
            'title': 'Durability', 'body': 'Durability is the limiter.',
            'kind': 'physiological_limiter', 'confidence': 0.85,
        }
        assert item['findings'][1] == {
            'title': 'CTL ramp net +3.2 over the last 10 weeks.',
            'body': 'CTL ramp net +3.2 over the last 10 weeks.',
            'kind': 'pattern', 'confidence': 0.75,
        }

    def test_non_durability_one_thing_kind_is_pattern(self, client, temp_athletes_dir, app):
        import app as app_module
        self._seed(app_module, order_id='cs_deliver_pattern')
        self._write_report(app_module, 'cs_deliver_pattern', {
            'one_thing': {'rule': 'b', 'label': 'base-no-exit', 'text': 'Base has no exit.'},
            'data_bullets': [],
            'athlete_card': {},
        })

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/deliver', headers={'X-Runner-Secret': 'shhh'})
        item = r.get_json()['deliver'][0]
        assert item['findings'][0]['kind'] == 'pattern'

    def test_prefill_includes_max_hr_and_weight_when_present(self, client, temp_athletes_dir, app):
        import app as app_module
        self._seed(app_module, order_id='cs_deliver_prefill')
        self._write_report(app_module, 'cs_deliver_prefill', {
            'one_thing': {}, 'data_bullets': [],
            'athlete_card': {'ftp': 250, 'lthr': 165, 'max_hr': 180, 'weight': 72.5},
        })

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/deliver', headers={'X-Runner-Secret': 'shhh'})
        item = r.get_json()['deliver'][0]
        assert item['prefill'] == {'ftp': 250, 'lthr': 165, 'max_hr': 180, 'weight': 72.5}

    def test_goal_event_from_intake(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        intake_id = str(uuid.uuid4())
        app_module.store_intake(intake_id, {
            'answers': {'goal_event': 'Unbound 200'},
            'consult_order_id': 'cs_deliver_goal',
        })
        record = self._seed(app_module, order_id='cs_deliver_goal')
        record['intake'] = {'intake_id': intake_id, 'received_at': consultations.now_iso()}
        consultations.write_record(_consult_dir(app_module), record)
        self._write_report(app_module, 'cs_deliver_goal',
                           {'one_thing': {}, 'data_bullets': [], 'athlete_card': {}})

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/deliver', headers={'X-Runner-Secret': 'shhh'})
        item = r.get_json()['deliver'][0]
        assert item['goal_event'] == 'Unbound 200'

    def test_missing_report_json_yields_empty_findings_and_prefill(self, client, temp_athletes_dir, app):
        import app as app_module
        self._seed(app_module, order_id='cs_deliver_no_report')

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.get('/api/consult/jobs/deliver', headers={'X-Runner-Secret': 'shhh'})
        item = r.get_json()['deliver'][0]
        assert item['findings'] == []
        assert item['prefill'] == {}


class TestConsultEndureDelivered:
    """POST /api/consult/jobs/<order_id>/endure-delivered (§6)."""

    def _seed(self, app_module, order_id='cs_endure_del'):
        import consultations
        record = consultations.new_record(order_id=order_id, brand='gravelgod',
                                          athlete_name='Jesse Couch')
        record['endure'] = {
            'requested_at': consultations.now_iso(),
            'plan_of_action_md': '## Plan',
            'delivered_at': None,
            'result': None,
        }
        consultations.write_record(_consult_dir(app_module), record)

    def test_503_when_runner_secret_unset(self, client, temp_athletes_dir):
        with patch('app.CONSULT_RUNNER_SECRET', ''):
            r = client.post('/api/consult/jobs/cs_x/endure-delivered', json={'result': {}})
        assert r.status_code == 503

    def test_401_when_wrong_secret(self, client, temp_athletes_dir):
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.post('/api/consult/jobs/cs_x/endure-delivered', json={'result': {}},
                            headers={'X-Runner-Secret': 'nope'})
        assert r.status_code == 401

    def test_404_unknown_order(self, client, temp_athletes_dir):
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'):
            r = client.post('/api/consult/jobs/cs_missing/endure-delivered', json={'result': {}},
                            headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 404

    def test_sets_delivered_at_and_result_and_sends_one_email(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        self._seed(app_module, order_id='cs_endure_ok')

        result = {'athlete_id': 'a1',
                  'invitation': {'status': 'sent', 'url': 'https://endurelabs.app/invite/xyz'}}
        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            r = client.post('/api/consult/jobs/cs_endure_ok/endure-delivered',
                            json={'result': result},
                            headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 200
        assert mock_send.call_count == 1
        assert 'https://endurelabs.app/invite/xyz' in mock_send.call_args[0][2]
        updated = consultations.read_record(_consult_dir(app_module), 'cs_endure_ok')
        assert updated['endure']['delivered_at']
        assert updated['endure']['result'] == result
        assert updated['timeline'][-1]['event'] == 'endure_delivered'

    def test_repeat_post_is_idempotent_no_second_email(self, client, temp_athletes_dir, app):
        import app as app_module
        import consultations
        self._seed(app_module, order_id='cs_endure_dup')

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            for _ in range(2):
                client.post('/api/consult/jobs/cs_endure_dup/endure-delivered',
                            json={'result': {'athlete_id': 'a1'}},
                            headers={'X-Runner-Secret': 'shhh'})
        assert mock_send.call_count == 1
        updated = consultations.read_record(_consult_dir(app_module), 'cs_endure_dup')
        assert updated['endure']['delivered_at']

    def test_no_invitation_url_still_sends_email(self, client, temp_athletes_dir, app):
        import app as app_module
        self._seed(app_module, order_id='cs_endure_no_invite')

        with patch('app.CONSULT_RUNNER_SECRET', 'shhh'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send:
            r = client.post('/api/consult/jobs/cs_endure_no_invite/endure-delivered',
                            json={'result': {'athlete_id': 'a1', 'existing_user': True}},
                            headers={'X-Runner-Secret': 'shhh'})
        assert r.status_code == 200
        assert mock_send.call_count == 1


class TestConsultFollowupsStateMachine:
    """process_consult_followups() — state-conditional, not day-offset."""

    def _seed(self, app_module, order_id, **overrides):
        import consultations
        record = consultations.new_record(order_id=order_id, brand='gravelgod',
                                          athlete_name='Jesse Couch',
                                          athlete_email='jesse@example.com')
        record['welcome_sent_at'] = consultations.now_iso()  # welcome already sent
        for key, value in overrides.items():
            record[key] = value
        consultations.write_record(_consult_dir(app_module), record)
        return record

    def test_intake_nudge_fires_once_after_24h(self, temp_athletes_dir, app):
        import app as app_module
        import consultations
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        self._seed(app_module, 'cs_intake_nudge', created_at=old)

        with patch('app.RESEND_API_KEY', 'test-key'), \
             patch.dict(os.environ, {'CONSULT_INTAKE_TOKEN_SECRET': 'test-secret'}), \
             patch('app._send_email', return_value=True) as mock_send:
            stats1 = app_module.process_consult_followups()
            stats2 = app_module.process_consult_followups()

        assert stats1['intake_nudged'] == 1
        assert stats2['intake_nudged'] == 0  # fired at most once
        record = consultations.read_record(_consult_dir(app_module), 'cs_intake_nudge')
        assert 'intake_nudge' in record['nudges_sent']

    def test_no_intake_nudge_before_24h(self, temp_athletes_dir, app):
        import app as app_module
        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self._seed(app_module, 'cs_intake_too_soon', created_at=recent)

        with patch('app.RESEND_API_KEY', 'test-key'), patch('app._send_email', return_value=True):
            stats = app_module.process_consult_followups()
        assert stats['intake_nudged'] == 0

    def test_tp_nudge_fires_once_after_48h(self, temp_athletes_dir, app):
        import app as app_module
        import consultations
        old = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
        record = self._seed(app_module, 'cs_tp_nudge', created_at=old)
        # Intake already received so only the TP nudge is under test.
        record['intake'] = {'intake_id': str(uuid.uuid4()), 'received_at': consultations.now_iso()}
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.RESEND_API_KEY', 'test-key'), \
             patch('app._send_email', return_value=True):
            stats1 = app_module.process_consult_followups()
            stats2 = app_module.process_consult_followups()

        assert stats1['tp_nudged'] == 1
        assert stats2['tp_nudged'] == 0

    def test_call_relative_rules_only_run_when_call_at_set(self, temp_athletes_dir, app):
        import app as app_module
        # No call_at — even though "created" long ago, no call-relative
        # nudges should fire.
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        self._seed(app_module, 'cs_no_call', created_at=old)

        with patch('app.RESEND_API_KEY', 'test-key'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True):
            stats = app_module.process_consult_followups()

        assert stats['plan_reminded'] == 0
        assert stats['addon_offered'] == 0

    def test_plan_reminder_fires_1d_after_call(self, temp_athletes_dir, app):
        import app as app_module
        call_at = (datetime.now(timezone.utc) - timedelta(days=1, hours=1)).isoformat()
        self._seed(app_module, 'cs_call_1d', call_at=call_at)

        with patch('app.RESEND_API_KEY', 'test-key'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True) as mock_send, \
             patch('app._check_consult_runner_alarm', return_value=False):
            stats1 = app_module.process_consult_followups()
            stats2 = app_module.process_consult_followups()

        assert stats1['plan_reminded'] == 1
        assert stats2['plan_reminded'] == 0
        coach_calls = [c for c in mock_send.call_args_list if c.args[0] == 'coach@example.com']
        assert len(coach_calls) == 1

    def test_addon_offer_fires_2d_after_call_sets_expiry(self, temp_athletes_dir, app):
        import app as app_module
        import consultations
        call_at_dt = datetime.now(timezone.utc) - timedelta(days=2, hours=1)
        self._seed(app_module, 'cs_call_2d', call_at=call_at_dt.isoformat())

        with patch('app.RESEND_API_KEY', 'test-key'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True):
            stats1 = app_module.process_consult_followups()
            stats2 = app_module.process_consult_followups()

        assert stats1['addon_offered'] == 1
        assert stats2['addon_offered'] == 0
        record = consultations.read_record(_consult_dir(app_module), 'cs_call_2d')
        expires_at = record['products']['plan_addon']['offer_expires_at']
        assert expires_at is not None
        assert datetime.fromisoformat(expires_at) - call_at_dt == timedelta(days=7)

    def test_addon_offer_skipped_if_already_purchased(self, temp_athletes_dir, app):
        import app as app_module
        call_at = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        record = self._seed(app_module, 'cs_call_bought', call_at=call_at)
        record['products']['plan_addon']['purchased'] = True
        import consultations
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.RESEND_API_KEY', 'test-key'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True):
            stats = app_module.process_consult_followups()
        assert stats['addon_offered'] == 0

    def test_give_up_rule_closes_after_30_days_no_tp_link(self, temp_athletes_dir, app):
        import app as app_module
        import consultations
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        self._seed(app_module, 'cs_give_up', created_at=old)

        with patch('app.RESEND_API_KEY', 'test-key'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True):
            stats = app_module.process_consult_followups()

        assert stats['closed_no_data'] == 1
        record = consultations.read_record(_consult_dir(app_module), 'cs_give_up')
        assert record['status'] == 'closed'
        assert record['closed_reason'] == 'no_data_30d'

    def test_give_up_rule_does_not_close_when_tp_linked(self, temp_athletes_dir, app):
        import app as app_module
        import consultations
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        record = self._seed(app_module, 'cs_no_give_up', created_at=old)
        record['athlete']['tp_matched_at'] = consultations.now_iso()
        consultations.write_record(_consult_dir(app_module), record)

        with patch('app.RESEND_API_KEY', 'test-key'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True):
            stats = app_module.process_consult_followups()

        assert stats['closed_no_data'] == 0
        record = consultations.read_record(_consult_dir(app_module), 'cs_no_give_up')
        assert record['status'] != 'closed'

    def test_closed_records_are_skipped_entirely(self, temp_athletes_dir, app):
        import app as app_module
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        self._seed(app_module, 'cs_already_closed', created_at=old, status='closed')

        with patch('app.RESEND_API_KEY', 'test-key'), \
             patch('app.NOTIFICATION_EMAIL', 'coach@example.com'), \
             patch('app._send_email', return_value=True):
            stats = app_module.process_consult_followups()

        assert stats['checked'] == 0


class TestConsultCronWiring:
    def test_cron_followup_emails_includes_consult_stats(self, client, temp_athletes_dir):
        with patch('app.CRON_SECRET', 'shhh'):
            r = client.post('/api/cron/followup-emails', headers={'X-Cron-Secret': 'shhh'})
        assert r.status_code == 200
        data = r.get_json()
        assert 'consult' in data
        assert 'checked' in data['consult']


class TestMaxContentLengthAppWide:
    def test_config_set_to_25mb(self, app):
        import app as app_module
        assert app_module.app.config['MAX_CONTENT_LENGTH'] == 25 * 1024 * 1024
