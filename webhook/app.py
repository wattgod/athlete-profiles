"""
Gravel God Webhook Receiver

Receives Stripe webhooks after successful payment,
creates Stripe Checkout Sessions from questionnaire data,
triggers the training plan pipeline, and delivers to athlete.

Deploy to: Railway
"""

import os
import sys
import re
import json
import copy
import contextlib
import functools
import hmac
import fcntl
import hashlib
import logging
import subprocess
import threading
import uuid
import math
import shutil
import zipfile
import base64
import binascii
from html import escape as html_escape
import requests as http_requests
from pathlib import Path
from datetime import datetime, timedelta, date, timezone
from flask import Flask, request, jsonify, send_file, make_response, redirect
from flask_limiter import Limiter
import stripe
from provider_revenue import (
    ProviderRevenueError,
    build_stripe_revenue_receipt,
    parse_reconciliation_window,
)
import yaml

from fulfillment_state import (APPLIED, APPROVED, BLOCKED_REVIEW, CANCELLED,
                               CONFIRMED,
                               RELEASE_STATUSES, FulfillmentStateError,
                               approval_matches_release, bind_legacy_order,
                               confirm_after_send,
                               finalize_transitional_release,
                               load as load_fulfillment_state,
                               migrate_v1_to_quarantine,
                               open_verified_release_artifact,
                               external_notification_projection,
                               redact_sensitive_review_items,
                               record_seal_mismatch,
                               transition as transition_fulfillment,
                               verify_release_artifact,
                               verify_release_manifest, write_generation)
from download_tokens import (ARTIFACT_AUDIENCE, DownloadTokenError,
                             issue_download_token, keys_configured as download_keys_configured,
                             revoke_download_token, verify_download_token)
from review_auth import (ReviewAuthError, create_review_session,
                         issue_review_token, keys_configured as review_keys_configured,
                         load_review_session, verify_review_token)
from review_surface import render_bootstrap, render_review_page
import consultations
from consult_intake_tokens import (ConsultIntakeTokenError,
                                   issue_intake_token as issue_consult_intake_token,
                                   verify_intake_token as verify_consult_intake_token)
from email_templates import (TP_INVITE_LINK as CONSULT_TP_INVITE_LINK,
                             build_consult_welcome_email,
                             build_consult_runner_alarm_email,
                             build_consult_endure_delivered_email,
                             CONSULT_INTAKE_NUDGE_SUBJECT, CONSULT_INTAKE_NUDGE_TEMPLATE,
                             CONSULT_TP_NUDGE_SUBJECT, CONSULT_TP_NUDGE_TEMPLATE,
                             CONSULT_ADDON_OFFER_SUBJECT, CONSULT_ADDON_OFFER_TEMPLATE)

import endure_delivery
from preview_contract import PreviewContractError
from preview_service import PreviewProviderUnavailable, build_public_preview
from training_plan_addons import (
    AddonSelectionError,
    resolve_plan_addons,
    stripe_line_items_for_addons,
)
from signwell_client import SignWellClient, SignWellError, verify_event_hash

# The shared registry lives under athletes/config because that directory is
# copied into the Railway image. Import its loader from the adjacent scripts
# directory in both repo and /app layouts.
_ATHLETE_SCRIPTS = Path(__file__).resolve().parent.parent / 'athletes' / 'scripts'
if str(_ATHLETE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ATHLETE_SCRIPTS))
from brand_config import default_brand, load_brands, normalize_brand
from apply_contract import schema_path as apply_contract_schema_path

app = Flask(__name__)

# App-wide request body cap. Nothing today needs more than the consult
# runner's report bundle (report.md + report.json + receipts.zip), so that
# 25MB figure (docs/CONSULT_ENGINE_SPEC.md §5) also serves as the global
# ceiling — Flask returns 413 automatically for any request over this.
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024


def _get_real_ip():
    """Get client IP, handling Railway/proxy X-Forwarded-For."""
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or '127.0.0.1'


limiter = Limiter(
    key_func=_get_real_ip,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


class _BearerQueryRedactionFilter(logging.Filter):
    """Redact stale bearer URLs from application-managed logs."""

    # Query-token authentication is not supported, but old links, scanners,
    # and rejected requests can still reach logs. Match a single URL-decoding
    # pass for every character in ``token`` as defense in depth.
    _TOKEN = re.compile(
        r'([?&](?:t|%74)(?:o|%6f)(?:k|%6b)(?:e|%65)(?:n|%6e)=)'
        r'[^&\s"]+',
        re.IGNORECASE,
    )

    @classmethod
    def _redact(cls, value):
        if isinstance(value, str):
            return cls._TOKEN.sub(r'\1[REDACTED]', value)
        if isinstance(value, tuple):
            return tuple(cls._redact(item) for item in value)
        if isinstance(value, dict):
            return {key: cls._redact(item) for key, item in value.items()}
        return value

    def filter(self, record):
        record.msg = self._redact(record.msg)
        record.args = self._redact(record.args)
        return True


_bearer_query_redaction = _BearerQueryRedactionFilter()
logger = logging.getLogger('gravel-god-webhook')
logger.addFilter(_bearer_query_redaction)
logging.getLogger('werkzeug').addFilter(_bearer_query_redaction)
logging.getLogger('gunicorn.access').addFilter(_bearer_query_redaction)

# =============================================================================
# CONFIGURATION - Fail fast if critical config missing in production
# =============================================================================

IS_PRODUCTION = os.environ.get('FLASK_ENV') == 'production'

WOOCOMMERCE_SECRET = os.environ.get('WOOCOMMERCE_SECRET', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
ATHLETES_DIR = os.environ.get('ATHLETES_DIR', '/app/athletes')
SCRIPTS_DIR = os.environ.get('SCRIPTS_DIR', '/app/athletes/scripts')
DATA_DIR = os.environ.get('DATA_DIR', ATHLETES_DIR)  # Persistent volume for intake/logs
DELIVERIES_DIR = os.path.join(DATA_DIR, 'deliveries')  # Persistent: zipped plans for download

# Multi-brand support — this webhook serves all brand sites. Brand is derived
# from the request Origin at checkout creation, stored in Stripe metadata,
# and read back in the webhook handlers (success URLs, GA4 routing, emails).
BRANDS = load_brands(resolve_env=True)
DEFAULT_BRAND = default_brand()

# CORS derives from the same source of truth as checkout routing.
ALLOWED_ORIGINS = sorted({
    origin
    for cfg in BRANDS.values()
    for origin in (cfg['site'], cfg['site'].replace('://', '://www.'))
})


def _brand_from_origin(origin: str) -> str:
    """Map a request Origin header to a brand key."""
    origin = (origin or '').lower()
    for key, cfg in BRANDS.items():
        host = cfg.get('site', '').lower().replace('https://', '').replace('http://', '')
        if host and host in origin:
            return key
    return DEFAULT_BRAND


def _brand_config(brand: str) -> dict:
    return BRANDS.get(normalize_brand(brand), BRANDS[DEFAULT_BRAND])


def _coaching_config(brand: str) -> dict:
    """Return the brand-scoped coaching contract, failing closed by default."""
    return _brand_config(brand).get('coaching') or {'enabled': False, 'tiers': {}}


def _coaching_tier_config(brand: str, tier: str) -> dict:
    return (_coaching_config(brand).get('tiers') or {}).get(tier) or {}

# Email notifications for new orders
NOTIFICATION_EMAIL = os.environ.get('NOTIFICATION_EMAIL', '')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
# Compatibility alias used by existing tests/deploy env. Brand-aware sends use
# the registry's email.resend_from (which resolves RESEND_FROM for gravelgod).
RESEND_FROM = _brand_config(DEFAULT_BRAND)['email']['resend_from']

# Configure Stripe
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# Validate required config in production
if IS_PRODUCTION:
    if not STRIPE_WEBHOOK_SECRET:
        logger.warning("STRIPE_WEBHOOK_SECRET not set — webhook verification disabled")
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY required in production")

# Pricing — $15/week computed from race date, capped at $249
PRICE_PER_WEEK_CENTS = 1500   # $15/week
PRICE_CAP_CENTS = 24900       # $249 max
MIN_WEEKS = 4                 # Minimum 4 weeks ($60)
STRIPE_PRODUCT_NAME = 'Custom Training Plan'

# Pre-built Stripe price IDs (from scripts/create_stripe_products.py)
# Training plan prices keyed by weeks (4–16, plus 17+ cap)
TRAINING_PLAN_PRICE_IDS = {
    4: 'price_1T2ekOLoaHDbEqSqRbpy02qh',
    5: 'price_1T2ekOLoaHDbEqSqpJx9E1yq',
    6: 'price_1T2ekOLoaHDbEqSqY1A8y6LK',
    7: 'price_1T2ekPLoaHDbEqSq7mnndDhP',
    8: 'price_1T2ekPLoaHDbEqSqevidiXbx',
    9: 'price_1T2ekPLoaHDbEqSqkTTpr9dN',
    10: 'price_1T2ekQLoaHDbEqSqJr4wjnF8',
    11: 'price_1T2ekQLoaHDbEqSqJFJBGMkS',
    12: 'price_1T2ekQLoaHDbEqSqScrmfxRF',
    13: 'price_1T2ekQLoaHDbEqSqZ4o7bj8B',
    14: 'price_1T2ekRLoaHDbEqSq3q1cniEc',
    15: 'price_1T2ekRLoaHDbEqSqzhPHsmaP',
    16: 'price_1T2ekRLoaHDbEqSqFXGSA95u',
    17: 'price_1T2ekRLoaHDbEqSqgQVjT7FI',  # 17+ weeks (cap)
}

COACHING_PRICE_IDS = {
    'min': 'price_1T2z58LoaHDbEqSqeb8lLS9g',   # $199/4wk
    'mid': 'price_1T2z6SLoaHDbEqSqQIChOlOn',   # $299/4wk
    'max': 'price_1T2z7MLoaHDbEqSqoWpedvF5',   # $1,200/4wk
}

# One-time $99 setup fee added to all coaching checkouts
COACHING_SETUP_FEE_PRICE_ID = 'price_1T2yzQLoaHDbEqSqXKe6gNuF'  # $99 one-time (live)
COACHING_SETUP_FEE_CENTS = 9900
# Private, case-specific waiver. This coupon is applied by the backend only;
# athletes are never shown a public promotion code or a promotion-code field.
COACHING_SETUP_FEE_WAIVER_COUPON_ID = os.environ.get(
    'COACHING_SETUP_FEE_WAIVER_COUPON_ID', 'coaching_setup_waiver_99_v1')
COACHING_BOOKING_URL = os.environ.get('COACHING_BOOKING_URL', '')
COACHING_ESIGN_PROVIDER = os.environ.get(
    'COACHING_ESIGN_PROVIDER', 'signwell').strip().lower()
SIGNWELL_API_KEY = os.environ.get('SIGNWELL_API_KEY', '')
SIGNWELL_WEBHOOK_ID = os.environ.get('SIGNWELL_WEBHOOK_ID', '')
SIGNWELL_SYNTHETIC_TEMPLATE_ID = os.environ.get(
    'SIGNWELL_SYNTHETIC_TEMPLATE_ID', '')
SIGNWELL_LIVE_SEND_ENABLED = (
    os.environ.get('SIGNWELL_LIVE_SEND_ENABLED', '').lower() == 'true')
SIGNWELL_TEST_MODE = (
    os.environ.get('SIGNWELL_TEST_MODE', 'true').lower() == 'true')
SIGNWELL_REMINDERS_ENABLED = (
    os.environ.get('SIGNWELL_REMINDERS_ENABLED', '').lower() == 'true')

CONSULTING_PRICE_ID = 'price_1T2ekVLoaHDbEqSq0GGfoBEX'  # $150/hr, quantity=hours
CONSULTING_PRICE_CENTS = 15000  # $150/hr — used to seed the consult record when
                                # Stripe's amount_total is absent (see docs/CONSULT_ENGINE_SPEC.md §1)

# CONSULT-ENGINE C1 (docs/CONSULT_ENGINE_SPEC.md). All optional/off by
# default: unset envs disable the add-on line item, the runner routes
# (503), and leave the booking link blank (never a broken link).
CONSULT_PLAN_ADDON_PRICE_ID = os.environ.get('CONSULT_PLAN_ADDON_PRICE_ID', '')
CONSULT_PLAN_ADDON_AMOUNT_CENTS = 10000  # $100 custom-plan add-on (Matti, 2026-08-17)
CONSULT_BOOKING_URL = os.environ.get('CONSULT_BOOKING_URL', '')
CONSULT_RUNNER_SECRET = os.environ.get('CONSULT_RUNNER_SECRET', '')
CONSULT_ANALYSIS_LEASE_MINUTES = 90
CONSULT_ANALYSIS_MAX_ATTEMPTS = 3
CONSULT_RUNNER_HEARTBEAT_STALE_HOURS = 6   # §6: "Railway emails Matti if silent > 6 h"
CONSULT_RUNNER_ALARM_COOLDOWN_HOURS = 24   # at most one alarm per day

# Stripe Tax — requires Stripe Tax to be enabled at account level first.
# Set ENABLE_AUTOMATIC_TAX=true in Railway env vars after completing Stripe Tax setup.
ENABLE_AUTOMATIC_TAX = os.environ.get('ENABLE_AUTOMATIC_TAX', '').lower() == 'true'

# Intake data expiry (24 hours)
INTAKE_EXPIRY_HOURS = None  # Never auto-delete — intake data is tiny and needed for retries

# Checkout session expiry — short expiry triggers Stripe's recovery flow sooner
CHECKOUT_EXPIRY_MINUTES = 60

# Cron endpoint secret (prevents unauthorized triggers)
CRON_SECRET = os.environ.get('CRON_SECRET', '')
COACHING_INTAKE_SECRET = os.environ.get('COACHING_INTAKE_SECRET', '')
# Legacy direct-to-Stripe coaching endpoint. The controlled intake pipeline is
# the production path; explicit opt-in exists only for a deliberate rollback.
COACHING_DIRECT_CHECKOUT_ENABLED = (
    os.environ.get('COACHING_DIRECT_CHECKOUT_ENABLED', '').lower() == 'true'
)

# Pipeline timeout. Must stay BELOW gunicorn's --timeout (600 in Dockerfile)
# so the timeout path can still send the FAILED notification email before
# gunicorn kills the worker.
PIPELINE_TIMEOUT = int(os.environ.get('PIPELINE_TIMEOUT', '480'))

# Public simulator is independently kill-switched from paid fulfillment.
PUBLIC_PLAN_PREVIEW_ENABLED = (
    os.environ.get('PUBLIC_PLAN_PREVIEW_ENABLED', '').lower() == 'true')
PUBLIC_PLAN_PREVIEW_MAX_BYTES = 16 * 1024


def _pipeline_error_excerpt(result: dict, limit: int = 500) -> str:
    """Best error excerpt from a pipeline result.

    intake_to_plan.py reports most failures on stdout (stderr is often
    empty), so fall back to the TAIL of stdout — that's where the
    error/traceback lands.
    """
    stderr = (result.get('stderr') or '').strip()
    if stderr:
        return stderr[:limit]
    stdout = (result.get('stdout') or '').strip()
    return stdout[-limit:] if stdout else ''

# =============================================================================
# SECURITY HEADERS
# =============================================================================

@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'

    # CORS for checkout API (questionnaire form submits cross-origin)
    origin = request.headers.get('Origin', '')
    if origin in ALLOWED_ORIGINS or not IS_PRODUCTION:
        response.headers['Access-Control-Allow-Origin'] = origin or '*'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'

    return response


# =============================================================================
# PERIODIC CLEANUP
# =============================================================================

_last_intake_cleanup = datetime.now()


@app.before_request
def _periodic_intake_cleanup():
    """Hourly housekeeping: stale intakes + orphaned pipeline jobs."""
    global _last_intake_cleanup
    now = datetime.now()
    if (now - _last_intake_cleanup).total_seconds() > 3600:
        _last_intake_cleanup = now
        try:
            cleanup_stale_intakes()
        except Exception as e:
            logger.warning(f"Periodic intake cleanup failed: {e}")
        try:
            stats = sweep_stuck_jobs()
            if stats.get('retried') or stats.get('failed'):
                logger.warning(f"Periodic job sweep: {stats}")
        except Exception as e:
            logger.error(f"Periodic job sweep failed: {e}")
        try:
            consult_stats = sweep_stuck_consultations()
            if consult_stats.get('reopened') or consult_stats.get('needs_attention'):
                logger.warning(f"Periodic consult sweep: {consult_stats}")
        except Exception as e:
            logger.error(f"Periodic consult sweep failed: {e}")


# =============================================================================
# INPUT VALIDATION
# =============================================================================

# Strict athlete ID pattern
ATHLETE_ID_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$|^[a-z0-9]$')
MAX_ATHLETE_ID_LENGTH = 64
MAX_NAME_LENGTH = 100


def validate_athlete_id(athlete_id: str) -> bool:
    """Validate athlete ID is safe for filesystem use."""
    if not athlete_id:
        return False
    if len(athlete_id) > MAX_ATHLETE_ID_LENGTH:
        return False
    if not ATHLETE_ID_PATTERN.match(athlete_id):
        return False
    if '..' in athlete_id or '/' in athlete_id or '\\' in athlete_id:
        return False
    return True


def sanitize_athlete_id(name: str) -> str:
    """Convert a name to a safe athlete ID."""
    if not name or len(name) > MAX_NAME_LENGTH:
        return ''
    safe_id = name.lower().strip()
    safe_id = re.sub(r'\s+', '_', safe_id)
    safe_id = re.sub(r'[^a-z0-9_-]', '', safe_id)
    safe_id = re.sub(r'_+', '_', safe_id)
    safe_id = safe_id.strip('_-')
    safe_id = safe_id[:MAX_ATHLETE_ID_LENGTH]
    return safe_id


def _mask_email(email: str) -> str:
    """Mask email for safe logging: 'user@example.com' → 'u***@e***.com'"""
    if not email or '@' not in email:
        return '***'
    local, domain = email.rsplit('@', 1)
    parts = domain.rsplit('.', 1)
    masked_local = local[0] + '***' if local else '***'
    masked_domain = parts[0][0] + '***' if parts[0] else '***'
    tld = '.' + parts[1] if len(parts) > 1 else ''
    return f'{masked_local}@{masked_domain}{tld}'


def _send_email(to: str, subject: str, body: str, html: str = None, reply_to: str = None,
                attachments: list = None, brand: str = DEFAULT_BRAND):
    """Send email via Resend HTTP API. Returns True on success.

    attachments: list of (filename, path-or-sealed-bytes) tuples; files are
    base64-encoded. Confirmation passes bytes from an already-verified open
    descriptor so the sender never reopens a mutable release path.
    Resend caps total message size at 40MB.
    """
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not configured — cannot send email")
        return False

    payload = {
        'from': _brand_config(brand).get('email', {}).get('resend_from') or RESEND_FROM,
        'to': [to],
        'subject': subject,
        'text': body,
    }
    if html:
        payload['html'] = html
    if reply_to:
        payload['reply_to'] = reply_to
    if attachments:
        import base64
        encoded = []
        for fname, source in attachments:
            try:
                content = (bytes(source) if isinstance(source, (bytes, bytearray))
                           else Path(source).read_bytes())
                encoded.append({
                    'filename': fname,
                    'content': base64.b64encode(content).decode(),
                })
            except Exception as e:
                logger.warning(f"Skipping attachment {fname}: {e}")
        if encoded:
            payload['attachments'] = encoded

    try:
        resp = http_requests.post(
            'https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {RESEND_API_KEY}'},
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info(f"Email sent: {subject} → {_mask_email(to)}")
            return True
        else:
            logger.error(f"Resend API error {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Failed to send email via Resend: {e}")
        return False


def _build_phase1_generation_email(details: dict) -> tuple:
    """State-aware generation notice containing review-only controls."""
    details = external_notification_projection(details)
    name = str(details.get('name') or 'Unknown')
    order_id = str(details.get('order_id') or '')
    status = str(details.get('fulfillment_status') or 'BLOCKED_REVIEW')
    issues = redact_sensitive_review_items(details.get('blocking_issues') or [])
    unavailable = details.get('fulfillment_state') == 'unavailable'
    if unavailable and not any(i.get('id') == 'STATE_UNAVAILABLE' for i in issues):
        issues = [{
            'id': 'STATE_UNAVAILABLE', 'severity': 'CRITICAL',
            'message': 'Fulfillment state unavailable; repair and regenerate.',
            'waivable': False,
        }]
        status = 'BLOCKED_REVIEW'
    blocked = status == 'BLOCKED_REVIEW' or bool(issues) or unavailable
    label = 'BLOCKED REVIEW' if blocked else 'GENERATED — REVIEW REQUIRED'
    base_url = 'https://athlete-custom-training-plan-pipeline-production.up.railway.app'
    review_token = details.get('review_token') or ''
    # The bearer stays in the URL fragment: browsers do not send fragments in
    # HTTP requests, access logs, or Referer headers. Static bootstrap JS POSTs
    # it once to create the server-side review session.
    review_url = (f'{base_url}/review/{order_id}#token={review_token}'
                  if order_id and review_token else '')
    rows = []
    text_rows = []
    for issue in issues:
        waivable = bool(issue.get('waivable', False))
        rid = str(issue.get('id') or 'UNKNOWN')
        severity = str(issue.get('severity') or 'ERROR')
        message = str(issue.get('message') or '')
        policy = 'waivable with reason' if waivable else 'non-waivable — fix and regenerate'
        rows.append(
            '<li><code>' + html_escape(rid) + '</code> [' + html_escape(severity)
            + '] — ' + html_escape(message) + ' <strong>(' + policy + ')</strong></li>')
        text_rows.append(f'- {rid} [{severity}] — {message} ({policy})')
    issues_html = '<ul>' + ''.join(rows) + '</ul>' if rows else '<p>No blockers.</p>'
    links_html = ''
    links_text = ''
    if review_url:
        links = []
        text_links = []
        links.append(f'<p><a href="{html_escape(review_url)}">Open review page</a></p>')
        text_links.append(f'Review page: {review_url}')
        links_html = ''.join(links)
        links_text = '\n' + '\n'.join(text_links) + '\n'
    subject = f"[GG] {label}: {name} — order {order_id}"
    text = (f'{label}: {name}\nOrder: {order_id}\n\nBlockers:\n'
            + ('\n'.join(text_rows) if text_rows else '- none') + links_text
            + '\nNo release artifact is available before approval.\n')
    html = f"""<div style="font-family:Arial,sans-serif;max-width:680px">
      <h2>{html_escape(label)}: {html_escape(name)}</h2>
      <p>Order <code>{html_escape(order_id)}</code></p>
      <h3>Blockers</h3>{issues_html}{links_html}
      <p><strong>No release artifact is available before approval.</strong></p>
    </div>"""
    return subject, text, html


def _build_training_plan_email(details: dict) -> tuple:
    """Build coach notification email — athlete info + step-by-step fulfillment checklist."""
    details = external_notification_projection(details)
    name = details.get('name', 'Unknown')
    email = details.get('email', '')
    tier = details.get('tier', 'custom')
    order_id = details.get('order_id', '')
    race_name = details.get('race_name', '')
    race_date = details.get('race_date', '')
    ftp = details.get('ftp', '')
    weight_kg = details.get('weight_kg', '')
    hours = details.get('hours_per_week', '')
    weeks = details.get('plan_weeks', '')
    workouts = details.get('workout_count', '')
    methodology = details.get('methodology', '')
    athlete_id = details.get('athlete_id', '')
    pipeline_ok = details.get('pipeline_success', True)
    error_msg = details.get('error', '')
    download_token = details.get('download_token', '')
    brand = normalize_brand(details.get('brand'))
    subject_prefix = _brand_config(brand).get('subject_prefix', '[GG]')

    # Phase 1 generation notices are constrained to review-only surfaces.
    # Failed pipeline notices retain the older recovery email below.
    if pipeline_ok and ('fulfillment_status' in details
                        or 'fulfillment_state' in details
                        or 'blocking_issues' in details):
        return _build_phase1_generation_email(details)

    # Phase 4b delivery branching: endure-target orders that delivered get
    # the Endure review checklist; a failed Endure push falls back to the
    # unchanged TrainingPeaks checklist with a loud flag.
    delivery_target = details.get('delivery_target', 'trainingpeaks')
    endure = details.get('endure_delivery') or {}
    endure_ok = (delivery_target == 'endure'
                 and endure.get('status') in ('delivered', 'already_delivered'))
    endure_failed = delivery_target == 'endure' and not endure_ok
    endure_review_url = endure.get('review_url', '')

    base_url = 'https://athlete-custom-training-plan-pipeline-production.up.railway.app'
    download_full = f'{base_url}/api/download/{athlete_id}?type=full&token={download_token}' if download_token else ''

    status_color = '#1A8A82' if pipeline_ok else '#c0392b'
    status_label = 'REVIEW REQUIRED' if pipeline_ok else 'PIPELINE FAILED'

    # Edge-case review flags — profiles where automation is most likely to
    # need a human eye before delivery. The plan still generated and passed
    # the compliance gate; these flag elevated-judgment cases.
    review_flags = []
    try:
        _hours_num = float(str(hours).split('-')[0]) if hours else 0
    except (ValueError, TypeError):
        _hours_num = 0
    if _hours_num and _hours_num < 6:
        review_flags.append('Very low hours (<6h/wk) — check workout fit')
    try:
        _weeks_num = int(weeks) if weeks else 0
    except (ValueError, TypeError):
        _weeks_num = 0
    if _weeks_num > 26:
        review_flags.append(f'Long plan ({_weeks_num} wks) — check phase balance')
    _age = details.get('age', 0)
    try:
        _age = int(_age)
    except (ValueError, TypeError):
        _age = 0
    if _age >= 55:
        review_flags.append(f'Masters athlete ({_age}) — check recovery spacing')
    if details.get('risk_factors'):
        review_flags.append(
            'Risk factors: ' + ', '.join(str(r) for r in details['risk_factors']))

    # A failed Endure push is loud to the coach, invisible to the customer:
    # the ZWO package below is complete, deliver via TrainingPeaks as usual.
    if pipeline_ok and endure_failed:
        review_flags.insert(0,
            'ENDURE DELIVERY FAILED ('
            + str(endure.get('error') or 'no response') + ') — '
            'full ZWO package is intact; deliver via TrainingPeaks below. '
            'Streak reset.')

    # Strongest flag: the plan DELIVERED but an automatic compliance check
    # failed. The order is NOT lost — it just needs a human pass before sending.
    needs_review = bool(details.get('needs_review'))
    if needs_review:
        review_flags.insert(0,
            'AUTO-CHECK FAILED — plan delivered but a compliance rule was '
            'flagged. Review coaching_brief.md and adjust before sending.')

    subject = f"{subject_prefix} {'New order' if pipeline_ok else 'FAILED'}: {name} — {race_name or 'training plan'}"
    if pipeline_ok and needs_review:
        status_label = 'ACTION REQUIRED'
        subject = subject.replace(f'{subject_prefix} New order',
                                  f'{subject_prefix} ⚠ ACTION REQUIRED')
    elif pipeline_ok and review_flags:
        subject = subject.replace(f'{subject_prefix} New order',
                                  f'{subject_prefix} New order ⚠ REVIEW')

    # Shared athlete + plan info block
    info_html = f"""
    <h3 style="margin: 0 0 12px; font-size: 15px; color: #59473c;">Athlete</h3>
    <table style="font-size: 14px; border-collapse: collapse; width: 100%;">
      <tr><td style="padding: 4px 12px 4px 0; color: #888; width: 120px;">Name</td><td style="padding: 4px 0;"><strong>{name}</strong></td></tr>
      <tr><td style="padding: 4px 12px 4px 0; color: #888;">Email</td><td style="padding: 4px 0;"><a href="mailto:{email}">{email}</a></td></tr>
      {'<tr><td style="padding: 4px 12px 4px 0; color: #888;">FTP</td><td style="padding: 4px 0;">' + str(ftp) + 'W</td></tr>' if ftp else ''}
      {'<tr><td style="padding: 4px 12px 4px 0; color: #888;">Weight</td><td style="padding: 4px 0;">' + str(weight_kg) + ' kg</td></tr>' if weight_kg else ''}
      {'<tr><td style="padding: 4px 12px 4px 0; color: #888;">Hours/week</td><td style="padding: 4px 0;">' + str(hours) + '</td></tr>' if hours else ''}
    </table>

    <h3 style="margin: 20px 0 12px; font-size: 15px; color: #59473c;">Plan</h3>
    <table style="font-size: 14px; border-collapse: collapse; width: 100%;">
      {'<tr><td style="padding: 4px 12px 4px 0; color: #888; width: 120px;">Race</td><td style="padding: 4px 0;"><strong>' + race_name + '</strong></td></tr>' if race_name else ''}
      {'<tr><td style="padding: 4px 12px 4px 0; color: #888;">Race date</td><td style="padding: 4px 0;">' + race_date + '</td></tr>' if race_date else ''}
      {'<tr><td style="padding: 4px 12px 4px 0; color: #888;">Duration</td><td style="padding: 4px 0;">' + str(weeks) + ' weeks</td></tr>' if weeks else ''}
      {'<tr><td style="padding: 4px 12px 4px 0; color: #888;">Workouts</td><td style="padding: 4px 0;">' + str(workouts) + ' ZWO files</td></tr>' if workouts else ''}
      {'<tr><td style="padding: 4px 12px 4px 0; color: #888;">Methodology</td><td style="padding: 4px 0;">' + methodology + '</td></tr>' if methodology else ''}
    </table>"""

    # Delivery steps (checklist items 5-6) + confirm wording branch on the
    # order's delivery target. TP path is byte-for-byte unchanged.
    if endure_ok:
        platform_label = 'Endure'
        review_link_html = (f'<a href="{endure_review_url}">open the plan in Endure</a>'
                           if endure_review_url else 'open the plan in Endure')
        delivery_steps_html = f"""<li><strong>Review block 1 in Endure</strong> — {review_link_html} and check week 1 against the package above</li>
      <li><strong>Approve the block in Endure</strong> — approval writes the activities to {name}'s calendar; Endure then sends their invitation email</li>"""
        delivery_steps_text = (
            f"5. Review block 1 in Endure: {endure_review_url or 'open the plan in Endure'}\n"
            f"6. Approve the block in Endure — approval writes the activities "
            f"to {name}'s calendar; Endure sends their invitation email\n")
        endure_ids_html = (
            '<p style="font-size: 12px; color: #999; margin: 8px 0 0;">Endure: '
            + ' &middot; '.join(
                f'{k} <code>{endure.get(k)}</code>'
                for k in ('athlete_id', 'plan_id', 'block_id', 'invitation_id')
                if endure.get(k))
            + '</p>') if endure else ''
    else:
        platform_label = 'TrainingPeaks'
        delivery_steps_html = f"""<li><strong>Create athlete in TrainingPeaks</strong> — add <a href="mailto:{email}">{name}</a> to your coach account</li>
      <li><strong>Import ZWO files</strong> — drag workouts into their TP calendar, starting week 1</li>"""
        delivery_steps_text = (
            f"5. Create {name} in TrainingPeaks, add to coach account\n"
            f"6. Import ZWO files into their TP calendar\n")
        endure_ids_html = ''

    if pipeline_ok:
        html = f"""
<div style="font-family: 'Helvetica Neue', Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #333;">
  <div style="background: {status_color}; color: white; padding: 16px 24px; border-radius: 4px 4px 0 0;">
    <h2 style="margin: 0; font-size: 18px;">{status_label}: {name}</h2>
    <p style="margin: 4px 0 0; opacity: 0.9; font-size: 14px;">{race_name} &middot; {tier} tier</p>
  </div>

  <div style="background: #f9f9f7; padding: 24px; border: 1px solid #e0e0e0; border-top: none;">
    {info_html}

    {'<div style="margin: 16px 0; padding: 12px 16px; background: #fff3cd; border: 2px solid #B7950B; border-radius: 4px;"><strong style="color: #59473c;">⚠ REVIEW FLAGS</strong><ul style="margin: 8px 0 0; padding-left: 20px; font-size: 13px;">' + ''.join('<li>' + f + '</li>' for f in review_flags) + '</ul></div>' if review_flags else ''}

    {'<div style="margin: 24px 0; text-align: center;"><a href="' + download_full + '" style="display: inline-block; background: #59473c; color: white; padding: 14px 28px; text-decoration: none; border-radius: 4px; font-size: 15px; font-weight: bold;">Download Full Package</a></div>' if download_full else ''}

    <h3 style="margin: 24px 0 12px; font-size: 15px; color: #59473c;">Fulfillment checklist</h3>
    <ol style="font-size: 14px; padding-left: 20px; line-height: 2.0;">
      <li><strong>Download the package</strong> (button above)</li>
      <li><strong>Review quality</strong> — open <code>plan_preview.html</code>, check the week grid and quality gates</li>
      <li><strong>Read coaching brief</strong> — <code>coaching_brief.md</code> maps questionnaire answers to plan decisions</li>
      <li><strong>Spot-check workouts</strong> — open <code>training_guide.html</code>, check weeks 1, mid, and final</li>
      {delivery_steps_html}
      <li><strong>Send confirmation email</strong> — let them know the plan is live on {platform_label}:<br>
        <code style="font-size: 12px; background: #f0ede8; padding: 4px 8px; border-radius: 3px; display: inline-block; margin-top: 4px;">curl -X POST {base_url}/api/confirm/{athlete_id} -H "X-Cron-Secret: $CRON_SECRET"</code></li>
    </ol>
    {endure_ids_html}

    <div style="margin: 20px 0; padding: 12px 16px; background: #fff; border-left: 3px solid #B7950B;">
      <p style="margin: 0; font-size: 13px; color: #666;">
        <strong>Timeline:</strong> Customer got a payment confirmation email automatically. They're expecting the plan within 24 hours. Don't let it sit.
      </p>
    </div>

    <hr style="border: none; border-top: 1px solid #e0e0e0; margin: 24px 0 16px;">
    <p style="font-size: 12px; color: #999; margin: 0;">
      Athlete ID: {athlete_id} &middot; Order: {order_id}<br>
      Pipeline: passed &middot; {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}
    </p>
  </div>
</div>"""
    else:
        html = f"""
<div style="font-family: 'Helvetica Neue', Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #333;">
  <div style="background: {status_color}; color: white; padding: 16px 24px; border-radius: 4px 4px 0 0;">
    <h2 style="margin: 0; font-size: 18px;">PIPELINE FAILED: {name}</h2>
    <p style="margin: 4px 0 0; opacity: 0.9; font-size: 14px;">{race_name} &middot; Order {order_id}</p>
  </div>

  <div style="background: #f9f9f7; padding: 24px; border: 1px solid #e0e0e0; border-top: none;">
    {info_html}

    <div style="margin: 20px 0; padding: 16px; background: #fdf2f2; border: 1px solid #e8c4c4; border-radius: 4px;">
      <h3 style="margin: 0 0 8px; font-size: 15px; color: #c0392b;">Error</h3>
      <pre style="font-size: 12px; white-space: pre-wrap; margin: 0; color: #666;">{error_msg or 'Check Railway logs for details.'}</pre>
    </div>

    <h3 style="margin: 20px 0 12px; font-size: 15px; color: #59473c;">Recovery steps</h3>
    <ol style="font-size: 14px; padding-left: 20px; line-height: 2.0;">
      <li><strong>Check Railway logs</strong>: <code>railway logs --service stripe-webhook</code></li>
      <li><strong>Fix the issue</strong>, re-run locally: <code>pbpaste | python3 intake_to_plan.py</code></li>
      <li><strong>Email {name}</strong> — let them know there's a short delay, don't ghost them</li>
    </ol>

    <hr style="border: none; border-top: 1px solid #e0e0e0; margin: 24px 0 16px;">
    <p style="font-size: 12px; color: #999; margin: 0;">
      Athlete ID: {athlete_id} &middot; Order: {order_id}<br>
      Pipeline: FAILED &middot; {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}
    </p>
  </div>
</div>"""

    # Plain text fallback
    text = f"""{'ACTION REQUIRED' if pipeline_ok and needs_review else ('REVIEW REQUIRED' if pipeline_ok else 'PIPELINE FAILED')}: {name}
Race: {race_name} ({race_date})
Tier: {tier} | FTP: {ftp}W | Hours: {hours}/wk
Plan: {weeks} weeks, {workouts} workouts
Methodology: {methodology}
Order: {order_id} | Athlete ID: {athlete_id}
{'Download: ' + download_full if download_full else ''}

{'Fulfillment checklist:' if pipeline_ok else 'Recovery steps:'}
"""
    if pipeline_ok:
        text += f"""1. Download the package (link above)
2. Review plan_preview.html — check week grid and quality gates
3. Read coaching_brief.md — questionnaire-to-decision trace
4. Spot-check training_guide.html (weeks 1, mid, final)
{delivery_steps_text}7. Send confirmation: curl -X POST {base_url}/api/confirm/{athlete_id} -H "X-Cron-Secret: $CRON_SECRET"

Timeline: Customer got payment confirmation. They expect the plan within 24h.
"""
    else:
        text += f"""1. Check Railway logs: railway logs --service stripe-webhook
2. Fix the issue, re-run locally: pbpaste | python3 intake_to_plan.py
3. Email {name} — let them know there's a short delay

ERROR: {error_msg}
"""
    return subject, text, html


def _build_coaching_email(details: dict) -> tuple:
    """Build subject + HTML for a coaching subscription notification."""
    name = details.get('name', 'Unknown')
    email = details.get('email', '')
    tier = details.get('tier', 'unknown')
    subscription_id = details.get('subscription_id', '')
    order_id = details.get('order_id', '')
    brand = normalize_brand(details.get('brand'))
    brand_cfg = _brand_config(brand)
    subject_prefix = brand_cfg.get('subject_prefix', '[GG]')
    tier_label = _coaching_tier_config(brand, tier).get('label', tier.title())

    subject = f"{subject_prefix} New coaching: {name} — {tier_label}"
    html = f"""
<div style="font-family: 'Helvetica Neue', Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #333;">
  <div style="background: #59473c; color: white; padding: 16px 24px; border-radius: 4px 4px 0 0;">
    <h2 style="margin: 0; font-size: 18px;">New coaching: {name}</h2>
    <p style="margin: 4px 0 0; opacity: 0.9; font-size: 14px;">{tier_label} coaching &middot; Order {order_id}</p>
  </div>
  <div style="background: #f9f9f7; padding: 24px; border: 1px solid #e0e0e0; border-top: none;">
    <table style="font-size: 14px; border-collapse: collapse; width: 100%;">
      <tr><td style="padding: 4px 12px 4px 0; color: #888;">Name</td><td><strong>{name}</strong></td></tr>
      <tr><td style="padding: 4px 12px 4px 0; color: #888;">Email</td><td><a href="mailto:{email}">{email}</a></td></tr>
      <tr><td style="padding: 4px 12px 4px 0; color: #888;">Tier</td><td>{tier_label}</td></tr>
      <tr><td style="padding: 4px 12px 4px 0; color: #888;">Subscription</td><td><code>{subscription_id}</code></td></tr>
    </table>
    <h3 style="margin: 20px 0 12px; font-size: 15px; color: #59473c;">Next steps</h3>
    <ol style="font-size: 14px; padding-left: 20px; line-height: 1.8;">
      <li>Send welcome email to <a href="mailto:{email}">{name}</a> within 24 hours</li>
      <li>Schedule intake call</li>
      <li>Set up TrainingPeaks shared calendar</li>
    </ol>
  </div>
</div>"""
    text = f"New coaching: {name} ({email}), {tier_label}, subscription {subscription_id}, order {order_id}"
    return subject, text, html


def _build_consulting_email(details: dict) -> tuple:
    """Build subject + HTML for a consulting booking notification."""
    name = details.get('name', 'Unknown')
    email = details.get('email', '')
    hours = details.get('hours', '1')
    order_id = details.get('order_id', '')
    brand = normalize_brand(details.get('brand'))
    subject_prefix = _brand_config(brand).get('subject_prefix', '[GG]')

    subject = f"{subject_prefix} Consulting booked: {name} — {hours}hr"
    html = f"""
<div style="font-family: 'Helvetica Neue', Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #333;">
  <div style="background: #B7950B; color: white; padding: 16px 24px; border-radius: 4px 4px 0 0;">
    <h2 style="margin: 0; font-size: 18px;">Consulting: {name}</h2>
    <p style="margin: 4px 0 0; opacity: 0.9; font-size: 14px;">{hours} hour(s) &middot; Order {order_id}</p>
  </div>
  <div style="background: #f9f9f7; padding: 24px; border: 1px solid #e0e0e0; border-top: none;">
    <table style="font-size: 14px; border-collapse: collapse; width: 100%;">
      <tr><td style="padding: 4px 12px 4px 0; color: #888;">Name</td><td><strong>{name}</strong></td></tr>
      <tr><td style="padding: 4px 12px 4px 0; color: #888;">Email</td><td><a href="mailto:{email}">{email}</a></td></tr>
      <tr><td style="padding: 4px 12px 4px 0; color: #888;">Hours</td><td>{hours}</td></tr>
    </table>
    <h3 style="margin: 20px 0 12px; font-size: 15px; color: #59473c;">Next steps</h3>
    <ol style="font-size: 14px; padding-left: 20px; line-height: 1.8;">
      <li>Email <a href="mailto:{email}">{name}</a> to schedule the call</li>
      <li>Send calendar invite with video link</li>
    </ol>
  </div>
</div>"""
    text = f"Consulting booked: {name} ({email}), {hours}hr, order {order_id}"
    return subject, text, html


_GA4_CLIENT_ID_RE = re.compile(r'^\d+\.\d+$')
_GA4_SESSION_ID_RE = re.compile(r'^\d+$')


def _validated_ga4_attribution(client_id=None, session_id=None) -> tuple:
    """Return Stripe/GA-safe browser attribution identifiers or empty values."""
    safe_client_id = str(client_id or '').strip()
    safe_session_id = str(session_id or '').strip()
    if not _GA4_CLIENT_ID_RE.fullmatch(safe_client_id):
        safe_client_id = ''
    if not _GA4_SESSION_ID_RE.fullmatch(safe_session_id):
        safe_session_id = ''
    return safe_client_id, safe_session_id


def _payload_ga4_attribution(data: dict) -> tuple:
    """Extract explicit consent plus valid GA browser identifiers.

    ``unknown`` keeps older checkout clients backward-compatible during a
    staggered deploy. Browser identifiers are accepted only with an explicit
    ``granted`` signal.
    """
    raw_consent = str(data.pop('analytics_consent', '') or '').strip().lower()
    consent = raw_consent if raw_consent in ('granted', 'denied') else 'unknown'
    raw_client_id = data.pop('ga4_client_id', '')
    raw_session_id = data.pop('ga4_session_id', '')
    if consent != 'granted':
        return '', '', consent
    client_id, session_id = _validated_ga4_attribution(
        raw_client_id, raw_session_id)
    return client_id, session_id, consent


def _apply_ga4_metadata(metadata: dict, client_id: str, session_id: str,
                        analytics_consent: str) -> None:
    """Attach the bounded attribution contract to Stripe metadata in place."""
    metadata['analytics_consent'] = analytics_consent
    if client_id:
        metadata['ga4_client_id'] = client_id
    if session_id:
        metadata['ga4_session_id'] = session_id


def _send_ga4_purchase(order_id: str, value_cents, product_type: str,
                       item_name: str, brand: str = DEFAULT_BRAND,
                       client_id: str = '', session_id: str = '',
                       analytics_consent: str = 'unknown'):
    """Record a purchase in GA4 via Measurement Protocol (server-side).

    This is the sole purchase-event source.  When consented browser identifiers
    were captured before Stripe redirect, the Measurement Protocol event is
    joined to that acquisition session.  Otherwise it falls back to a
    deterministic order-scoped client id.  Routes to the brand's GA4 property,
    never raises, and skips test orders.
    """
    cfg = _brand_config(brand)
    if not cfg['ga4_mp_api_secret'] or not cfg['ga4_measurement_id']:
        return
    if str(analytics_consent or '').strip().lower() == 'denied':
        return
    if order_id.startswith(('test_', 'cs_test_')):
        return
    try:
        safe_client_id, safe_session_id = _validated_ga4_attribution(
            client_id, session_id)
        event_params = {
            'transaction_id': order_id,
            'currency': 'USD',
            'value': round((value_cents or 0) / 100, 2),
            'product_type': product_type,
            'event_source': 'stripe_webhook',
            'items': [{
                'item_name': item_name,
                'item_category': product_type,
                'price': round((value_cents or 0) / 100, 2),
                'quantity': 1,
            }],
        }
        if safe_session_id:
            event_params['session_id'] = int(safe_session_id)
            event_params['engagement_time_msec'] = 1
        payload = {
            # Deterministic fallback keeps webhook retries on one GA identity.
            'client_id': safe_client_id or f'srv.{order_id[-16:] or "order"}',
            'events': [{
                'name': 'purchase',
                'params': event_params,
            }],
        }
        resp = http_requests.post(
            'https://www.google-analytics.com/mp/collect',
            params={'measurement_id': cfg['ga4_measurement_id'],
                    'api_secret': cfg['ga4_mp_api_secret']},
            json=payload,
            timeout=5,
        )
        if resp.status_code >= 300:
            logger.warning(f"GA4 MP purchase non-2xx: {resp.status_code}")
        else:
            logger.info(f"GA4 purchase recorded: {product_type} "
                        f"${(value_cents or 0) / 100:.2f} ({order_id[:24]})")
    except Exception as e:
        logger.warning(f"GA4 MP purchase failed (non-fatal): {e}")


def _notify_new_order(product_type: str, details: dict):
    """Send rich notification for new order. Falls back to CRITICAL log if Resend not configured."""
    details = external_notification_projection(details)
    if product_type in ('training_plan', 'training_plan_FAILED'):
        details['pipeline_success'] = product_type == 'training_plan'
        subject, text, html = _build_training_plan_email(details)
    elif product_type == 'coaching':
        subject, text, html = _build_coaching_email(details)
    elif product_type == 'consulting':
        subject, text, html = _build_consulting_email(details)
    else:
        # Fallback for TEST and unknown types
        subject = f"[Gravel God] {product_type}: {details.get('name', 'Unknown')}"
        text = '\n'.join(f"  {k}: {v}" for k, v in details.items())
        html = None

    brand = normalize_brand(details.get('brand'))
    if NOTIFICATION_EMAIL and RESEND_API_KEY:
        if not _send_email(NOTIFICATION_EMAIL, subject, text, html=html, brand=brand):
            logger.critical(f"NEW ORDER: {subject}\n{text}")
    else:
        logger.critical(f"NEW ORDER: {subject}\n{text}")


def _send_payment_confirmation(customer_email: str, customer_name: str,
                               race_name: str = '', plan_weeks: str = '',
                               brand: str = DEFAULT_BRAND):
    """Send immediate payment confirmation to customer.

    Auto-fires on successful Stripe checkout. Tells them what they bought,
    that we're building their plan, and when to expect it. Sign-off and
    site link follow the brand the customer bought from.
    """
    if not RESEND_API_KEY:
        logger.warning("Cannot send payment confirmation — RESEND_API_KEY not set")
        return

    brand_cfg = _brand_config(brand)
    brand_name = brand_cfg['name']
    brand_site = brand_cfg['site'].replace('https://', '')

    first_name = customer_name.split()[0] if customer_name else 'there'
    race_mention = f' for {race_name}' if race_name else ''
    weeks_mention = f'{plan_weeks}-week ' if plan_weeks else ''

    subject = f'Payment confirmed — your {weeks_mention}training plan{race_mention}'

    tp_connect_url = 'https://home.trainingpeaks.com/attachtocoach?sharedKey=2OTEPC6BXNVQU'

    text = f"""Hey {first_name},

Payment received — thank you.

YOUR ONE ACTION ITEM:
Connect to my coaching account on TrainingPeaks so I can push your workouts there:
{tp_connect_url}

If you don't have a TrainingPeaks account, create a free one first at trainingpeaks.com, then click the link above.

WHAT HAPPENS NEXT:
1. Your custom {weeks_mention}training plan{race_mention} is being built right now.
2. I'll review it personally and make sure everything is dialed.
3. Within 24 hours, your workouts will be live on your TrainingPeaks calendar.
4. You'll get an email when it's ready with your training guide (PDF).

Questions? Reply to this email.

— Matti, {brand_name}
{brand_site}
"""

    html = f"""
<div style="font-family: 'Helvetica Neue', Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #333;">
  <div style="background: #59473c; color: white; padding: 24px; border-radius: 4px 4px 0 0;">
    <h1 style="margin: 0; font-size: 22px;">Payment confirmed</h1>
    <p style="margin: 6px 0 0; opacity: 0.9; font-size: 15px;">{weeks_mention}training plan{race_mention}</p>
  </div>

  <div style="background: #f9f9f7; padding: 24px; border: 1px solid #e0e0e0; border-top: none;">
    <p style="font-size: 15px; line-height: 1.6;">Hey {first_name},</p>

    <p style="font-size: 15px; line-height: 1.6;">Payment received — thank you.</p>

    <div style="margin: 20px 0; padding: 20px; background: #fff; border: 2px solid #1A8A82; border-radius: 6px;">
      <h3 style="margin: 0 0 8px; font-size: 16px; color: #59473c;">Your one action item</h3>
      <p style="margin: 0 0 16px; font-size: 14px; color: #555;">Connect to my coaching account on TrainingPeaks so I can push your workouts there:</p>
      <div style="text-align: center;">
        <a href="{tp_connect_url}" style="display: inline-block; background: #1A8A82; color: white; padding: 14px 32px; text-decoration: none; border-radius: 4px; font-size: 15px; font-weight: bold;">Connect on TrainingPeaks</a>
      </div>
      <p style="margin: 12px 0 0; font-size: 12px; color: #999; text-align: center;">Don't have a TrainingPeaks account? <a href="https://www.trainingpeaks.com/athlete-edition/" style="color: #1A8A82;">Create a free one first</a>, then click above.</p>
    </div>

    <h3 style="margin: 24px 0 12px; font-size: 15px; color: #59473c;">What happens next</h3>
    <ol style="font-size: 14px; padding-left: 20px; line-height: 2.2;">
      <li>Your custom {weeks_mention}training plan{race_mention} is <strong>being built right now</strong>.</li>
      <li>I'll <strong>review it personally</strong> and make sure everything is dialed.</li>
      <li>Within <strong>24 hours</strong>, your workouts will be live on your TrainingPeaks calendar.</li>
      <li>You'll get an email when it's ready with your <strong>training guide</strong> (PDF).</li>
    </ol>

    <div style="margin: 24px 0; padding: 12px 16px; background: #f5f5f0; border-left: 3px solid #B7950B;">
      <p style="margin: 0; font-size: 13px; color: #666;"><strong>Delivery timeline:</strong> Most plans are ready same-day. Maximum 24 hours. I'll email you the moment it's live.</p>
    </div>

    <p style="font-size: 14px; line-height: 1.6;">Questions? Reply to this email.</p>

    <p style="font-size: 14px; margin-top: 24px; color: #666;">— Matti, {brand_name}<br>
    <a href="{brand_cfg['site']}" style="color: #1A8A82;">{brand_site}</a></p>
  </div>
</div>"""

    ok = _send_email(customer_email, subject, text, html=html,
                     reply_to=NOTIFICATION_EMAIL, brand=brand)
    if ok:
        logger.info(f"Payment confirmation sent to {_mask_email(customer_email)}")
    else:
        logger.error(f"Failed to send payment confirmation to {_mask_email(customer_email)}")


def _send_coaching_payment_confirmation(customer_email: str, customer_name: str,
                                        tier: str, brand: str = DEFAULT_BRAND) -> bool:
    """Confirm an active coaching subscription and give one conditional TP step.

    The coaching tier stays Min/Mid/Max. "Premium" is reserved for the
    TrainingPeaks account benefit so the two products cannot be confused.
    """
    if not customer_email:
        logger.warning("Cannot send coaching confirmation — customer email missing")
        return False

    brand_cfg = _brand_config(brand)
    coaching_cfg = _coaching_config(brand)
    tier_cfg = _coaching_tier_config(brand, tier)
    tier_label = tier_cfg.get('label', tier.title())
    first_name = customer_name.split()[0] if customer_name else 'there'
    attach_url = coaching_cfg.get('trainingpeaks_attach_url', '')
    premium_included = bool(coaching_cfg.get('trainingpeaks_premium_included'))
    signature = brand_cfg.get('email', {})
    signature_name = signature.get('signature_name', 'Matti')
    signature_org = signature.get('signature_organization', brand_cfg['name'])
    signature_site = signature.get(
        'signature_site', brand_cfg['site'].replace('https://', ''))

    premium_line = (
        "TrainingPeaks Premium is included with your coaching. "
        "You do not need to purchase it separately.\n\n"
        if premium_included else ''
    )
    attach_line = (
        "If your TrainingPeaks account is not already connected to my coaching "
        f"account, connect it here:\n{attach_url}\n\n"
        "If it is already connected, skip that step.\n\n"
        if attach_url else ''
    )
    booking_line = (
        f"Book your kickoff call:\n{COACHING_BOOKING_URL}\n\n"
        if COACHING_BOOKING_URL.startswith('https://') else '')
    subject = f'Payment confirmed — {tier_label} coaching'
    body = (
        f"Hey {first_name},\n\n"
        f"Your {tier_label} coaching subscription is active.\n\n"
        + premium_line
        + attach_line
        + booking_line
        + "I’ll review your intake and training history, then follow up with "
          "the first concrete steps.\n\n"
        + "Questions before then? Reply here.\n\n"
        + f"— {signature_name}\n{signature_org}\n{signature_site}"
    )
    ok = _send_email(customer_email, subject, body,
                     reply_to=NOTIFICATION_EMAIL or None, brand=brand)
    if not ok:
        logger.critical(
            f"COACHING CONFIRMATION FAILED: brand={brand}, tier={tier}, "
            f"email={_mask_email(customer_email)}")
    return ok


def _send_coaching_onboarding_handoff(case: dict, checkout_url: str) -> bool:
    """Send the approved athlete one compact platform + payment handoff."""
    brand = normalize_brand(case.get('brand'))
    brand_cfg = _brand_config(brand)
    coaching_cfg = _coaching_config(brand)
    tier = case.get('tier', '')
    tier_label = _coaching_tier_config(brand, tier).get('label', tier.title())
    name = case.get('athlete', {}).get('name', '')
    email = case.get('athlete', {}).get('email', '')
    first_name = name.split()[0] if name else 'there'
    attach_url = coaching_cfg.get('trainingpeaks_attach_url', '')
    setup_fee_waived = (
        _coaching_gate_status(case, 'setup_fee_waiver') == 'approved')
    fee_line = (
        "I waived the $99 setup fee for this case; checkout already reflects "
        "that waiver.\n\n"
        if setup_fee_waived else
        "Checkout includes the one-time $99 setup fee for intake analysis and "
        "your first plan build.\n\n"
    )
    signature = brand_cfg.get('email', {})

    subject = f'Next steps — {tier_label} coaching'
    body = (
        f"Hey {first_name},\n\n"
        "I’ve reviewed your intake. Here are the two onboarding steps.\n\n"
        "1. TrainingPeaks\n"
        "If your account is not already connected to my coaching account, "
        f"connect it here:\n{attach_url}\n"
        "If it is already connected, skip this step. TrainingPeaks Premium "
        "is included with coaching; do not purchase it separately.\n\n"
        f"2. Start {tier_label} coaching\n{checkout_url}\n"
        + fee_line
        + "Once checkout is complete, I’ll review your training history and "
        "follow up with the first concrete steps.\n\n"
        f"— {signature.get('signature_name', 'Matti')}\n"
        f"{signature.get('signature_organization', brand_cfg['name'])}\n"
        f"{signature.get('signature_site', brand_cfg['site'].replace('https://', ''))}"
    )
    return _send_email(email, subject, body,
                       reply_to=NOTIFICATION_EMAIL or None, brand=brand)


def _build_plan_notification_details(order_data: dict, result: dict,
                                     intake_data: dict = None) -> dict:
    """Build enriched details dict for training plan notifications."""
    profile = order_data.get('profile', {})
    fitness = profile.get('fitness_markers', {})
    target = profile.get('target_race', {})
    schedule = profile.get('weekly_schedule', {})

    # Parse workout count from pipeline stdout
    stdout = result.get('stdout', '')
    workout_count = ''
    for line in stdout.split('\n'):
        if '.zwo files' in line:
            # e.g. "  workouts/            145 .zwo files"
            parts = line.strip().split()
            for i, p in enumerate(parts):
                if p == '.zwo':
                    workout_count = parts[i - 1] if i > 0 else ''
                    break

    # Parse plan weeks from stdout
    plan_weeks = ''
    for line in stdout.split('\n'):
        if '-week plan' in line:
            for word in line.split():
                if word.endswith('-week'):
                    plan_weeks = word.replace('-week', '')
                    break

    # Try to get methodology from athlete dir
    methodology = ''
    athlete_id = order_data.get('athlete_id', '')
    meth_path = Path(ATHLETES_DIR) / athlete_id / 'methodology.yaml'
    if meth_path.exists():
        try:
            import yaml
            with open(meth_path) as f:
                meth_data = yaml.safe_load(f)
                methodology = meth_data.get('name', meth_data.get('methodology', ''))
        except Exception:
            pass

    return {
        'name': profile.get('name', ''),
        'email': profile.get('email', ''),
        'tier': order_data.get('tier', 'custom'),
        'order_id': order_data.get('order_id', ''),
        'athlete_id': athlete_id,
        'delivery_target': order_data.get(
            'delivery_platform', order_data.get('delivery_target', 'trainingpeaks')),
        'race_name': target.get('name', intake_data.get('race_name', '') if intake_data else ''),
        'race_date': target.get('date', intake_data.get('race_date', '') if intake_data else ''),
        'ftp': fitness.get('ftp_watts', intake_data.get('ftp', '') if intake_data else ''),
        'weight_kg': fitness.get('weight_kg', ''),
        'hours_per_week': (schedule.get('hours_per_week', '')
                          or schedule.get('cycling_hours_target', '')
                          or (intake_data.get('hours_per_week', '') if intake_data else '')),
        'plan_weeks': plan_weeks,
        'workout_count': workout_count,
        'methodology': methodology,
        'brand': normalize_brand(order_data.get('brand') or profile.get('brand')),
        'error': _pipeline_error_excerpt(result) if not result.get('success') else '',
        # The plan delivered, but the automatic coach checks flagged it — review
        # coaching_brief.md before sending. Distinct from a clean delivery.
        'needs_review': 'GG_NEEDS_REVIEW=1' in stdout,
    }


def _log_product_event(product_type: str, order_id: str, **details):
    """Write a product event to the order log. Shared by coaching/consulting handlers."""
    log_dir = Path(DATA_DIR) / '.logs'
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{datetime.now().strftime('%Y-%m')}.jsonl"
    log_entry = {
        'timestamp': datetime.now().isoformat(),
        'product_type': product_type,
        'order_id': order_id,
        **details,
        'success': True,
    }
    try:
        with open(log_file, 'a') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(json.dumps(log_entry) + '\n')
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except IOError as e:
        logger.error(f"Failed to write {product_type} log: {e}")


def validate_order_data(order_data: dict) -> tuple:
    """Validate order data, return (is_valid, error_message)."""
    athlete_id = order_data.get('athlete_id', '')
    if not validate_athlete_id(athlete_id):
        return False, f"Invalid athlete ID: {athlete_id}"

    profile = order_data.get('profile', {})
    if not profile.get('name'):
        return False, "Missing athlete name"

    if not profile.get('email'):
        return False, "Missing athlete email"

    # Validate email format loosely
    email = profile.get('email', '')
    if '@' not in email or '.' not in email:
        return False, f"Invalid email format: {email}"

    # Validate numeric fields if present
    fitness = profile.get('fitness_markers', {})
    if fitness.get('weight_kg') is not None:
        weight = fitness['weight_kg']
        if not (30 <= weight <= 200):
            return False, f"Invalid weight: {weight}"

    if fitness.get('ftp_watts') is not None:
        ftp = fitness['ftp_watts']
        if not (50 <= ftp <= 600):
            return False, f"Invalid FTP: {ftp}"

    return True, None


# =============================================================================
# SIGNATURE VERIFICATION
# =============================================================================

def verify_woocommerce_signature(payload: bytes, signature: str) -> bool:
    """Verify WooCommerce webhook signature."""
    if not WOOCOMMERCE_SECRET:
        if IS_PRODUCTION:
            logger.error("WOOCOMMERCE_SECRET not configured in production")
            return False
        logger.warning("WOOCOMMERCE_SECRET not set - skipping verification (dev mode)")
        return True

    expected = hmac.new(
        WOOCOMMERCE_SECRET.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()

    # WooCommerce sends base64 encoded signature, not hex
    import base64
    try:
        sig_bytes = base64.b64decode(signature)
        sig_hex = sig_bytes.hex()
    except Exception:
        sig_hex = signature  # Fallback to raw comparison

    return hmac.compare_digest(expected, sig_hex)


def verify_stripe_signature(payload: bytes, signature: str) -> bool:
    """Verify Stripe webhook signature."""
    if not STRIPE_WEBHOOK_SECRET:
        if IS_PRODUCTION:
            logger.error("STRIPE_WEBHOOK_SECRET not configured in production")
            return False
        logger.warning("STRIPE_WEBHOOK_SECRET not set - skipping verification (dev mode)")
        return True

    try:
        stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
        return True
    except stripe.error.SignatureVerificationError as e:
        logger.warning(f"Stripe signature verification failed: {e}")
        return False
    except Exception as e:
        logger.error(f"Stripe verification error: {e}")
        return False


# =============================================================================
# IDEMPOTENCY
# =============================================================================

def check_idempotency(order_id: str) -> bool:
    """Check if this order has already been processed. Returns True if duplicate."""
    if not order_id:
        return False

    processed_file = Path(DATA_DIR) / '.processed_orders.json'

    try:
        if processed_file.exists():
            with open(processed_file, 'r') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                processed = json.load(f)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                if order_id in processed:
                    if _drill_reprocess_allowed(order_id):
                        logger.info(
                            f"Cancelled synthetic drill {order_id} may be reprocessed")
                        return False
                    logger.info(f"Duplicate order detected: {order_id}")
                    return True
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Error reading processed orders: {e}")

    return False


def _drill_reprocess_allowed(order_id: str) -> bool:
    """Same-day drill leftovers may be cancelled and generated again.

    Real paid orders stay idempotent even after CANCELLED. Only the
    synthetic `drill-YYYYMMDD` identity is reusable, and only when the
    authoritative state is a pre-apply cancellation.
    """
    if not str(order_id).startswith('drill-'):
        return False
    try:
        state = load_fulfillment_state(_fulfillment_status_path(order_id))
    except FulfillmentStateError:
        return False
    if state.get('status') != CANCELLED:
        return False
    if state.get('application'):
        return False
    return True


def mark_order_processed(order_id: str, athlete_id: str):
    """Mark an order as processed."""
    if not order_id:
        return

    processed_file = Path(DATA_DIR) / '.processed_orders.json'
    processed_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(processed_file, 'a+') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.seek(0)
            try:
                processed = json.load(f)
            except json.JSONDecodeError:
                processed = {}

            processed[order_id] = {
                'athlete_id': athlete_id,
                'processed_at': datetime.now().isoformat()
            }

            f.seek(0)
            f.truncate()
            json.dump(processed, f, indent=2)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except IOError as e:
        logger.error(f"Error marking order processed: {e}")


# =============================================================================
# INTAKE STORAGE — Questionnaire data stored temporarily before payment
# =============================================================================

def get_intake_dir() -> Path:
    """Get or create the intake storage directory on persistent volume."""
    intake_dir = Path(DATA_DIR) / '.intake'
    intake_dir.mkdir(parents=True, exist_ok=True)
    return intake_dir


def store_intake(intake_id: str, data: dict):
    """Store questionnaire data for later retrieval after payment."""
    intake_dir = get_intake_dir()
    intake_file = intake_dir / f'{intake_id}.json'

    with open(intake_file, 'w') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        json.dump({
            'intake_id': intake_id,
            'stored_at': datetime.now().isoformat(),
            'data': data,
        }, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    logger.info(f"Stored intake {intake_id}")


def load_intake(intake_id: str) -> dict:
    """Load stored questionnaire data. Returns empty dict if not found."""
    # Validate intake_id is a valid UUID to prevent path traversal
    try:
        uuid.UUID(intake_id)
    except (ValueError, AttributeError):
        logger.warning(f"Invalid intake_id format: {intake_id}")
        return {}

    intake_file = get_intake_dir() / f'{intake_id}.json'
    if not intake_file.exists():
        logger.warning(f"Intake not found: {intake_id}")
        return {}

    try:
        with open(intake_file, 'r') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            content = json.load(f)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return content.get('data', {})
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading intake {intake_id}: {e}")
        return {}


def _coaching_intake_path(case_id: str) -> Path:
    """Resolve one durable coaching case without allowing path traversal."""
    uuid.UUID(case_id)
    root = Path(DATA_DIR) / 'coaching_intakes'
    root.mkdir(parents=True, exist_ok=True)
    return root / f'{case_id}.json'


@contextlib.contextmanager
def _coaching_operation_lock(name: str):
    """Serialize a case/provider mutation across threads and worker processes."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(name or '')):
        raise ValueError('invalid coaching operation lock name')
    root = Path(DATA_DIR) / '.locks'
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / f'{name}.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _serialized_coaching_case(operation: str):
    def decorate(view):
        @functools.wraps(view)
        def wrapped(case_id, *args, **kwargs):
            with _coaching_operation_lock(f'{operation}-{case_id}'):
                return view(case_id, *args, **kwargs)
        return wrapped
    return decorate


def _serialized_coaching_provider(operation: str):
    def decorate(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            with _coaching_operation_lock(operation):
                return view(*args, **kwargs)
        return wrapped
    return decorate


def _read_coaching_intake(case_id: str) -> dict:
    try:
        path = _coaching_intake_path(case_id)
        return json.loads(path.read_text()) if path.exists() else {}
    except (ValueError, OSError, json.JSONDecodeError):
        return {}


def _iter_coaching_intakes():
    """Yield valid durable coaching cases, skipping corrupt/non-case files."""
    root = Path(DATA_DIR) / 'coaching_intakes'
    if not root.exists():
        return
    for path in sorted(root.glob('*.json')):
        try:
            case = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            logger.warning(f'Could not read coaching case {path.name}')
            continue
        if (isinstance(case, dict) and
                case.get('schema') == 'coaching_onboarding_case/v1' and
                case.get('case_id')):
            yield case


def _stripe_object_id(value) -> str:
    """Normalize expandable Stripe IDs without trusting object reprs."""
    if isinstance(value, dict):
        return str(value.get('id') or '')
    return str(value or '')


def _stripe_invoice_subscription_id(invoice: dict) -> str:
    """Support both legacy and current Stripe invoice subscription shapes."""
    direct = _stripe_object_id(invoice.get('subscription'))
    if direct:
        return direct
    parent = invoice.get('parent') or {}
    details = parent.get('subscription_details') or {}
    return _stripe_object_id(details.get('subscription'))


def _find_coaching_case_for_billing(*, case_id: str = '',
                                    subscription_id: str = '',
                                    customer_id: str = '') -> dict:
    """Resolve a billing event to exactly one case using provider identifiers."""
    if case_id:
        candidate = _read_coaching_intake(case_id)
        if candidate:
            billing = candidate.get('billing') or {}
            receipt = (candidate.get('receipts') or {}).get('stripe_payment') or {}
            known_subscription = str(
                billing.get('subscription_id') or receipt.get('subscription_id') or '')
            known_customer = str(
                billing.get('customer_id') or receipt.get('customer_id') or '')
            if ((not subscription_id or not known_subscription or
                 hmac.compare_digest(subscription_id, known_subscription)) and
                    (not customer_id or not known_customer or
                     hmac.compare_digest(customer_id, known_customer))):
                return candidate

    matches = []
    for candidate in _iter_coaching_intakes() or ():
        billing = candidate.get('billing') or {}
        receipt = (candidate.get('receipts') or {}).get('stripe_payment') or {}
        known_subscription = str(
            billing.get('subscription_id') or receipt.get('subscription_id') or '')
        known_customer = str(
            billing.get('customer_id') or receipt.get('customer_id') or '')
        if subscription_id and known_subscription == subscription_id:
            matches.append(candidate)
        elif customer_id and known_customer == customer_id:
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else {}


def _write_coaching_intake(case: dict, *, create_only: bool = False) -> bool:
    """Persist one case atomically; return False for a duplicate create."""
    path = _coaching_intake_path(case['case_id'])
    payload = (json.dumps(case, indent=2, sort_keys=True) + '\n').encode()
    if create_only:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return True

    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    with open(tmp, 'wb') as handle:
        os.chmod(tmp, 0o600)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return True


_COACHING_EVENT_DETAIL_KEYS = {
    'from_state', 'to_state', 'gate', 'status', 'email_sent',
    'recovered_from', 'recovery_disposition',
}


def _record_coaching_event(case: dict, event_name: str, source_id: str,
                           *, details: dict | None = None,
                           occurred_at: str | None = None) -> bool:
    """Append one privacy-minimized, idempotent lifecycle event to a case.

    The case file remains the durable source of truth. Analytics projections
    never copy athlete name, email, questionnaire answers, URLs, or free text.
    """
    event_name = str(event_name or '').strip().lower()
    source_id = str(source_id or '').strip()
    if not re.fullmatch(r'[a-z0-9_]{3,80}', event_name) or not source_id:
        raise ValueError('A canonical event_name and source_id are required')
    event_id = hashlib.sha256(
        f"{case.get('case_id', '')}\0{event_name}\0{source_id}".encode('utf-8')
    ).hexdigest()[:24]
    events = case.setdefault('analytics_events', [])
    if any(item.get('event_id') == event_id for item in events):
        return False
    safe_details = {}
    for key, value in (details or {}).items():
        if key not in _COACHING_EVENT_DETAIL_KEYS:
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            safe_details[key] = value
    events.append({
        'schema': 'coaching_funnel_event/v1',
        'event_id': event_id,
        'event_name': event_name,
        'occurred_at': occurred_at or datetime.now(timezone.utc).isoformat(),
        'source_ref_sha256': hashlib.sha256(
            source_id.encode('utf-8')).hexdigest()[:24],
        'brand': normalize_brand(case.get('brand')),
        'tier': str(case.get('tier') or ''),
        'details': safe_details,
    })
    return True


_COACHING_INTAKE_REQUIRED = {
    'age': 'age',
    'sex': 'sex',
    'weight': 'weight',
    'height_ft': 'height (feet)',
    'height_in': 'height (inches)',
    'primary_goal': 'primary goal',
    'years_cycling': 'years cycling',
    'longest_ride': 'longest recent ride',
    'rhr_baseline': 'resting heart rate baseline',
    'sleep_hours_baseline': 'typical sleep',
    'sleep_quality': 'sleep quality',
    'recovery_speed': 'recovery speed',
    'training_platform': 'training platform',
    'trainer_access': 'indoor trainer access',
    'hours_per_week': 'weekly training hours',
    'long_ride_days': 'long-ride day availability',
    'interval_days': 'interval day availability',
    'life_stress': 'life stress',
    'strength_current': 'current strength training',
    'checkin_frequency': 'check-in frequency',
    'feedback_detail': 'feedback detail',
    'autonomy': 'autonomy preference',
    'missed_workout_response': 'missed-workout response',
}

_COACHING_INTAKE_REQUIRED_XC = {
    'age': 'age',
    'sex': 'sex',
    'weight': 'weight',
    'height': 'height',
    'primary_goal': 'primary goal',
    'years_skiing': 'years skiing',
    'discipline_pref': 'classic/skate preference',
    'racing_experience': 'ski-racing experience',
    'weekly_hours': 'current weekly training hours',
    'max_hours': 'maximum weekly training hours',
    'preferred_days': 'preferred training days',
    'feedback_freq': 'feedback frequency',
}

_COACHING_INTAKE_FOLLOWUP = {
    'date_of_birth': 'date of birth (age becomes stale and birthday reminders need a date)',
    'home_timezone': 'home timezone',
    'home_location': 'home location',
    'desired_start_date': 'desired coaching start date',
    'preferred_contact_channel': 'preferred contact channel',
    'trainingpeaks_connection_status': 'TrainingPeaks coach-connection status',
}

_COACHING_INTAKE_NOT_ASKED = {
    'health_clearance_status': 'health-clearance status when applicable',
    'coaching_agreement_status': 'coaching agreement receipt',
    'data_consent_status': 'data-use consent receipt',
}


def _coaching_age(questionnaire: dict) -> int | None:
    """Return the submitted whole-number age without treating it as verified."""
    try:
        age = int(str((questionnaire or {}).get('age', '')).strip())
    except (TypeError, ValueError):
        return None
    return age if 0 < age < 130 else None


def _coaching_is_minor(case_or_questionnaire: dict) -> bool:
    questionnaire = case_or_questionnaire.get(
        'questionnaire', case_or_questionnaire)
    age = _coaching_age(questionnaire)
    return age is not None and age < 18


def _coaching_intake_audit(questionnaire: dict, brand: str = DEFAULT_BRAND) -> dict:
    """Separate incomplete, unasked, and self-reported facts for coach review."""
    questionnaire = questionnaire if isinstance(questionnaire, dict) else {}
    required = (_COACHING_INTAKE_REQUIRED_XC
                if normalize_brand(brand) == 'xcskilabs'
                else _COACHING_INTAKE_REQUIRED)

    def _blank(value) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (list, tuple, dict)):
            return not value
        return False

    missing_required = [
        label for field, label in required.items()
        if _blank(questionnaire.get(field))
    ]
    age = _coaching_age(questionnaire)
    if age is not None and age < 13:
        missing_required.append(
            'athlete must be at least 13 for this intake path')
    if age is not None and age < 18:
        for field, label in (
            ('guardian_name', 'parent/guardian full name'),
            ('guardian_email', 'parent/guardian email'),
            ('guardian_relationship', 'parent/guardian relationship'),
        ):
            if _blank(questionnaire.get(field)):
                missing_required.append(label)
    if questionnaire.get('primary_goal') == 'specific_race':
        goal_fields = (
            (('target_race', 'target race'),
             ('goal_details', 'definition of race success'))
            if normalize_brand(brand) == 'xcskilabs'
            else (('race_list', 'target race list'),
                  ('success_definition', 'definition of race success'))
        )
        for field, label in goal_fields:
            if _blank(questionnaire.get(field)):
                missing_required.append(label)

    missing_followup = [
        label for field, label in _COACHING_INTAKE_FOLLOWUP.items()
        if _blank(questionnaire.get(field))
    ]

    unasked = [
        label for field, label in _COACHING_INTAKE_NOT_ASKED.items()
        if _blank(questionnaire.get(field))
    ]
    if age is not None and age < 18:
        unasked.append('parent/guardian consent receipt')
    unasked.append('coach decision on any private setup-fee waiver')
    health_disclosure_present = any(
        not _blank(questionnaire.get(field))
        for field in ('injuries', 'past_injuries', 'medical_conditions', 'medications')
    )
    unverified = [
        'identity and age',
        'training history and current workload',
        'FTP, heart-rate, HRV, sleep, and recovery baselines',
        'availability and life constraints',
        'target events and outcome goals',
        'TrainingPeaks coach connection',
        'TrainingPeaks Premium activation',
        'Stripe payment and any case-specific setup-fee waiver',
    ]

    return {
        'schema': 'coaching_intake_audit/v1',
        # Compatibility alias: historically `missing` meant required intake data.
        'missing': missing_required,
        'missing_required': missing_required,
        'missing_followup': missing_followup,
        'unasked': unasked,
        'unverified': unverified,
        'gates': {
            'intake_completeness': (
                'blocked' if missing_required else
                ('needs_follow_up' if missing_followup else 'ready_for_fit_review')
            ),
            'fit_review': 'pending_coach_review',
            'health_clearance': (
                'review_disclosure' if health_disclosure_present else 'not_collected'),
            'coaching_agreement': 'not_collected',
            'data_consent': 'not_collected',
            'guardian_consent': (
                'not_collected' if age is not None and age < 18
                else 'not_applicable'),
            'payment': 'not_started',
            'trainingpeaks_connection': 'unverified',
            'trainingpeaks_premium': 'unverified',
            'plan_release': 'blocked',
        },
    }


_COACHING_VERIFICATION_RULES = {
    'identity': {'verified'},
    'health_clearance': {'cleared', 'not_required'},
    'coaching_agreement': {'signed'},
    'data_consent': {'signed'},
    'guardian_consent': {'signed'},
    'setup_fee_waiver': {'approved'},
    'trainingpeaks_connection': {'verified'},
    'trainingpeaks_premium': {'active'},
    'athlete_context': {'sealed'},
    'plan_draft': {'ready'},
    'coach_plan_approval': {'approved'},
    'onboarding_ramp': {'complete'},
}

_COACHING_PAYMENT_PREREQUISITES = (
    ('coach_fit', 'approved', 'coach fit approval'),
    ('identity', 'verified', 'identity verification'),
    ('health_clearance', ('cleared', 'not_required'), 'health-clearance disposition'),
    ('coaching_agreement', 'signed', 'signed coaching agreement receipt'),
    ('data_consent', 'signed', 'signed data-use consent receipt'),
)

_COACHING_RELEASE_PREREQUISITES = _COACHING_PAYMENT_PREREQUISITES + (
    ('payment', 'confirmed', 'Stripe payment confirmation'),
    ('trainingpeaks_connection', 'verified', 'TrainingPeaks coach connection'),
    ('trainingpeaks_premium', 'active', 'TrainingPeaks Premium activation'),
    ('athlete_context', 'sealed', 'sealed athlete context'),
    ('plan_draft', 'ready', 'plan draft'),
    ('coach_plan_approval', 'approved', 'coach plan approval'),
    ('onboarding_materials', 'ready', 'athlete onboarding materials'),
)


def _coaching_gate_status(case: dict, gate: str) -> str:
    if gate == 'payment':
        if not case.get('receipts', {}).get('stripe_payment'):
            return 'pending'
        standing = str((case.get('billing') or {}).get('standing') or 'healthy')
        if standing in ('healthy', 'trialing', 'canceling_at_period_end'):
            return 'confirmed'
        return standing
    if gate == 'guardian_consent' and not _coaching_is_minor(case):
        return 'not_applicable'
    if gate == 'onboarding_materials':
        return ('ready' if case.get('onboarding_materials', {}).get('delivered_at')
                else 'pending')
    return str(case.get('verifications', {}).get(gate, {}).get('status') or 'pending')


def _coaching_payment_prerequisites(case: dict) -> tuple:
    prerequisites = _COACHING_PAYMENT_PREREQUISITES
    if _coaching_is_minor(case):
        prerequisites += ((
            'guardian_consent', 'signed',
            'signed parent/guardian consent receipt'),)
    return prerequisites


def _coaching_release_prerequisites(case: dict) -> tuple:
    prerequisites = _COACHING_RELEASE_PREREQUISITES
    if _coaching_is_minor(case):
        prerequisites += ((
            'guardian_consent', 'signed',
            'signed parent/guardian consent receipt'),)
    return prerequisites


def _coaching_blockers(case: dict, prerequisites: tuple) -> list[str]:
    blockers = []
    for gate, expected, label in prerequisites:
        actual = _coaching_gate_status(case, gate)
        accepted = expected if isinstance(expected, tuple) else (expected,)
        if actual not in accepted:
            blockers.append(label)
    return blockers


def _coaching_case_readiness(case: dict) -> dict:
    """Derive the next action from evidence; never trust a mutable state alone."""
    payment_blockers = _coaching_blockers(
        case, _coaching_payment_prerequisites(case))
    release_blockers = _coaching_blockers(
        case, _coaching_release_prerequisites(case))
    statuses = {
        gate: _coaching_gate_status(case, gate)
        for gate in ('coach_fit', 'identity', 'health_clearance',
                     'coaching_agreement', 'data_consent', 'guardian_consent',
                     'setup_fee_waiver', 'payment',
                     'trainingpeaks_connection', 'trainingpeaks_premium',
                     'athlete_context', 'plan_draft', 'coach_plan_approval',
                     'onboarding_materials', 'onboarding_ramp')
    }

    if statuses['coach_fit'] != 'approved':
        state, next_action = 'FIT_REVIEW', 'Coach reviews fit and scope'
    elif statuses['identity'] != 'verified':
        state, next_action = 'IDENTITY_REVIEW', 'Verify athlete identity'
    elif statuses['health_clearance'] not in ('cleared', 'not_required'):
        state, next_action = 'HEALTH_REVIEW', 'Record health-clearance disposition'
    elif (_coaching_is_minor(case) and
          statuses['guardian_consent'] != 'signed'):
        state, next_action = (
            'GUARDIAN_CONSENT_PENDING',
            'Collect signed parent/guardian consent')
    elif statuses['coaching_agreement'] != 'signed' or statuses['data_consent'] != 'signed':
        state, next_action = 'TERMS_PENDING', 'Collect signed agreement and data consent'
    elif statuses['payment'] in ('past_due', 'action_required', 'unpaid', 'incomplete'):
        state, next_action = (
            'BILLING_ACTION_REQUIRED',
            'Resolve the Stripe billing issue before releasing more service')
    elif statuses['payment'] in ('ended', 'canceled', 'paused'):
        state, next_action = (
            'SUBSCRIPTION_ENDED',
            'Confirm the end of service or a new approved subscription')
    elif statuses['payment'] != 'confirmed':
        if case.get('checkout', {}).get('url'):
            state, next_action = 'PAYMENT_PENDING', 'Await Stripe webhook confirmation'
        else:
            state, next_action = 'PAYMENT_READY', 'Create approved payment handoff'
    elif (statuses['trainingpeaks_connection'] != 'verified' or
          statuses['trainingpeaks_premium'] != 'active'):
        state, next_action = 'PLATFORM_SETUP', 'Verify TrainingPeaks connection and Premium'
    elif statuses['athlete_context'] != 'sealed':
        state, next_action = 'CONTEXT_SEAL', 'Seal athlete_context/v1'
    elif statuses['plan_draft'] != 'ready':
        state, next_action = 'PLAN_DRAFT', 'Prepare initial plan proposal'
    elif statuses['coach_plan_approval'] != 'approved':
        state, next_action = 'PLAN_APPROVAL', 'Coach reviews and approves the plan'
    elif statuses['onboarding_materials'] != 'ready':
        state, next_action = (
            'ONBOARDING_MATERIALS', 'Generate athlete onboarding materials')
    elif statuses['onboarding_ramp'] != 'complete':
        state, next_action = 'ONBOARDING_RAMP', 'Complete first-30-day ramp'
    else:
        state, next_action = 'ACTIVE', 'Continue active coaching lifecycle'

    return {
        'schema': 'coaching_onboarding_readiness/v1',
        'state': state,
        'next_action': next_action,
        'gate_statuses': statuses,
        'payment_allowed': not payment_blockers,
        'payment_blockers': payment_blockers,
        'plan_release_allowed': not release_blockers,
        'plan_release_blockers': release_blockers,
    }


def _coaching_esign_readiness(case: dict) -> dict:
    """Describe the configured SignWell/legal boundary without issuing it."""
    minor = _coaching_is_minor(case)
    required = {
        'COACHING_ESIGN_PROVIDER': os.environ.get(
            'COACHING_ESIGN_PROVIDER', COACHING_ESIGN_PROVIDER),
        'COACHING_LEGAL_APPROVAL_RECEIPT': os.environ.get(
            'COACHING_LEGAL_APPROVAL_RECEIPT', ''),
        'COACHING_AGREEMENT_TEMPLATE_ID': os.environ.get(
            'COACHING_AGREEMENT_TEMPLATE_ID', ''),
        'COACHING_AGREEMENT_TEMPLATE_VERSION': os.environ.get(
            'COACHING_AGREEMENT_TEMPLATE_VERSION', ''),
        'COACHING_DATA_CONSENT_TEMPLATE_ID': os.environ.get(
            'COACHING_DATA_CONSENT_TEMPLATE_ID', ''),
        'COACHING_DATA_CONSENT_TEMPLATE_VERSION': os.environ.get(
            'COACHING_DATA_CONSENT_TEMPLATE_VERSION', ''),
    }
    if minor:
        required.update({
            'COACHING_GUARDIAN_CONSENT_TEMPLATE_ID': os.environ.get(
                'COACHING_GUARDIAN_CONSENT_TEMPLATE_ID', ''),
            'COACHING_GUARDIAN_CONSENT_TEMPLATE_VERSION': os.environ.get(
                'COACHING_GUARDIAN_CONSENT_TEMPLATE_VERSION', ''),
        })
    provider = str(required.get('COACHING_ESIGN_PROVIDER') or '').strip().lower()
    if provider == 'signwell':
        required.update({
            'SIGNWELL_API_KEY': os.environ.get(
                'SIGNWELL_API_KEY', SIGNWELL_API_KEY),
            'SIGNWELL_WEBHOOK_ID': os.environ.get(
                'SIGNWELL_WEBHOOK_ID', SIGNWELL_WEBHOOK_ID),
        })
    missing = sorted(key for key, value in required.items() if not str(value).strip())
    adapter_enabled = provider in {'signwell', 'manual_receipt'}
    blockers = list(missing)
    if provider and not adapter_enabled:
        blockers.append(f'provider_adapter_not_implemented:{provider}')
    if provider == 'signwell':
        template_keys = [
            key for key in required
            if key.endswith('_TEMPLATE_ID') and required.get(key)
        ]
        for key in template_keys:
            try:
                uuid.UUID(str(required[key]))
            except (ValueError, TypeError, AttributeError):
                blockers.append(f'invalid_signwell_template_id:{key}')
    if provider == 'signwell' and IS_PRODUCTION:
        if not SIGNWELL_LIVE_SEND_ENABLED:
            blockers.append('SIGNWELL_LIVE_SEND_ENABLED')
        if SIGNWELL_TEST_MODE:
            blockers.append('SIGNWELL_TEST_MODE_must_be_false_in_production')
    return {
        'schema': 'coaching_esign_readiness/v1',
        'status': 'ready' if not blockers and adapter_enabled else 'blocked',
        'provider': provider or 'disabled',
        'minor_packet_required': minor,
        'missing_configuration': missing,
        'blockers': blockers,
        'manual_receipt_route': f"/api/coaching-intakes/{case.get('case_id')}/verify",
        'test_mode': SIGNWELL_TEST_MODE if provider == 'signwell' else None,
        'automatic_reminders': (
            SIGNWELL_REMINDERS_ENABLED if provider == 'signwell' else False),
        'issuance_side_effects': (
            'sends_provider_signature_request_when_operator_posts'
            if provider == 'signwell' else 'none'),
    }


def _signwell_template_contract(case: dict) -> list[dict]:
    templates = [
        {
            'gate': 'coaching_agreement',
            'template_id': os.environ.get('COACHING_AGREEMENT_TEMPLATE_ID', ''),
            'document_version': os.environ.get(
                'COACHING_AGREEMENT_TEMPLATE_VERSION', ''),
        },
        {
            'gate': 'data_consent',
            'template_id': os.environ.get('COACHING_DATA_CONSENT_TEMPLATE_ID', ''),
            'document_version': os.environ.get(
                'COACHING_DATA_CONSENT_TEMPLATE_VERSION', ''),
        },
    ]
    if _coaching_is_minor(case):
        templates.append({
            'gate': 'guardian_consent',
            'template_id': os.environ.get(
                'COACHING_GUARDIAN_CONSENT_TEMPLATE_ID', ''),
            'document_version': os.environ.get(
                'COACHING_GUARDIAN_CONSENT_TEMPLATE_VERSION', ''),
        })
    return templates


def _signwell_expected_recipients(case: dict) -> list[dict]:
    athlete = case.get('athlete') or {}
    recipient_auth = {
        'enabled': True,
        'methods': ['email'],
        'expire_after_access': True,
    }
    recipients = []
    if _coaching_is_minor(case):
        guardian = case.get('guardian') or {}
        recipients.append({
            'id': 'guardian',
            'placeholder_name': os.environ.get(
                'SIGNWELL_GUARDIAN_PLACEHOLDER', 'Guardian'),
            'name': str(guardian.get('name') or ''),
            'email': str(guardian.get('email') or '').lower(),
            'delivery_method': 'email',
            'passcode_delivery': dict(recipient_auth),
        })
    recipients.append({
        'id': 'athlete',
        'placeholder_name': os.environ.get(
            'SIGNWELL_ATHLETE_PLACEHOLDER', 'Athlete'),
        'name': str(athlete.get('name') or ''),
        'email': str(athlete.get('email') or '').lower(),
        'delivery_method': 'email',
        'passcode_delivery': dict(recipient_auth),
    })
    return recipients


def _build_signwell_packet_request(case: dict) -> dict:
    templates = _signwell_template_contract(case)
    template_ids = list(dict.fromkeys(
        item['template_id'] for item in templates if item['template_id']))
    brand_cfg = _brand_config(case.get('brand'))
    payload = {
        'test_mode': SIGNWELL_TEST_MODE,
        'template_ids': template_ids,
        'name': f"{brand_cfg['name']} coaching onboarding {case['case_id'][:8]}",
        'recipients': _signwell_expected_recipients(case),
        'draft': False,
        'expires_in': 30,
        'reminders': SIGNWELL_REMINDERS_ENABLED,
        'apply_signing_order': _coaching_is_minor(case),
        'embedded_signing': False,
        'allow_decline': True,
        'allow_reassign': False,
        'custom_requester_name': brand_cfg['name'],
        'metadata': {
            'case_id': case['case_id'],
            'brand': normalize_brand(case.get('brand')),
            'tier': str(case.get('tier') or ''),
            'legal_approval_receipt': os.environ.get(
                'COACHING_LEGAL_APPROVAL_RECEIPT', ''),
            'template_versions': '|'.join(
                f"{item['gate']}:{item['document_version']}"
                for item in templates),
        },
    }
    requester_email = str(os.environ.get('SIGNWELL_REQUESTER_EMAIL') or '').strip()
    if requester_email and '@' in requester_email:
        payload['custom_requester_email'] = requester_email
    return payload


def _find_coaching_case_for_signwell(document_id: str,
                                     case_id: str = '') -> dict:
    if case_id:
        candidate = _read_coaching_intake(case_id)
        if (candidate and
                str((candidate.get('esign_packet') or {}).get('document_id') or '') ==
                document_id):
            return candidate
    matches = [
        case for case in (_iter_coaching_intakes() or ())
        if str((case.get('esign_packet') or {}).get('document_id') or '') ==
        document_id
    ]
    return matches[0] if len(matches) == 1 else {}


def _store_signwell_completed_pdf(case_id: str, document_id: str,
                                  content: bytes) -> tuple[str, str]:
    uuid.UUID(case_id)
    uuid.UUID(document_id)
    root = Path(DATA_DIR) / 'coaching_esign' / case_id
    root.mkdir(parents=True, exist_ok=True)
    path = root / f'{document_id}.pdf'
    digest = hashlib.sha256(content).hexdigest()
    if path.exists():
        existing = path.read_bytes()
        if hashlib.sha256(existing).hexdigest() != digest:
            raise SignWellError('Stored SignWell PDF hash mismatch')
    else:
        tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
        with open(tmp, 'wb') as handle:
            os.chmod(tmp, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    return str(path.relative_to(Path(DATA_DIR))), digest


def _validate_signwell_readback(case: dict, document: dict,
                                document_id: str) -> None:
    if str(document.get('id') or '') != document_id:
        raise SignWellError('SignWell document identity mismatch')
    if str(document.get('status') or '').lower() != 'completed':
        raise SignWellError('SignWell document is not completed')
    metadata = document.get('metadata') or {}
    if str(metadata.get('case_id') or '') != case.get('case_id'):
        raise SignWellError('SignWell case metadata mismatch')
    expected_templates = {
        item['template_id'] for item in _signwell_template_contract(case)
        if item['template_id']
    }
    actual_template_values = document.get('template_ids') or []
    if not actual_template_values and document.get('template_id'):
        actual_template_values = [document['template_id']]
    actual_templates = set(actual_template_values)
    if expected_templates != actual_templates:
        raise SignWellError('SignWell template-version binding mismatch')
    expected_emails = {
        item['email'] for item in _signwell_expected_recipients(case)
    }
    recipients = document.get('recipients') or []
    actual_emails = {
        str(item.get('email') or '').lower() for item in recipients
    }
    if expected_emails != actual_emails:
        raise SignWellError('SignWell signer identity mismatch')
    if any(str(item.get('status') or '').lower() not in ('completed', 'signed')
           for item in recipients):
        raise SignWellError('Not every SignWell signer is complete')


def _record_signwell_completion(case: dict, document: dict,
                                document_id: str, relative_path: str,
                                pdf_sha256: str, completed_at: str) -> None:
    packet = case.setdefault('esign_packet', {})
    packet.update({
        'status': 'completed',
        'completed_at': completed_at,
        'signed_document_path': relative_path,
        'signed_document_sha256': pdf_sha256,
        'audit_page_included': True,
    })
    recipient_by_email = {
        str(item.get('email') or '').lower(): item
        for item in (document.get('recipients') or [])
    }
    athlete = case.get('athlete') or {}
    guardian = case.get('guardian') or {}
    for template in _signwell_template_contract(case):
        gate = template['gate']
        signer = guardian if gate == 'guardian_consent' else athlete
        signer_email = str(signer.get('email') or '').lower()
        provider_signer = recipient_by_email.get(signer_email) or {}
        receipt = {
            'status': 'signed',
            'verified_at': completed_at,
            'actor': 'signwell_webhook',
            'source_id': document_id,
            'provider': 'signwell',
            'document_version': template['document_version'],
            'template_id': template['template_id'],
            'receipt_id': f'signwell:{document_id}:{gate}',
            'signer_name': str(signer.get('name') or ''),
            'signer_email': signer_email,
            'signer_role': (
                str(guardian.get('relationship') or 'legal_guardian')
                if gate == 'guardian_consent' else 'athlete'),
            'provider_signer_id': str(provider_signer.get('id') or ''),
            'signed_document_path': relative_path,
            'signed_document_sha256': pdf_sha256,
            'audit_page_included': True,
            'legal_approval_receipt': os.environ.get(
                'COACHING_LEGAL_APPROVAL_RECEIPT', ''),
        }
        case.setdefault('verifications', {})[gate] = receipt
        case.setdefault('verification_history', []).append({
            'gate': gate, **receipt,
        })


def _refresh_coaching_case(case: dict, *, actor: str, reason: str,
                           source_id: str) -> dict:
    readiness = _coaching_case_readiness(case)
    old_state = case.get('state')
    new_state = readiness['state']
    case['readiness'] = readiness
    case['state'] = new_state
    if old_state != new_state:
        transition_at = datetime.now(timezone.utc).isoformat()
        case.setdefault('transitions', []).append({
            'from_state': old_state,
            'to_state': new_state,
            'actor': actor,
            'timestamp': transition_at,
            'reason': reason,
            'source_id': source_id,
        })
        _record_coaching_event(
            case, 'coaching_state_changed',
            f'{source_id}:{old_state or "none"}:{new_state}',
            details={'from_state': old_state, 'to_state': new_state},
            occurred_at=transition_at)
        if new_state == 'ACTIVE':
            _record_coaching_event(
                case, 'coaching_active', source_id,
                occurred_at=transition_at)
    return readiness


def cleanup_stale_intakes():
    """No-op. Intake files are kept permanently — they're small and needed for retries."""
    pass


# =============================================================================
# PRICE COMPUTATION
# =============================================================================

def compute_plan_price(race_date_str: str) -> dict:
    """Compute plan price based on weeks until A-race.

    Returns dict with weeks, price_cents, price_display.
    $15/week, minimum 4 weeks ($60), capped at $249.
    """
    try:
        race_date = datetime.strptime(race_date_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        # If no valid date, use minimum price
        return {
            'weeks': MIN_WEEKS,
            'price_cents': MIN_WEEKS * PRICE_PER_WEEK_CENTS,
            'price_display': f'${MIN_WEEKS * PRICE_PER_WEEK_CENTS // 100}',
        }

    today = date.today()
    days_until = (race_date - today).days
    weeks = max(MIN_WEEKS, math.ceil(days_until / 7))

    price_cents = min(weeks * PRICE_PER_WEEK_CENTS, PRICE_CAP_CENTS)

    return {
        'weeks': weeks,
        'price_cents': price_cents,
        'price_display': f'${price_cents // 100}',
    }


# =============================================================================
# DATA EXTRACTION
# =============================================================================

def extract_woocommerce_data(data: dict) -> dict:
    """Extract athlete info from WooCommerce order."""
    billing = data.get('billing', {})
    meta = {item['key']: item['value'] for item in data.get('meta_data', [])}
    line_items = data.get('line_items', [])

    # Determine tier from product SKU (more reliable than name)
    tier = 'race_ready'  # default
    for item in line_items:
        sku = item.get('sku', '').lower()
        if sku == 'training-starter':
            tier = 'starter'
        elif sku == 'training-full-build':
            tier = 'full_build'
        elif sku == 'training-race-ready':
            tier = 'race_ready'
        else:
            # Fallback to name matching
            product_name = item.get('name', '').lower()
            if 'starter' in product_name:
                tier = 'starter'
            elif 'full' in product_name and 'build' in product_name:
                tier = 'full_build'

    # Generate athlete ID from name
    first_name = billing.get('first_name', '').strip()
    last_name = billing.get('last_name', '').strip()
    name = f"{first_name} {last_name}".strip()
    athlete_id = sanitize_athlete_id(name)

    raw_delivery_platform = str(
        meta.get('delivery_platform') or meta.get('delivery_target') or 'trainingpeaks'
    ).strip().lower()
    if raw_delivery_platform not in ('trainingpeaks', 'endure', 'manual'):
        raw_delivery_platform = 'trainingpeaks'
    brand = normalize_brand(meta.get('brand'))
    created_raw = str(
        data.get('date_created_gmt') or data.get('date_created') or ''
    ).strip()
    try:
        order_created_at = datetime.fromisoformat(
            created_raw.replace('Z', '+00:00')).astimezone(timezone.utc)
        order_created_at = order_created_at.isoformat().replace('+00:00', 'Z')
    except (ValueError, TypeError):
        order_created_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    return {
        'athlete_id': athlete_id,
        'order_id': str(data.get('id', '')),
        'order_created_at': order_created_at,
        'weeks_purchased': safe_int(meta.get('plan_weeks')),
        'tier': tier,
        'brand': brand,
        'delivery_platform': raw_delivery_platform,
        'delivery_target': raw_delivery_platform,
        'profile': {
            'name': name,
            'email': billing.get('email', '').strip().lower(),
            'brand': brand,
            'age': safe_int(meta.get('age')),
            'fitness_markers': {
                'weight_kg': safe_float(meta.get('weight_kg')),
                'ftp_watts': safe_int(meta.get('ftp_watts')),
            },
            'target_race': {
                'name': meta.get('race_name', ''),
                'date': meta.get('race_date', ''),
                'distance_miles': safe_float(meta.get('race_distance_miles')),
                'elevation_gain_ft': safe_int(meta.get('race_elevation_ft')),
                'terrain': meta.get('race_terrain', 'gravel'),
            },
            'weekly_schedule': {
                'cycling_hours_target': safe_float(meta.get('cycling_hours', 10)),
                'strength_hours': safe_float(meta.get('strength_hours', 2)),
                'preferred_long_day': meta.get('preferred_long_day', 'saturday'),
            },
            'experience_level': meta.get('experience_level', 'intermediate'),
            'race_goal': meta.get('race_goal', 'finish'),
            'limiters': meta.get('limiters', ''),
            'notes': meta.get('notes', ''),
        }
    }


def _woocommerce_meta(data: dict) -> dict:
    """Return the Woo order metadata mapping without interpreting values."""
    return {
        str(item.get('key') or ''): item.get('value')
        for item in data.get('meta_data', [])
        if isinstance(item, dict) and str(item.get('key') or '').strip()
    }


def _woocommerce_list(value, default: list[str]) -> list[str]:
    """Normalize the list-like values Woo custom fields serialize."""
    if isinstance(value, list):
        values = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        values = decoded if isinstance(decoded, list) else text.split(',')
    else:
        values = []
    normalized = [str(item).strip() for item in values if str(item).strip()]
    return normalized or list(default)


def extract_woocommerce_intake(data: dict, order_data: dict) -> dict:
    """Reconstruct the paid questionnaire from complete Woo order metadata.

    Historical Woo orders carry only a sparse profile and correctly enter the
    non-releasable STATE_UNAVAILABLE quarantine. A checkout integration that
    persisted the complete questionnaire sets ``intake_complete=true``; only
    then do we invoke the same markdown/pipeline path used by Stripe intake.
    """
    meta = _woocommerce_meta(data)
    complete = str(meta.get('intake_complete') or '').strip().lower()
    if complete not in {'1', 'true', 'yes'}:
        return {}

    profile = order_data.get('profile') or {}
    target = profile.get('target_race') or {}
    schedule = profile.get('weekly_schedule') or {}
    weight_kg = safe_float(meta.get('weight_kg'))
    weight_lbs = round(weight_kg * 2.2046226218, 1) if weight_kg else ''
    race_name = str(target.get('name') or '').strip()
    race_date = str(target.get('date') or '').strip()
    distance = target.get('distance_miles')
    distance_text = f'{distance:g} miles' if isinstance(distance, (int, float)) else str(distance or '')
    goal_map = {
        'finish': 'Finish Strong', 'compete': 'Compete', 'podium': 'Podium',
    }
    goal = goal_map.get(str(profile.get('race_goal') or '').strip().lower(), 'Finish Strong')
    long_day = str(schedule.get('preferred_long_day') or 'saturday').strip()
    return {
        'name': profile.get('name', ''),
        'email': profile.get('email', ''),
        'brand': order_data.get('brand', DEFAULT_BRAND),
        'sex': meta.get('sex', ''),
        'age': profile.get('age'),
        'weight': weight_lbs,
        'height_ft': meta.get('height_ft', ''),
        'height_in': meta.get('height_in', ''),
        'ftp': (profile.get('fitness_markers') or {}).get('ftp_watts'),
        'powerOrHr': meta.get('power_or_hr', 'power'),
        'hr_max': meta.get('hr_max', ''),
        'hr_threshold': meta.get('hr_threshold', ''),
        'hr_resting': meta.get('hr_resting', ''),
        'devices': meta.get('devices', 'power meter, hr strap'),
        'years_cycling': meta.get('years_cycling', '5'),
        'prior_plan_experience': meta.get('prior_plan_experience', '3'),
        'hours_per_week': str(schedule.get('cycling_hours_target') or '8'),
        'trainer_access': meta.get('trainer_access', 'smart trainer'),
        'long_ride_days': _woocommerce_list(
            meta.get('long_ride_days'), [long_day]),
        'interval_days': _woocommerce_list(
            meta.get('interval_days'), ['tuesday', 'thursday']),
        'off_days': _woocommerce_list(meta.get('off_days'), ['monday']),
        'strength_current': meta.get('strength_current', '2x/week'),
        'strength_want': meta.get('strength_want', 'yes'),
        'strength_equipment': meta.get('strength_equipment', 'full gym'),
        'sleep_quality': meta.get('sleep_quality', 'good'),
        'stress_level': meta.get('stress_level', 'moderate'),
        'injuries': meta.get('injuries', 'None'),
        'course_facts_mode': meta.get('course_facts_mode', ''),
        'athlete_timezone': meta.get('athlete_timezone', 'America/Denver'),
        'race_slug': meta.get('race_slug', ''),
        'races': [{
            'name': race_name,
            'slug': meta.get('race_slug', ''),
            'date': race_date,
            'distance': distance_text,
            'priority': 'A',
            'goal': goal,
        }],
    }


def extract_stripe_data(data: dict) -> dict:
    """Extract athlete info from Stripe checkout session.

    If an intake_id is present in metadata, loads the full questionnaire
    data from the intake store (rich data from the form). Otherwise falls
    back to extracting from Stripe metadata (sparse).
    """
    session = data.get('data', {}).get('object', {})
    metadata = session.get('metadata', {})
    customer_details = session.get('customer_details', {})

    # Check for intake data from questionnaire flow
    intake_id = metadata.get('intake_id', '')
    intake_data = load_intake(intake_id) if intake_id else {}

    name = (
        intake_data.get('name')
        or customer_details.get('name', metadata.get('name', 'Unknown'))
    ).strip()
    athlete_id = sanitize_athlete_id(name)

    email = (
        intake_data.get('email')
        or customer_details.get('email', '')
    ).strip().lower()

    # Tier from metadata — computed pricing model uses 'custom' as default
    tier = metadata.get('tier', 'custom')

    # Build profile — intake data provides the rich questionnaire fields
    if intake_data:
        # Convert weight from lbs to kg if provided
        weight_lbs = safe_float(intake_data.get('weight'))
        weight_kg = round(weight_lbs * 0.453592, 1) if weight_lbs else None

        profile = {
            'name': name,
            'email': email,
            'sex': intake_data.get('sex', ''),
            'age': safe_int(intake_data.get('age')),
            'fitness_markers': {
                'weight_kg': weight_kg,
                'ftp_watts': safe_int(intake_data.get('ftp')),
                'hr_max': safe_int(intake_data.get('hr_max')),
                'hr_threshold': safe_int(intake_data.get('hr_threshold')),
                'hr_resting': safe_int(intake_data.get('hr_resting')),
                'power_or_hr': intake_data.get('powerOrHr', ''),
                'pw_ratio': intake_data.get('pwRatio', ''),
            },
            'target_race': {
                'name': intake_data.get('race_name', ''),
                'date': intake_data.get('race_date', ''),
                'distance_miles': intake_data.get('race_distance', ''),
                'goal': intake_data.get('race_goal', ''),
            },
            'races': intake_data.get('races', []),
            'weekly_schedule': {
                'hours_per_week': intake_data.get('hours_per_week', ''),
                'trainer_access': intake_data.get('trainer_access', ''),
                'long_ride_days': intake_data.get('long_ride_days', []),
                'interval_days': intake_data.get('interval_days', []),
                'off_days': intake_data.get('off_days', []),
            },
            'strength': {
                'current': intake_data.get('strength_current', ''),
                'want': intake_data.get('strength_want', ''),
                'equipment': intake_data.get('strength_equipment', ''),
            },
            'experience_level': intake_data.get('years_cycling', ''),
            'sleep_quality': intake_data.get('sleep_quality', ''),
            'stress_level': intake_data.get('stress_level', ''),
            'injuries': intake_data.get('injuries', ''),
            'notes': intake_data.get('notes', ''),
            'blindspots': intake_data.get('blindspots', []),
        }
    else:
        # Sparse fallback from Stripe metadata only
        profile = {
            'name': name,
            'email': email,
            'age': safe_int(metadata.get('age')),
            'fitness_markers': {
                'weight_kg': safe_float(metadata.get('weight_kg')),
                'ftp_watts': safe_int(metadata.get('ftp_watts')),
            },
            'target_race': {
                'name': metadata.get('race_name', ''),
                'date': metadata.get('race_date', ''),
                'distance_miles': safe_float(metadata.get('race_distance_miles')),
                'elevation_gain_ft': safe_int(metadata.get('race_elevation_ft')),
                'terrain': metadata.get('race_terrain', 'gravel'),
            },
            'weekly_schedule': {
                'cycling_hours_target': safe_float(metadata.get('cycling_hours', 10)),
                'strength_hours': safe_float(metadata.get('strength_hours', 2)),
                'preferred_long_day': metadata.get('preferred_long_day', 'saturday'),
            },
            'experience_level': metadata.get('experience_level', 'intermediate'),
            'race_goal': metadata.get('race_goal', 'finish'),
            'limiters': metadata.get('limiters', ''),
            'notes': metadata.get('notes', ''),
        }

    # Carry brand as data. intake_to_plan resolves the race-derived candidate
    # before applying brand authority; forcing discipline here would make the
    # conflict detector unreachable.
    _brand = normalize_brand(metadata.get('brand') or intake_data.get('brand'))
    profile['brand'] = _brand
    profile['discipline_default'] = _brand_config(_brand)['discipline']

    raw_delivery_platform = str(
        metadata.get('delivery_target')
        or os.environ.get('DELIVERY_TARGET_DEFAULT', 'trainingpeaks')
    ).strip().lower()
    if raw_delivery_platform not in ('trainingpeaks', 'endure', 'manual'):
        raw_delivery_platform = 'trainingpeaks'
    created_raw = session.get('created')
    try:
        order_created_at = datetime.fromtimestamp(
            int(created_raw), tz=timezone.utc).isoformat().replace('+00:00', 'Z')
    except (TypeError, ValueError, OSError):
        order_created_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    return {
        'athlete_id': athlete_id,
        'order_id': session.get('id', ''),
        'order_created_at': order_created_at,
        'weeks_purchased': intake_data.get('computed_weeks') or metadata.get('plan_weeks'),
        'tier': tier,
        'brand': _brand,
        # Phase 1 preserves the purchased platform but never pushes before
        # approval. delivery_target remains a compatibility projection.
        'delivery_platform': raw_delivery_platform,
        'delivery_target': raw_delivery_platform,
        'profile': profile,
    }


def safe_int(val):
    """Safely convert to int with bounds checking."""
    try:
        if val is None or val == '':
            return None
        result = int(val)
        # Sanity bounds
        if result < 0 or result > 100000:
            return None
        return result
    except (ValueError, TypeError):
        return None


def safe_float(val):
    """Safely convert to float with bounds checking."""
    try:
        if val is None or val == '':
            return None
        result = float(val)
        # Sanity bounds
        if result < 0 or result > 100000:
            return None
        return result
    except (ValueError, TypeError):
        return None


# =============================================================================
# PROFILE CREATION (with file locking)
# =============================================================================

def create_athlete_profile(order_data: dict) -> tuple:
    """Create athlete profile YAML from order data with atomic write."""
    athlete_id = order_data['athlete_id']
    athlete_dir = Path(ATHLETES_DIR) / athlete_id
    athlete_dir.mkdir(parents=True, exist_ok=True)

    profile = order_data['profile'].copy()
    profile['tier'] = order_data['tier']
    profile['order_id'] = order_data['order_id']
    profile['created_at'] = datetime.now().isoformat()

    profile_path = athlete_dir / 'profile.yaml'
    temp_path = athlete_dir / '.profile.yaml.tmp'

    # Atomic write with file locking
    try:
        with open(temp_path, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            yaml.dump(profile, f, default_flow_style=False, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        # Atomic rename
        temp_path.rename(profile_path)

    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        raise e

    return athlete_id, profile_path


# =============================================================================
# PIPELINE EXECUTION
# =============================================================================

def _questionnaire_to_markdown(intake_data: dict, name: str = '', email: str = '',
                               fulfillment: dict = None) -> str:
    """Convert web questionnaire JSON into the markdown format intake_to_plan.py expects."""
    name = name or intake_data.get('name', 'Unknown Athlete')
    email = email or intake_data.get('email', '')

    # Build race list
    races = intake_data.get('races', [])
    race_lines = []
    for r in races:
        priority = r.get('priority', 'A')
        race_lines.append(f"  {r.get('name', 'Unknown')} ({r.get('date', 'TBD')}, "
                          f"{r.get('distance', '~100 mi')}, priority {priority})")

    # Map long_ride_days/interval_days/off_days
    long_days = ', '.join(intake_data.get('long_ride_days', ['Saturday']))
    interval_days = ', '.join(intake_data.get('interval_days', ['Tuesday', 'Thursday']))
    off_days = ', '.join(intake_data.get('off_days', []))

    # Height
    height_ft = intake_data.get('height_ft', '')
    height_in = intake_data.get('height_in', '')
    height_str = f"{height_ft}'{height_in}\"" if height_ft else ''

    # Goal mapping
    goal_map = {'Survive': 'finish', 'Finish Strong': 'finish', 'Compete': 'compete', 'Podium': 'podium'}
    a_race = next((r for r in races if r.get('priority') == 'A'), races[0] if races else {})
    goal = goal_map.get(a_race.get('goal', ''), 'finish')
    race_format = (a_race.get('race_format') or a_race.get('event_format')
                   or intake_data.get('race_format')
                   or intake_data.get('event_format', ''))
    race_demands = (a_race.get('race_demands')
                    or intake_data.get('race_demands'))
    race_demands_text = (json.dumps(race_demands, sort_keys=True,
                                    separators=(',', ':'))
                         if race_demands is not None else '')
    road_category = (intake_data.get('road_category')
                     or intake_data.get('license_category', ''))

    # The race the customer SELECTED on the site carries its slug — the pipeline
    # resolves the target race by this ID (exact), skipping fuzzy name-matching.
    target_slug = intake_data.get('race_slug') or a_race.get('slug', '') or ''

    # Carry brand separately from its discipline fallback so intake_to_plan can
    # inspect a conflicting race-derived candidate before it forces the output.
    _brand_raw = (intake_data.get('brand') or '').strip().lower()
    _brand = _brand_raw if _brand_raw in BRANDS else ''
    _discipline_hint = _brand_config(_brand)['discipline'] if _brand else ''
    fulfillment = fulfillment or {}

    md = f"""# Athlete Intake: {name}
Email: {email}
Submitted: {datetime.now().strftime('%Y-%m-%d')}

## Basic Info
- Sex: {intake_data.get('sex', 'Male')}
- Age: {intake_data.get('age', '')}
- Weight: {intake_data.get('weight', '')} lbs
- Height: {height_str}

## Goals
- Primary Goal: specific_race
- Brand: {_brand}
- Race Slug: {target_slug}
- Course Facts Mode: {intake_data.get('course_facts_mode', '')}
- Discipline: {_discipline_hint}
- Race Format: {race_format}
- Race Demands: {race_demands_text}
- Road Category: {road_category}
- Races:
{chr(10).join(race_lines)}
- Success: {a_race.get('goal', 'finish')}

## Current Fitness
- FTP: {intake_data.get('ftp', 'unknown')}
- Training Metric: {intake_data.get('powerOrHr', '')}
- HR Max: {intake_data.get('hr_max', '')}
- HR Threshold: {intake_data.get('hr_threshold', '')}
- W/kg: {intake_data.get('pwRatio', '')}
- Years Cycling: {intake_data.get('years_cycling', '3')}
- Years Structured: {intake_data.get('prior_plan_experience', '1')}
- Longest Recent Ride: {intake_data.get('longest_ride', '3-4 hrs')}

## Recovery & Baselines
- Resting HR: {intake_data.get('hr_resting', '')}
- Typical Sleep: {intake_data.get('sleep_quality', '7 hours')}
- Sleep Quality: {intake_data.get('sleep_quality', 'good')}

## Equipment
- Indoor Trainer: {intake_data.get('trainer_access', 'smart trainer')}
- Devices: {intake_data.get('devices') or 'unknown'}

## Schedule
- Weekly Hours Available: {intake_data.get('hours_per_week', '10')}
- Current Volume: {intake_data.get('hours_per_week', '8')}
- Long Ride Days: {long_days}
- Interval Days: {interval_days}
- Off Days: {off_days or 'None'}
- Programmed Midweek Max Minutes: {intake_data.get('programmed_midweek_max_minutes', '')}
- Travel Dates: {intake_data.get('travel_dates', '') or 'None'}

## Strength
- Current: {intake_data.get('strength_current', 'none')}
- Include: {intake_data.get('strength_want', 'no')}
- Equipment: {intake_data.get('strength_equipment', 'minimal')}

## Health
- Current Injuries: {intake_data.get('injuries', 'None')}

## Work & Life
- Life Stress: {intake_data.get('stress_level', 'moderate')}

## Nutrition
- Training Fuel: {intake_data.get('training_fuel', intake_data.get('current_carbs_g_per_hour', ''))}

## Additional
- Notes: {intake_data.get('notes', '')}

## Fulfillment
- Order ID: {fulfillment.get('order_id', '')}
- Delivery Platform: {fulfillment.get('delivery_platform', 'manual')}
- Order Created At: {fulfillment.get('order_created_at', '')}
- Generation At: {fulfillment.get('generation_at', datetime.now().isoformat())}
- Effective Date: {fulfillment.get('effective_date', '')}
- Planning Horizon End: {fulfillment.get('planning_horizon_end', '')}
- Publication Horizon Weeks: {fulfillment.get('publication_horizon_weeks', '')}
- Weeks Purchased: {fulfillment.get('weeks_purchased', '')}
- Athlete Timezone: {fulfillment.get('athlete_timezone', intake_data.get('athlete_timezone', ''))}
"""
    return md


def run_pipeline(athlete_id: str, deliver: bool = True, intake_data: dict = None,
                 order_data: dict = None) -> dict:
    """Run the full training plan pipeline via intake_to_plan.py."""
    script_path = Path(SCRIPTS_DIR) / 'intake_to_plan.py'

    # The historical no-intake fallback invoked generate_full_package.py with
    # --deliver, exposing guides and ZWOs outside order/revision/seal authority.
    # Missing intake is recoverable by the coach, but it is never releasable.
    if not intake_data:
        message = (
            'No intake was attached; legacy delivery is disabled because it '
            'cannot produce a seal-bound order revision. Coach action is required.'
        )
        logger.error(f"Refusing no-intake pipeline for {athlete_id}: {message}")
        return {'success': False, 'stdout': '', 'stderr': message,
                'fulfillment_state': 'unavailable', 'artifact_dir': None}
    if not script_path.exists():
        return {
            'success': False,
            'stdout': '',
            'stderr': f'Pipeline script not found: {script_path}'
        }
    cmd = ['python3', str(script_path)]
    logger.info(f"Running intake pipeline for {athlete_id}")

    # Each order gets a private generation root. Athlete slugs are labels, not
    # persistence keys; sharing the historical athletes/<slug> directory lets
    # repeat/concurrent orders overwrite one another.
    pipeline_env = {**os.environ, 'GG_AUTO_EMAIL': 'true'}
    if intake_data.get('generation_clock'):
        pipeline_env['GG_FIXED_NOW'] = str(intake_data['generation_clock'])
    work_athletes_dir = None
    if intake_data and order_data and order_data.get('order_id'):
        work_athletes_dir = (Path(DATA_DIR) / 'order-work'
                             / _safe_order_id(order_data['order_id']) / 'athletes')
        work_athletes_dir.mkdir(parents=True, exist_ok=True)
        pipeline_env['GG_ATHLETES_BASE_DIR'] = str(work_athletes_dir)
        pipeline_env['GG_DELIVERY_DIR'] = str(work_athletes_dir.parent / 'review')

    # Generate markdown input for intake pipeline
    stdin_data = None
    if intake_data:
        name = intake_data.get('name', '')
        email = intake_data.get('email', '')
        order_data = order_data or {}
        stdin_data = _questionnaire_to_markdown(
            intake_data, name=name, email=email,
            fulfillment={
                'order_id': order_data.get('order_id', ''),
                'delivery_platform': order_data.get(
                    'delivery_platform', order_data.get('delivery_target', 'manual')),
                'order_created_at': order_data.get('order_created_at', ''),
                'generation_at': intake_data.get('generation_clock') or datetime.now().isoformat(),
                'weeks_purchased': order_data.get(
                    'weeks_purchased', intake_data.get('computed_weeks', '')),
                'athlete_timezone': intake_data.get('athlete_timezone', ''),
            })
        logger.info(f"Generated {len(stdin_data)} char markdown intake for {athlete_id}")

    try:
        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=PIPELINE_TIMEOUT,
            cwd=SCRIPTS_DIR,
            env=pipeline_env
        )

        success = result.returncode == 0
        if success:
            logger.info(f"Pipeline succeeded for {athlete_id}")
        else:
            logger.error(
                f"Pipeline failed for {athlete_id}: "
                f"{_pipeline_error_excerpt({'stderr': result.stderr, 'stdout': result.stdout})}"
            )

        artifact_dir = None
        if work_athletes_dir and work_athletes_dir.exists():
            candidates = [path for path in work_athletes_dir.iterdir()
                          if path.is_dir()]
            generated = [path for path in candidates
                         if (path / 'fulfillment_status.json').exists()]
            if len(generated) == 1:
                artifact_dir = str(generated[0])
            elif generated:
                exact = [path for path in generated
                         if path.name.replace('-', '_') == athlete_id.replace('-', '_')]
                artifact_dir = str(exact[0]) if len(exact) == 1 else None

        return {
            'success': success,
            'stdout': result.stdout,
            'stderr': result.stderr,
            'fulfillment_state': (
                'unavailable' if 'GG_FULFILLMENT_STATE=unavailable' in result.stdout
                else 'available'
            ),
            'artifact_dir': artifact_dir,
        }

    except subprocess.TimeoutExpired:
        logger.error(f"Pipeline timeout for {athlete_id}")
        return {
            'success': False,
            'stdout': '',
            'stderr': f'Pipeline timed out after {PIPELINE_TIMEOUT}s'
        }
    except subprocess.SubprocessError as e:
        logger.error(f"Pipeline subprocess error for {athlete_id}: {e}")
        return {
            'success': False,
            'stdout': '',
            'stderr': str(e)
        }


# =============================================================================
# DELIVERY PERSISTENCE — zip deliverables to persistent volume
# =============================================================================

# Files the customer gets (order matters for zip listing)
CUSTOMER_DELIVERABLES = [
    'training_guide.html',
    'training_guide.pdf',
    'dashboard.html',
    'plan_preview.html',
    'fueling.yaml',
]
# Review-bundle files are human-readable and non-executable by construction.
REVIEW_DELIVERABLES = [
    'plan_preview.html',
    'coaching_brief.md',
    'training_guide.html',
    'plan_summary.yaml',
    'fueling.yaml',
]
# Private generated inputs retained for audit/sealing but never exposed in the
# review bundle.
PRIVATE_DELIVERABLES = [
    'coaching_brief.md',
    'personal_email.md',
    'plan_summary.yaml',
    'profile.yaml',
    'plan_dates.yaml',
    'methodology.yaml',
    'derived.yaml',
    'intake_backup.json',
    'fulfillment_manifest.json',
    'plan_ir.json',
    'tp_manifest.json',
    'canonical_training_model.json',
    'apply_contract.json',
]


def _safe_order_id(order_id: str) -> str:
    value = str(order_id or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('invalid order_id')
    return value


def _orders_root() -> Path:
    return Path(DELIVERIES_DIR) / 'orders'


def _order_dir(order_id: str) -> Path:
    return _orders_root() / _safe_order_id(order_id)


def _order_lookup_path(athlete_id: str) -> Path:
    return Path(DELIVERIES_DIR) / 'athlete-lookups' / f'{_normalize_athlete_id(athlete_id)}.json'


def _record_order_lookup(order_id: str, athlete_id: str) -> None:
    path = _order_lookup_path(athlete_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix('.lock')
    with open(lock_path, 'a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            current = json.loads(path.read_text()) if path.exists() else {'order_ids': []}
        except (OSError, json.JSONDecodeError):
            current = {'order_ids': []}
        current['athlete_id'] = _normalize_athlete_id(athlete_id)
        current['order_ids'] = sorted(set(current.get('order_ids', []) + [order_id]))
        tmp = path.with_name(f'.{path.name}.tmp')
        tmp.write_text(json.dumps(current, indent=2, sort_keys=True) + '\n')
        os.replace(tmp, path)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _legacy_ledger_candidates(athlete_id: str) -> list[str]:
    processed_path = Path(DATA_DIR) / '.processed_orders.json'
    try:
        processed = json.loads(processed_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return sorted(
        order for order, entry in processed.items()
        if _normalize_athlete_id(str(entry.get('athlete_id') or ''))
        == _normalize_athlete_id(athlete_id)
    )


def _migrate_legacy_path(legacy_path: Path) -> dict | None:
    """Recoverably migrate/tombstone one old athlete-keyed state file."""
    raw = json.loads(legacy_path.read_text())
    if raw.get('schema_version') == 'tombstone/v1':
        migrated = Path(str(raw.get('migrated_to') or ''))
        if migrated.exists():
            state = load_fulfillment_state(migrated)
            _record_order_lookup(state['order_id'], state['athlete_id'])
            return state
        return None
    if raw.get('schema_version') != 1:
        return None
    _, state = migrate_v1_to_quarantine(
        legacy_path, _orders_root(),
        ledger_candidates=_legacy_ledger_candidates(
            str(raw.get('athlete_id') or legacy_path.parent.name)),
    )
    _record_order_lookup(state['order_id'], state['athlete_id'])
    return state


def migrate_all_v1_states() -> dict:
    """Startup-complete migration of every old athlete-keyed v1 state."""
    stats = {'migrated': 0, 'tombstones_verified': 0, 'failed': 0}
    deliveries = Path(DELIVERIES_DIR)
    if not deliveries.exists():
        return stats
    for legacy_path in sorted(deliveries.glob('*/fulfillment_status.json')):
        try:
            raw = json.loads(legacy_path.read_text())
            version = raw.get('schema_version')
            if version not in (1, 'tombstone/v1'):
                continue
            state = _migrate_legacy_path(legacy_path)
            if state:
                key = 'migrated' if version == 1 else 'tombstones_verified'
                stats[key] += 1
        except (OSError, json.JSONDecodeError, FulfillmentStateError) as exc:
            stats['failed'] += 1
            logger.error(
                f'Legacy fulfillment migration failed closed for {legacy_path}: {exc}')
    return stats


def _resolve_order_id(ref: str) -> str | None:
    try:
        direct = _order_dir(ref)
    except ValueError:
        return None
    if (direct / 'fulfillment_status.json').exists():
        return ref

    # Examine the old athlete path before trusting an existing v2 lookup. A
    # repeat customer's newer lookup must never shadow an unmigrated v1 file.
    legacy_path = (Path(DELIVERIES_DIR) / _normalize_athlete_id(ref)
                   / 'fulfillment_status.json')
    if legacy_path.exists():
        try:
            _migrate_legacy_path(legacy_path)
        except (OSError, json.JSONDecodeError, FulfillmentStateError) as exc:
            logger.error(f'Legacy fulfillment migration failed closed for {ref}: {exc}')
            return None

    lookup = _order_lookup_path(ref)
    try:
        order_ids = (json.loads(lookup.read_text()).get('order_ids') or [])
    except (OSError, json.JSONDecodeError):
        order_ids = []
    if len(order_ids) == 1:
        return order_ids[0]

    return None


def _resolve_review_order_id(ref: str) -> str | None:
    """Resolve only an explicit order id without migration or lookup writes."""
    try:
        direct = _order_dir(ref)
    except ValueError:
        return None
    return ref if (direct / 'fulfillment_status.json').is_file() else None


def _resolve_generated_athlete_dir(athlete_id: str) -> Path:
    """Return the directory containing generated plan artifacts.

    The webhook creates an underscore ID (``example_athlete``), while
    ``intake_to_plan.py`` normalizes the generated package to a hyphenated ID
    (``example-athlete``).  When both exist, the directory with generation
    markers is authoritative; otherwise persistence can silently copy the
    webhook's pre-generation ``profile.yaml`` into the coach package.
    """
    athletes_root = Path(ATHLETES_DIR)
    exact = athletes_root / athlete_id
    alternate = athletes_root / athlete_id.replace('_', '-')
    candidates = [exact]
    if alternate != exact:
        candidates.append(alternate)

    generation_markers = (
        'workouts', 'derived.yaml', 'plan_summary.yaml', 'training_guide.html',
    )
    generated = [
        directory for directory in candidates
        if directory.is_dir()
        and any((directory / marker).exists() for marker in generation_markers)
    ]
    if generated:
        return generated[0]

    for directory in candidates:
        if directory.is_dir():
            return directory

    for directory in athletes_root.iterdir():
        if (directory.is_dir()
                and directory.name.replace('-', '_')
                == athlete_id.replace('-', '_')):
            return directory
    return exact


def persist_deliverables(order_id: str, athlete_id: str = '', source_dir: Path | str = None,
                         delivery_platform: str = 'manual',
                         state_unavailable: bool = False) -> dict:
    """Persist one immutable order revision, review bundle, and gated release."""
    order_id = _safe_order_id(order_id)
    athlete_id = athlete_id or order_id
    # Prefer the directory that contains generated plan artifacts when both
    # webhook (underscore) and intake pipeline (hyphen) IDs exist.
    athlete_dir = Path(source_dir) if source_dir else _resolve_generated_athlete_dir(athlete_id)
    source_state_path = athlete_dir / 'fulfillment_status.json'
    order_root = _order_dir(order_id)
    order_root.mkdir(parents=True, exist_ok=True)
    state_path = order_root / 'fulfillment_status.json'

    existing_state = None
    if state_path.exists():
        existing_state = load_fulfillment_state(state_path)

    if state_unavailable:
        state = write_generation(state_path, athlete_id, [{
            'id': 'STATE_UNAVAILABLE', 'source': 'webhook',
            'severity': 'CRITICAL',
            'message': 'Pipeline state was unavailable; repair and regenerate before release.',
            'review_value': {'state_available': False, 'release_allowed': False},
            'basis': 'durable pipeline-state load during order persistence',
            'sensitivity': 'internal',
        }], order_id=order_id, delivery_platform=delivery_platform)
    else:
        state = load_fulfillment_state(source_state_path)
        if state['order_id'] != order_id:
            raise FulfillmentStateError('generated state order_id mismatch')
        if state['delivery_platform'] != delivery_platform:
            raise FulfillmentStateError('generated state delivery_platform mismatch')
        if (existing_state
                and existing_state['generation_revision'] == state['generation_revision']
                and existing_state.get('model_seal')):
            raise FulfillmentStateError(
                'sealed revision already exists; call write_generation before persisting corrections'
            )
        shutil.copy2(source_state_path, state_path)
        state = load_fulfillment_state(state_path)

    revision = state['generation_revision']
    revision_dir = order_root / 'revisions' / f'r{revision}'
    if revision_dir.exists():
        if ((existing_state
             and existing_state['generation_revision'] == revision
             and existing_state.get('model_seal'))
                or (revision_dir / 'release_manifest.json').exists()):
            raise FulfillmentStateError(
                'sealed revision directory is immutable; call write_generation first'
            )
        shutil.rmtree(revision_dir)
    artifact_dir = revision_dir / 'artifacts'
    artifact_dir.mkdir(parents=True)

    copied = []
    missing = []

    # Also check the ~/Downloads path (where intake_to_plan.py copies curated files)
    source_dir = athlete_dir

    # Copy workouts/
    workouts_src = source_dir / 'workouts'
    if not workouts_src.exists():
        workouts_src = athlete_dir / 'workouts'
    if workouts_src.exists():
        workouts_dst = artifact_dir / 'workouts'
        shutil.copytree(workouts_src, workouts_dst)
        zwo_count = len(list(workouts_dst.glob('*.zwo')))
        copied.append(f'workouts/ ({zwo_count} .zwo files)')
    else:
        missing.append('workouts/')

    # Copy individual files
    for fname in sorted(set(CUSTOMER_DELIVERABLES + PRIVATE_DELIVERABLES + REVIEW_DELIVERABLES)):
        src = source_dir / fname
        if not src.exists():
            src = athlete_dir / fname  # Fallback to raw athlete dir
        if src.exists():
            shutil.copy2(src, artifact_dir / fname)
            copied.append(fname)
        elif fname in ('training_guide.pdf',):
            pass  # PDF is optional (no Chrome on Railway)
        else:
            missing.append(fname)

    review_zip = revision_dir / f'{order_id}-review-bundle.zip'
    with zipfile.ZipFile(review_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
        for fname in REVIEW_DELIVERABLES:
            path = artifact_dir / fname
            if path.exists():
                archive.write(path, fname)

    customer_zip = revision_dir / f'{order_id}-customer-bundle.zip'
    with zipfile.ZipFile(customer_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
        for fname in CUSTOMER_DELIVERABLES:
            path = artifact_dir / fname
            if path.exists():
                archive.write(path, fname)
        if (artifact_dir / 'workouts').exists():
            for workout in sorted((artifact_dir / 'workouts').rglob('*')):
                if workout.is_file():
                    archive.write(workout, workout.relative_to(artifact_dir))

    state = finalize_transitional_release(
        state_path, revision_dir, expected_revision=revision)
    _record_order_lookup(order_id, athlete_id)

    logger.info(f"Persisted deliverables for {athlete_id}: "
                f"{len(copied)} files, review={review_zip.stat().st_size // 1024}KB, "
                f"customer={customer_zip.stat().st_size // 1024}KB")

    return {
        'delivery_dir': str(order_root),
        'revision_dir': str(revision_dir),
        'review_zip': str(review_zip),
        'customer_zip': str(customer_zip),
        'review_zip_size': review_zip.stat().st_size,
        'customer_zip_size': customer_zip.stat().st_size,
        'state': state,
        'copied': copied,
        'missing': missing,
    }


def _create_zip(source_dir: Path, zip_path: Path, exclude_zip: bool = True,
                exclude_files: set = None):
    """Create a zip file from source_dir contents."""
    exclude_files = exclude_files or set()
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(source_dir.rglob('*')):
            if item.is_file():
                rel = item.relative_to(source_dir)
                if exclude_zip and item.suffix == '.zip':
                    continue
                if rel.name in exclude_files:
                    continue
                zf.write(item, rel)


def _normalize_athlete_id(athlete_id: str) -> str:
    """Normalize athlete_id for consistent token generation (underscore form)."""
    return athlete_id.replace('-', '_')


def _generate_download_token(
    order_id: str, artifact: str = 'review_bundle', *,
    parent_review: dict | None = None,
) -> str:
    order_id = _resolve_order_id(order_id) or order_id
    state = load_fulfillment_state(_fulfillment_status_path(order_id))
    return issue_download_token(
        order_id=state['order_id'], athlete_id=state['athlete_id'],
        generation_revision=state['generation_revision'], artifact=artifact,
        parent_review_jti=str((parent_review or {}).get('jti') or ''),
        parent_review_kid=str((parent_review or {}).get('kid') or ''),
    )


def _generate_review_token(order_id: str, issued_to: str = '') -> str:
    """Issue one action-scoped, revision-bound coach review credential."""
    order_id = _resolve_order_id(order_id) or order_id
    state = load_fulfillment_state(_fulfillment_status_path(order_id))
    return issue_review_token(
        order_id=state['order_id'], athlete_id=state['athlete_id'],
        generation_revision=state['generation_revision'],
        issued_to=(issued_to or NOTIFICATION_EMAIL or 'operator-notification'),
    )


def _verify_download_token(order_id: str, token: str, artifact: str) -> dict:
    order_id = _resolve_order_id(order_id) or order_id
    state = load_fulfillment_state(_fulfillment_status_path(order_id))
    return verify_download_token(
        token, expected_order_id=state['order_id'],
        expected_athlete_id=state['athlete_id'],
        expected_revision=state['generation_revision'],
        expected_artifact=artifact,
        expected_audience=ARTIFACT_AUDIENCE[artifact],
        revocation_path=Path(DATA_DIR) / 'token_revocations.json')


# =============================================================================
# LOGGING
# =============================================================================

def log_order(order_data: dict, result: dict):
    """Log order processing for tracking with file locking.

    Includes email, name, product_type so follow-up email system can find orders.
    """
    log_dir = Path(DATA_DIR) / '.logs'
    log_dir.mkdir(exist_ok=True)

    log_entry = {
        'timestamp': datetime.now().isoformat(),
        'product_type': 'training_plan',
        'athlete_id': order_data['athlete_id'],
        'order_id': order_data['order_id'],
        'tier': order_data['tier'],
        'brand': normalize_brand(order_data.get('brand')
                                 or order_data.get('profile', {}).get('brand')),
        'email': order_data.get('profile', {}).get('email', ''),
        'name': order_data.get('profile', {}).get('name', ''),
        'success': result['success'],
        'error': _pipeline_error_excerpt(result) if not result['success'] else None,
    }

    log_file = log_dir / f"{datetime.now().strftime('%Y-%m')}.jsonl"

    try:
        with open(log_file, 'a') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(json.dumps(log_entry) + '\n')
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except IOError as e:
        logger.error(f"Failed to write log: {e}")


# =============================================================================
# ASYNC PIPELINE JOBS — durable JSON job records + background threads
#
# The pipeline takes minutes; Stripe times out webhook responses at ~20s.
# The webhook handler now writes a job record to the persistent volume
# (DATA_DIR/jobs/{athlete_id}.json), spawns the pipeline in a background
# thread, and returns 200 to Stripe immediately. Job records survive
# Railway restarts; sweep_stuck_jobs() retries jobs orphaned mid-generation
# (on startup, hourly, and via POST /api/jobs/sweep for external cron).
#
# SYNC_PIPELINE=1 preserves the old inline path (tests / local debugging).
# =============================================================================

JOBS_DIR = os.path.join(DATA_DIR, 'jobs')

# A queued/running job untouched for this long is considered orphaned by a
# restart or crash and gets retried by the sweep (max JOB_MAX_ATTEMPTS).
JOB_STUCK_AFTER_MINUTES = int(os.environ.get('JOB_STUCK_AFTER_MINUTES', '30'))
JOB_MAX_ATTEMPTS = int(os.environ.get('JOB_MAX_ATTEMPTS', '2'))

# Serializes job-file writes within this process (cross-process safety comes
# from atomic tempfile + os.replace; gunicorn runs 2 workers).
_jobs_write_lock = threading.Lock()


def _sync_pipeline_mode() -> bool:
    """True when SYNC_PIPELINE=1 — run the pipeline inline in the request."""
    return os.environ.get('SYNC_PIPELINE', '') == '1'


def _canonical_job_path(order_id: str) -> Path:
    return Path(JOBS_DIR) / 'orders' / f'{_safe_order_id(order_id)}.json'


def _job_path(ref: str) -> Path:
    """Compatibility lookup path; canonical records live under orders/."""
    return Path(JOBS_DIR) / f'{_normalize_athlete_id(ref)}.json'


def _write_job(job: dict):
    """Atomically persist a job record (temp file + os.replace)."""
    job['updated_at'] = datetime.now().isoformat()
    order_id = job.get('order_id') or f"legacy-job-{_normalize_athlete_id(job['athlete_id'])}"
    job['order_id'] = order_id
    order_id = _safe_order_id(order_id)
    path = _canonical_job_path(order_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp')
    with _jobs_write_lock:
        with open(tmp, 'w') as f:
            json.dump(job, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # Athlete-keyed file is a lookup only. Keep enough compatibility for
        # older operational tooling when exactly one order is known.
        lookup_path = _job_path(job['athlete_id'])
        try:
            lookup = json.loads(lookup_path.read_text()) if lookup_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            lookup = {}
        order_ids = sorted(set((lookup.get('order_ids') or []) + [order_id]))
        lookup_payload = {'athlete_id': job['athlete_id'], 'order_ids': order_ids}
        if len(order_ids) == 1:
            lookup_payload.update(job)
            lookup_payload['order_ids'] = order_ids
        lookup_tmp = lookup_path.with_name(f'.{lookup_path.name}.tmp')
        lookup_tmp.write_text(json.dumps(lookup_payload, indent=2) + '\n')
        os.replace(lookup_tmp, lookup_path)


def _read_job(ref: str) -> dict:
    """Load a job record. Returns None if absent or unreadable."""
    try:
        direct = _canonical_job_path(ref)
    except ValueError:
        direct = None
    if direct and direct.exists():
        path = direct
    else:
        lookup_path = _job_path(ref)
        if not lookup_path.exists():
            return None
        try:
            lookup = json.loads(lookup_path.read_text())
            order_ids = lookup.get('order_ids') or []
            if len(order_ids) == 1:
                path = _canonical_job_path(order_ids[0])
            elif lookup.get('order_id'):
                # Read-only compatibility for pre-v2 job files.
                return lookup
            else:
                return None
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning(f"Unreadable job lookup {lookup_path.name}: {e}")
            return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Unreadable job record {path.name}: {e}")
        return None


def _update_job(ref: str, **fields) -> dict:
    """Read-modify-write a job record."""
    job = _read_job(ref)
    if not job:
        raise ValueError(f'job not found: {ref}')
    job.update(fields)
    _write_job(job)
    return job


def _execute_plan_job(job: dict, intake_data: dict = None):
    """Run the full generation flow for one job and keep its record updated.

    Runs in a background thread by default (inline when SYNC_PIPELINE=1).
    Preserves the exact behaviors of the old synchronous path: run pipeline
    → log order → persist zips → coach notification (success or FAILED).
    Never raises — a crashed job is marked failed and the operator notified
    loudly; the customer never sees an error (order-killer-prevention rule).

    Returns the pipeline result dict.
    """
    athlete_id = job['athlete_id']
    order_data = job.get('order_data', {})
    if intake_data is None and job.get('intake_id'):
        intake_data = load_intake(job['intake_id'])

    try:
        if not _read_job(job['order_id']):
            _write_job(job)
        _update_job(job['order_id'], status='running',
                    started_at=datetime.now().isoformat())

        result = run_pipeline(athlete_id, deliver=True,
                              intake_data=intake_data or None,
                              order_data=order_data)

        persisted = None
        persistence_error = ''
        quarantine_requested = result.get('fulfillment_state') == 'unavailable'
        if result['success'] or quarantine_requested:
            source_dir = result.get('artifact_dir')
            if not source_dir:
                source_dir = ((Path(ATHLETES_DIR) / athlete_id)
                              if quarantine_requested
                              else _resolve_generated_athlete_dir(athlete_id))
            try:
                persisted = persist_deliverables(
                    order_data.get('order_id', ''), athlete_id,
                    source_dir=source_dir,
                    delivery_platform=order_data.get(
                        'delivery_platform', order_data.get('delivery_target', 'manual')),
                    state_unavailable=quarantine_requested,
                )
            except Exception as e:
                logger.error(f"Failed to persist deliverables for {athlete_id}: {e}")
                persistence_error = str(e)
                result['fulfillment_state'] = 'unavailable'
                try:
                    persisted = persist_deliverables(
                        order_data.get('order_id', ''), athlete_id,
                        source_dir=source_dir,
                        delivery_platform=order_data.get(
                            'delivery_platform', order_data.get('delivery_target', 'manual')),
                        state_unavailable=True,
                    )
                except Exception as quarantine_exc:
                    persistence_error = (
                        f'{persistence_error}; quarantine failed: {quarantine_exc}'
                    ).strip('; ')
                    logger.exception('Could not persist STATE_UNAVAILABLE quarantine')

            if not persisted:
                result['success'] = False
                result['fulfillment_state'] = 'unavailable'
                result['stderr'] = (
                    persistence_error
                    or 'Persistence returned no durable order state'
                )
            elif quarantine_requested:
                # Reaching a durable non-waivable BLOCKED_REVIEW state is a
                # successful order workflow outcome, not a dead pipeline job.
                result['success'] = True
                result['quarantined'] = True

        log_order(order_data, result)

        details = _build_plan_notification_details(order_data, result,
                                                   intake_data or None)
        details['fulfillment_state'] = result.get('fulfillment_state', 'unavailable')
        if persisted:
            details['fulfillment_status'] = persisted['state']['status']
            details['blocking_issues'] = persisted['state']['blocking_issues']
            details['required_confirmations'] = persisted['state']['required_confirmations']
        if result['success'] and persisted:
            try:
                details['review_token'] = _generate_review_token(
                    order_data.get('order_id', ''), NOTIFICATION_EMAIL)
            except (ReviewAuthError, FulfillmentStateError) as exc:
                # The durable blocked order remains the authority. Missing
                # link-signing config suppresses review download access and is
                # loud to the coach, but must not turn quarantine into a dead
                # pipeline job.
                logger.error(
                    f'Review token unavailable for order '
                    f"{order_data.get('order_id', '')}: {exc}")
            _notify_new_order('training_plan', details)
            _update_job(job['order_id'], status='succeeded',
                        finished_at=datetime.now().isoformat(), error=None)
        else:
            _notify_new_order('training_plan_FAILED', details)
            _update_job(job['order_id'], status='failed',
                        finished_at=datetime.now().isoformat(),
                        error=_pipeline_error_excerpt(result))
        return result

    except Exception as e:
        # Loud to the operator, never customer-visible.
        logger.critical(
            f"PLAN JOB CRASHED for {athlete_id} "
            f"(order {job.get('order_id', '?')}): {e}", exc_info=True)
        try:
            _update_job(job['order_id'], status='failed',
                        finished_at=datetime.now().isoformat(),
                        error=str(e)[:500])
            details = _build_plan_notification_details(
                order_data,
                {'success': False, 'stdout': '', 'stderr': str(e)[:500]},
                intake_data or None)
            _notify_new_order('training_plan_FAILED', details)
        except Exception:
            logger.exception(f"Failed to record job crash for {athlete_id}")
        return {'success': False, 'stdout': '', 'stderr': str(e)}


def _start_job_thread(job: dict, intake_data: dict = None) -> threading.Thread:
    """Spawn the job in a background (non-daemon) thread.

    Separate function so tests can patch it to run inline/deterministically.
    daemon=False: on graceful shutdown the worker waits for the thread
    (gunicorn --graceful-timeout 30); a hard kill is what the sweep handles.
    """
    t = threading.Thread(
        target=_execute_plan_job,
        args=(job,),
        kwargs={'intake_data': intake_data},
        name=f'plan-job-{job["athlete_id"]}',
        daemon=False,
    )
    t.start()
    return t


def _spawn_plan_job(order_data: dict, intake_id: str = '',
                    intake_data: dict = None) -> tuple:
    """Write a queued job record and launch generation.

    Returns (job, sync_result). sync_result is the pipeline result when
    SYNC_PIPELINE=1 (inline execution), else None (background thread).

    Guards against the same order running twice. A repeat customer may have
    multiple simultaneous orders without either being suppressed.
    """
    athlete_id = order_data['athlete_id']

    order_id = _safe_order_id(order_data.get('order_id', ''))
    existing = _read_job(order_id)
    if existing and existing.get('status') in ('queued', 'running'):
        logger.warning(
            f"Job for order {order_id} already {existing['status']} "
            f"(order {existing.get('order_id', '?')}) — not spawning duplicate")
        return existing, None

    job = {
        'athlete_id': athlete_id,
        'order_id': order_id,
        'intake_id': intake_id or '',
        # Coexistence flag (Phase 4b) — resolved at checkout, recorded here
        # so /api/confirm and the sweep can branch on it after a restart.
        'delivery_target': order_data.get('delivery_target', 'trainingpeaks'),
        'status': 'queued',
        'attempts': 1,
        'max_attempts': JOB_MAX_ATTEMPTS,
        'created_at': datetime.now().isoformat(),
        'started_at': None,
        'finished_at': None,
        'error': None,
        # Full order_data so sweep retries are self-contained after restart.
        'order_data': order_data,
    }
    _write_job(job)

    if _sync_pipeline_mode():
        result = _execute_plan_job(job, intake_data=intake_data)
        return job, result

    _start_job_thread(job, intake_data=intake_data)
    return job, None


def sweep_stuck_jobs() -> dict:
    """Retry jobs orphaned in queued/running (e.g. by a Railway restart).

    A job untouched for JOB_STUCK_AFTER_MINUTES is respawned with
    attempts+1; past JOB_MAX_ATTEMPTS it's marked failed and the operator
    is notified loudly. Runs on startup, hourly (before_request), and via
    POST /api/jobs/sweep (X-Cron-Secret) for external cron wiring.
    """
    stats = {'scanned': 0, 'retried': 0, 'failed': 0}
    jobs_dir = Path(JOBS_DIR)
    if not jobs_dir.exists():
        return stats

    stuck_before = datetime.now() - timedelta(minutes=JOB_STUCK_AFTER_MINUTES)

    for path in sorted((jobs_dir / 'orders').glob('*.json')):
        job = _read_job(path.stem)
        if not job or job.get('status') not in ('queued', 'running'):
            continue
        stats['scanned'] += 1

        try:
            updated_at = datetime.fromisoformat(job.get('updated_at', ''))
        except (ValueError, TypeError):
            updated_at = datetime.min
        if updated_at > stuck_before:
            continue  # Recently touched — probably still running

        athlete_id = job['athlete_id']
        attempts = int(job.get('attempts', 1))
        max_attempts = int(job.get('max_attempts', JOB_MAX_ATTEMPTS))

        if attempts >= max_attempts:
            logger.critical(
                f"PLAN JOB ORPHANED after {attempts} attempts: {athlete_id} "
                f"(order {job.get('order_id', '?')}) — marking failed, "
                f"manual re-run required")
            _update_job(job['order_id'], status='failed',
                        finished_at=datetime.now().isoformat(),
                        error=f'Job stuck after {attempts} attempts '
                              f'(likely restart mid-generation)')
            try:
                details = _build_plan_notification_details(
                    job.get('order_data', {}),
                    {'success': False, 'stdout': '',
                     'stderr': f'Job orphaned after {attempts} attempts — '
                               f'likely a restart mid-generation. '
                               f'Re-run the pipeline manually.'},
                    None)
                _notify_new_order('training_plan_FAILED', details)
            except Exception:
                logger.exception(f"Failed to notify for orphaned job {athlete_id}")
            stats['failed'] += 1
        else:
            job['attempts'] = attempts + 1
            job['status'] = 'queued'
            job['error'] = None
            _write_job(job)
            logger.warning(
                f"Retrying stuck job for {athlete_id} "
                f"(attempt {job['attempts']}/{max_attempts})")
            if _sync_pipeline_mode():
                _execute_plan_job(job)
            else:
                _start_job_thread(job)
            stats['retried'] += 1

    return stats


def sweep_stuck_consultations() -> dict:
    """Expired analysis leases: back to open (attempts < max) or
    needs_attention (copies the JOB_STUCK_AFTER_MINUTES pattern above, for
    the runner's claim lease instead of a pipeline job). Runs on the same
    hourly before_request cadence as sweep_stuck_jobs(); no dedicated
    endpoint in C1 — the operator lever is POST /api/consult/<id>/op."""
    stats = {'scanned': 0, 'reopened': 0, 'needs_attention': 0}
    now = datetime.now(timezone.utc)
    for record in consultations.list_records(DELIVERIES_DIR):
        if record.get('status') != 'analysis_running':
            continue
        analysis = record.get('analysis') or {}
        lease_raw = analysis.get('lease_expires_at')
        if not lease_raw:
            continue
        try:
            lease_expires = datetime.fromisoformat(lease_raw)
        except (TypeError, ValueError):
            continue
        if lease_expires.tzinfo is None:
            lease_expires = lease_expires.replace(tzinfo=timezone.utc)
        if lease_expires > now:
            continue

        stats['scanned'] += 1
        order_id = record['order_id']
        attempts = int(analysis.get('attempts', 0))

        if attempts < CONSULT_ANALYSIS_MAX_ATTEMPTS:
            def _reopen(r):
                a = r.setdefault('analysis', {})
                a['claimed_by'] = None
                a['lease_expires_at'] = None
                r['status'] = 'open'
                consultations.append_timeline(r, 'error', 'lease expired — reopened')
            consultations.update_record(DELIVERIES_DIR, order_id, _reopen)
            stats['reopened'] += 1
        else:
            def _flag(r):
                r['status'] = 'needs_attention'
                consultations.append_timeline(r, 'error', f'lease expired after {attempts} attempts')
            updated = consultations.update_record(DELIVERIES_DIR, order_id, _flag)
            try:
                _notify_consult_needs_attention(
                    updated, reason=f'analysis lease expired after {attempts} attempts')
            except Exception:
                logger.exception(f"Failed to notify needs_attention for {order_id}")
            stats['needs_attention'] += 1

    return stats


# =============================================================================
# ROUTES
# =============================================================================




def _required_runtime_paths() -> dict:
    """Repo-root files the production image must ship for offline apply-contract.

    apply_contract.schema_path() is parents[2]/schemas/apply_contract_v1.schema.json
    (/app/schemas/... in the Railway image). athletes/config is already copied
    via COPY athletes/; no other webhook/athletes/scripts runtime reads sit
    outside the image COPY list.
    """
    return {
        'apply_contract_schema': apply_contract_schema_path(),
    }


def _runtime_packaging_ok() -> tuple:
    checks = {}
    ok = True
    for name, path in _required_runtime_paths().items():
        present = path.is_file()
        checks[name] = present
        if not present:
            ok = False
    return ok, checks


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint with dependency checks."""
    packaging_ok, packaging = _runtime_packaging_ok()
    token_config = {
        'review': review_keys_configured(),
        'download': download_keys_configured(),
    }
    tokens_ok = all(token_config.values())
    checks = {
        'service': 'gravel-god-webhook',
        'status': 'ok',
        'athletes_dir': Path(ATHLETES_DIR).exists(),
        'scripts_dir': Path(SCRIPTS_DIR).exists(),
        'data_dir': Path(DATA_DIR).exists(),
        'runtime_files': packaging,
        'token_config': token_config,
    }

    if (not checks['athletes_dir'] or not checks['scripts_dir']
            or not packaging_ok):
        checks['status'] = 'degraded'
    if IS_PRODUCTION and not tokens_ok:
        checks['status'] = 'degraded'

    # Endure delivery ops status (Decision 2 streak) — only present when the
    # feature is configured, so env-off means a byte-identical health payload.
    if endure_delivery.is_enabled():
        streak = endure_delivery.read_streak(DATA_DIR)
        checks['endure_delivery'] = {
            'enabled': True,
            'default_target': endure_delivery.resolve_delivery_target({}),
            'consecutive_successes': streak['consecutive_successes'],
            'total_successes': streak['total_successes'],
            'total_failures': streak['total_failures'],
            'last_status': streak['last_status'],
            'updated_at': streak['updated_at'],
        }

    status_code = 200 if checks['status'] == 'ok' else 503
    return jsonify(checks), status_code


# =============================================================================
# PHASE 2 COACH REVIEW — signed-link login, server session, CSRF approval
# =============================================================================

REVIEW_SESSION_COOKIE = 'gg_review_session'


def _review_sessions_root() -> Path:
    return Path(DATA_DIR) / 'review-sessions'


def _review_revocation_path() -> Path:
    return Path(DATA_DIR) / 'token_revocations.json'


def _review_response(html: str, status: int = 200, *, nonce: str = ''):
    response = make_response(html, status)
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Referrer-Policy'] = 'no-referrer'
    script_policy = f"'nonce-{nonce}'" if nonce else "'none'"
    response.headers['Content-Security-Policy'] = (
        "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        f"script-src {script_policy}; style-src 'unsafe-inline'; "
        "form-action 'self'"
    )
    return response


def _review_bootstrap(status: int = 200):
    nonce = uuid.uuid4().hex
    return _review_response(render_bootstrap(nonce), status, nonce=nonce)


def _authorized_review(order_ref: str):
    """Return (order_id, current state, review session), without leaking on failure."""
    order_id = _resolve_review_order_id(order_ref)
    if not order_id:
        raise ReviewAuthError('review session is unavailable')
    session = load_review_session(
        _review_sessions_root(), request.cookies.get(REVIEW_SESSION_COOKIE, ''),
        order_id=order_id, revocation_path=_review_revocation_path(),
    )
    try:
        state = load_fulfillment_state(_fulfillment_status_path(order_id))
    except FulfillmentStateError as exc:
        raise ReviewAuthError('review state is unavailable') from exc
    if state.get('status') == CANCELLED:
        raise ReviewAuthError('review credential is cancelled')
    if (session.get('athlete_id') != state.get('athlete_id')
            or session.get('generation_revision') != state.get('generation_revision')):
        raise ReviewAuthError('review link is superseded by a newer revision')
    return order_id, state, session


def _render_authorized_review(
    order_id: str, state: dict, session: dict, *, error: str = '', status: int = 200,
):
    download_available = False
    seal_error = ''
    revision_dir = (_order_dir(order_id) / 'revisions'
                    / f"r{state['generation_revision']}")
    try:
        verify_release_manifest(state, revision_dir)
        download_available = not (
            state.get('status') in RELEASE_STATUSES
            and not approval_matches_release(state)
        )
    except FulfillmentStateError as exc:
        seal_error = str(exc)
        if isinstance(exc, FulfillmentStateError) and state.get('model_seal'):
            try:
                state = record_seal_mismatch(
                    _fulfillment_status_path(order_id), str(exc))
            except FulfillmentStateError:
                pass
    combined_error = error or (f'Sealed review unavailable: {seal_error}' if seal_error else '')
    return _review_response(render_review_page(
        state, csrf_token=session['csrf_token'],
        download_available=download_available,
        error=combined_error,
    ), status)


@app.route('/review/<order_ref>', methods=['GET'])
@limiter.limit('30/minute')
def review_order(order_ref):
    """Scanner-safe shell or the authenticated, revision-bound review page."""
    try:
        order_id, state, session = _authorized_review(order_ref)
    except ReviewAuthError:
        # Generic shell contains no order existence, athlete, artifact, or
        # state data. The URL-fragment token is exchanged by its static script.
        return _review_bootstrap()
    return _render_authorized_review(order_id, state, session)


@app.route('/review/<order_ref>/session', methods=['POST'])
@limiter.limit('10/minute')
def open_review_session(order_ref):
    """Exchange a fragment-carried review bearer for an opaque server session."""
    order_id = _resolve_order_id(order_ref)
    token = str(request.form.get('token') or '')
    if not order_id or not token:
        return _review_bootstrap(401)
    try:
        state = load_fulfillment_state(_fulfillment_status_path(order_id))
        if state.get('status') == CANCELLED:
            return _review_bootstrap(401)
        claims = verify_review_token(
            token, order_id=state['order_id'], athlete_id=state['athlete_id'],
            generation_revision=state['generation_revision'],
            revocation_path=_review_revocation_path(),
        )
        session_id, session = create_review_session(
            _review_sessions_root(), claims)
    except (FulfillmentStateError, ReviewAuthError):
        return _review_bootstrap(401)
    response = redirect(f'/review/{order_id}', code=303)
    response.set_cookie(
        REVIEW_SESSION_COOKIE, session_id,
        max_age=max(1, session['expires_at'] - int(datetime.now(timezone.utc).timestamp())),
        secure=IS_PRODUCTION, httponly=True, samesite='Strict',
        path=f'/review/{order_id}',
    )
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@app.route('/review/<order_ref>/approve', methods=['POST'])
@limiter.limit('10/minute')
def approve_review_order(order_ref):
    """Approve exactly the values and sealed revision rendered by the page."""
    try:
        order_id, state, session = _authorized_review(order_ref)
    except ReviewAuthError as exc:
        return _review_response(
            '<!doctype html><title>Review unavailable</title>'
            '<p>Review session unavailable or superseded.</p>', 409)
    supplied_csrf = str(request.form.get('csrf_token') or '')
    if (not supplied_csrf
            or not hmac.compare_digest(supplied_csrf, session['csrf_token'])):
        return _render_authorized_review(
            order_id, state, session, error='Invalid review form token.', status=403)
    try:
        expected_revision = int(str(request.form.get('generation_revision') or ''))
    except ValueError:
        return _render_authorized_review(
            order_id, state, session, error='Invalid generation revision.', status=400)
    expected_catalog_digest = str(
        request.form.get('review_catalog_digest') or '').strip()
    if not expected_catalog_digest:
        return _render_authorized_review(
            order_id, state, session, error='Missing review catalog digest.', status=400)

    decisions = [
        {
            'item_id': item_id,
            'revision': expected_revision,
            'disposition': 'confirmed',
        }
        for item_id in request.form.getlist('confirm_item')
    ]
    for encoded in request.form.getlist('resolved_item'):
        item_id, separator, choice = str(encoded).partition('::')
        if separator and item_id and choice:
            decisions.append({
                'item_id': item_id,
                'revision': expected_revision,
                'disposition': f'resolved:{choice}',
            })
    blocker_ids = [item['id'] for item in state.get('blocking_issues', [])]
    waived_ids = request.form.getlist('waive_item')
    waiver = None
    if blocker_ids:
        waiver = {
            'rule_ids': waived_ids,
            'reason': str(request.form.get('waiver_reason') or '').strip(),
        }
    try:
        transition_fulfillment(
            _fulfillment_status_path(order_id), APPROVED,
            session['credential'], waiver=waiver,
            expected_revision=expected_revision,
            expected_catalog_digest=expected_catalog_digest,
            review_decisions=decisions,
            credential=session['credential'],
        )
    except FulfillmentStateError as exc:
        try:
            state = load_fulfillment_state(_fulfillment_status_path(order_id))
        except FulfillmentStateError:
            pass
        return _render_authorized_review(
            order_id, state, session, error=str(exc), status=409)
    return redirect(f'/review/{order_id}', code=303)


@app.route('/review/<order_ref>/d2/identity', methods=['POST'])
@limiter.limit('10/minute')
def select_review_identity(order_ref):
    """State-changing coach command selecting one probed candidate."""
    try:
        order_id, state, session = _authorized_review(order_ref)
    except ReviewAuthError:
        return _review_response(
            '<!doctype html><title>Review unavailable</title>'
            '<p>Review session unavailable or superseded.</p>', 409)
    supplied_csrf = str(request.form.get('csrf_token') or '')
    if (not supplied_csrf
            or not hmac.compare_digest(supplied_csrf, session['csrf_token'])):
        return _render_authorized_review(
            order_id, state, session, error='Invalid review form token.', status=403)
    try:
        expected_revision = int(str(request.form.get('generation_revision') or ''))
        from d2_identity import select_identity_candidate
        changed = select_identity_candidate(
            _fulfillment_status_path(order_id), expected_revision,
            str(request.form.get('tp_athlete_id') or ''),
            actor=session['credential'],
        )
        _queue_d2_regeneration(order_id, changed)
    except (ValueError, FulfillmentStateError) as exc:
        return _render_authorized_review(
            order_id, state, session, error=str(exc), status=409)
    return redirect(f'/review/{order_id}', code=303)


@app.route('/review/<order_ref>/d2/resolve', methods=['POST'])
@limiter.limit('10/minute')
def resolve_review_d2_item(order_ref):
    """Execute a D2 selector as a server-side state command."""
    try:
        order_id, state, session = _authorized_review(order_ref)
    except ReviewAuthError:
        return _review_response(
            '<!doctype html><title>Review unavailable</title>'
            '<p>Review session unavailable or superseded.</p>', 409)
    supplied_csrf = str(request.form.get('csrf_token') or '')
    if (not supplied_csrf
            or not hmac.compare_digest(supplied_csrf, session['csrf_token'])):
        return _render_authorized_review(
            order_id, state, session, error='Invalid review form token.', status=403)
    try:
        expected_revision = int(str(request.form.get('generation_revision') or ''))
        item_id = str(request.form.get('resolution_item') or '')
        choice = str(request.form.get(f'resolution_choice:{item_id}') or '')
        from d2_identity import resolve_d2_item
        changed = resolve_d2_item(
            _fulfillment_status_path(order_id), expected_revision,
            item_id, choice, actor=session['credential'],
        )
        _queue_d2_regeneration(order_id, changed)
    except (ValueError, FulfillmentStateError) as exc:
        return _render_authorized_review(
            order_id, state, session, error=str(exc), status=409)
    return redirect(f'/review/{order_id}', code=303)


def _run_d2_manual_readback(order_id: str, state: dict, item_id: str) -> dict:
    """Issue and execute one order/identity-bound read-only inspect attempt."""
    from delivery.trainingpeaks.worker_service import (
        CannedProbeTransport, SERVER_PROBE_AUDIENCE, SERVER_PROBE_KID,
        WorkerAuthorizationError, build_server_read_only_worker,
    )
    from d2_identity import record_manual_readback

    fixture_path = os.environ.get('GG_WORKER_PROBES_FIXTURE', '').strip()
    if not fixture_path:
        raise FulfillmentStateError(
            'read-only worker transport is unavailable for manual readback')
    binding = state.get('platform_identity') or {}
    tp_id = str(binding.get('tp_athlete_id') or '').strip()
    if not tp_id or binding.get('order_id') != order_id:
        raise FulfillmentStateError(
            "manual readback requires this order's bound platform identity")

    transport = CannedProbeTransport.from_path(
        fixture_path, tp_athlete_id=tp_id)
    try:
        codec, worker = build_server_read_only_worker(transport)
    except WorkerAuthorizationError as exc:
        raise FulfillmentStateError(
            'read-only worker capability signing is unavailable') from exc
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    jti = 'manual-inspect-' + uuid.uuid4().hex
    claims = {
        'order_id': order_id,
        'subject': {'kind': 'identity_query', 'tp_athlete_id': tp_id},
        'action': 'inspect', 'audience': SERVER_PROBE_AUDIENCE,
        'iat': now_epoch - 1, 'exp': now_epoch + 300, 'jti': jti,
    }
    capability = codec.issue(claims, kid=SERVER_PROBE_KID)
    evidence = worker.inspect_account_evidence(
        tp_id, capability, now=now_epoch)
    return record_manual_readback(
        _fulfillment_status_path(order_id), state['generation_revision'],
        item_id, evidence,
    )


@app.route('/review/<order_ref>/d2/readback', methods=['POST'])
@limiter.limit('10/minute')
def complete_review_d2_readback(order_ref):
    """Authenticated command obtaining manual-correction evidence from worker."""
    try:
        order_id, state, session = _authorized_review(order_ref)
    except ReviewAuthError:
        return _review_response(
            '<!doctype html><title>Review unavailable</title>'
            '<p>Review session unavailable or superseded.</p>', 409)
    supplied_csrf = str(request.form.get('csrf_token') or '')
    if (not supplied_csrf
            or not hmac.compare_digest(supplied_csrf, session['csrf_token'])):
        return _render_authorized_review(
            order_id, state, session, error='Invalid review form token.', status=403)
    try:
        expected_revision = int(str(request.form.get('generation_revision') or ''))
        if expected_revision != state['generation_revision']:
            raise FulfillmentStateError('generation revision mismatch')
        item_id = str(request.form.get('readback_item') or '').strip()
        if item_id not in (state.get('d2_pending_requirements') or {}):
            raise FulfillmentStateError(
                'manual correction has no pending readback')
        _run_d2_manual_readback(order_id, state, item_id)
    except (ValueError, FulfillmentStateError) as exc:
        try:
            state = load_fulfillment_state(_fulfillment_status_path(order_id))
        except FulfillmentStateError:
            pass
        return _render_authorized_review(
            order_id, state, session, error=str(exc), status=409)
    return redirect(f'/review/{order_id}', code=303)


def _queue_d2_regeneration(order_id: str, state: dict) -> None:
    """Feed a durable D2 command back through the normal generation job.

    The command intent is already durable before this function runs. A queue
    failure therefore leaves a loud, non-approvable regeneration request
    instead of losing the coach's choice or presenting the old seal as valid.
    """
    request_record = state.get('regeneration_request') or {}
    if not request_record:
        # Unsealed fixture/unit flows can finish in the current producer run.
        return
    job = _read_job(order_id)
    if not job:
        raise FulfillmentStateError(
            'D2 regeneration is recorded but no durable generation job exists')
    intake_data = load_intake(job.get('intake_id')) if job.get('intake_id') else {}
    if not intake_data:
        prior_revision = request_record.get('prior_revision')
        backup = (_order_dir(order_id) / 'revisions' / f'r{prior_revision}'
                  / 'artifacts' / 'intake_backup.json')
        try:
            loaded = json.loads(backup.read_text(encoding='utf-8'))
            intake_data = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            intake_data = {}
    if not intake_data:
        raise FulfillmentStateError(
            'D2 regeneration is recorded but canonical intake is unavailable')
    for field, value in (state.get('canonical_input_overrides') or {}).items():
        if field not in {'hr_threshold', 'ftp', 'age', 'weight'}:
            raise FulfillmentStateError('D2 canonical input override is invalid')
        intake_data[field] = value

    # The order-work state is the producer's revision source. Install the
    # already-atomic authoritative intent there before the long generation.
    work_root = (Path(DATA_DIR) / 'order-work' / _safe_order_id(order_id)
                 / 'athletes')
    athlete_id = str(state['athlete_id'])
    candidates = [work_root / athlete_id, work_root / athlete_id.replace('_', '-')]
    source_dir = next(
        (candidate for candidate in candidates if candidate.is_dir()), candidates[-1])
    source_dir.mkdir(parents=True, exist_ok=True)
    source_state = source_dir / 'fulfillment_status.json'
    source_tmp = source_state.with_name(f'.{source_state.name}.d2.tmp')
    payload = json.dumps(state, indent=2, sort_keys=True) + '\n'
    try:
        with open(source_tmp, 'w', encoding='utf-8') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(source_tmp, source_state)
    finally:
        with contextlib.suppress(FileNotFoundError):
            source_tmp.unlink()

    order_data = copy.deepcopy(job.get('order_data') or {})
    order_data.setdefault('order_id', order_id)
    order_data.setdefault('athlete_id', athlete_id)
    _spawn_plan_job(
        order_data, intake_id=str(job.get('intake_id') or ''),
        intake_data=intake_data,
    )


@app.route('/review/<order_ref>/bundle', methods=['POST'])
@limiter.limit('20/minute')
def download_review_bundle_session(order_ref):
    """Download the review bundle through the revocation-aware page session."""
    try:
        order_id, state, session = _authorized_review(order_ref)
    except ReviewAuthError:
        return _review_response(
            '<!doctype html><title>Review unavailable</title>'
            '<p>Review session unavailable or superseded.</p>', 401)
    supplied_csrf = str(request.form.get('csrf_token') or '')
    if (not supplied_csrf
            or not hmac.compare_digest(supplied_csrf, session['csrf_token'])):
        return _review_response(
            '<!doctype html><title>Review unavailable</title>'
            '<p>Invalid review form token.</p>', 403)
    if (state.get('status') in RELEASE_STATUSES
            and not approval_matches_release(state)):
        return _review_response(
            '<!doctype html><title>Review unavailable</title>'
            '<p>Approval not authoritative — regenerate/re-approve.</p>', 409)
    revision_dir = (_order_dir(order_id) / 'revisions'
                    / f"r{state['generation_revision']}")
    filename = f'{order_id}-review-bundle.zip'
    try:
        zip_handle = open_verified_release_artifact(
            state, revision_dir, filename, require_approval=False)
    except FulfillmentStateError as exc:
        try:
            record_seal_mismatch(_fulfillment_status_path(order_id), str(exc))
        except FulfillmentStateError:
            logger.exception(
                f'Could not record review-bundle seal mismatch for order {order_id}')
        return _review_response(
            '<!doctype html><title>Review unavailable</title>'
            '<p>Sealed review bundle unavailable.</p>', 409)
    response = send_file(
        zip_handle, mimetype='application/zip', as_attachment=True,
        download_name=filename,
    )
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


# =============================================================================
# DELIVERY ENDPOINTS — download zips, send to customer
# =============================================================================

@app.route('/api/download/<order_id>', methods=['GET'])
def download_deliverables(order_id):
    """Download one typed, order/revision-bound artifact."""
    if 'type' in request.args:
        return jsonify({'error': 'Unknown artifact type'}), 400
    artifact = request.args.get('artifact', 'review_bundle')
    if artifact not in ARTIFACT_AUDIENCE:
        return jsonify({'error': 'Unknown artifact type'}), 400
    # Review bundles are available only through the revision-bound review
    # session POST above. Do not retain a second GET capability surface.
    if artifact == 'review_bundle':
        return jsonify({'error': 'Unauthorized'}), 401
    resolved_order_id = _resolve_order_id(order_id)
    if not resolved_order_id:
        return jsonify({'error': 'Fulfillment state unavailable'}), 409
    try:
        state = load_fulfillment_state(_fulfillment_status_path(resolved_order_id))
    except FulfillmentStateError:
        return jsonify({'error': 'Fulfillment state unavailable'}), 409
    # Auth: operator secret or a typed Authorization bearer. Query parameters
    # are deliberately never credentials because request targets cross proxy
    # and access-log boundaries before application redaction can run.
    secret = request.headers.get('X-Cron-Secret', '')
    authorization = str(request.headers.get('Authorization') or '')
    token = (authorization[7:].strip()
             if authorization.lower().startswith('bearer ') else '')
    has_secret = secret and hmac.compare_digest(secret, os.environ.get('CRON_SECRET', ''))
    try:
        has_token = bool(token and _verify_download_token(
            resolved_order_id, token, artifact))
    except (DownloadTokenError, FulfillmentStateError):
        has_token = False

    if not has_secret and not has_token:
        return jsonify({'error': 'Unauthorized'}), 401

    if not approval_matches_release(state):
        return jsonify({'error': 'plan not released'}), 409

    revision_dir = _order_dir(resolved_order_id) / 'revisions' / f"r{state['generation_revision']}"
    filename = f'{resolved_order_id}-customer-bundle.zip'
    try:
        zip_handle = open_verified_release_artifact(
            state, revision_dir, filename, require_approval=True,
        )
    except FulfillmentStateError as exc:
        logger.error(f"Download seal verification failed for order {resolved_order_id}: {exc}")
        try:
            record_seal_mismatch(
                _fulfillment_status_path(resolved_order_id), str(exc))
        except FulfillmentStateError:
            logger.exception(
                f"Could not record seal mismatch for order {resolved_order_id}")
        return jsonify({'error': 'plan not released'}), 409

    response = send_file(
        zip_handle,
        mimetype='application/zip',
        as_attachment=True,
        download_name=filename,
    )
    return response


# Order/session references: Stripe session ids (cs_...), test ids, or
# athlete ids. Never used as a filesystem path without validation.
_ORDER_REF_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')

# Customer-facing status copy — honest, never an error or a "we broke".
_MSG_IN_PROGRESS = ("Your custom plan is being generated right now — "
                    "you'll get an email as soon as it's ready.")
_MSG_FINISHING = ("We're putting the finishing touches on your plan "
                  "and will email it to you shortly.")
_MSG_READY = "Your plan is ready — check your email for the details."


@app.route('/api/order-status/<ref>', methods=['GET'])
@limiter.limit("30/minute")
def order_status(ref):
    """Customer-facing order status for the success page.

    <ref> is either the Stripe checkout session id (the success page gets
    ?session_id={CHECKOUT_SESSION_ID}) or an athlete id. Returns
    {status, download_ready, message} where status is ready|processing|
    unknown. A failed job reads as "processing" with a gentle finishing
    message — failures are loud to the operator (email/logs), invisible
    to the customer.
    """
    if not ref or not _ORDER_REF_RE.match(ref):
        return jsonify({'status': 'unknown', 'download_ready': False}), 404

    order_id = None
    is_session_ref = (
        ref.startswith('cs_') or ref.startswith('test_')
        or ref.startswith('drill-')
    )
    if is_session_ref:
        order_id = ref
        # Ensure the order is known even if generation has not written state.
        processed_file = Path(DATA_DIR) / '.processed_orders.json'
        entry = None
        try:
            if processed_file.exists():
                entry = json.loads(processed_file.read_text()).get(ref)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"order-status: could not read processed orders: {e}")
        if not entry:
            # Stripe's webhook may simply not have arrived yet — honest
            # in-progress, never an error the customer has to interpret.
            return jsonify({'status': 'processing', 'download_ready': False,
                            'message': _MSG_IN_PROGRESS})
    else:
        order_id = _resolve_order_id(ref)
        if not order_id:
            return jsonify({'status': 'unknown', 'download_ready': False}), 404

    download_ready = False
    state = None
    try:
        state = load_fulfillment_state(_fulfillment_status_path(order_id))
        if approval_matches_release(state):
            revision_dir = _order_dir(order_id) / 'revisions' / f"r{state['generation_revision']}"
            verify_release_artifact(
                state, revision_dir, f'{order_id}-customer-bundle.zip')
            download_ready = True
    except FulfillmentStateError as exc:
        if state is not None:
            try:
                state = record_seal_mismatch(
                    _fulfillment_status_path(order_id), str(exc))
            except FulfillmentStateError:
                state = None
        else:
            state = None

    job = _read_job(order_id) or {}
    job_status = job.get('status', '')
    operator_secret = str(request.headers.get('X-Cron-Secret') or '')
    configured_secret = str(os.environ.get('CRON_SECRET') or '')
    operator_authenticated = bool(
        operator_secret and configured_secret
        and hmac.compare_digest(operator_secret, configured_secret)
    )
    operator_fields = {}
    if operator_authenticated:
        review_bundle_exists = False
        if state is not None:
            revision_dir = (_order_dir(order_id) / 'revisions'
                            / f"r{state['generation_revision']}")
            try:
                verify_release_artifact(
                    state, revision_dir, f'{order_id}-review-bundle.zip')
                review_bundle_exists = True
            except FulfillmentStateError:
                review_bundle_exists = False
        operator_fields = {
            'generation_complete': bool(
                state is not None and job_status == 'succeeded'),
            'job_status': job_status or None,
            'fulfillment_status': (
                state.get('status') if state is not None else None),
            'blocker_ids': sorted(
                issue.get('id') for issue in (state or {}).get('blocking_issues', [])
                if issue.get('id')),
            'review_bundle_exists': review_bundle_exists,
        }

    if download_ready:
        return jsonify({'status': 'ready', 'download_ready': download_ready,
                        'message': _MSG_READY, **operator_fields})
    if job_status in ('queued', 'running', 'succeeded') or state is not None:
        return jsonify({'status': 'processing', 'download_ready': False,
                        'message': _MSG_IN_PROGRESS, **operator_fields})
    if job_status == 'failed':
        # Operator already notified loudly; the customer sees a calm
        # "finishing up" — the coach recovers the order manually.
        return jsonify({'status': 'processing', 'download_ready': False,
                        'message': _MSG_FINISHING, **operator_fields})
    if is_session_ref:
        # Order known (idempotency mark) but no job record — legacy or
        # sync-mode order. Report in-progress; email delivery still applies.
        return jsonify({'status': 'processing', 'download_ready': False,
                        'message': _MSG_IN_PROGRESS, **operator_fields})
    return jsonify({'status': 'unknown', 'download_ready': False}), 404


@app.route('/api/jobs/sweep', methods=['POST'])
@limiter.limit("5/minute")
def jobs_sweep():
    """Retry jobs orphaned by a restart. Secured by X-Cron-Secret.

    Also runs automatically on startup and hourly; this endpoint exists so
    an external cron can add a third safety net (wire later — do NOT add a
    GitHub workflow here).
    """
    secret = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not hmac.compare_digest(secret, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        stats = sweep_stuck_jobs()
        logger.info(f"Job sweep complete: {stats}")
        return jsonify({'status': 'ok', **stats})
    except Exception as e:
        logger.exception(f"Job sweep error: {e}")
        return jsonify({'error': 'Internal error'}), 500


@app.route('/api/download-tokens/revoke', methods=['POST'])
@limiter.limit("10/minute")
def revoke_download_capability():
    """Authenticated operational revocation for one link jti and/or key kid."""
    configured_secret = os.environ.get('CRON_SECRET', '')
    supplied_secret = request.headers.get('X-Cron-Secret', '')
    if (not configured_secret or not supplied_secret
            or not hmac.compare_digest(supplied_secret, configured_secret)):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or _has_client_timestamp(data):
        return jsonify({'error': 'JSON body without client timestamps is required'}), 400
    jti = str(data.get('jti') or '').strip()
    kid = str(data.get('kid') or '').strip()
    if not jti and not kid:
        return jsonify({'error': 'jti or kid is required'}), 400
    try:
        revoke_download_token(
            Path(DATA_DIR) / 'token_revocations.json', jti=jti, kid=kid)
    except DownloadTokenError as exc:
        return jsonify({'error': str(exc)}), 409
    logger.warning(
        f"Download capability revoked: jti={jti or '-'} kid={kid or '-'}")
    return jsonify({'status': 'revoked', 'jti': jti or None, 'kid': kid or None})


def _fulfillment_status_path(order_id: str) -> Path:
    return _order_dir(order_id) / 'fulfillment_status.json'


def _has_client_timestamp(value) -> bool:
    if isinstance(value, dict):
        return any('timestamp' in str(key).lower() or _has_client_timestamp(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_has_client_timestamp(item) for item in value)
    return False


APPLY_GATE_TOKEN_TTL_SECONDS = 5 * 60


def _apply_gate_secret() -> bytes:
    secret = os.environ.get('CRON_SECRET', '')
    if not secret:
        raise FulfillmentStateError('CRON_SECRET is required for apply gate tokens')
    return secret.encode('utf-8')


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _issue_apply_gate_token(state: dict, tp_manifest_sha256: str) -> str:
    """REFUSED legacy browser grant issuer; Phase 5 uses D1 exchange grants."""
    raise FulfillmentStateError(
        'legacy browser apply grant issuance is REFUSED in Phase 4')


def _verify_apply_gate_token(token: str) -> dict:
    try:
        encoded, supplied = str(token or '').split('.', 1)
        expected = hmac.new(
            _apply_gate_secret(), encoded.encode('ascii'), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64url_decode(supplied), expected):
            raise ValueError('signature')
        claims = json.loads(_b64url_decode(encoded))
        if not isinstance(claims, dict):
            raise ValueError('claims')
    except (ValueError, TypeError, json.JSONDecodeError,
            UnicodeError, binascii.Error) as exc:
        raise FulfillmentStateError('invalid apply gate token') from exc
    now = int(datetime.now(timezone.utc).timestamp())
    if claims.get('v') != 1 or claims.get('aud') != 'trainingpeaks_apply_gate':
        raise FulfillmentStateError('invalid apply gate token audience')
    if not isinstance(claims.get('exp'), int) or claims['exp'] <= now:
        raise FulfillmentStateError('apply gate token expired')
    if (not isinstance(claims.get('iat'), int)
            or claims['exp'] <= claims['iat']
            or claims['exp'] - claims['iat'] > APPLY_GATE_TOKEN_TTL_SECONDS):
        raise FulfillmentStateError('apply gate token lifetime is invalid')
    return claims


def _tp_manifest_record(manifest: dict | None) -> dict:
    return next(
        (item for item in (manifest or {}).get('artifacts', [])
         if item.get('path') == 'artifacts/tp_manifest.json'),
        {},
    )


@app.route('/api/fulfillment/<order_ref>/transition', methods=['POST'])
def transition_fulfillment_state(order_ref):
    """Record the coach's authenticated review/application transition."""
    secret = request.headers.get('X-Cron-Secret', '')
    if not secret or not hmac.compare_digest(secret, os.environ.get('CRON_SECRET', '')):
        return jsonify({'error': 'Unauthorized'}), 401
    order_id = _resolve_order_id(order_ref)
    if not order_id:
        return jsonify({'error': 'Fulfillment state unavailable'}), 409
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or _has_client_timestamp(data):
        return jsonify({'error': 'JSON body without client timestamps is required'}), 400
    destination = str(data.get('to', ''))
    expected_revision = data.get('generation_revision')
    expected_catalog_digest = data.get('review_catalog_digest')
    review_decisions = data.get('confirmations')
    if destination == APPROVED:
        if (not isinstance(expected_revision, int)
                or not isinstance(expected_catalog_digest, str)
                or not expected_catalog_digest.strip()
                or not isinstance(review_decisions, list)):
            return jsonify({
                'error': ('APPROVED requires generation_revision and a '
                          'review_catalog_digest plus confirmations list from '
                          'the current review catalog')
            }), 400
    try:
        state = transition_fulfillment(
            _fulfillment_status_path(order_id), destination,
            ('operator-secret' if destination == APPROVED
             else str(data.get('coach', ''))),
            waiver=data.get('waiver'),
            platform=str(data.get('platform', '')), evidence=str(data.get('evidence', '')),
            expected_revision=(expected_revision if destination == APPROVED else None),
            expected_catalog_digest=(
                expected_catalog_digest if destination == APPROVED else ''),
            review_decisions=(review_decisions if destination == APPROVED else None),
            credential=('operator-secret'
                        if destination in (APPROVED, CANCELLED) else ''),
            metadata=({'reason': str(data.get('reason') or '').strip()}
                      if destination == CANCELLED else None),
        )
    except FulfillmentStateError as exc:
        return jsonify({'error': str(exc)}), 409
    return jsonify({'order_id': order_id, 'athlete_id': state['athlete_id'],
                    'status': state['status'],
                    'generation_revision': state['generation_revision']}), 200


@app.route('/api/fulfillment/<order_ref>/status', methods=['GET'])
def fulfillment_status(order_ref):
    """Authoritative fulfillment state for the athlete — status, timestamps,
    and an evidence summary (approval/waiver/application/confirmation).

    This is the source of truth the TP apply CLI polls for its APPROVED
    preflight gate; ``fulfillment_status.json`` is deliberately excluded from
    downloaded packages, so a stale local snapshot can never satisfy that
    gate (spec sol r2 F1). Same auth as the transition endpoint.
    """
    secret = request.headers.get('X-Cron-Secret', '')
    if not secret or not hmac.compare_digest(secret, os.environ.get('CRON_SECRET', '')):
        return jsonify({'error': 'Unauthorized'}), 401
    order_id = _resolve_order_id(order_ref)
    if not order_id:
        return jsonify({'error': 'Fulfillment state not found'}), 404
    try:
        state = load_fulfillment_state(_fulfillment_status_path(order_id))
    except FulfillmentStateError:
        return jsonify({'error': 'Fulfillment state not found'}), 404
    seal_verified = False
    manifest = None
    if state.get('model_seal') and not state.get('legacy'):
        revision_dir = (_order_dir(order_id) / 'revisions'
                        / f"r{state['generation_revision']}")
        try:
            manifest = verify_release_manifest(state, revision_dir)
            seal_verified = True
        except FulfillmentStateError as exc:
            logger.error(
                f"Status seal verification failed for order {order_id}: {exc}")
            try:
                state = record_seal_mismatch(
                    _fulfillment_status_path(order_id), str(exc))
            except FulfillmentStateError:
                return jsonify({'error': 'Fulfillment state unavailable'}), 409
    tp_record = _tp_manifest_record(manifest)
    release_authorized = bool(seal_verified and approval_matches_release(state))
    response = {
        'order_id': order_id,
        'athlete_id': state['athlete_id'],
        'delivery_platform': state['delivery_platform'],
        'status': state['status'],
        'legacy': bool(state.get('legacy')),
        'release_authorized': release_authorized,
        'seal_verified': seal_verified,
        'tp_manifest_sha256': tp_record.get('sha256'),
        'generation_revision': state['generation_revision'],
        'updated_at': state['updated_at'],
        'blocking_issues': state['blocking_issues'],
        'required_confirmations': state['required_confirmations'],
        'soft_confirmations': state.get('soft_confirmations', []),
        'review_catalog_version': state.get('review_catalog_version'),
        'review_items': state.get('review_items', []),
        'model_seal': state['model_seal'],
        'release_manifest_digest': state['release_manifest_digest'],
        'approval': state['approval'],
        'waiver': state['waiver'],
        'application': state['application'],
        'confirmation': state['confirmation'],
        'superseded_approvals': state.get('superseded_approvals', []),
    }
    from fulfillment_state import external_state_projection
    return jsonify(external_state_projection(response)), 200


@app.route('/api/fulfillment/<order_ref>/apply-gate', methods=['GET'])
def live_trainingpeaks_apply_gate(order_ref):
    """Legacy browser apply is outside D1 and unconditionally disabled."""
    response = jsonify({
        'error': ('legacy browser apply gate is REFUSED in Phase 4; '
                  'the worker is read-only'),
    })
    response.headers['Access-Control-Allow-Origin'] = 'https://app.trainingpeaks.com'
    response.headers['Cache-Control'] = 'no-store'
    return response, 409


@app.route('/api/fulfillment/<order_ref>/bind-legacy', methods=['POST'])
def bind_legacy_fulfillment_order(order_ref):
    """Authenticated coach assertion for a quarantined schema-v1 order."""
    secret = request.headers.get('X-Cron-Secret', '')
    if not secret or not hmac.compare_digest(secret, os.environ.get('CRON_SECRET', '')):
        return jsonify({'error': 'Unauthorized'}), 401
    order_id = _resolve_order_id(order_ref)
    if not order_id:
        return jsonify({'error': 'Fulfillment state unavailable'}), 409
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or _has_client_timestamp(data):
        return jsonify({'error': 'JSON body without client timestamps is required'}), 400
    try:
        state = bind_legacy_order(
            _fulfillment_status_path(order_id),
            str(data.get('ledger_order_id') or ''),
            str(data.get('coach') or ''),
        )
    except FulfillmentStateError as exc:
        return jsonify({'error': str(exc)}), 409
    return jsonify({
        'order_id': state['order_id'],
        'athlete_id': state['athlete_id'],
        'legacy_binding': state['legacy_binding'],
        'status': state['status'],
    }), 200


@app.route('/api/confirm/<order_ref>', methods=['POST'])
def confirm_plan_ready(order_ref):
    """Send "your plan is live on TrainingPeaks" email to customer.

    Coach triggers this AFTER reviewing the plan and importing to TP.
    Secured by X-Cron-Secret.
    """
    secret = request.headers.get('X-Cron-Secret', '')
    if not secret or not hmac.compare_digest(secret, os.environ.get('CRON_SECRET', '')):
        return jsonify({'error': 'Unauthorized'}), 401

    order_id = _resolve_order_id(order_ref)
    if not order_id:
        return jsonify({'error': 'Fulfillment state unavailable'}), 409

    # Fail closed before looking up an order or constructing/sending mail.
    # The actual send+transition below is also serialized for exactly-once mail.
    try:
        state = load_fulfillment_state(_fulfillment_status_path(order_id))
    except FulfillmentStateError:
        return jsonify({'error': 'Fulfillment state unavailable'}), 409
    if state.get('legacy'):
        return jsonify({
            'error': 'Legacy order is quarantined and must be regenerated before confirmation'
        }), 409
    if state.get('delivery_platform') == 'endure':
        return jsonify({
            'error': 'Endure confirmation is disabled in Phase 1 by D4/R9 condition 11'
        }), 409
    if state.get('delivery_platform') != 'trainingpeaks':
        return jsonify({
            'error': 'This Phase 1 confirmation route is TrainingPeaks-only'
        }), 409
    if state['status'] not in (APPLIED, CONFIRMED):
        return jsonify({'error': 'Plan must be APPLIED before confirmation'}), 409
    norm_id = _normalize_athlete_id(state['athlete_id'])
    if state['status'] == CONFIRMED:
        return jsonify({'status': 'confirmed', 'athlete_id': norm_id}), 200

    revision_dir = (_order_dir(order_id) / 'revisions'
                    / f"r{state['generation_revision']}")
    try:
        if not approval_matches_release(state):
            raise FulfillmentStateError(
                'release approval does not match the current seal')
        manifest = verify_release_manifest(state, revision_dir)
        artifact_paths = {
            str(item.get('path') or '') for item in manifest['artifacts']}

        personal_email_bytes = None
        personal_relative = 'artifacts/personal_email.md'
        if personal_relative in artifact_paths:
            handle = open_verified_release_artifact(
                state, revision_dir, personal_relative)
            try:
                personal_email_bytes = handle.read()
            finally:
                handle.close()

        intake_backup_bytes = None
        intake_relative = 'artifacts/intake_backup.json'
        if intake_relative in artifact_paths:
            handle = open_verified_release_artifact(
                state, revision_dir, intake_relative)
            try:
                intake_backup_bytes = handle.read()
            finally:
                handle.close()

        guide_attachments = []
        for guide_name in ('training_guide.pdf', 'training_guide.html'):
            relative = f'artifacts/{guide_name}'
            if relative not in artifact_paths:
                continue
            handle = open_verified_release_artifact(state, revision_dir, relative)
            try:
                guide_attachments.append((guide_name, handle.read()))
            finally:
                handle.close()
            break
    except FulfillmentStateError as exc:
        logger.error(
            f'Confirmation seal verification failed for order {order_id}: {exc}')
        try:
            record_seal_mismatch(_fulfillment_status_path(order_id), str(exc))
        except FulfillmentStateError:
            return jsonify({'error': 'Fulfillment state unavailable'}), 409
        return jsonify({'error': 'Release seal verification failed'}), 409

    # Find customer email and race from order logs
    log_dir = Path(DATA_DIR) / '.logs'
    customer_email = None
    customer_name = None
    race_name = None
    brand = DEFAULT_BRAND

    for log_file in sorted(log_dir.glob('*.jsonl'), reverse=True):
        try:
            with open(log_file) as f:
                for line in f:
                    entry = json.loads(line.strip())
                    if (entry.get('order_id') == order_id
                            and entry.get('success')):
                        customer_email = entry.get('email', '')
                        customer_name = entry.get('name', '')
                        brand = normalize_brand(entry.get('brand'))
                        break
        except (json.JSONDecodeError, IOError):
            continue
        if customer_email:
            break

    if not customer_email:
        return jsonify({'error': 'Customer email not found in order logs'}), 404

    # Load intake backup for race details
    if intake_backup_bytes is not None:
        try:
            backup = json.loads(intake_backup_bytes)
            race_name = backup.get('race_name', '')
            brand = normalize_brand(backup.get('brand') or brand)
        except Exception:
            pass

    first_name = customer_name.split()[0] if customer_name else 'there'
    race_mention = f' for {race_name}' if race_name else ''
    brand_cfg = _brand_config(brand)
    signature = brand_cfg.get('email', {})
    signature_name = signature.get('signature_name', 'Matti')
    signature_org = signature.get('signature_organization', brand_cfg['name'])
    signature_site = signature.get('signature_site', brand_cfg['site'].replace('https://', ''))

    def _send_and_mark(send):
        """Keep mail send and CONFIRMED transition in one athlete lock."""
        try:
            action, _ = confirm_after_send(
                _fulfillment_status_path(order_id), send,
                metadata={'provider': 'resend'},
            )
            return action != 'idempotent' or True, None
        except RuntimeError:
            return False, ('email', 502)
        except FulfillmentStateError:
            return False, ('state', 409)

    # Check for personalized email generated by the pipeline
    if personal_email_bytes is not None:
        try:
            personal_md = personal_email_bytes.decode('utf-8').strip()
            # Extract subject line (first line starting with **Subject:**)
            subject = f'Your training plan{race_mention} is live on TrainingPeaks'
            for line in personal_md.split('\n'):
                if line.startswith('**Subject:**'):
                    subject = line.replace('**Subject:**', '').strip()
                    break

            # Strip the subject line from body
            body_lines = [l for l in personal_md.split('\n')
                          if not l.startswith('**Subject:**')]
            text_body = '\n'.join(body_lines).strip()

            # Convert markdown bold to HTML
            import re
            html_body = text_body
            html_body = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html_body)
            html_body = html_body.replace('\n\n', '</p><p style="font-size: 15px; line-height: 1.6;">')
            html_body = html_body.replace('\n', '<br>')
            html_body = f"""
<div style="font-family: 'Helvetica Neue', Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #333;">
  <div style="background: #59473c; color: white; padding: 24px; border-radius: 4px 4px 0 0;">
    <h1 style="margin: 0; font-size: 22px;">Your plan is live</h1>
    {f'<p style="margin: 6px 0 0; opacity: 0.9; font-size: 15px;">{race_name}</p>' if race_name else ''}
  </div>
  <div style="background: #f9f9f7; padding: 24px; border: 1px solid #e0e0e0; border-top: none;">
    <p style="font-size: 15px; line-height: 1.6;">{html_body}</p>
  </div>
</div>"""

            ok, failure = _send_and_mark(lambda: _send_email(
                customer_email, subject, text_body, html=html_body,
                reply_to=NOTIFICATION_EMAIL, attachments=guide_attachments,
                brand=brand))
            if ok:
                logger.info(f"Sent personal email to {_mask_email(customer_email)} for {norm_id}")
                return jsonify({
                    'status': 'confirmed',
                    'athlete_id': norm_id,
                    'email': _mask_email(customer_email),
                    'source': 'personal_email.md',
                })
            else:
                return jsonify({'error': 'Failed to send personal email' if failure and failure[0] == 'email' else 'Fulfillment state unavailable'}), failure[1]
        except Exception as e:
            logger.warning(f"Failed to load personal_email.md, falling back to generic: {e}")

    # --- Fallback: generic confirmation email ---
    subject = f'Your training plan{race_mention} is live on TrainingPeaks'

    text_body = f"""Hey {first_name},

Your custom training plan{race_mention} is built, reviewed, and live on TrainingPeaks.

Here's what to do:
1. Connect with me on TrainingPeaks: https://home.trainingpeaks.com/attachtocoach?sharedKey=2OTEPC6BXNVQU
2. Your calendar now has every workout loaded, day by day, through race week.
3. Each workout has target power zones, duration, and structure — just follow the plan.
4. Do today's workout. Don't overthink it.

A few things to know:
- Week 1 is calibration. It may feel easy. That's intentional.
- If life gets in the way and you miss a day, skip it and move on. Don't double up.
- I can see your completed workouts in TP. I'm watching — in a good way.

If you have questions at any point, just reply to this email.

— {signature_name}, {signature_org}
{signature_site}
"""

    html_body = f"""
<div style="font-family: 'Helvetica Neue', Arial, sans-serif; max-width: 600px; margin: 0 auto; color: #333;">
  <div style="background: #59473c; color: white; padding: 24px; border-radius: 4px 4px 0 0;">
    <h1 style="margin: 0; font-size: 22px;">Your plan is live</h1>
    {f'<p style="margin: 6px 0 0; opacity: 0.9; font-size: 15px;">{race_name}</p>' if race_name else ''}
  </div>

  <div style="background: #f9f9f7; padding: 24px; border: 1px solid #e0e0e0; border-top: none;">
    <p style="font-size: 15px; line-height: 1.6;">Hey {first_name},</p>

    <p style="font-size: 15px; line-height: 1.6;">Your custom training plan{race_mention} is built, reviewed, and <strong>live on TrainingPeaks</strong>.</p>

    <h3 style="margin: 24px 0 12px; font-size: 16px; color: #59473c;">Get started</h3>
    <ol style="font-size: 14px; padding-left: 20px; line-height: 2.2;">
      <li><strong><a href="https://home.trainingpeaks.com/attachtocoach?sharedKey=2OTEPC6BXNVQU" style="color: #1A8A82;">Connect with me on TrainingPeaks</a></strong> — click this link to attach to my coach account.</li>
      <li><strong>Check your calendar</strong> — every workout is loaded, day by day, through race week.</li>
      <li><strong>Follow the structure</strong> — each workout has target power zones, duration, and intervals.</li>
      <li><strong>Do today's workout.</strong> Don't overthink it.</li>
    </ol>

    <div style="margin: 24px 0; padding: 16px; background: #fff; border-left: 3px solid #59473c;">
      <p style="margin: 0 0 8px; font-size: 14px; color: #555;"><strong>Good to know:</strong></p>
      <ul style="font-size: 14px; padding-left: 18px; line-height: 1.8; color: #555; margin: 0;">
        <li>Week 1 is calibration. It may feel easy. That's intentional.</li>
        <li>If life gets in the way, skip the day and move on. Don't double up.</li>
        <li>I can see your completed workouts in TP. I'm watching — in a good way.</li>
      </ul>
    </div>

    <p style="font-size: 14px; line-height: 1.6;">Questions at any point? Just reply to this email.</p>


    <p style="font-size: 14px; margin-top: 24px; color: #666;">— {signature_name}, {signature_org}<br>
    <a href="{brand_cfg['site']}" style="color: #1A8A82;">{signature_site}</a></p>
  </div>
</div>"""

    ok, failure = _send_and_mark(lambda: _send_email(
        customer_email, subject, text_body, html=html_body,
        reply_to=NOTIFICATION_EMAIL, attachments=guide_attachments,
        brand=brand))
    if ok:
        logger.info(f"Sent plan confirmation to {_mask_email(customer_email)} for {norm_id}")
        return jsonify({
            'status': 'confirmed',
            'athlete_id': norm_id,
            'email': _mask_email(customer_email),
        })
    else:
        return jsonify({'error': 'Failed to send confirmation email' if failure and failure[0] == 'email' else 'Fulfillment state unavailable'}), failure[1]


@app.route('/api/questionnaire-started', methods=['POST', 'OPTIONS'])
@limiter.limit("10/minute")
def questionnaire_started():
    """Log when a user fills in name + email on the questionnaire.

    Stores contact info so we can follow up if they abandon the form
    before reaching Stripe checkout. Deduplicates by email within 24hrs.
    """
    if request.method == 'OPTIONS':
        return '', 204

    data = request.get_json(silent=True)
    if not data:
        return '', 204  # Fail silently — this is a beacon

    email = (data.get('email') or '').strip().lower()
    name = (data.get('name') or '').strip()
    if not email or '@' not in email:
        return '', 204

    # Scheduled checkout probes must never enter the abandoned-questionnaire
    # lifecycle. Otherwise the daily health workflow writes a synthetic lead
    # and can notify the coach even though no customer started a form.
    source = (data.get('source') or '').strip().lower()
    monitor_prefixes = ('healthcheck@', 'checkout-monitor@', 'monitor@')
    if source == 'health-check' and email.startswith(monitor_prefixes):
        logger.info("Questionnaire health check ignored before persistence")
        return jsonify({'status': 'ignored'}), 200

    # Store in monthly questionnaire-starts log (dedup by email within 24hrs)
    log_dir = Path(DATA_DIR) / '.logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    starts_file = log_dir / f"questionnaire-starts-{now.strftime('%Y-%m')}.jsonl"

    # Check current + previous month for recent duplicate (24hr window can span months)
    prev_month = (now.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
    files_to_check = [starts_file, log_dir / f"questionnaire-starts-{prev_month}.jsonl"]
    try:
        for check_file in files_to_check:
            if not check_file.exists():
                continue
            with open(check_file, 'r') as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        if (entry.get('email') == email and
                                (now - datetime.fromisoformat(entry['timestamp'])).total_seconds() < 86400):
                            return jsonify({'status': 'already_tracked'}), 200
                    except (json.JSONDecodeError, KeyError):
                        continue
    except IOError:
        pass

    entry = {
        'timestamp': now.isoformat(),
        'email': email,
        'name': name,
        'sections_reached': data.get('sections_reached', 0),
        'source': data.get('source', ''),
        'user_agent': request.headers.get('User-Agent', '')[:200],
    }

    try:
        with open(starts_file, 'a') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            f.write(json.dumps(entry) + '\n')
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except IOError as e:
        logger.error(f"Failed to log questionnaire start: {e}")

    logger.info(f"Questionnaire started: {_mask_email(email)} ({name.split()[0] if name else '?'})")

    # Notify coach of new questionnaire start
    if NOTIFICATION_EMAIL and RESEND_API_KEY:
        subject = f"Questionnaire started: {name or 'Unknown'}"
        body = (
            f"Someone started the training plan questionnaire.\n\n"
            f"Name: {name}\n"
            f"Email: {email}\n"
            f"Time: {now.strftime('%Y-%m-%d %H:%M')} UTC\n\n"
            f"If they don't complete checkout within a few hours, "
            f"consider a personal follow-up.\n"
        )
        _send_email(NOTIFICATION_EMAIL, subject, body)

    return jsonify({'status': 'tracked'}), 200


@app.route('/api/create-checkout', methods=['POST', 'OPTIONS'])
@limiter.limit("20/minute")
def create_checkout():
    """Create a Stripe Checkout Session from questionnaire data.

    Receives the full questionnaire submission, stores it temporarily,
    creates a Stripe Checkout Session, and returns the checkout URL.
    The customer completes payment on Stripe's hosted page, then the
    webhook handler loads the stored data to build the profile.
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        return '', 204

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    # Only explicitly consented browser identifiers matching GA4's web formats
    # may cross the trust boundary into intake storage and Stripe metadata.
    ga4_client_id, ga4_session_id, analytics_consent = \
        _payload_ga4_attribution(data)
    data['analytics_consent'] = analytics_consent
    if ga4_client_id:
        data['ga4_client_id'] = ga4_client_id
    if ga4_session_id:
        data['ga4_session_id'] = ga4_session_id

    # Validate required fields
    email = (data.get('email') or '').strip().lower()
    if not email or '@' not in email or '.' not in email:
        return jsonify({'error': 'Valid email is required'}), 400

    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400

    # Validate at least one race
    races = data.get('races', [])
    if not races:
        return jsonify({'error': 'At least one race is required'}), 400

    # Compute price from A-race date
    a_race = next((r for r in races if r.get('priority') == 'A'), races[0])
    race_date_str = a_race.get('date', '')
    if not race_date_str:
        return jsonify({'error': 'A-race date is required'}), 400

    # Reject race dates more than 7 days in the past
    try:
        parsed_race_date = datetime.strptime(race_date_str, '%Y-%m-%d').date()
        if (date.today() - parsed_race_date).days > 7:
            return jsonify({'error': 'Race date cannot be more than 7 days in the past'}), 400
    except ValueError:
        return jsonify({'error': 'Invalid race date format (expected YYYY-MM-DD)'}), 400

    pricing = compute_plan_price(race_date_str)

    # Brand follows the requesting site (gravelgodcycling.com / roadielabs.com)
    brand = _brand_from_origin(request.headers.get('Origin', ''))
    brand_cfg = _brand_config(brand)

    if not brand_cfg.get('training_plan_generation_enabled', True):
        return jsonify({
            'error': f"{brand_cfg.get('name', brand)} does not support training-plan "
                     "generation yet"
        }), 400

    try:
        addon_selection = resolve_plan_addons(data.get('plan_addons'), brand)
        addon_line_items = stripe_line_items_for_addons(
            addon_selection['optional'])
    except AddonSelectionError as exc:
        return jsonify({'error': str(exc)}), 400

    # Generate intake ID and store questionnaire data
    intake_id = str(uuid.uuid4())
    data['computed_price_cents'] = pricing['price_cents']
    data['computed_weeks'] = pricing['weeks']
    data['brand'] = brand
    data['plan_addons'] = addon_selection['all']
    store_intake(intake_id, data)

    # Look up pre-built price ID, capping at 17 for 17+ weeks
    price_key = min(pricing['weeks'], 17)
    price_id = TRAINING_PLAN_PRICE_IDS.get(price_key)

    # Create Stripe Checkout Session
    try:
        line_items = [{'price': price_id, 'quantity': 1}] if price_id else [{
            'price_data': {
                'currency': 'usd',
                'product_data': {
                    'name': STRIPE_PRODUCT_NAME,
                    'description': f"{pricing['weeks']}-week custom training plan",
                },
                'unit_amount': pricing['price_cents'],
            },
            'quantity': 1,
        }]
        line_items.extend(addon_line_items)

        expires_at = int((datetime.now() + timedelta(minutes=CHECKOUT_EXPIRY_MINUTES)).timestamp())

        checkout_metadata = {
            'intake_id': intake_id,
            'product_type': 'training_plan',
            'tier': 'custom',
            'athlete_name': name,
            'weeks': str(pricing['weeks']),
            'price_cents': str(pricing['price_cents']),
            'brand': brand,
            'plan_addons': ','.join(addon_selection['all']),
        }
        if ga4_client_id:
            checkout_metadata['ga4_client_id'] = ga4_client_id
        if ga4_session_id:
            checkout_metadata['ga4_session_id'] = ga4_session_id
        checkout_metadata['analytics_consent'] = analytics_consent

        session_kwargs = dict(
            line_items=line_items,
            mode='payment',
            customer_email=email,
            customer_creation='always',
            client_reference_id=intake_id,
            metadata=checkout_metadata,
            success_url=f"{brand_cfg['site']}/training-plans/success/?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{brand_cfg['site']}{brand_cfg['questionnaire_path']}",
            expires_at=expires_at,
            after_expiration={
                'recovery': {
                    'enabled': True,
                    'allow_promotion_codes': True,
                }
            },
            consent_collection={
                'promotions': 'auto',
            },
        )
        if ENABLE_AUTOMATIC_TAX:
            session_kwargs['automatic_tax'] = {'enabled': True}

        checkout_session = stripe.checkout.Session.create(**session_kwargs)

        logger.info(f"Created checkout session {checkout_session.id} for intake {intake_id} "
                     f"({pricing['weeks']}wk, {pricing['price_display']}, {_mask_email(email)})")

        return jsonify({
            'checkout_url': checkout_session.url,
            'intake_id': intake_id,
            'price': pricing,
        })

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error creating checkout: {e}")
        return jsonify({'error': 'Payment service error. Please try again.'}), 502
    except Exception as e:
        logger.exception(f"Checkout creation error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


def _verify_coaching_checkout_contract(
        brand: str, tier: str, setup_fee_waived: bool = False) -> tuple[bool, str]:
    """Read back live Stripe terms before exposing an approved checkout."""
    coaching_cfg = _coaching_config(brand)
    tier_cfg = _coaching_tier_config(brand, tier)
    try:
        recurring = stripe.Price.retrieve(COACHING_PRICE_IDS[tier])._to_dict_recursive()
        setup = stripe.Price.retrieve(
            COACHING_SETUP_FEE_PRICE_ID)._to_dict_recursive()
        coupon = (
            stripe.Coupon.retrieve(
                COACHING_SETUP_FEE_WAIVER_COUPON_ID)._to_dict_recursive()
            if setup_fee_waived else None)
    except (KeyError, AttributeError, stripe.error.StripeError) as exc:
        return False, f'provider readback failed: {type(exc).__name__}'

    recurring_terms = recurring.get('recurring') or {}
    expected_cadence = int(coaching_cfg.get('billing_period_days', 0))
    checks = {
        'tier price is active': recurring.get('active') is True,
        'tier amount matches registry': recurring.get('unit_amount') == tier_cfg.get('price_cents'),
        'tier currency is USD': recurring.get('currency') == 'usd',
        'tier cadence is four weeks': (
            expected_cadence == 28 and recurring_terms.get('interval') == 'week' and
            recurring_terms.get('interval_count') == 4),
        'setup price is active': setup.get('active') is True,
        'setup fee amount matches registry': (
            setup.get('unit_amount') == coaching_cfg.get('setup_fee_cents')),
        'setup fee is one-time USD': (
            setup.get('type') == 'one_time' and setup.get('currency') == 'usd'),
        'waiver coupon is fixed $99 once': (not setup_fee_waived or (
            coupon.get('valid') is True and
            coupon.get('amount_off') == coaching_cfg.get('setup_fee_cents') and
            coupon.get('currency') == 'usd' and
            coupon.get('duration') == 'once' and
            coupon.get('percent_off') is None)),
    }
    failed = [label for label, passed in checks.items() if not passed]
    return (not failed, '; '.join(failed))


def _create_coaching_checkout_session(name: str, email: str, tier: str,
                                      brand: str, intake_id: str = '',
                                      setup_fee_waived: bool = False,
                                      ga4_client_id: str = '',
                                      ga4_session_id: str = '',
                                      analytics_consent: str = 'unknown'):
    """Create the Stripe object after the caller has authorized the handoff."""
    brand_cfg = _brand_config(brand)
    coaching_cfg = _coaching_config(brand)
    price_id = COACHING_PRICE_IDS[tier]
    expires_at = int((datetime.now() + timedelta(
        minutes=CHECKOUT_EXPIRY_MINUTES)).timestamp())

    line_items = [{'price': price_id, 'quantity': 1}]
    if COACHING_SETUP_FEE_PRICE_ID:
        line_items.append({'price': COACHING_SETUP_FEE_PRICE_ID, 'quantity': 1})

    metadata = {
        'product_type': 'coaching',
        'tier': tier,
        'athlete_name': name,
        'brand': brand,
        'setup_fee_waived': str(bool(setup_fee_waived)).lower(),
    }
    if intake_id:
        metadata['intake_id'] = intake_id
    safe_client_id, safe_session_id = _validated_ga4_attribution(
        ga4_client_id, ga4_session_id)
    if analytics_consent != 'granted':
        safe_client_id, safe_session_id = '', ''
    _apply_ga4_metadata(
        metadata, safe_client_id, safe_session_id, analytics_consent)

    subscription_metadata = {
        'tier': tier,
        'athlete_name': name,
        'brand': brand,
    }
    if intake_id:
        subscription_metadata['intake_id'] = intake_id
    _apply_ga4_metadata(
        subscription_metadata, safe_client_id, safe_session_id,
        analytics_consent)

    session_kwargs = dict(
        line_items=line_items,
        mode='subscription',
        customer_email=email,
        phone_number_collection={'enabled': True},
        metadata=metadata,
        subscription_data={'metadata': subscription_metadata},
        success_url=(f"{brand_cfg['site']}{coaching_cfg['success_path']}"
                     '?session_id={CHECKOUT_SESSION_ID}'),
        cancel_url=f"{brand_cfg['site']}{coaching_cfg['path']}",
        expires_at=expires_at,
        after_expiration={'recovery': {'enabled': True}},
        consent_collection={'promotions': 'auto'},
    )
    if setup_fee_waived:
        session_kwargs['discounts'] = [{
            'coupon': COACHING_SETUP_FEE_WAIVER_COUPON_ID,
        }]
    if ENABLE_AUTOMATIC_TAX:
        session_kwargs['automatic_tax'] = {'enabled': True}
    return stripe.checkout.Session.create(**session_kwargs)


@app.route('/api/coaching-intakes', methods=['POST', 'OPTIONS'])
@limiter.limit("20/minute")
def receive_coaching_intake():
    """Receive one brand-scoped application from the trusted edge worker."""
    if request.method == 'OPTIONS':
        return '', 204
    if not COACHING_INTAKE_SECRET:
        return jsonify({'error': 'Coaching intake is not configured'}), 503
    supplied = request.headers.get('X-Coaching-Intake-Secret', '')
    if not supplied or not hmac.compare_digest(supplied, COACHING_INTAKE_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    case_id = str(data.get('submission_id') or '').strip()
    try:
        uuid.UUID(case_id)
    except (ValueError, AttributeError):
        return jsonify({'error': 'Valid submission_id is required'}), 400

    raw_brand = str(data.get('brand') or '').strip().lower()
    if raw_brand not in BRANDS:
        return jsonify({'error': 'Unknown brand'}), 400
    coaching_cfg = _coaching_config(raw_brand)
    if not coaching_cfg.get('enabled'):
        return jsonify({'error': 'Coaching is not available for this brand'}), 400

    tier = str(data.get('tier') or '').strip().lower()
    if not _coaching_tier_config(raw_brand, tier):
        return jsonify({'error': 'Valid coaching tier is required'}), 400
    name = str(data.get('name') or '').strip()
    email = str(data.get('email') or '').strip().lower()
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    if not email or '@' not in email or '.' not in email:
        return jsonify({'error': 'Valid email is required'}), 400

    questionnaire = data.get('questionnaire') or {}
    if not isinstance(questionnaire, dict):
        return jsonify({'error': 'Questionnaire must be an object'}), 400
    if len(json.dumps(questionnaire)) > 200_000:
        return jsonify({'error': 'Questionnaire is too large'}), 413
    ga4_client_id, ga4_session_id, analytics_consent = \
        _payload_ga4_attribution(data)
    age = _coaching_age(questionnaire)
    if age is not None and age < 13:
        return jsonify({
            'error': 'This intake currently supports athletes age 13 and older'
        }), 400
    if age is not None and age < 18:
        guardian_email = str(questionnaire.get('guardian_email') or '').strip().lower()
        guardian_relationship = str(
            questionnaire.get('guardian_relationship') or '').strip()
        if (not guardian_email or '@' not in guardian_email or
                '.' not in guardian_email):
            return jsonify({'error': 'Valid parent/guardian email is required'}), 400
        if guardian_relationship not in ('parent', 'legal_guardian'):
            return jsonify({
                'error': 'Parent/guardian relationship must be parent or legal_guardian'
            }), 400

    now = datetime.now(timezone.utc).isoformat()
    case = {
        'schema': 'coaching_onboarding_case/v1',
        'case_id': case_id,
        'athlete_key': (
            'email_sha256:' + hashlib.sha256(email.encode('utf-8')).hexdigest()
        ),
        'brand': raw_brand,
        'tier': tier,
        'state': 'FIT_REVIEW',
        'athlete': {
            'name': name,
            'email': email,
            'is_minor': bool(age is not None and age < 18),
        },
        'source': {
            'type': 'coaching_intake_form',
            'submission_id': case_id,
            'submitted_at': now,
            'analytics_consent': analytics_consent,
        },
        'questionnaire': questionnaire,
        'intake_audit': _coaching_intake_audit(questionnaire, raw_brand),
        'transitions': [{
            'from_state': None,
            'to_state': 'FIT_REVIEW',
            'actor': 'athlete',
            'timestamp': now,
            'reason': 'Coaching intake submitted',
            'source_id': case_id,
        }],
        'receipts': {},
        'verifications': {},
    }
    if ga4_client_id:
        case['source']['ga4_client_id'] = ga4_client_id
    if ga4_session_id:
        case['source']['ga4_session_id'] = ga4_session_id
    _record_coaching_event(
        case, 'coaching_intake_submitted', case_id, occurred_at=now)
    if age is not None and age < 18:
        case['guardian'] = {
            'name': str(questionnaire.get('guardian_name') or '').strip(),
            'email': str(questionnaire.get('guardian_email') or '').strip().lower(),
            'relationship': str(
                questionnaire.get('guardian_relationship') or '').strip(),
        }
    case['readiness'] = _coaching_case_readiness(case)
    if not _write_coaching_intake(case, create_only=True):
        existing = _read_coaching_intake(case_id)
        return jsonify({
            'success': True,
            'duplicate': True,
            'case_id': case_id,
            'state': existing.get('state', 'FIT_REVIEW'),
        })

    brand_cfg = _brand_config(raw_brand)
    tier_label = _coaching_tier_config(raw_brand, tier).get('label', tier.title())
    first_name = name.split()[0] if name else 'there'
    receipt_subject = f'Coaching intake received — {brand_cfg["name"]}'
    receipt_body = (
        f"Hey {first_name},\n\n"
        "I have your coaching intake. I’ll review it and usually reply within "
        "two business days with the next steps or any questions I need "
        "answered.\n\n"
        "You do not need to submit the form again.\n\n"
        f"— {brand_cfg['email'].get('signature_name', 'Matti')}\n"
        f"{brand_cfg['email'].get('signature_organization', brand_cfg['name'])}\n"
        f"{brand_cfg['email'].get('signature_site', brand_cfg['site'].replace('https://', ''))}"
    )
    receipt_sent = _send_email(
        email, receipt_subject, receipt_body,
        reply_to=NOTIFICATION_EMAIL or None, brand=raw_brand)
    case['receipts']['athlete_intake_email'] = {
        'sent': receipt_sent,
        'attempted_at': datetime.now(timezone.utc).isoformat(),
    }

    approve_url = (
        'https://athlete-custom-training-plan-pipeline-production.up.railway.app'
        f'/api/coaching-intakes/{case_id}/approve')
    review_url = (
        'https://athlete-custom-training-plan-pipeline-production.up.railway.app'
        f'/api/coaching-intakes/{case_id}')
    audit = case['intake_audit']
    follow_up = (
        audit['missing_required'] + audit['missing_followup'] + audit['unasked']
    )[:8]
    follow_up_text = (
        '\n'.join(f'- {item}' for item in follow_up)
        if follow_up else '- None identified by the intake audit'
    )
    coach_subject = (
        f"{brand_cfg.get('subject_prefix', '[GG]')} Coaching application: "
        f"{name} — {tier_label}")
    coach_body = (
        f"Coaching application received\n\n"
        f"Name: {name}\nEmail: {email}\nBrand: {raw_brand}\n"
        f"Tier: {tier_label}\nCase: {case_id}\n\n"
        f"Intake audit: {len(audit['missing_required'])} required missing, "
        f"{len(audit['missing_followup'])} optional follow-ups, "
        f"{len(audit['unasked'])} not yet asked, "
        f"{len(audit['unverified'])} unverified groups.\n"
        f"First follow-ups:\n{follow_up_text}\n\n"
        "Review the questionnaire before approving fit. Fit approval does not "
        "create a charge or treat the application as acceptance. Identity, "
        "health disposition, agreement, and data-consent receipts must be "
        "verified before the separate payment handoff.\n\n"
        f"Review:\ncurl '{review_url}' -H 'X-Cron-Secret: $CRON_SECRET'\n\n"
        "Approve:\n"
        f"curl -X POST '{approve_url}' -H 'X-Cron-Secret: $CRON_SECRET'"
    )
    coach_sent = bool(NOTIFICATION_EMAIL) and _send_email(
        NOTIFICATION_EMAIL, coach_subject, coach_body,
        reply_to=email, brand=raw_brand)
    case['receipts']['coach_notification'] = {
        'sent': coach_sent,
        'attempted_at': datetime.now(timezone.utc).isoformat(),
    }
    _write_coaching_intake(case)
    if not coach_sent:
        logger.critical(
            f"COACHING INTAKE NEEDS REVIEW: case={case_id}, brand={raw_brand}, "
            f"athlete={_mask_email(email)}")

    return jsonify({
        'success': True,
        'case_id': case_id,
        'state': 'FIT_REVIEW',
        'receipt_sent': receipt_sent,
    }), 201


@app.route('/api/coaching-intakes/<case_id>', methods=['GET'])
@limiter.limit("60/minute")
def get_coaching_intake(case_id):
    """Private operator read of one onboarding case and its questionnaire."""
    supplied = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not supplied or not hmac.compare_digest(supplied, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401
    case = _read_coaching_intake(case_id)
    if not case:
        return jsonify({'error': 'Coaching intake not found'}), 404
    if 'intake_audit' not in case:
        case['intake_audit'] = _coaching_intake_audit(
            case.get('questionnaire', {}), case.get('brand', DEFAULT_BRAND))
    case['readiness'] = _coaching_case_readiness(case)
    case['esign_readiness'] = _coaching_esign_readiness(case)
    return jsonify(case)


@app.route('/api/coaching-intakes', methods=['GET'])
@limiter.limit("30/minute")
def list_coaching_intakes():
    """Private operator queue; questionnaire contents stay in case detail."""
    supplied = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not supplied or not hmac.compare_digest(supplied, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401
    requested_state = str(request.args.get('state') or '').strip().upper()
    requested_brand = str(request.args.get('brand') or '').strip().lower()
    rows = []
    for case in _iter_coaching_intakes() or ():
        readiness = _coaching_case_readiness(case)
        if requested_state and readiness['state'] != requested_state:
            continue
        if requested_brand and normalize_brand(case.get('brand')) != requested_brand:
            continue
        reminders = case.get('onboarding_reminders') or []
        rows.append({
            'case_id': case.get('case_id'),
            'brand': normalize_brand(case.get('brand')),
            'tier': case.get('tier'),
            'athlete': {
                'name': (case.get('athlete') or {}).get('name'),
                'email': (case.get('athlete') or {}).get('email'),
                'is_minor': bool((case.get('athlete') or {}).get('is_minor')),
            },
            'submitted_at': (case.get('source') or {}).get('submitted_at'),
            'state': readiness['state'],
            'next_action': readiness['next_action'],
            'billing_standing': (case.get('billing') or {}).get('standing'),
            'suggested_reminders': sum(
                1 for item in reminders if item.get('status') == 'suggested'),
        })
    rows.sort(key=lambda item: str(item.get('submitted_at') or ''), reverse=True)
    return jsonify({
        'schema': 'coaching_operator_queue/v1',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'count': len(rows),
        'cases': rows,
    })


@app.route('/api/coaching-intakes/<case_id>/esign-readiness', methods=['GET'])
@limiter.limit("30/minute")
def coaching_esign_readiness(case_id):
    """Fail-closed status for legal templates and the chosen e-sign adapter."""
    supplied = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not supplied or not hmac.compare_digest(supplied, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401
    case = _read_coaching_intake(case_id)
    if not case:
        return jsonify({'error': 'Coaching intake not found'}), 404
    readiness = _coaching_esign_readiness(case)
    return jsonify(readiness), (200 if readiness['status'] == 'ready' else 409)


@app.route('/api/coaching-intakes/<case_id>/esign-packet', methods=['POST'])
@limiter.limit("10/minute")
@_serialized_coaching_case('signwell-send')
def create_coaching_esign_packet(case_id):
    """Send one operator-approved, case-bound SignWell signature request."""
    supplied = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not supplied or not hmac.compare_digest(supplied, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401
    case = _read_coaching_intake(case_id)
    if not case:
        return jsonify({'error': 'Coaching intake not found'}), 404
    readiness = _coaching_esign_readiness(case)
    if readiness['provider'] != 'signwell' or readiness['status'] != 'ready':
        return jsonify({
            'error': 'SignWell packet issuance is not configured',
            'esign_readiness': readiness,
        }), 409
    blockers = _coaching_blockers(case, (
        ('coach_fit', 'approved', 'coach fit approval'),
        ('identity', 'verified', 'identity verification'),
        ('health_clearance', ('cleared', 'not_required'),
         'health-clearance disposition'),
    ))
    if blockers:
        return jsonify({
            'error': 'SignWell packet is blocked by onboarding gates',
            'blockers': blockers,
        }), 409
    existing = case.get('esign_packet') or {}
    if existing.get('document_id'):
        return jsonify({
            'success': True,
            'duplicate': True,
            'case_id': case_id,
            'document_id': existing['document_id'],
            'status': existing.get('status'),
            'test_mode': existing.get('test_mode'),
        })

    try:
        document = SignWellClient(SIGNWELL_API_KEY).create_document_from_templates(
            _build_signwell_packet_request(case))
    except SignWellError as exc:
        logger.error(f'SignWell packet creation failed for case {case_id}: {exc}')
        return jsonify({'error': 'SignWell packet creation failed'}), 502
    document_id = str(document.get('id') or '')
    now = datetime.now(timezone.utc).isoformat()
    case['esign_packet'] = {
        'schema': 'coaching_esign_packet_receipt/v1',
        'provider': 'signwell',
        'document_id': document_id,
        'status': str(document.get('status') or 'sent').lower(),
        'requested_at': now,
        'test_mode': bool(document.get('test_mode', SIGNWELL_TEST_MODE)),
        'templates': _signwell_template_contract(case),
        'recipient_roles': [
            {
                'id': item['id'],
                'placeholder_name': item['placeholder_name'],
                'email': item['email'],
            }
            for item in _signwell_expected_recipients(case)
        ],
        'allow_reassign': False,
        'automatic_reminders': SIGNWELL_REMINDERS_ENABLED,
        'processed_event_ids': [],
    }
    _record_coaching_event(
        case, 'coaching_signwell_packet_sent', document_id,
        details={'status': case['esign_packet']['status']}, occurred_at=now)
    _write_coaching_intake(case)
    return jsonify({
        'success': True,
        'duplicate': False,
        'case_id': case_id,
        'document_id': document_id,
        'status': case['esign_packet']['status'],
        'test_mode': case['esign_packet']['test_mode'],
        'delivery': 'signwell_provider_email',
        'automatic_reminders': SIGNWELL_REMINDERS_ENABLED,
    }), 201


@app.route('/api/coaching-intakes/<case_id>/billing-portal', methods=['POST'])
@limiter.limit("10/minute")
def create_coaching_billing_portal(case_id):
    """Create a short-lived Stripe portal URL for an authenticated operator.

    The endpoint never emails the link and never changes the subscription.
    The caller must authenticate the athlete before handing the URL to them.
    """
    supplied = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not supplied or not hmac.compare_digest(supplied, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401
    case = _read_coaching_intake(case_id)
    if not case:
        return jsonify({'error': 'Coaching intake not found'}), 404
    billing = case.get('billing') or {}
    receipt = (case.get('receipts') or {}).get('stripe_payment') or {}
    customer_id = str(
        billing.get('customer_id') or receipt.get('customer_id') or '')
    subscription_id = str(
        billing.get('subscription_id') or receipt.get('subscription_id') or '')
    if not customer_id or not customer_id.startswith('cus_'):
        return jsonify({
            'error': 'A case-bound Stripe customer receipt is required'
        }), 409
    data = request.get_json(silent=True) or {}
    mode = str(data.get('mode') or 'manage').strip().lower()
    if mode not in {'manage', 'cancel'}:
        return jsonify({'error': 'mode must be manage or cancel'}), 400
    if mode == 'cancel' and not subscription_id.startswith('sub_'):
        return jsonify({
            'error': 'A case-bound Stripe subscription receipt is required'
        }), 409
    brand_cfg = _brand_config(case.get('brand'))
    coaching_cfg = _coaching_config(case.get('brand'))
    return_url = f"{brand_cfg['site']}{coaching_cfg.get('path', '/coaching/')}"
    kwargs = {'customer': customer_id, 'return_url': return_url}
    if mode == 'cancel':
        kwargs['flow_data'] = {
            'type': 'subscription_cancel',
            'subscription_cancel': {'subscription': subscription_id},
            'after_completion': {'type': 'redirect', 'redirect': {
                'return_url': return_url,
            }},
        }
    try:
        portal = stripe.billing_portal.Session.create(**kwargs)
    except (AttributeError, stripe.error.StripeError) as exc:
        logger.error(
            f'Could not create coaching billing portal for {case_id}: {exc}')
        return jsonify({'error': 'Billing portal is not available'}), 503
    portal_id = str(getattr(portal, 'id', '') or '')
    portal_url = str(getattr(portal, 'url', '') or '')
    if not portal_url.startswith('https://billing.stripe.com/'):
        logger.critical(f'Unexpected Stripe portal URL for case {case_id}')
        return jsonify({'error': 'Billing portal provider response was invalid'}), 502
    now = datetime.now(timezone.utc).isoformat()
    case.setdefault('billing_portal_receipts', []).append({
        'portal_session_id': portal_id,
        'mode': mode,
        'created_at': now,
        'actor': 'coach',
    })
    _record_coaching_event(
        case, 'coaching_billing_portal_created', portal_id or str(uuid.uuid4()),
        details={'status': mode}, occurred_at=now)
    _write_coaching_intake(case)
    return jsonify({
        'success': True,
        'case_id': case_id,
        'mode': mode,
        'portal_url': portal_url,
        'expires': 'short_lived_provider_session',
        'delivery': 'operator_must_authenticate_athlete_before_handoff',
    })


@app.route('/api/coaching-intakes/<case_id>/approve', methods=['POST'])
@limiter.limit("20/minute")
def approve_coaching_intake(case_id):
    """Record coach fit approval without creating payment or accepting terms."""
    supplied = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not supplied or not hmac.compare_digest(supplied, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401

    case = _read_coaching_intake(case_id)
    if not case:
        return jsonify({'error': 'Coaching intake not found'}), 404
    existing = case.setdefault('verifications', {}).get('coach_fit', {})
    duplicate = existing.get('status') == 'approved'
    if not duplicate:
        now = datetime.now(timezone.utc).isoformat()
        case['verifications']['coach_fit'] = {
            'status': 'approved',
            'verified_at': now,
            'actor': 'coach',
            'source_id': case_id,
        }
        _record_coaching_event(
            case, 'coaching_fit_approved', case_id, occurred_at=now)
    readiness = _refresh_coaching_case(
        case, actor='coach', reason='Coach approved athlete fit', source_id=case_id)
    _write_coaching_intake(case)

    return jsonify({
        'success': True,
        'case_id': case_id,
        'state': case['state'],
        'duplicate': duplicate,
        'readiness': readiness,
    })


@app.route('/api/coaching-intakes/<case_id>/verify', methods=['POST'])
@limiter.limit("60/minute")
def verify_coaching_intake_gate(case_id):
    """Attach an operator-authenticated evidence receipt to one onboarding gate."""
    supplied = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not supplied or not hmac.compare_digest(supplied, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401

    case = _read_coaching_intake(case_id)
    if not case:
        return jsonify({'error': 'Coaching intake not found'}), 404
    data = request.get_json(silent=True) or {}
    gate = str(data.get('gate') or '').strip()
    status = str(data.get('status') or '').strip()
    source_id = str(data.get('source_id') or '').strip()
    if gate not in _COACHING_VERIFICATION_RULES:
        return jsonify({'error': 'Unknown verification gate'}), 400
    if status not in _COACHING_VERIFICATION_RULES[gate]:
        allowed = sorted(_COACHING_VERIFICATION_RULES[gate])
        return jsonify({'error': f'Invalid status for {gate}', 'allowed': allowed}), 400
    if not source_id:
        return jsonify({'error': 'source_id is required for an evidence receipt'}), 400

    document_version = str(data.get('document_version') or '').strip()
    receipt_id = str(data.get('receipt_id') or '').strip()
    note = str(data.get('note') or '').strip()
    if gate in ('coaching_agreement', 'data_consent', 'guardian_consent'):
        if not document_version or not receipt_id:
            return jsonify({
                'error': f'{gate} requires document_version and receipt_id'
            }), 400
    signer_name = str(data.get('signer_name') or '').strip()
    signer_email = str(data.get('signer_email') or '').strip().lower()
    signer_role = str(data.get('signer_role') or '').strip().lower()
    athlete_id = str(data.get('athlete_id') or '').strip()
    if gate == 'guardian_consent':
        if not _coaching_is_minor(case):
            return jsonify({
                'error': 'guardian_consent is only valid for a minor athlete'
            }), 400
        guardian = case.get('guardian', {})
        if (not signer_name or not signer_email or
                signer_role not in ('parent', 'legal_guardian')):
            return jsonify({
                'error': ('guardian_consent requires signer_name, signer_email, '
                          'and signer_role parent or legal_guardian')
            }), 400
        if signer_email != guardian.get('email'):
            return jsonify({
                'error': 'guardian signer email does not match the intake'
            }), 400
    if gate == 'setup_fee_waiver' and not note:
        return jsonify({
            'error': 'setup_fee_waiver requires a case-specific note'
        }), 400
    if gate == 'health_clearance' and status == 'not_required' and not note:
        return jsonify({
            'error': 'health_clearance not_required requires a policy note'
        }), 400
    if gate == 'health_clearance' and status == 'cleared' and not receipt_id:
        return jsonify({
            'error': 'health_clearance cleared requires a clinician receipt_id'
        }), 400

    existing = case.setdefault('verifications', {}).get(gate, {})
    duplicate = (
        existing.get('status') == status and
        existing.get('source_id') == source_id and
        existing.get('document_version', '') == document_version and
        existing.get('receipt_id', '') == receipt_id and
        existing.get('signer_email', '') == signer_email
    )
    if not duplicate:
        receipt = {
            'status': status,
            'verified_at': datetime.now(timezone.utc).isoformat(),
            'actor': 'coach',
            'source_id': source_id,
        }
        if document_version:
            receipt['document_version'] = document_version
        if receipt_id:
            receipt['receipt_id'] = receipt_id
        if note:
            receipt['note'] = note
        if signer_name:
            receipt['signer_name'] = signer_name
        if signer_email:
            receipt['signer_email'] = signer_email
        if signer_role:
            receipt['signer_role'] = signer_role
        if gate == 'athlete_context' and athlete_id:
            if sanitize_athlete_id(athlete_id) != athlete_id:
                return jsonify({'error': 'athlete_id must be a canonical slug'}), 400
            receipt['athlete_id'] = athlete_id
        case['verifications'][gate] = receipt
        case.setdefault('verification_history', []).append({
            'gate': gate,
            **receipt,
        })
        gate_event = {
            'coaching_agreement': 'coaching_agreement_signed',
            'data_consent': 'coaching_data_consent_signed',
            'guardian_consent': 'coaching_guardian_consent_signed',
            'trainingpeaks_connection': 'coaching_trainingpeaks_connected',
            'trainingpeaks_premium': 'coaching_trainingpeaks_premium_active',
            'athlete_context': 'coaching_context_sealed',
            'coach_plan_approval': 'coaching_plan_approved',
            'onboarding_ramp': 'coaching_onboarding_ramp_complete',
        }.get(gate, 'coaching_gate_verified')
        _record_coaching_event(
            case, gate_event, source_id,
            details={'gate': gate, 'status': status},
            occurred_at=receipt['verified_at'])

    readiness = _refresh_coaching_case(
        case, actor='coach', reason=f'Verified onboarding gate: {gate}',
        source_id=source_id)
    _write_coaching_intake(case)
    return jsonify({
        'success': True,
        'case_id': case_id,
        'gate': gate,
        'status': status,
        'duplicate': duplicate,
        'state': case['state'],
        'readiness': readiness,
    })


@app.route('/api/coaching-intakes/<case_id>/onboarding-materials', methods=['POST'])
@limiter.limit("20/minute")
def create_coaching_onboarding_materials(case_id):
    """Generate the welcome guide from a sealed, paid onboarding case."""
    supplied = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not supplied or not hmac.compare_digest(supplied, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401
    if not COACHING_BOOKING_URL.startswith('https://'):
        return jsonify({
            'error': 'COACHING_BOOKING_URL is not configured with a verified HTTPS URL'
        }), 503

    case = _read_coaching_intake(case_id)
    if not case:
        return jsonify({'error': 'Coaching intake not found'}), 404
    context_receipt = case.get('verifications', {}).get('athlete_context', {})
    athlete_id = str(context_receipt.get('athlete_id') or '').strip()
    if context_receipt.get('status') != 'sealed' or not athlete_id:
        return jsonify({
            'error': 'A sealed athlete context with canonical athlete_id is required'
        }), 409

    try:
        from coaching_onboarding_materials import (
            generate_from_case, render_onboarding_email)
        context_path, welcome_path = generate_from_case(
            case, athlete_id, Path(ATHLETES_DIR), COACHING_BOOKING_URL)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409

    now = datetime.now(timezone.utc).isoformat()
    context = yaml.safe_load(context_path.read_text(encoding='utf-8')) or {}
    subject, body = render_onboarding_email(context)
    existing_delivery = case.get('onboarding_materials', {})
    athlete_sent = bool(existing_delivery.get('athlete_sent'))
    guardian_sent = bool(existing_delivery.get('guardian_sent'))
    if not athlete_sent:
        athlete_sent = _send_email(
            case.get('athlete', {}).get('email', ''), subject, body,
            reply_to=NOTIFICATION_EMAIL or None, brand=case.get('brand'))
    guardian_required = _coaching_is_minor(case)
    if guardian_required and not guardian_sent:
        guardian_sent = _send_email(
            case.get('guardian', {}).get('email', ''), subject, body,
            reply_to=NOTIFICATION_EMAIL or None, brand=case.get('brand'))
    delivery_complete = athlete_sent and (guardian_sent or not guardian_required)
    case['onboarding_materials'] = {
        'schema': 'coaching_onboarding_materials/v1',
        'generated_at': now,
        'athlete_id': athlete_id,
        'context_filename': context_path.name,
        'welcome_filename': welcome_path.name,
        'athlete_sent': athlete_sent,
        'guardian_required': guardian_required,
        'guardian_sent': guardian_sent if guardian_required else None,
    }
    if delivery_complete:
        case['onboarding_materials']['delivered_at'] = now
        _record_coaching_event(
            case, 'coaching_onboarding_delivered', case_id,
            occurred_at=now)
    _refresh_coaching_case(
        case, actor='system', reason='Athlete onboarding materials generated',
        source_id=case_id)
    _write_coaching_intake(case)
    if not delivery_complete:
        logger.critical(f"COACHING ONBOARDING DELIVERY FAILED: case={case_id}")
        return jsonify({
            'error': 'Onboarding materials were generated, but delivery failed',
            'case_id': case_id,
        }), 502
    return jsonify({
        'success': True,
        'case_id': case_id,
        'athlete_id': athlete_id,
        'generated_at': now,
        'delivered_at': now,
        'artifacts': [context_path.name, welcome_path.name],
    })


@app.route('/api/coaching-intakes/<case_id>/payment-handoff', methods=['POST'])
@limiter.limit("20/minute")
def create_coaching_payment_handoff(case_id):
    """Create checkout only after every pre-payment evidence gate passes."""
    supplied = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not supplied or not hmac.compare_digest(supplied, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401

    case = _read_coaching_intake(case_id)
    if not case:
        return jsonify({'error': 'Coaching intake not found'}), 404
    readiness = _coaching_case_readiness(case)
    if not readiness['payment_allowed']:
        return jsonify({
            'error': 'Payment handoff is blocked by unmet onboarding gates',
            'case_id': case_id,
            'blockers': readiness['payment_blockers'],
            'readiness': readiness,
        }), 409

    setup_fee_waived = (
        _coaching_gate_status(case, 'setup_fee_waiver') == 'approved')
    contract_ok, contract_error = _verify_coaching_checkout_contract(
        case['brand'], case['tier'], setup_fee_waived=setup_fee_waived)
    if not contract_ok:
        logger.critical(
            f"COACHING PAYMENT CONTRACT BLOCKED: case={case_id}, "
            f"reason={contract_error}")
        return jsonify({
            'error': 'Payment setup could not be verified; no checkout was created',
            'case_id': case_id,
        }), 503

    checkout = case.get('checkout') or {}
    if not checkout.get('url'):
        brand = case['brand']
        tier = case['tier']
        athlete = case['athlete']
        source = case.get('source') or {}
        try:
            session = _create_coaching_checkout_session(
                athlete['name'], athlete['email'], tier, brand,
                intake_id=case_id,
                setup_fee_waived=setup_fee_waived,
                ga4_client_id=source.get('ga4_client_id', ''),
                ga4_session_id=source.get('ga4_session_id', ''),
                analytics_consent=source.get('analytics_consent', 'unknown'))
        except stripe.error.StripeError as exc:
            logger.error(f"Stripe error creating coaching handoff {case_id}: {exc}")
            return jsonify({'error': 'Payment service error. Please try again.'}), 502

        now = datetime.now(timezone.utc).isoformat()
        case['checkout'] = {
            'session_id': session.id,
            'url': session.url,
            'created_at': now,
            'handoff_sent': False,
            'setup_fee_waived': (
                _coaching_gate_status(case, 'setup_fee_waiver') == 'approved'),
        }
        _record_coaching_event(
            case, 'coaching_checkout_created', session.id,
            occurred_at=now)
        _refresh_coaching_case(
            case, actor='system', reason='Approved Stripe checkout created',
            source_id=session.id)
        _write_coaching_intake(case)

    if not case['checkout'].get('handoff_sent'):
        sent = _send_coaching_onboarding_handoff(case, case['checkout']['url'])
        case['checkout']['handoff_sent'] = sent
        case['checkout']['handoff_attempted_at'] = datetime.now(timezone.utc).isoformat()
        if sent:
            _record_coaching_event(
                case, 'coaching_checkout_handoff_sent',
                case['checkout']['session_id'], details={'email_sent': True},
                occurred_at=case['checkout']['handoff_attempted_at'])
        _write_coaching_intake(case)
        if not sent:
            logger.critical(f"COACHING HANDOFF FAILED: case={case_id}")
            return jsonify({
                'error': 'Checkout created, but onboarding email failed',
                'case_id': case_id,
                'state': 'PAYMENT_PENDING',
            }), 502

    return jsonify({
        'success': True,
        'case_id': case_id,
        'state': case['state'],
        'checkout_url': case['checkout']['url'],
        'handoff_sent': True,
    })


@app.route('/api/create-coaching-checkout', methods=['POST', 'OPTIONS'])
@limiter.limit("20/minute")
def create_coaching_checkout():
    """Create a Stripe Checkout Session for coaching subscription.

    Expects JSON: {email, name, tier: "min"|"mid"|"max", intake_id?}
    Brand is derived from the trusted request Origin. The response carries
    the exact post-checkout onboarding contract needed by any brand frontend.
    """
    if request.method == 'OPTIONS':
        return '', 204

    if not COACHING_DIRECT_CHECKOUT_ENABLED:
        return jsonify({
            'error': 'Direct coaching checkout is disabled; complete the coaching intake first'
        }), 409

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    ga4_client_id, ga4_session_id, analytics_consent = \
        _payload_ga4_attribution(data)

    brand = _brand_from_origin(request.headers.get('Origin', ''))
    brand_cfg = _brand_config(brand)
    coaching_cfg = _coaching_config(brand)
    if not coaching_cfg.get('enabled'):
        return jsonify({
            'error': f"{brand_cfg.get('name', brand)} coaching is not available"
        }), 400

    email = (data.get('email') or '').strip().lower()
    if not email or '@' not in email or '.' not in email:
        return jsonify({'error': 'Valid email is required'}), 400

    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400

    tier = (data.get('tier') or '').strip().lower()
    tier_cfg = _coaching_tier_config(brand, tier)
    if tier not in COACHING_PRICE_IDS or not tier_cfg:
        return jsonify({'error': f'Invalid tier: {tier}. Must be min, mid, or max'}), 400

    intake_id = (data.get('intake_id') or '').strip()
    if intake_id:
        try:
            uuid.UUID(intake_id)
        except (ValueError, AttributeError):
            return jsonify({'error': 'Invalid intake_id'}), 400

    try:
        checkout_session = _create_coaching_checkout_session(
            name, email, tier, brand, intake_id=intake_id,
            ga4_client_id=ga4_client_id, ga4_session_id=ga4_session_id,
            analytics_consent=analytics_consent)

        logger.info(f"Created coaching checkout {checkout_session.id} "
                     f"(brand={brand}, tier={tier}, setup_fee=$99, {_mask_email(email)})")

        return jsonify({
            'checkout_url': checkout_session.url,
            'brand': brand,
            'tier': tier,
            'tier_label': tier_cfg.get('label', tier.title()),
            'setup_fee_cents': coaching_cfg.get('setup_fee_cents', 0),
            'setup_fee_waived': False,
            'trainingpeaks': {
                'attach_url': coaching_cfg.get('trainingpeaks_attach_url', ''),
                'premium_included': bool(coaching_cfg.get('trainingpeaks_premium_included')),
            },
        })

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error creating coaching checkout: {e}")
        return jsonify({'error': 'Payment service error. Please try again.'}), 502
    except Exception as e:
        logger.exception(f"Coaching checkout error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/create-consulting-checkout', methods=['POST', 'OPTIONS'])
@limiter.limit("20/minute")
def create_consulting_checkout():
    """Create a Stripe Checkout Session for consulting.

    Expects JSON: {email, name, hours: 1, plan_addon: false}
    Returns: {checkout_url}

    plan_addon adds the $100 custom-plan add-on as a second line item —
    gated on CONSULT_PLAN_ADDON_PRICE_ID being configured (Stripe-before-
    display: the sell page must not offer what isn't priced yet).
    """
    if request.method == 'OPTIONS':
        return '', 204

    origin = request.headers.get('Origin', '')
    brand = _brand_from_origin(origin)

    data = request.get_json(silent=True)
    if not data:
        logger.warning(f"Consulting checkout invalid JSON (origin={origin}, brand={brand})")
        return jsonify({'error': 'Invalid JSON'}), 400

    ga4_client_id, ga4_session_id, analytics_consent = \
        _payload_ga4_attribution(data)

    email = (data.get('email') or '').strip().lower()
    if not email or '@' not in email or '.' not in email:
        logger.warning(f"Consulting checkout missing/invalid email (origin={origin}, brand={brand})")
        return jsonify({'error': 'Valid email is required'}), 400

    name = (data.get('name') or '').strip()
    if not name:
        logger.warning(f"Consulting checkout missing name (origin={origin}, brand={brand})")
        return jsonify({'error': 'Name is required'}), 400

    hours = data.get('hours', 1)
    try:
        hours = int(hours)
        if hours < 1 or hours > 10:
            logger.warning(f"Consulting checkout invalid hours range: {hours} (origin={origin}, brand={brand})")
            return jsonify({'error': 'Hours must be between 1 and 10'}), 400
    except (ValueError, TypeError):
        logger.warning(f"Consulting checkout invalid hours type: {hours!r} (origin={origin}, brand={brand})")
        return jsonify({'error': 'Invalid hours value'}), 400

    plan_addon = bool(data.get('plan_addon')) and bool(CONSULT_PLAN_ADDON_PRICE_ID)

    # Brand from the requesting site, same as /api/create-checkout.
    brand_cfg = _brand_config(brand)

    try:
        expires_at = int((datetime.now() + timedelta(minutes=CHECKOUT_EXPIRY_MINUTES)).timestamp())

        line_items = [{'price': CONSULTING_PRICE_ID, 'quantity': hours}]
        if plan_addon:
            line_items.append({'price': CONSULT_PLAN_ADDON_PRICE_ID, 'quantity': 1})

        consulting_path = brand_cfg.get('consulting_path', '/consulting/')
        consulting_success_path = brand_cfg.get('consulting_success_path', '/consulting/confirmed/')

        checkout_metadata = {
            'product_type': 'consulting',
            'athlete_name': name,
            'hours': str(hours),
            'plan_addon': '1' if plan_addon else '0',
            'brand': brand,
        }
        _apply_ga4_metadata(
            checkout_metadata, ga4_client_id, ga4_session_id,
            analytics_consent)

        session_kwargs = dict(
            line_items=line_items,
            mode='payment',
            customer_email=email,
            customer_creation='always',
            metadata=checkout_metadata,
            success_url=f"{brand_cfg['site']}{consulting_success_path}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{brand_cfg['site']}{consulting_path}",
            expires_at=expires_at,
            after_expiration={
                'recovery': {
                    'enabled': True,
                    'allow_promotion_codes': True,
                }
            },
            consent_collection={
                'promotions': 'auto',
            },
        )
        if ENABLE_AUTOMATIC_TAX:
            session_kwargs['automatic_tax'] = {'enabled': True}

        checkout_session = stripe.checkout.Session.create(**session_kwargs)

        logger.info(f"Created consulting checkout {checkout_session.id} "
                     f"({hours}hr, addon={plan_addon}, brand={brand}, {_mask_email(email)})")

        return jsonify({'checkout_url': checkout_session.url})

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error creating consulting checkout: {e}")
        return jsonify({'error': 'Payment service error. Please try again.'}), 502
    except Exception as e:
        logger.exception(f"Consulting checkout error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/api/create-consult-addon-checkout', methods=['POST', 'OPTIONS'])
@limiter.limit("20/minute")
def create_consult_addon_checkout():
    """Post-call add-on purchase: Checkout for the $100 plan add-on only.

    Expects JSON: {ref}  — the consult order_id. Returns {checkout_url}.
    Purchasable up to 7 days after the call (enforced by the offer email's
    expiry and by Matti's own follow-up, not re-validated server-side here
    — a late purchase still delivers a plan, it's just outside the SLA).
    """
    if request.method == 'OPTIONS':
        return '', 204

    if not CONSULT_PLAN_ADDON_PRICE_ID:
        return jsonify({'error': 'CONSULT_PLAN_ADDON_PRICE_ID not configured'}), 503

    data = request.get_json(silent=True) or {}
    ref = str(data.get('ref') or '').strip()
    if not ref:
        return jsonify({'error': 'ref is required'}), 400
    try:
        safe_ref = consultations._safe_order_id(ref)
    except consultations.ConsultationError:
        return jsonify({'error': 'invalid ref'}), 400

    record = consultations.read_record(DELIVERIES_DIR, safe_ref)
    if record is None:
        return jsonify({'error': 'consultation not found'}), 404

    brand = normalize_brand(record.get('brand'))
    brand_cfg = _brand_config(brand)
    email = (record.get('athlete') or {}).get('email', '')

    try:
        expires_at = int((datetime.now() + timedelta(minutes=CHECKOUT_EXPIRY_MINUTES)).timestamp())

        consulting_path = brand_cfg.get('consulting_path', '/consulting/')
        consulting_success_path = brand_cfg.get('consulting_success_path', '/consulting/confirmed/')

        session_kwargs = dict(
            line_items=[{'price': CONSULT_PLAN_ADDON_PRICE_ID, 'quantity': 1}],
            mode='payment',
            customer_creation='always',
            metadata={
                'product_type': 'consult_addon',
                'consult_order_id': safe_ref,
                'brand': brand,
            },
            success_url=f"{brand_cfg['site']}{consulting_success_path}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{brand_cfg['site']}{consulting_path}",
            expires_at=expires_at,
            after_expiration={
                'recovery': {
                    'enabled': True,
                    'allow_promotion_codes': True,
                }
            },
            consent_collection={
                'promotions': 'auto',
            },
        )
        if email:
            session_kwargs['customer_email'] = email
        if ENABLE_AUTOMATIC_TAX:
            session_kwargs['automatic_tax'] = {'enabled': True}

        checkout_session = stripe.checkout.Session.create(**session_kwargs)

        logger.info(f"Created consult add-on checkout {checkout_session.id} for {safe_ref}")

        return jsonify({'checkout_url': checkout_session.url})

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error creating consult add-on checkout: {e}")
        return jsonify({'error': 'Payment service error. Please try again.'}), 502
    except Exception as e:
        logger.exception(f"Consult add-on checkout error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/webhook/woocommerce', methods=['POST'])
def woocommerce_webhook():
    """Handle WooCommerce order webhook."""
    signature = request.headers.get('X-WC-Webhook-Signature', '')

    if not verify_woocommerce_signature(request.data, signature):
        logger.warning("Invalid WooCommerce signature")
        return jsonify({'error': 'Invalid signature'}), 401

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    # Only process completed orders
    if data.get('status') not in ['completed', 'processing']:
        return jsonify({'status': 'ignored', 'reason': 'Order not completed'})

    try:
        order_data = extract_woocommerce_data(data)
        intake_data = extract_woocommerce_intake(data, order_data)

        # Idempotency check
        if check_idempotency(order_data['order_id']):
            return jsonify({
                'status': 'duplicate',
                'message': 'Order already processed'
            })

        # Validate order data
        is_valid, error_msg = validate_order_data(order_data)
        if not is_valid:
            logger.error(f"Invalid order data: {error_msg}")
            return jsonify({'error': error_msg}), 400

        athlete_id, profile_path = create_athlete_profile(order_data)

        # Mark as processed BEFORE pipeline to prevent TOCTOU race with
        # webhook retries. Stripe/WooCommerce retry if we don't respond within
        # ~20s, and the pipeline takes up to 5 minutes. Without this, retries
        # pass the idempotency check and start duplicate pipelines.
        mark_order_processed(order_data['order_id'], athlete_id)

        # Queue generation, return immediately (same async path as Stripe).
        job, sync_result = _spawn_plan_job(
            order_data, intake_data=intake_data or None)

        if sync_result is not None:
            # SYNC_PIPELINE=1 — legacy inline path (tests / local debugging)
            if sync_result['success']:
                return jsonify({
                    'status': 'success',
                    'athlete_id': athlete_id,
                    'message': 'Training plan generated and delivered'
                })
            return jsonify({
                'status': 'pipeline_failed',
                'athlete_id': athlete_id,
                'message': 'Order received but pipeline failed. Manual intervention required.'
            })

        return jsonify({
            'status': 'accepted',
            'athlete_id': athlete_id,
            'job_status': job.get('status', 'queued'),
            'message': 'Training plan generation queued'
        })

    except Exception as e:
        logger.exception(f"WooCommerce webhook error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@app.route('/webhook/signwell', methods=['POST'])
@limiter.limit("60/minute")
@_serialized_coaching_provider('signwell-webhook')
def signwell_webhook():
    """Verify SignWell, read back the provider record, and seal signed terms."""
    if not SIGNWELL_WEBHOOK_ID:
        return jsonify({'error': 'SignWell webhook is not configured'}), 503
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'error': 'Invalid JSON'}), 400
    if not verify_event_hash(payload, SIGNWELL_WEBHOOK_ID):
        logger.warning('Invalid SignWell event hash')
        return jsonify({'error': 'Invalid SignWell event hash'}), 401

    event = payload.get('event') or {}
    event_type = str(event.get('type') or '')
    document = ((payload.get('data') or {}).get('object') or {})
    document_id = str(document.get('id') or '')
    try:
        uuid.UUID(document_id)
    except (ValueError, TypeError, AttributeError):
        return jsonify({'error': 'Invalid SignWell document ID'}), 400
    metadata = document.get('metadata') or {}
    case_id = str(metadata.get('case_id') or '')
    case = _find_coaching_case_for_signwell(document_id, case_id)
    if not case:
        logger.info(f'Ignoring unbound SignWell event {event_type}')
        return jsonify({'status': 'ignored', 'reason': 'No bound coaching case'})
    packet = case.get('esign_packet') or {}
    if str(packet.get('provider') or '') != 'signwell':
        return jsonify({'error': 'Provider binding mismatch'}), 409

    event_ref = hashlib.sha256(
        f"{document_id}\0{event_type}\0{event.get('time')}\0{event.get('hash')}".encode()
    ).hexdigest()[:32]
    processed = packet.setdefault('processed_event_ids', [])
    if event_ref in processed:
        return jsonify({'status': 'duplicate', 'case_id': case['case_id']})
    if packet.get('status') == 'completed' and event_type != 'document_completed':
        processed.append(event_ref)
        del processed[:-100]
        _write_coaching_intake(case)
        return jsonify({
            'status': 'ignored', 'reason': 'Packet is already complete',
            'case_id': case['case_id'],
        })

    now = datetime.now(timezone.utc).isoformat()
    if event_type == 'document_completed':
        try:
            client = SignWellClient(SIGNWELL_API_KEY)
            readback = client.get_document(document_id)
            if bool(readback.get('test_mode')) != bool(packet.get('test_mode')):
                raise SignWellError('SignWell test-mode binding mismatch')
            _validate_signwell_readback(case, readback, document_id)
            completed_pdf = client.get_completed_pdf(document_id)
            relative_path, pdf_sha256 = _store_signwell_completed_pdf(
                case['case_id'], document_id, completed_pdf)
        except SignWellError as exc:
            logger.error(
                f'SignWell completion readback failed for {document_id}: {exc}')
            return jsonify({'error': 'SignWell completion readback failed'}), 503

        completed_at = str(readback.get('updated_at') or now)
        if packet.get('test_mode'):
            packet.update({
                'status': 'test_completed',
                'completed_at': completed_at,
                'signed_document_path': relative_path,
                'signed_document_sha256': pdf_sha256,
                'audit_page_included': True,
                'legal_effect': 'none_test_mode',
            })
            event_name = 'coaching_signwell_test_completed'
        else:
            _record_signwell_completion(
                case, readback, document_id, relative_path,
                pdf_sha256, completed_at)
            event_name = 'coaching_signwell_packet_completed'
        processed = packet.setdefault('processed_event_ids', [])
        processed.append(event_ref)
        del processed[:-100]
        _record_coaching_event(
            case, event_name, event_ref,
            details={'status': packet.get('status')}, occurred_at=completed_at)
        readiness = _refresh_coaching_case(
            case, actor='signwell_webhook',
            reason='SignWell packet completed with provider readback',
            source_id=document_id)
        _write_coaching_intake(case)
        return jsonify({
            'status': packet.get('status'),
            'case_id': case['case_id'],
            'case_state': readiness['state'],
            'legal_effect': (
                'none_test_mode' if packet.get('test_mode') else 'receipt_recorded'),
        })

    statuses = {
        'document_created': 'created',
        'document_sent': 'sent',
        'document_viewed': 'viewed',
        'document_in_progress': 'in_progress',
        'document_signed': 'in_progress',
        'document_declined': 'declined',
        'document_expired': 'expired',
        'document_canceled': 'canceled',
        'document_bounced': 'delivery_failed',
        'document_error': 'error',
    }
    if event_type not in statuses:
        return jsonify({'status': 'ignored', 'reason': f'Event type: {event_type}'})
    packet['status'] = statuses[event_type]
    packet['last_event_at'] = now
    packet['last_event_type'] = event_type
    processed.append(event_ref)
    del processed[:-100]
    _record_coaching_event(
        case, f'coaching_signwell_{statuses[event_type]}', event_ref,
        details={'status': statuses[event_type]}, occurred_at=now)
    _write_coaching_intake(case)
    if statuses[event_type] in {
            'declined', 'expired', 'canceled', 'delivery_failed', 'error'}:
        logger.critical(
            f"COACHING ESIGN NEEDS ATTENTION: case={case['case_id']}, "
            f"status={statuses[event_type]}")
    return jsonify({
        'status': statuses[event_type],
        'case_id': case['case_id'],
    })


@app.route('/webhook/stripe', methods=['POST'])
@limiter.limit("60/minute")
def stripe_webhook():
    """Handle Stripe checkout and recurring-subscription lifecycle events."""
    signature = request.headers.get('Stripe-Signature', '')

    if not verify_stripe_signature(request.data, signature):
        logger.warning("Invalid Stripe signature")
        return jsonify({'error': 'Invalid signature'}), 401

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    event_type = data.get('type', '')

    # Route by event type
    if event_type == 'checkout.session.expired':
        return _handle_checkout_expired(data)
    if event_type in {
            'invoice.paid', 'invoice.payment_failed',
            'invoice.payment_action_required',
            'customer.subscription.updated', 'customer.subscription.deleted',
            'customer.subscription.paused', 'customer.subscription.resumed'}:
        return _handle_coaching_billing_lifecycle(data)
    elif event_type != 'checkout.session.completed':
        return jsonify({'status': 'ignored', 'reason': f'Event type: {event_type}'})

    try:
        session = data.get('data', {}).get('object', {})
        metadata = session.get('metadata', {})
        product_type = metadata.get('product_type', 'training_plan')
        order_id = session.get('id', '')

        # Check if this was a recovered session
        recovered_from = session.get('recovered_from')
        if recovered_from:
            logger.info(f"Recovered checkout from expired session {recovered_from}")

        # Idempotency check (applies to all product types)
        if check_idempotency(order_id):
            return jsonify({
                'status': 'duplicate',
                'message': 'Order already processed'
            })

        # Route by product type
        if product_type == 'coaching':
            return _handle_coaching_webhook(session, metadata, order_id)
        elif product_type == 'consulting':
            return _handle_consulting_webhook(session, metadata, order_id)
        elif product_type == 'consult_addon':
            return _handle_consult_addon_webhook(session, metadata, order_id)
        else:
            return _handle_training_plan_webhook(data, order_id)

    except Exception as e:
        logger.exception(f"Stripe webhook error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


def _coaching_billing_event_identity(event: dict) -> tuple[str, str, str]:
    """Return case, subscription, and customer IDs from a Stripe event."""
    obj = ((event.get('data') or {}).get('object') or {})
    event_type = str(event.get('type') or '')
    if event_type.startswith('invoice.'):
        subscription_id = _stripe_invoice_subscription_id(obj)
        parent = obj.get('parent') or {}
        metadata = (
            obj.get('metadata') or
            (parent.get('subscription_details') or {}).get('metadata') or {})
    else:
        subscription_id = _stripe_object_id(obj.get('id'))
        metadata = obj.get('metadata') or {}
    return (
        str(metadata.get('intake_id') or ''),
        subscription_id,
        _stripe_object_id(obj.get('customer')),
    )


def _handle_coaching_billing_lifecycle(event: dict):
    """Update one onboarding case from a verified recurring-billing event.

    This never emails the athlete or changes a Stripe subscription. It keeps
    plan-release truth aligned with Stripe and raises an operator alert when
    billing needs attention.
    """
    event_type = str(event.get('type') or '')
    obj = ((event.get('data') or {}).get('object') or {})
    case_id, subscription_id, customer_id = _coaching_billing_event_identity(event)
    case = _find_coaching_case_for_billing(
        case_id=case_id, subscription_id=subscription_id,
        customer_id=customer_id)
    if not case:
        logger.info(
            f'Ignoring non-coaching or unbound billing event {event_type}')
        return jsonify({
            'status': 'ignored',
            'reason': 'No uniquely bound coaching case',
        })

    event_id = str(event.get('id') or '').strip()
    if not event_id:
        event_id = hashlib.sha256(json.dumps(
            event, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:32]
    billing = case.setdefault('billing', {
        'schema': 'coaching_subscription_billing/v1',
    })
    processed = billing.setdefault('processed_event_ids', [])
    if event_id in processed:
        return jsonify({'status': 'duplicate', 'case_id': case['case_id']})

    try:
        event_created = int(event.get('created') or 0)
        last_event_created = int(billing.get('last_event_created') or 0)
    except (TypeError, ValueError):
        event_created = last_event_created = 0
    if event_created and last_event_created and event_created < last_event_created:
        processed.append(event_id)
        del processed[:-100]
        _write_coaching_intake(case)
        return jsonify({
            'status': 'ignored',
            'reason': 'Stale out-of-order billing event',
            'case_id': case['case_id'],
        })

    now = datetime.now(timezone.utc).isoformat()
    if subscription_id:
        billing['subscription_id'] = subscription_id
    if customer_id:
        billing['customer_id'] = customer_id
    billing['last_event_type'] = event_type
    billing['last_event_at'] = now
    if event_created:
        billing['last_event_created'] = event_created

    if event_type == 'invoice.paid':
        billing['standing'] = 'healthy'
        billing['last_paid_at'] = now
        billing['last_paid_invoice_id'] = _stripe_object_id(obj.get('id'))
        event_name = 'coaching_renewal_paid'
    elif event_type == 'invoice.payment_failed':
        billing['standing'] = 'past_due'
        billing['last_failed_at'] = now
        billing['last_failed_invoice_id'] = _stripe_object_id(obj.get('id'))
        event_name = 'coaching_payment_failed'
    elif event_type == 'invoice.payment_action_required':
        billing['standing'] = 'action_required'
        billing['action_required_at'] = now
        event_name = 'coaching_payment_action_required'
    elif event_type == 'customer.subscription.deleted':
        billing['provider_status'] = str(obj.get('status') or 'canceled')
        billing['standing'] = 'ended'
        billing['ended_at'] = now
        event_name = 'coaching_subscription_ended'
    elif event_type == 'customer.subscription.paused':
        billing['provider_status'] = 'paused'
        billing['standing'] = 'paused'
        event_name = 'coaching_subscription_paused'
    elif event_type == 'customer.subscription.resumed':
        billing['provider_status'] = str(obj.get('status') or 'active')
        billing['standing'] = 'healthy'
        event_name = 'coaching_subscription_resumed'
    else:
        provider_status = str(obj.get('status') or '').lower()
        cancel_at_period_end = bool(obj.get('cancel_at_period_end'))
        standing_by_status = {
            'active': 'canceling_at_period_end' if cancel_at_period_end else 'healthy',
            'trialing': 'trialing',
            'past_due': 'past_due',
            'unpaid': 'unpaid',
            'incomplete': 'incomplete',
            'incomplete_expired': 'ended',
            'paused': 'paused',
            'canceled': 'ended',
        }
        billing['provider_status'] = provider_status or 'unknown'
        billing['standing'] = standing_by_status.get(provider_status, 'action_required')
        billing['cancel_at_period_end'] = cancel_at_period_end
        if obj.get('current_period_end') is not None:
            billing['current_period_end'] = obj.get('current_period_end')
        event_name = (
            'coaching_subscription_cancel_scheduled'
            if cancel_at_period_end and provider_status in ('active', 'trialing')
            else 'coaching_subscription_updated')

    processed.append(event_id)
    del processed[:-100]
    _record_coaching_event(
        case, event_name, event_id,
        details={'status': billing.get('standing')}, occurred_at=now)
    readiness = _refresh_coaching_case(
        case, actor='stripe_webhook', reason=f'Stripe event: {event_type}',
        source_id=event_id)
    _write_coaching_intake(case)

    if billing.get('standing') in {
            'past_due', 'action_required', 'unpaid', 'incomplete',
            'paused', 'ended'}:
        logger.critical(
            f"COACHING BILLING NEEDS ATTENTION: case={case['case_id']}, "
            f"standing={billing.get('standing')}, event={event_type}")
    return jsonify({
        'status': 'updated',
        'case_id': case['case_id'],
        'billing_standing': billing.get('standing'),
        'case_state': readiness['state'],
    })


def _handle_checkout_expired(data: dict):
    """Handle expired checkout session — send recovery email if customer opted in.

    Includes idempotency to prevent duplicate recovery emails when Stripe retries.
    Returns 200 on all paths (even errors) to prevent Stripe from retrying.
    """
    try:
        session = data.get('data', {}).get('object', {})
        session_id = session.get('id', '')

        # Idempotency — prevent duplicate recovery emails on Stripe retry
        expired_key = f'expired_{session_id}'
        if check_idempotency(expired_key):
            return jsonify({'status': 'duplicate', 'message': 'Expired session already handled'})

        # Mark early to prevent duplicates from concurrent retries
        mark_order_processed(expired_key, 'recovery')

        email = session.get('customer_details', {}).get('email', '')
        metadata = session.get('metadata', {})
        product_type = metadata.get('product_type', 'training_plan')
        athlete_name = metadata.get('athlete_name', '')
        consent = session.get('consent', {})
        intake_id = metadata.get('intake_id', '')
        case = (_read_coaching_intake(intake_id)
                if product_type == 'coaching' and intake_id else {})
        now = datetime.now(timezone.utc).isoformat()
        if case:
            checkout = case.setdefault('checkout', {})
            checkout['expired_at'] = now
            checkout['recovery_disposition'] = 'evaluating'
            _record_coaching_event(
                case, 'coaching_checkout_expired', session_id,
                occurred_at=now)

            if case.get('receipts', {}).get('stripe_payment'):
                checkout['recovery_disposition'] = 'payment_already_confirmed'
                _write_coaching_intake(case)
                return jsonify({
                    'status': 'ignored', 'reason': 'Payment already confirmed'})
            if case.get('state') != 'PAYMENT_PENDING':
                checkout['recovery_disposition'] = 'case_not_payment_pending'
                _write_coaching_intake(case)
                return jsonify({
                    'status': 'ignored', 'reason': 'Case is not payment pending'})
            if checkout.get('recovery_sent_at'):
                checkout['recovery_disposition'] = 'case_recovery_already_sent'
                _write_coaching_intake(case)
                return jsonify({
                    'status': 'ignored', 'reason': 'Case recovery already sent'})

        # Health monitors create never-paid sessions that expire hourly —
        # without this guard, enabling checkout.session.expired turns them
        # into a recovery-email firehose (guard added when the event
        # subscription was finally enabled, Jul 2026).
        MONITOR_EMAILS = ('checkout-monitor@', 'healthcheck@', 'monitor@',
                          'gravelgodcoaching@gmail.com')
        if email and any(email.lower().startswith(m) or m in email.lower()
                         for m in MONITOR_EMAILS):
            logger.info(f"Expired checkout {session_id} — monitor session, skipping recovery")
            if case:
                case['checkout']['recovery_disposition'] = 'monitor_session'
                _write_coaching_intake(case)
            return jsonify({'status': 'ignored', 'reason': 'Monitor session'})

        # Stripe provides a recovery URL when after_expiration.recovery is enabled
        recovery = session.get('after_expiration', {}).get('recovery', {})
        recovery_url = recovery.get('url', '')

        if not email or not recovery_url:
            logger.info(f"Expired checkout {session_id} — no email or recovery URL")
            if case:
                case['checkout']['recovery_disposition'] = 'no_recovery_url'
                _write_coaching_intake(case)
            return jsonify({'status': 'ignored', 'reason': 'No recovery possible'})

        # Only send if customer opted in to promotional emails
        if consent.get('promotions') != 'opt_in':
            logger.info(f"Expired checkout — customer did not opt in ({_mask_email(email)})")
            if case:
                case['checkout']['recovery_disposition'] = 'no_promotional_consent'
                _write_coaching_intake(case)
            return jsonify({'status': 'ignored', 'reason': 'No promotional consent'})

        # Build product-specific recovery email
        recovery_sent = _send_recovery_email(
            email, athlete_name, product_type, metadata, recovery_url)

        if case:
            case['checkout']['recovery_disposition'] = (
                'sent' if recovery_sent else 'delivery_failed')
            case['checkout']['recovery_attempted_at'] = now
            if recovery_sent:
                case['checkout']['recovery_sent_at'] = now
                _record_coaching_event(
                    case, 'coaching_checkout_recovery_sent', session_id,
                    details={'email_sent': True}, occurred_at=now)
            else:
                _record_coaching_event(
                    case, 'coaching_checkout_recovery_failed', session_id,
                    details={'email_sent': False}, occurred_at=now)
            _write_coaching_intake(case)

        if not recovery_sent:
            return jsonify({
                'status': 'error',
                'message': 'Recovery delivery failed; logged for manual review'})

        _log_product_event('cart_recovery', session_id,
                           email=email, original_product=product_type,
                           recovery_url_sent=True)

        logger.info(f"Sent recovery email for {product_type} to {_mask_email(email)}")
        return jsonify({'status': 'recovery_sent'})

    except Exception as e:
        logger.exception(f"Error handling expired checkout: {e}")
        # Return 200 even on error to prevent Stripe from retrying and
        # sending duplicate recovery emails. The idempotency mark is already
        # set, so retries would be caught, but 200 is cleaner.
        return jsonify({'status': 'error', 'message': 'Logged for manual review'})


def _send_recovery_email(email: str, name: str, product_type: str,
                         metadata: dict, recovery_url: str) -> bool:
    """Send a recovery email for an abandoned checkout."""
    first_name = name.split()[0] if name else 'there'
    brand = normalize_brand(metadata.get('brand'))
    brand_cfg = _brand_config(brand)
    signature = brand_cfg.get('email', {})
    signature_name = signature.get('signature_name', 'Matti')
    signature_org = signature.get('signature_organization', brand_cfg['name'])
    signature_site = signature.get('signature_site', brand_cfg['site'].replace('https://', ''))

    if product_type == 'training_plan':
        weeks = metadata.get('weeks', '')
        subject = f"Your {weeks}-week training plan is still waiting"
        body = (
            f"Hey {first_name},\n\n"
            f"You were building a custom {weeks}-week training plan — "
            f"looks like you didn't finish checking out.\n\n"
            f"Your plan details are saved. Pick up where you left off:\n"
            f"{recovery_url}\n\n"
            f"Your race is coming up. The sooner you start structured training, "
            f"the stronger you'll be on race day.\n\n"
            f"— {signature_name}, {signature_org}\n"
            f"{signature_site}"
        )
    elif product_type == 'coaching':
        tier = metadata.get('tier', '')
        tier_label = _coaching_tier_config(brand, tier).get('label', tier.title())
        subject = "Your coaching spot is still available"
        body = (
            f"Hey {first_name},\n\n"
            f"You were signing up for {tier_label} coaching — "
            f"your spot is still open.\n\n"
            f"Pick up where you left off:\n"
            f"{recovery_url}\n\n"
            f"If you have a question before completing checkout, reply here.\n\n"
            f"— {signature_name}, {signature_org}\n"
            f"{signature_site}"
        )
    else:  # consulting
        hours = metadata.get('hours', '1')
        subject = "Your consulting session is ready to book"
        body = (
            f"Hey {first_name},\n\n"
            f"You were booking a {hours}-hour consulting session — "
            f"still interested?\n\n"
            f"Complete your booking:\n"
            f"{recovery_url}\n\n"
            f"— Matti, Gravel God Cycling\n"
            f"gravelgodcycling.com"
        )

    reply_to = NOTIFICATION_EMAIL or None
    if RESEND_API_KEY:
        sent = _send_email(
            email, subject, body, reply_to=reply_to, brand=brand)
        if not sent:
            logger.critical(
                f"ABANDONED CART — email failed\n"
                f"  Email: {_mask_email(email)}\n  Product: {product_type}\n"
                f"  Recovery URL: {recovery_url}"
            )
        else:
            logger.info(f"Recovery email sent to {_mask_email(email)}")
        return bool(sent)
    else:
        logger.critical(
            f"ABANDONED CART — Resend not configured, manual follow-up needed\n"
            f"  Email: {_mask_email(email)}\n  Name: {name}\n"
            f"  Product: {product_type}\n  Recovery URL: {recovery_url}"
        )
        return False


def _handle_training_plan_webhook(data: dict, order_id: str):
    """Handle training plan checkout completion — create profile + run pipeline."""
    _webhook_brand = normalize_brand(
        data.get('data', {}).get('object', {}).get('metadata', {}).get('brand', DEFAULT_BRAND))
    if not _brand_config(_webhook_brand).get(
            'training_plan_generation_enabled', True):
        logger.error(
            f"Training plan webhook rejected: brand {_webhook_brand!r} "
            "has training-plan generation disabled")
        return jsonify({
            'error': f"{_brand_config(_webhook_brand).get('name', _webhook_brand)} does not "
                     "support training-plan generation yet"
        }), 400

    order_data = extract_stripe_data(data)

    is_valid, error_msg = validate_order_data(order_data)
    if not is_valid:
        logger.error(f"Invalid order data: {error_msg}")
        return jsonify({'error': error_msg}), 400

    athlete_id, profile_path = create_athlete_profile(order_data)

    # Load intake data for pipeline and backup
    intake_id = data.get('data', {}).get('object', {}).get('metadata', {}).get('intake_id', '')
    intake_data = load_intake(intake_id) if intake_id else {}
    if intake_data:
        backup_path = Path(ATHLETES_DIR) / athlete_id / 'intake_backup.json'
        try:
            with open(backup_path, 'w') as f:
                json.dump(intake_data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to backup intake data: {e}")

    # Mark BEFORE pipeline — see WooCommerce handler comment for rationale
    mark_order_processed(order_data['order_id'], athlete_id)

    # Record purchase in GA4 server-side, honoring explicit analytics denial.
    session_obj = data.get('data', {}).get('object', {})
    session_metadata = session_obj.get('metadata', {})
    brand = session_metadata.get('brand', DEFAULT_BRAND)
    _send_ga4_purchase(order_data['order_id'], session_obj.get('amount_total'),
                       'training_plan', 'Custom Training Plan', brand=brand,
                       client_id=session_metadata.get('ga4_client_id', ''),
                       session_id=session_metadata.get('ga4_session_id', ''),
                       analytics_consent=session_metadata.get(
                           'analytics_consent', 'unknown'))

    # Send instant payment confirmation to customer (before pipeline runs)
    customer_email = order_data['profile'].get('email', '')
    customer_name = order_data['profile'].get('name', '')
    race_name = intake_data.get('race_name', '') if intake_data else ''
    _send_payment_confirmation(customer_email, customer_name, race_name=race_name,
                               brand=brand)

    # Queue generation and return 200 to Stripe immediately — the pipeline
    # takes minutes, Stripe times out at ~20s. The background job handles
    # log_order, ZIP persistence, and the coach notification email.
    job, sync_result = _spawn_plan_job(order_data, intake_id=intake_id,
                                       intake_data=intake_data or None)

    if sync_result is not None:
        # SYNC_PIPELINE=1 — legacy inline path (tests / local debugging)
        if sync_result['success']:
            return jsonify({
                'status': 'success',
                'athlete_id': athlete_id,
                'message': 'Training plan generated and delivered'
            })
        return jsonify({
            'status': 'pipeline_failed',
            'athlete_id': athlete_id,
            'message': 'Order received but pipeline failed. Manual intervention required.'
        })

    return jsonify({
        'status': 'accepted',
        'athlete_id': athlete_id,
        'job_status': job.get('status', 'queued'),
        'message': 'Training plan generation queued'
    })


def _handle_coaching_webhook(session: dict, metadata: dict, order_id: str):
    """Handle coaching subscription checkout completion — log + notify."""
    tier = metadata.get('tier', 'unknown')
    brand = normalize_brand(metadata.get('brand'))
    name = metadata.get('athlete_name', 'Unknown')
    email = session.get('customer_details', {}).get('email', '')
    subscription_id = _stripe_object_id(session.get('subscription'))
    customer_id = _stripe_object_id(session.get('customer'))
    intake_id = metadata.get('intake_id', '')
    recovered_from = str(session.get('recovered_from') or '')

    logger.info(f"Coaching subscription started: {name} ({_mask_email(email)}), "
                f"tier={tier}, subscription={subscription_id}")

    if intake_id:
        case = _read_coaching_intake(intake_id)
        if not case:
            logger.critical(
                f"COACHING PAYMENT CASE MISSING: case={intake_id}, "
                f"checkout={order_id}")
            return jsonify({
                'error': 'Coaching onboarding case was not found'
            }), 409
        if case.get('receipts', {}).get('stripe_payment'):
            logger.warning(
                f"Coaching case {intake_id} already has a payment receipt; "
                f"ignoring additional checkout {order_id}")
            mark_order_processed(order_id, sanitize_athlete_id(name))
            return jsonify({
                'status': 'duplicate_case_payment',
                'message': 'Coaching payment already recorded for this case',
            })
        if case:
            expected_session = str(case.get('checkout', {}).get('session_id') or '')
            session_matches = (
                order_id == expected_session or
                (recovered_from and recovered_from == expected_session)
            )
            if not session_matches:
                logger.critical(
                    f"COACHING PAYMENT IDENTITY MISMATCH: case={intake_id}, "
                    f"checkout={order_id}")
                return jsonify({
                    'error': 'Coaching checkout does not match the approved handoff'
                }), 409
            now = datetime.now(timezone.utc).isoformat()
            case.setdefault('receipts', {})['stripe_payment'] = {
                'checkout_session_id': order_id,
                'subscription_id': subscription_id,
                'customer_id': customer_id,
                'confirmed_at': now,
            }
            billing = case.setdefault('billing', {
                'schema': 'coaching_subscription_billing/v1',
            })
            billing.update({
                'subscription_id': subscription_id,
                'customer_id': customer_id,
                'checkout_payment_status': str(
                    session.get('payment_status') or 'paid'),
            })
            billing.setdefault('provider_status', 'active')
            billing.setdefault('standing', 'healthy')
            billing.setdefault('last_event_type', 'checkout.session.completed')
            billing.setdefault('last_event_at', now)
            billing.setdefault('processed_event_ids', [])
            if recovered_from:
                case['receipts']['stripe_payment']['recovered_from'] = recovered_from
                case.setdefault('checkout', {})['recovered_session_id'] = order_id
                _record_coaching_event(
                    case, 'coaching_checkout_recovered', order_id,
                    details={'recovered_from': recovered_from},
                    occurred_at=now)
            _record_coaching_event(
                case, 'coaching_payment_confirmed', order_id,
                occurred_at=now)
            _refresh_coaching_case(
                case, actor='stripe_webhook',
                reason='Coaching subscription payment confirmed',
                source_id=order_id)
            _write_coaching_intake(case)
    mark_order_processed(order_id, sanitize_athlete_id(name))
    _send_ga4_purchase(order_id, session.get('amount_total'),
                       'coaching', f'Coaching ({tier})',
                       brand=brand,
                       client_id=metadata.get('ga4_client_id', ''),
                       session_id=metadata.get('ga4_session_id', ''),
                       analytics_consent=metadata.get(
                           'analytics_consent', 'unknown'))
    _log_product_event('coaching', order_id,
                       tier=tier, name=name, email=email,
                       subscription_id=subscription_id, brand=brand,
                       intake_id=intake_id)
    _notify_new_order('coaching', {
        'name': name,
        'email': email,
        'tier': tier,
        'subscription_id': subscription_id,
        'order_id': order_id,
        'brand': brand,
    })
    _send_coaching_payment_confirmation(email, name, tier, brand=brand)

    return jsonify({
        'status': 'success',
        'product_type': 'coaching',
        'tier': tier,
        'message': f'Coaching subscription ({tier}) started for {name}'
    })


def _apply_brand_signature(body: str, brand: str) -> str:
    """Swap the GG signature block + site links for the brand's own.

    Shared by the day-1/3/7 sequence (inline in process_followup_emails)
    and the CONSULT-ENGINE athlete emails below — factored out here so the
    new consult copy doesn't fork the substitution logic a third time.
    """
    if brand == DEFAULT_BRAND:
        return body
    brand_cfg = _brand_config(brand)
    signature = brand_cfg.get('email', {})
    return body.replace(
        '— Matti\nGravel God Coaching\ngravelgodcycling.com',
        f"— {signature.get('signature_name', 'Matti')}\n"
        f"{signature.get('signature_organization', brand_cfg['name'])}\n"
        f"{signature.get('signature_site', brand_cfg['site'].replace('https://', ''))}",
    ).replace('https://gravelgodcycling.com', brand_cfg['site'])


def _consult_intake_link(order_id: str, brand_cfg: dict) -> str:
    """Fragment-carried intake link — credentials never go in the query
    string (app.py:103-105 / :3286; only `token=` is log-redacted)."""
    try:
        token = issue_consult_intake_token(order_id=order_id)
    except ConsultIntakeTokenError as e:
        logger.error(f"Cannot mint consult intake token for {order_id}: {e}")
        return ''
    return f"{brand_cfg['site']}/consulting/intake/#ref={order_id}&t={token}"


def _send_consult_welcome(record: dict, brand: str) -> bool:
    """Athlete welcome for a paid consult: booking link, fragment-carried
    intake link, TP invite + three-step copy, no-TP fallback, add-on terms
    if bought. See docs/CONSULT_ENGINE_SPEC.md §3.2."""
    athlete = record.get('athlete') or {}
    email = athlete.get('email') or ''
    if not email:
        logger.warning(f"No athlete email for consult {record.get('order_id')} — skipping welcome")
        return False
    name = athlete.get('name') or ''
    first_name = name.split()[0] if name else 'there'
    order_id = record.get('order_id', '')
    brand_cfg = _brand_config(brand)
    plan_addon_bought = bool((record.get('products', {}).get('plan_addon') or {}).get('purchased'))

    subject, body = build_consult_welcome_email(
        first_name=first_name,
        booking_link=CONSULT_BOOKING_URL,
        intake_link=_consult_intake_link(order_id, brand_cfg),
        plan_addon_bought=plan_addon_bought,
        tp_invite_link=CONSULT_TP_INVITE_LINK,
    )
    body = _apply_brand_signature(body, brand)
    return _send_email(email, subject, body, reply_to=NOTIFICATION_EMAIL, brand=brand)


def _send_consult_intake_nudge(record: dict, brand: str, first_name: str) -> bool:
    athlete = record.get('athlete') or {}
    email = athlete.get('email') or ''
    if not email:
        return False
    brand_cfg = _brand_config(brand)
    intake_link = _consult_intake_link(record.get('order_id', ''), brand_cfg)
    if not intake_link:
        return False
    body = _apply_brand_signature(
        CONSULT_INTAKE_NUDGE_TEMPLATE.format(first_name=first_name, intake_link=intake_link),
        brand)
    return _send_email(email, CONSULT_INTAKE_NUDGE_SUBJECT, body,
                       reply_to=NOTIFICATION_EMAIL, brand=brand)


def _send_consult_tp_nudge(record: dict, brand: str, first_name: str) -> bool:
    athlete = record.get('athlete') or {}
    email = athlete.get('email') or ''
    if not email:
        return False
    body = _apply_brand_signature(
        CONSULT_TP_NUDGE_TEMPLATE.format(first_name=first_name, tp_invite_link=CONSULT_TP_INVITE_LINK),
        brand)
    return _send_email(email, CONSULT_TP_NUDGE_SUBJECT, body,
                       reply_to=NOTIFICATION_EMAIL, brand=brand)


def _send_consult_addon_offer(record: dict, brand: str, first_name: str) -> bool:
    athlete = record.get('athlete') or {}
    email = athlete.get('email') or ''
    if not email:
        return False
    body = _apply_brand_signature(
        CONSULT_ADDON_OFFER_TEMPLATE.format(first_name=first_name), brand)
    return _send_email(email, CONSULT_ADDON_OFFER_SUBJECT, body,
                       reply_to=NOTIFICATION_EMAIL, brand=brand)


def _send_consult_plan_reminder(record: dict) -> bool:
    """Coach-facing (Matti), +1 day after the call: send the plan-of-action."""
    order_id = record.get('order_id', '')
    name = (record.get('athlete') or {}).get('name', 'Unknown')
    subject = f"[GG] Plan-of-action due: {name} ({order_id})"
    text = (f"It's been a day since the consult call with {name} "
            f"(order {order_id}). Send the plan-of-action if you haven't.")
    brand = normalize_brand(record.get('brand'))
    if NOTIFICATION_EMAIL:
        return _send_email(NOTIFICATION_EMAIL, subject, text, brand=brand)
    logger.critical(f"CONSULT PLAN REMINDER: {subject}\n{text}")
    return False


def _notify_consult_needs_attention(record: dict, reason: str) -> None:
    """Coach-facing alert with the operator curl one-liners (§5)."""
    order_id = record.get('order_id', '')
    name = (record.get('athlete') or {}).get('name', 'Unknown')
    subject = f"[GG] Consult needs attention: {name} ({order_id})"
    text = (
        f"Consult {order_id} for {name} needs attention: {reason}\n\n"
        "Operator actions (X-Cron-Secret header):\n"
        f"  Set call time:  curl -X POST -H 'X-Cron-Secret: ...' "
        f"-d '{{\"call_at\": \"2026-01-01T15:00:00+00:00\"}}' "
        f".../api/consult/{order_id}/op\n"
        f"  Retry analysis: curl -X POST -H 'X-Cron-Secret: ...' "
        f"-d '{{\"retry\": true}}' .../api/consult/{order_id}/op\n"
        f"  Close:          curl -X POST -H 'X-Cron-Secret: ...' "
        f"-d '{{\"close\": \"reason\"}}' .../api/consult/{order_id}/op"
    )
    brand = normalize_brand(record.get('brand'))
    if NOTIFICATION_EMAIL:
        if _send_email(NOTIFICATION_EMAIL, subject, text, brand=brand):
            return
    logger.critical(f"CONSULT NEEDS ATTENTION: {subject}\n{text}")


def _handle_consulting_webhook(session: dict, metadata: dict, order_id: str):
    """Handle consulting checkout completion.

    Order matters (docs/CONSULT_ENGINE_SPEC.md §3): the consultation record
    and athlete welcome email happen BEFORE mark_order_processed, so a
    Resend timeout can't silently lose the welcome forever on Stripe's
    retry — the follow-up cron re-sends whenever welcome_sent_at is still
    null. Tolerates missing fields throughout and never raises — a broken
    email must not turn into a 500 that makes Stripe retry the whole
    handler (and risk a duplicate coach notification).
    """
    name = metadata.get('athlete_name', 'Unknown')
    try:
        hours = int(metadata.get('hours', '1'))
    except (TypeError, ValueError):
        hours = 1
    email = (session.get('customer_details') or {}).get('email', '')
    brand = normalize_brand(metadata.get('brand'))
    plan_addon = metadata.get('plan_addon') == '1'

    logger.info(f"Consulting booked: {name} ({_mask_email(email)}), {hours}hr")

    # 1. Write the consultation record — atomic, BEFORE mark_order_processed.
    record = None
    try:
        record = consultations.new_record(
            order_id=order_id, brand=brand, athlete_name=name,
            athlete_email=email, hours=hours,
            amount_cents=int(session.get('amount_total') or 0) or (CONSULTING_PRICE_CENTS * hours),
            plan_addon=plan_addon,
            plan_addon_amount_cents=CONSULT_PLAN_ADDON_AMOUNT_CENTS,
        )
        consultations.write_record(DELIVERIES_DIR, record)
    except Exception:
        logger.exception(f"Failed to write consultation record for {order_id}")
        record = None

    # 2. Athlete welcome email — leaves welcome_sent_at null on failure so
    #    process_consult_followups() retries it.
    if record is not None:
        try:
            sent = _send_consult_welcome(record, brand)
        except Exception:
            logger.exception(f"Consult welcome email failed for {order_id}")
            sent = False
        if sent:
            try:
                def _mark_welcome_sent(r):
                    r['welcome_sent_at'] = consultations._now_iso()
                    consultations.append_timeline(r, 'welcome_sent')
                record = consultations.update_record(DELIVERIES_DIR, order_id, _mark_welcome_sent)
            except Exception:
                logger.exception(f"Failed to record welcome_sent_at for {order_id}")

    # 3. Idempotency marker LAST.
    mark_order_processed(order_id, sanitize_athlete_id(name))

    # Best-effort telemetry — never critical for retry safety.
    try:
        _send_ga4_purchase(order_id, session.get('amount_total'),
                           'consulting', 'Consulting Session', brand=brand,
                           client_id=metadata.get('ga4_client_id', ''),
                           session_id=metadata.get('ga4_session_id', ''),
                           analytics_consent=metadata.get(
                               'analytics_consent', 'unknown'))
    except Exception:
        logger.exception("GA4 purchase event failed for consulting")
    try:
        _log_product_event('consulting', order_id, name=name, email=email, hours=str(hours))
    except Exception:
        logger.exception("Failed to log consulting product event")

    # 4. Coach email via _send_email directly — NOT _notify_new_order,
    #    whose external_notification_projection nulls athlete PII and
    #    replaces raw errors (fulfillment_state.py:159-190). This mail is
    #    coach-only and needs the real details.
    try:
        subject, text, html = _build_consulting_email({
            'name': name, 'email': email, 'hours': str(hours), 'order_id': order_id,
            'brand': brand,
        })
        if NOTIFICATION_EMAIL:
            _send_email(NOTIFICATION_EMAIL, subject, text, html=html, brand=brand)
        else:
            logger.critical(f"NEW ORDER: {subject}\n{text}")
    except Exception:
        logger.exception("Failed to send consulting coach notification")

    return jsonify({
        'status': 'success',
        'product_type': 'consulting',
        'hours': str(hours),
        'message': f'Consulting ({hours}hr) booked for {name}'
    })


def _handle_consult_addon_webhook(session: dict, metadata: dict, order_id: str):
    """Post-call add-on purchase — flips products.plan_addon on the
    EXISTING consult record. Idempotent on purchased_at: a Stripe retry
    for the same session finds the flag already set and sends no second
    coach email."""
    consult_order_id = str(metadata.get('consult_order_id') or '')
    email = (session.get('customer_details') or {}).get('email', '')
    brand = normalize_brand(metadata.get('brand'))

    mark_order_processed(order_id, sanitize_athlete_id(consult_order_id or 'consult-addon'))

    record = None
    already_purchased = False
    if consult_order_id:
        try:
            def _mutate(r):
                nonlocal already_purchased
                addon = r.setdefault('products', {}).setdefault('plan_addon', {})
                if addon.get('purchased_at'):
                    already_purchased = True
                    return
                addon['purchased'] = True
                addon['purchased_at'] = consultations._now_iso()
                consultations.append_timeline(r, 'addon_paid')
            record = consultations.update_record(DELIVERIES_DIR, consult_order_id, _mutate)
        except consultations.ConsultationError:
            logger.warning(f"consult add-on webhook: no record for {consult_order_id}")
        except Exception:
            logger.exception(f"consult add-on webhook failed for {consult_order_id}")

    if record is not None and not already_purchased:
        try:
            subject = f"[GG] Add-on plan purchased for consult {consult_order_id}"
            text = (f"Add-on custom plan purchased ({_mask_email(email)}) "
                    f"for consult order {consult_order_id}.")
            if NOTIFICATION_EMAIL:
                _send_email(NOTIFICATION_EMAIL, subject, text, brand=brand)
            else:
                logger.critical(f"NEW ORDER: {subject}\n{text}")
        except Exception:
            logger.exception("Failed to notify coach of consult add-on purchase")

    return jsonify({
        'status': 'success',
        'product_type': 'consult_addon',
        'consult_order_id': consult_order_id,
    })


# =============================================================================
# TEST ENDPOINT — runs the EXACT same code path as a real Stripe webhook.
# Secured by CRON_SECRET header. Requires intake_id with stored questionnaire.
# =============================================================================
@app.route('/webhook/test', methods=['POST'])
def test_webhook():
    """Simulate a real customer checkout → pipeline → notification flow.

    Runs the identical code path as _handle_training_plan_webhook:
    extract → validate → create profile → load intake → mark processed →
    run pipeline → log order → send notification email.

    Required: intake_id (from a stored questionnaire), name, email.
    """
    secret = request.headers.get('X-Cron-Secret', '')
    if not secret or not hmac.compare_digest(secret, os.environ.get('CRON_SECRET', '')):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json() or {}
    intake_id = data.get('intake_id', '')

    # If questionnaire data is provided inline, store it and generate an intake_id
    if not intake_id and data.get('questionnaire'):
        intake_id = str(uuid.uuid4())
        store_intake(intake_id, data['questionnaire'])
        logger.info(f"Test: stored inline questionnaire as {intake_id}")

    if not intake_id:
        return jsonify({'error': 'intake_id or questionnaire object is required'}), 400

    # Build a fake Stripe event that mirrors real checkout.session.completed
    order_id = 'test_' + datetime.now().strftime('%Y%m%d%H%M%S')
    fake_stripe_data = {
        'data': {
            'object': {
                'id': order_id,
                'metadata': {
                    'intake_id': intake_id,
                    'product_type': 'training_plan',
                    'tier': 'custom',
                    'athlete_name': data.get('name', 'Test Athlete'),
                },
                'customer_details': {
                    'email': data.get('email', 'test@example.com'),
                    'name': data.get('name', 'Test Athlete'),
                }
            }
        }
    }

    # === Same code path as _handle_training_plan_webhook ===

    order_data = extract_stripe_data(fake_stripe_data)

    is_valid, error_msg = validate_order_data(order_data)
    if not is_valid:
        return jsonify({'error': error_msg}), 400

    athlete_id, profile_path = create_athlete_profile(order_data)

    # Load intake data (questionnaire) for pipeline
    intake_data = load_intake(intake_id)
    if not intake_data:
        return jsonify({'error': f'Intake {intake_id} not found or expired'}), 404

    # Backup intake (same as real flow)
    backup_path = Path(ATHLETES_DIR) / athlete_id / 'intake_backup.json'
    try:
        with open(backup_path, 'w') as f:
            json.dump(intake_data, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to backup intake data: {e}")

    # Idempotency mark (same as real flow)
    mark_order_processed(order_data['order_id'], athlete_id)

    # Send instant payment confirmation to customer (same as real flow)
    customer_email = order_data['profile'].get('email', '')
    customer_name = order_data['profile'].get('name', '')
    race_name = intake_data.get('race_name', '') if intake_data else ''
    _send_payment_confirmation(customer_email, customer_name, race_name=race_name)

    # Run pipeline with deliver=True (same as real flow)
    result = run_pipeline(athlete_id, deliver=True, intake_data=intake_data,
                          order_data=order_data)

    # Log order (same as real flow)
    log_order(order_data, result)

    # Persist deliverables to volume + create zip (same as real flow).
    # A successful subprocess without durable state is not fulfillment.
    persisted = None
    persistence_error = ''
    if result['success']:
        try:
            persisted = persist_deliverables(
                order_data['order_id'], athlete_id,
                source_dir=(result.get('artifact_dir')
                            or _resolve_generated_athlete_dir(athlete_id)),
                delivery_platform=order_data.get(
                    'delivery_platform', order_data.get('delivery_target', 'manual')),
                state_unavailable=result.get('fulfillment_state') == 'unavailable',
            )
        except Exception as e:
            logger.error(f"Failed to persist deliverables for {athlete_id}: {e}")
            persistence_error = str(e)
        if not persisted:
            result['success'] = False
            result['fulfillment_state'] = 'unavailable'
            result['stderr'] = (
                persistence_error or 'Persistence returned no durable order state')

    # Send notification email (same as real flow)
    details = _build_plan_notification_details(order_data, result, intake_data)
    if result['success'] and persisted:
        details['fulfillment_state'] = result.get('fulfillment_state', 'unavailable')
        details['fulfillment_status'] = persisted['state']['status']
        details['blocking_issues'] = persisted['state']['blocking_issues']
        details['required_confirmations'] = persisted['state']['required_confirmations']
        try:
            details['review_token'] = _generate_review_token(
                order_data['order_id'], NOTIFICATION_EMAIL)
        except (ReviewAuthError, FulfillmentStateError) as exc:
            logger.error(
                f'Review capability unavailable for order '
                f"{order_data.get('order_id', '')}: {exc}")
        _notify_new_order('training_plan', details)
        return jsonify({
            'status': 'success',
            'athlete_id': athlete_id,
            'profile_path': str(profile_path),
            'message': 'Full flow complete: profile → pipeline → log → notification',
            'pipeline': result,
        })
    else:
        _notify_new_order('training_plan_FAILED', details)
        return jsonify({
            'status': 'pipeline_failed',
            'athlete_id': athlete_id,
            'profile_path': str(profile_path),
            'message': 'Pipeline failed. Notification sent.',
            'pipeline': result,
        })


# =============================================================================
# POST-PURCHASE FOLLOW-UP EMAIL SEQUENCE
# =============================================================================

# Day offsets and email templates for training plan follow-ups.
# Coaching and consulting follow-ups are manual (high-touch).
# Canonical copy lives in webhook/email_templates.py (zero-dep module,
# voice rules + tests in webhook/tests/test_email_templates.py).
from email_templates import FOLLOWUP_SEQUENCE  # noqa: E402


def _get_followup_log_path():
    """Path to the follow-up sent log."""
    log_dir = Path(DATA_DIR) / '.logs'
    log_dir.mkdir(exist_ok=True)
    return log_dir / 'followup_sent.jsonl'


def _get_sent_followups():
    """Load set of (order_id, day) tuples already sent."""
    sent = set()
    log_path = _get_followup_log_path()
    if log_path.exists():
        for line in log_path.read_text().strip().split('\n'):
            if not line:
                continue
            try:
                entry = json.loads(line)
                sent.add((entry['order_id'], entry['day']))
            except (json.JSONDecodeError, KeyError):
                continue
    return sent


def _mark_followup_sent(order_id: str, day: int, email: str):
    """Record that a follow-up was sent."""
    log_path = _get_followup_log_path()
    entry = json.dumps({
        'order_id': order_id,
        'day': day,
        'email': _mask_email(email),
        'sent_at': datetime.utcnow().isoformat(),
    })
    with open(log_path, 'a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(entry + '\n')
        fcntl.flock(f, fcntl.LOCK_UN)


def _send_followup_email(email: str, subject: str, body: str,
                         brand: str = DEFAULT_BRAND):
    """Send a follow-up email via Resend. Returns True on success."""
    if not RESEND_API_KEY:
        logger.warning(f"Resend not configured — skipping followup to {_mask_email(email)}")
        return False

    reply_to = NOTIFICATION_EMAIL or None
    return _send_email(email, subject, body, reply_to=reply_to, brand=brand)


def process_followup_emails():
    """Check order logs and send due follow-up emails. Returns stats dict.

    Reads from YYYY-MM.jsonl files (written by log_order and _log_product_event).
    Only processes training_plan orders that succeeded.
    """
    log_dir = Path(DATA_DIR) / '.logs'

    if not log_dir.exists():
        return {'checked': 0, 'sent': 0, 'skipped': 0, 'errors': 0}

    sent_followups = _get_sent_followups()
    now = datetime.utcnow()
    stats = {'checked': 0, 'sent': 0, 'skipped': 0, 'errors': 0}

    # Read from all YYYY-MM.jsonl files (the format log_order actually writes to)
    for log_file in sorted(log_dir.glob('20*.jsonl')):
        for line in log_file.read_text().strip().split('\n'):
            if not line:
                continue
            try:
                order = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Only follow up on training_plan orders (coaching/consulting are high-touch)
            if order.get('product_type') != 'training_plan':
                continue

            # Skip failed orders — no plan was delivered
            if not order.get('success', True):
                continue

            stats['checked'] += 1
            order_id = order.get('order_id', '')
            email = order.get('email', '')
            name = order.get('name', order.get('customer_name', ''))
            brand = normalize_brand(order.get('brand'))
            order_time = order.get('timestamp', order.get('processed_at', ''))

            if not email or not order_time or not order_id:
                continue

            try:
                order_dt = datetime.fromisoformat(order_time.replace('Z', '+00:00').replace('+00:00', ''))
            except (ValueError, AttributeError):
                continue

            days_since = (now - order_dt).days

            for followup in FOLLOWUP_SEQUENCE:
                day = followup['day']
                # Send on the target day or up to 2 days late (catch-up window)
                if days_since < day or days_since > day + 2:
                    continue
                if (order_id, day) in sent_followups:
                    continue

                first_name = name.split()[0] if name else 'there'
                body = followup['template'].format(first_name=first_name)
                if brand != DEFAULT_BRAND:
                    brand_cfg = _brand_config(brand)
                    signature = brand_cfg.get('email', {})
                    body = body.replace(
                        '— Matti\nGravel God Coaching\ngravelgodcycling.com',
                        f"— {signature.get('signature_name', 'Matti')}\n"
                        f"{signature.get('signature_organization', brand_cfg['name'])}\n"
                        f"{signature.get('signature_site', brand_cfg['site'].replace('https://', ''))}",
                    ).replace('https://gravelgodcycling.com', brand_cfg['site'])
                subject = followup['subject']

                if _send_followup_email(email, subject, body, brand=brand):
                    _mark_followup_sent(order_id, day, email)
                    sent_followups.add((order_id, day))
                    stats['sent'] += 1
                    logger.info(
                        f"Followup day {day} sent to {_mask_email(email)} "
                        f"(order {order_id})"
                    )
                else:
                    stats['errors'] += 1

    stats['skipped'] = stats['checked'] * len(FOLLOWUP_SEQUENCE) - stats['sent'] - stats['errors']
    return stats


def _mark_consult_nudge_sent(order_id: str, nudge: str) -> None:
    def _mutate(r):
        nudges = set(r.get('nudges_sent') or [])
        nudges.add(nudge)
        r['nudges_sent'] = sorted(nudges)
    consultations.update_record(DELIVERIES_DIR, order_id, _mutate)


def process_consult_followups() -> dict:
    """State-conditional follow-ups for paid consultations.

    Unlike FOLLOWUP_SEQUENCE (fixed day-offset, training_plan orders only —
    it hard-filters product_type=='training_plan' above and is keyed
    (order_id, day)), this walks consultations/*.json and applies rules
    keyed on record STATE: welcome missing → resend; +24h no intake →
    nudge; +48h no TP link → nudge with fallback; each nudge fires at most
    once (tracked in record['nudges_sent']). Call-relative rules only run
    when call_at is set: +1d → plan-of-action reminder to the coach if not
    closed; +2d → add-on offer if not purchased. Also applies the 30-day
    give-up rule. See docs/CONSULT_ENGINE_SPEC.md §3.5.
    """
    stats = {
        'checked': 0, 'welcome_resent': 0, 'intake_nudged': 0,
        'tp_nudged': 0, 'plan_reminded': 0, 'addon_offered': 0,
        'closed_no_data': 0, 'errors': 0, 'runner_alarm_sent': False,
    }
    now = datetime.now(timezone.utc)

    # Runner heartbeat alarm (§6): missing/stale heartbeat or ok=false,
    # at most once per CONSULT_RUNNER_ALARM_COOLDOWN_HOURS. One check per
    # cron run, independent of any individual consultation record.
    try:
        stats['runner_alarm_sent'] = _check_consult_runner_alarm(now)
    except Exception:
        logger.exception("Failed to check consult runner heartbeat")
        stats['errors'] += 1

    for record in consultations.list_records(DELIVERIES_DIR):
        if record.get('status') == 'closed':
            continue
        stats['checked'] += 1
        order_id = record['order_id']
        brand = normalize_brand(record.get('brand'))
        athlete = record.get('athlete') or {}
        name = athlete.get('name') or ''
        first_name = name.split()[0] if name else 'there'
        nudges_sent = set(record.get('nudges_sent') or [])

        # Give-up rule: no TP link 30 days after paid.
        if consultations.should_give_up(record, now=now):
            try:
                def _close(r):
                    r['status'] = 'closed'
                    r['closed_reason'] = 'no_data_30d'
                    consultations.append_timeline(r, 'closed', 'no_data_30d')
                closed = consultations.update_record(DELIVERIES_DIR, order_id, _close)
                _notify_consult_needs_attention(closed, reason='no_data_30d — auto-closed')
                stats['closed_no_data'] += 1
            except Exception:
                logger.exception(f"Failed to close stale consult {order_id}")
                stats['errors'] += 1
            continue

        # Welcome missing → resend (idempotent: only marks welcome_sent_at
        # on success, so a Resend outage keeps retrying every cron run).
        if not record.get('welcome_sent_at'):
            try:
                if _send_consult_welcome(record, brand):
                    def _mark_welcome(r):
                        r['welcome_sent_at'] = consultations._now_iso()
                        consultations.append_timeline(r, 'welcome_sent', 'cron resend')
                    record = consultations.update_record(DELIVERIES_DIR, order_id, _mark_welcome)
                    stats['welcome_resent'] += 1
                else:
                    stats['errors'] += 1
            except Exception:
                logger.exception(f"Consult welcome resend failed for {order_id}")
                stats['errors'] += 1

        created_at = consultations._parse_iso(record.get('created_at'))
        if created_at:
            age = now - created_at

            if (age >= timedelta(hours=24)
                    and not (record.get('intake') or {}).get('received_at')
                    and 'intake_nudge' not in nudges_sent):
                try:
                    if _send_consult_intake_nudge(record, brand, first_name):
                        _mark_consult_nudge_sent(order_id, 'intake_nudge')
                        stats['intake_nudged'] += 1
                    else:
                        stats['errors'] += 1
                except Exception:
                    logger.exception(f"Consult intake nudge failed for {order_id}")
                    stats['errors'] += 1

            if (age >= timedelta(hours=48)
                    and not athlete.get('tp_matched_at')
                    and 'tp_nudge' not in nudges_sent):
                try:
                    if _send_consult_tp_nudge(record, brand, first_name):
                        _mark_consult_nudge_sent(order_id, 'tp_nudge')
                        stats['tp_nudged'] += 1
                    else:
                        stats['errors'] += 1
                except Exception:
                    logger.exception(f"Consult TP nudge failed for {order_id}")
                    stats['errors'] += 1

        # Call-relative rules — ONLY when call_at is set (§3.5).
        call_at = consultations._parse_iso(record.get('call_at'))
        if call_at:
            since_call = now - call_at

            if (since_call >= timedelta(days=1)
                    and record.get('status') != 'closed'
                    and 'plan_reminder' not in nudges_sent):
                try:
                    if _send_consult_plan_reminder(record):
                        _mark_consult_nudge_sent(order_id, 'plan_reminder')
                        stats['plan_reminded'] += 1
                    else:
                        stats['errors'] += 1
                except Exception:
                    logger.exception(f"Consult plan reminder failed for {order_id}")
                    stats['errors'] += 1

            if (since_call >= timedelta(days=2)
                    and not (record.get('products', {}).get('plan_addon') or {}).get('purchased')
                    and 'addon_offer' not in nudges_sent):
                try:
                    if _send_consult_addon_offer(record, brand, first_name):
                        offer_expires_at = (call_at + timedelta(days=7)).isoformat()

                        def _set_offer_expiry(r):
                            r.setdefault('products', {}).setdefault('plan_addon', {})['offer_expires_at'] = offer_expires_at
                        consultations.update_record(DELIVERIES_DIR, order_id, _set_offer_expiry)
                        _mark_consult_nudge_sent(order_id, 'addon_offer')
                        stats['addon_offered'] += 1
                    else:
                        stats['errors'] += 1
                except Exception:
                    logger.exception(f"Consult add-on offer failed for {order_id}")
                    stats['errors'] += 1

    return stats


# =============================================================================
# CONSULT-ENGINE C1 ROUTES (docs/CONSULT_ENGINE_SPEC.md §4, §5)
# =============================================================================

@app.route('/api/consult-intake', methods=['POST', 'OPTIONS'])
@limiter.limit("10/minute")
def consult_intake():
    """Intake submission for a paid consult.

    Body: {ref, t, answers}. The token (`t`) is a purpose-scoped,
    fragment-carried credential (consult_intake_tokens.py) — CORS
    Allow-Headers stays Content-Type only (set_security_headers, above)
    because the token travels in the body, not a header or the query
    string. Idempotent: a second submission for the same ref replaces the
    stored intake and appends another timeline event. Never synthesizes an
    unanswered field — whatever the athlete sent (including "don't know")
    is stored verbatim; the runner is responsible for labeling any
    TrainingPeaks-sourced numbers as such rather than as the athlete's
    answer.
    """
    if request.method == 'OPTIONS':
        return '', 204

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON'}), 400

    ref = str(data.get('ref') or '').strip()
    token = str(data.get('t') or '').strip()
    answers = data.get('answers')
    if not ref or not token or not isinstance(answers, dict):
        return jsonify({'error': 'ref, t, and answers are required'}), 400

    try:
        safe_ref = consultations._safe_order_id(ref)
    except consultations.ConsultationError:
        return jsonify({'error': 'invalid ref'}), 400

    try:
        verify_consult_intake_token(token, expected_order_id=safe_ref)
    except ConsultIntakeTokenError:
        return jsonify({'error': 'invalid or expired token'}), 401

    record = consultations.read_record(DELIVERIES_DIR, safe_ref)
    if record is None:
        return jsonify({'error': 'consultation not found'}), 404

    intake_id = str(uuid.uuid4())
    store_intake(intake_id, {'answers': answers, 'consult_order_id': safe_ref})

    def _mutate(r):
        replaced = bool((r.get('intake') or {}).get('intake_id'))
        r['intake'] = {'intake_id': intake_id, 'received_at': consultations._now_iso()}
        consultations.append_timeline(r, 'intake_received', 'replaced prior intake' if replaced else '')
    consultations.update_record(DELIVERIES_DIR, safe_ref, _mutate)

    logger.info(f"Consult intake received for {safe_ref}")
    return jsonify({'status': 'ok'})


def _require_runner_secret():
    """503 when CONSULT_RUNNER_SECRET is unset, 401 when the header is
    wrong — same pattern as CRON_SECRET (cron_followup_emails, above).
    Returns a Flask response to short-circuit the caller, or None."""
    if not CONSULT_RUNNER_SECRET:
        return jsonify({'error': 'CONSULT_RUNNER_SECRET not configured'}), 503
    supplied = request.headers.get('X-Runner-Secret', '')
    if not hmac.compare_digest(supplied, CONSULT_RUNNER_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401
    return None


@app.route('/api/consult/jobs/pending', methods=['GET'])
@limiter.limit("60/minute")
def consult_jobs_pending():
    """Open records lacking tp_matched_at — the runner's roster matcher
    does ONE poll and matches by email, not one lookup per record."""
    auth_err = _require_runner_secret()
    if auth_err:
        return auth_err

    pending = []
    for record in consultations.list_records(DELIVERIES_DIR):
        if record.get('status') == 'closed':
            continue
        athlete = record.get('athlete') or {}
        if athlete.get('tp_matched_at'):
            continue
        intake_tp_email = ''
        intake_id = (record.get('intake') or {}).get('intake_id')
        if intake_id:
            intake_data = load_intake(intake_id) or {}
            intake_tp_email = str((intake_data.get('answers') or {}).get('tp_email') or '')
        pending.append({
            'order_id': record['order_id'],
            'email': athlete.get('email', ''),
            'intake_tp_email': intake_tp_email,
        })
    return jsonify({'pending': pending})


@app.route('/api/consult/jobs/ready', methods=['GET'])
@limiter.limit("60/minute")
def consult_jobs_ready():
    """TP-linked records ready for analysis (§5): status open, or
    analysis_running with an EXPIRED lease (a safety net ahead of the
    hourly sweep_stuck_consultations() sweep — a record can sit
    analysis_running with a dead lease for up to an hour otherwise),
    attempts under the max, never closed. A missing lease_expires_at on an
    analysis_running record counts as expired too, matching /claim's own
    "no live lease" check. Oldest first by created_at so the runner works
    the queue in order."""
    auth_err = _require_runner_secret()
    if auth_err:
        return auth_err

    now = datetime.now(timezone.utc)
    ready = []
    for record in consultations.list_records(DELIVERIES_DIR):
        if record.get('status') == 'closed':
            continue
        athlete = record.get('athlete') or {}
        if not athlete.get('tp_matched_at'):
            continue

        analysis = record.get('analysis') or {}
        attempts = int(analysis.get('attempts', 0))
        if attempts >= CONSULT_ANALYSIS_MAX_ATTEMPTS:
            continue

        status = record.get('status')
        if status == 'analysis_running':
            lease_raw = analysis.get('lease_expires_at')
            lease_expires = consultations._parse_iso(lease_raw) if lease_raw else None
            if lease_expires is not None and lease_expires > now:
                continue  # live lease — someone else has it
        elif status != 'open':
            continue  # report_ready / needs_attention are not analysis-ready

        intake_answers = None
        intake_id = (record.get('intake') or {}).get('intake_id')
        if intake_id:
            intake_data = load_intake(intake_id) or {}
            answers = intake_data.get('answers')
            if isinstance(answers, dict):
                intake_answers = answers

        plan_addon = (record.get('products') or {}).get('plan_addon') or {}
        ready.append((record.get('created_at') or '', {
            'order_id': record['order_id'],
            'tp_athlete_id': athlete.get('tp_athlete_id'),
            'email': athlete.get('email', ''),
            'intake_answers': intake_answers,
            'plan_addon': {
                'purchased': bool(plan_addon.get('purchased')),
                'purchased_at': plan_addon.get('purchased_at'),
            },
            'call_at': record.get('call_at'),
            'attempts': attempts,
        }))

    ready.sort(key=lambda pair: (pair[0], pair[1]['order_id']))
    return jsonify({'ready': [item for _, item in ready]})


@app.route('/api/consult/jobs/<order_id>/tp-linked', methods=['POST'])
@limiter.limit("60/minute")
def consult_job_tp_linked(order_id):
    """Runner match confirmed — sets tp_matched_at. Idempotent: a repeat
    call for an already-matched record is a no-op (no second timestamp)."""
    auth_err = _require_runner_secret()
    if auth_err:
        return auth_err

    data = request.get_json(silent=True) or {}
    tp_athlete_id = str(data.get('tp_athlete_id') or '').strip()
    if not tp_athlete_id:
        return jsonify({'error': 'tp_athlete_id is required'}), 400

    def _mutate(r):
        athlete = r.setdefault('athlete', {})
        if not athlete.get('tp_matched_at'):
            athlete['tp_athlete_id'] = tp_athlete_id
            athlete['tp_matched_at'] = consultations._now_iso()
            consultations.append_timeline(r, 'tp_linked', tp_athlete_id)
    try:
        record = consultations.update_record(DELIVERIES_DIR, order_id, _mutate)
    except consultations.ConsultationError:
        return jsonify({'error': 'not found'}), 404

    return jsonify({'status': 'ok', 'tp_matched_at': record['athlete']['tp_matched_at']})


@app.route('/api/consult/jobs/<order_id>/claim', methods=['POST'])
@limiter.limit("60/minute")
def consult_job_claim(order_id):
    """Lease the record for analysis: claimed_by, lease_expires_at = now +
    90min, attempts+1, status → analysis_running. Refuses (409) if a live
    lease already exists; sweep_stuck_consultations() reclaims expired
    leases."""
    auth_err = _require_runner_secret()
    if auth_err:
        return auth_err

    data = request.get_json(silent=True) or {}
    claimed_by = str(data.get('claimed_by') or 'runner').strip() or 'runner'
    now = datetime.now(timezone.utc)
    outcome = {}

    def _mutate(r):
        analysis = r.setdefault('analysis', {})
        lease_raw = analysis.get('lease_expires_at')
        live_lease = False
        if lease_raw:
            try:
                lease_expires = datetime.fromisoformat(lease_raw)
                if lease_expires.tzinfo is None:
                    lease_expires = lease_expires.replace(tzinfo=timezone.utc)
                live_lease = lease_expires > now
            except (TypeError, ValueError):
                live_lease = False
        if live_lease:
            outcome['conflict'] = True
            return
        analysis['claimed_by'] = claimed_by
        analysis['lease_expires_at'] = (now + timedelta(minutes=CONSULT_ANALYSIS_LEASE_MINUTES)).isoformat()
        analysis['attempts'] = int(analysis.get('attempts', 0)) + 1
        analysis['started_at'] = now.isoformat()
        r['status'] = 'analysis_running'
        consultations.append_timeline(r, 'claimed', claimed_by)
        outcome['conflict'] = False

    try:
        record = consultations.update_record(DELIVERIES_DIR, order_id, _mutate)
    except consultations.ConsultationError:
        return jsonify({'error': 'not found'}), 404

    if outcome.get('conflict'):
        return jsonify({'error': 'already claimed',
                        'claimed_by': record['analysis']['claimed_by']}), 409

    return jsonify({'status': 'claimed', 'lease_expires_at': record['analysis']['lease_expires_at']})


def _consult_report_to_html(markdown_text: str) -> str:
    """Minimal, dependency-free markdown→HTML for the coach report email
    (no markdown library in requirements.txt — full rendering lives in
    the attached report.md itself)."""
    escaped = html_escape(markdown_text)
    lines = []
    for line in escaped.split('\n'):
        if line.startswith('## '):
            lines.append(f"<h3>{line[3:]}</h3>")
        elif line.startswith('# '):
            lines.append(f"<h2>{line[2:]}</h2>")
        else:
            lines.append(line)
    body = '<br>'.join(lines)
    return f'<div style="font-family: Helvetica, Arial, sans-serif; white-space: pre-wrap;">{body}</div>'


@app.route('/api/consult/jobs/<order_id>/report', methods=['POST'])
@limiter.limit("60/minute")
def consult_job_report(order_id):
    """Multipart upload {report_md, report_json?, receipts?} → status
    report_ready + coach email (markdown→html) with attachments. Athlete
    gets nothing automatically. Idempotent: a repeat post for an already
    report_ready record still 200s but sends no second email — runner
    retries with backoff are guaranteed to hit this twice on occasion."""
    auth_err = _require_runner_secret()
    if auth_err:
        return auth_err

    try:
        safe_id = consultations._safe_order_id(order_id)
    except consultations.ConsultationError:
        return jsonify({'error': 'invalid order_id'}), 400

    record = consultations.read_record(DELIVERIES_DIR, safe_id)
    if record is None:
        return jsonify({'error': 'not found'}), 404

    report_md_file = request.files.get('report_md')
    if not report_md_file:
        return jsonify({'error': 'report_md is required'}), 400
    report_json_file = request.files.get('report_json')
    receipts_file = request.files.get('receipts')

    already_ready = (record.get('status') == 'report_ready'
                     and bool((record.get('analysis') or {}).get('report_path')))

    report_dir = Path(DELIVERIES_DIR) / 'consultations' / safe_id
    report_dir.mkdir(parents=True, exist_ok=True)

    report_md_bytes = report_md_file.read()
    report_md_path = report_dir / 'report.md'
    report_md_path.write_bytes(report_md_bytes)

    if report_json_file:
        (report_dir / 'report.json').write_bytes(report_json_file.read())

    receipts_path = None
    if receipts_file:
        receipts_path = report_dir / 'receipts.zip'
        receipts_path.write_bytes(receipts_file.read())

    def _mutate(r):
        r['status'] = 'report_ready'
        analysis = r.setdefault('analysis', {})
        analysis['report_path'] = str(report_md_path)
        analysis['finished_at'] = consultations._now_iso()
        analysis['error'] = None
        consultations.append_timeline(r, 'report')
    record = consultations.update_record(DELIVERIES_DIR, safe_id, _mutate)

    if not already_ready:
        try:
            report_text = report_md_bytes.decode('utf-8', errors='replace')
            attachments = [('report.md', report_md_bytes)]
            if receipts_path:
                attachments.append(('receipts.zip', receipts_path))
            name = (record.get('athlete') or {}).get('name', 'Unknown')
            subject = f"[GG] Consult report ready: {name} ({safe_id})"
            if NOTIFICATION_EMAIL:
                _send_email(NOTIFICATION_EMAIL, subject, report_text,
                           html=_consult_report_to_html(report_text),
                           attachments=attachments,
                           brand=normalize_brand(record.get('brand')))
            else:
                logger.critical(f"CONSULT REPORT READY: {subject}")
        except Exception:
            logger.exception(f"Failed to send report-ready email for {safe_id}")

    return jsonify({'status': 'ok', 'record_status': record['status']})


@app.route('/api/consult/jobs/<order_id>/error', methods=['POST'])
@limiter.limit("60/minute")
def consult_job_error(order_id):
    """Runner-reported failure → needs_attention + coach email (error text
    included — this mail is coach-only, no projection). Idempotent: the
    same error text posted twice sends only one alert."""
    auth_err = _require_runner_secret()
    if auth_err:
        return auth_err

    data = request.get_json(silent=True) or {}
    error_text = str(data.get('error') or '').strip()[:5000]

    record = consultations.read_record(DELIVERIES_DIR, order_id)
    if record is None:
        return jsonify({'error': 'not found'}), 404

    already_flagged = (record.get('status') == 'needs_attention'
                       and (record.get('analysis') or {}).get('error') == error_text)

    def _mutate(r):
        r['status'] = 'needs_attention'
        analysis = r.setdefault('analysis', {})
        analysis['error'] = error_text
        analysis['finished_at'] = consultations._now_iso()
        consultations.append_timeline(r, 'error', error_text[:200])
    record = consultations.update_record(DELIVERIES_DIR, order_id, _mutate)

    if not already_flagged:
        try:
            _notify_consult_needs_attention(record, reason=error_text or 'runner reported error')
        except Exception:
            logger.exception(f"Failed to notify consult error for {order_id}")

    return jsonify({'status': 'ok'})


@app.route('/api/consult/jobs/<order_id>', methods=['GET'])
@limiter.limit("60/minute")
def consult_job_get(order_id):
    """Full record read — X-Runner-Secret ONLY. The athlete intake token
    (consult_intake_tokens.py) is scoped to /api/consult-intake and can
    never read this."""
    auth_err = _require_runner_secret()
    if auth_err:
        return auth_err

    record = consultations.read_record(DELIVERIES_DIR, order_id)
    if record is None:
        return jsonify({'error': 'not found'}), 404
    return jsonify(record)


def _consult_delivery_findings(report: dict) -> list:
    """Findings for the Endure delivery payload (CD-1b §6 /
    endurelabs specs/consult-delivery/spec.md §3.1): the ONE thing first,
    then up to 7 non-placeholder data_bullets, each shaped
    {title, body, kind, confidence}. Read from the runner's stored
    report.json — never recomputed here."""
    findings = []
    one_thing = report.get('one_thing') or {}
    text = one_thing.get('text')
    if text:
        kind = 'physiological_limiter' if one_thing.get('label') == 'durability' else 'pattern'
        title = str(one_thing.get('label') or 'primary-finding').replace('-', ' ').title()
        findings.append({'title': title, 'body': text, 'kind': kind, 'confidence': 0.85})

    bullets = [
        str(b) for b in (report.get('data_bullets') or [])
        if b and not str(b).startswith('not available')
    ]
    for bullet_text in bullets[:7]:
        title = bullet_text if len(bullet_text) <= 60 else bullet_text[:57] + '...'
        findings.append({'title': title, 'body': bullet_text, 'kind': 'pattern', 'confidence': 0.75})

    return findings


def _consult_delivery_prefill(report: dict) -> dict:
    """ftp/lthr/max_hr(/weight if present) from report.json's athlete_card
    (CD-1b §6). Keys absent from athlete_card are simply omitted."""
    card = report.get('athlete_card') or {}
    prefill = {}
    for key in ('ftp', 'lthr', 'max_hr', 'weight'):
        value = card.get(key)
        if value is not None:
            prefill[key] = value
    return prefill


@app.route('/api/consult/jobs/deliver', methods=['GET'])
@limiter.limit("60/minute")
def consult_jobs_deliver():
    """Records flagged for Endure delivery by the operator endpoint (§6):
    endure.requested_at set, endure.delivered_at still null. Findings +
    prefill come from the report.json the runner already uploaded via
    /report (build_report.py, ~/gg-consult-runner/report/) — this route
    never recomputes analysis."""
    auth_err = _require_runner_secret()
    if auth_err:
        return auth_err

    items = []
    for record in consultations.list_records(DELIVERIES_DIR):
        endure = record.get('endure') or {}
        if not endure.get('requested_at') or endure.get('delivered_at'):
            continue

        order_id = record['order_id']
        athlete = record.get('athlete') or {}
        name = str(athlete.get('name') or '').strip()
        name_parts = name.split(None, 1)
        first_name = name_parts[0] if name_parts else ''
        last_name = name_parts[1] if len(name_parts) > 1 else ''

        goal_event = None
        intake_id = (record.get('intake') or {}).get('intake_id')
        if intake_id:
            intake_data = load_intake(intake_id) or {}
            answers = intake_data.get('answers')
            if isinstance(answers, dict):
                goal_event = answers.get('goal_event') or None

        report = {}
        report_path = Path(DELIVERIES_DIR) / 'consultations' / order_id / 'report.json'
        try:
            report = json.loads(report_path.read_text())
        except (OSError, json.JSONDecodeError):
            report = {}

        plan_addon = bool(((record.get('products') or {}).get('plan_addon') or {}).get('purchased'))

        items.append({
            'order_id': order_id,
            'tp_athlete_id': athlete.get('tp_athlete_id'),
            'email': athlete.get('email', ''),
            'first_name': first_name,
            'last_name': last_name,
            'consult_date': record.get('call_at') or record.get('created_at'),
            'goal_event': goal_event,
            'plan_addon': plan_addon,
            'plan_of_action_md': endure.get('plan_of_action_md', ''),
            'findings': _consult_delivery_findings(report),
            'prefill': _consult_delivery_prefill(report),
        })

    return jsonify({'deliver': items})


@app.route('/api/consult/jobs/<order_id>/endure-delivered', methods=['POST'])
@limiter.limit("60/minute")
def consult_job_endure_delivered(order_id):
    """Runner confirms delivery to Endure (§6): {result} → endure.
    delivered_at + endure.result, timeline, coach email with the invite
    URL when present. Idempotent: delivered_at is set once; a repeat post
    (runner retry with backoff) sends no second email."""
    auth_err = _require_runner_secret()
    if auth_err:
        return auth_err

    data = request.get_json(silent=True) or {}
    result = data.get('result') if isinstance(data.get('result'), dict) else {}

    existing = consultations.read_record(DELIVERIES_DIR, order_id)
    if existing is None:
        return jsonify({'error': 'not found'}), 404
    already_delivered = bool((existing.get('endure') or {}).get('delivered_at'))

    def _mutate(r):
        endure = r.setdefault('endure', {
            'requested_at': None, 'plan_of_action_md': '', 'delivered_at': None, 'result': None,
        })
        if not endure.get('delivered_at'):
            endure['delivered_at'] = consultations._now_iso()
        endure['result'] = result
        consultations.append_timeline(r, 'endure_delivered')

    try:
        record = consultations.update_record(DELIVERIES_DIR, order_id, _mutate)
    except consultations.ConsultationError:
        return jsonify({'error': 'not found'}), 404

    if not already_delivered:
        try:
            name = (record.get('athlete') or {}).get('name', 'Unknown')
            invitation = result.get('invitation') if isinstance(result.get('invitation'), dict) else {}
            invite_url = str(invitation.get('url') or '')
            subject, body = build_consult_endure_delivered_email(order_id, name, invite_url)
            if NOTIFICATION_EMAIL:
                _send_email(NOTIFICATION_EMAIL, subject, body, brand=normalize_brand(record.get('brand')))
            else:
                logger.critical(f"CONSULT ENDURE DELIVERED: {subject}")
        except Exception:
            logger.exception(f"Failed to send endure-delivered email for {order_id}")

    return jsonify({'status': 'ok', 'delivered_at': record['endure']['delivered_at']})


def _consult_runner_heartbeat_path() -> Path:
    return Path(DELIVERIES_DIR) / 'consult_runner_heartbeat.json'


def _write_consult_runner_heartbeat(data: dict) -> None:
    """Atomically persist the runner heartbeat (temp file + os.replace),
    same pattern as _write_job / consultations.write_record."""
    path = _consult_runner_heartbeat_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp')
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_consult_runner_heartbeat() -> dict:
    path = _consult_runner_heartbeat_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


@app.route('/api/consult/runner/heartbeat', methods=['POST'])
@limiter.limit("60/minute")
def consult_runner_heartbeat():
    """Runner liveness ping (§6: posted "every poll"). Persisted to
    DELIVERIES_DIR/consult_runner_heartbeat.json so
    process_consult_followups() can alert the coach when the runner goes
    silent (>CONSULT_RUNNER_HEARTBEAT_STALE_HOURS) or reports ok=false."""
    auth_err = _require_runner_secret()
    if auth_err:
        return auth_err

    data = request.get_json(silent=True) or {}
    runner_id = str(data.get('runner_id') or '').strip()
    ok = bool(data.get('ok'))
    detail = str(data.get('detail') or '').strip()[:2000]

    # Carry the alarm cooldown timestamp forward — it lives in this same
    # file so the 24h suppression survives across heartbeat writes.
    existing = _read_consult_runner_heartbeat()
    payload = {
        'runner_id': runner_id,
        'ok': ok,
        'detail': detail,
        'at': datetime.now(timezone.utc).isoformat(),
        'last_runner_alarm_at': existing.get('last_runner_alarm_at'),
    }
    _write_consult_runner_heartbeat(payload)
    return jsonify({'status': 'ok'})


def _check_consult_runner_alarm(now: datetime) -> bool:
    """Called from process_consult_followups(). Coach email when the
    heartbeat file is missing/stale (>CONSULT_RUNNER_HEARTBEAT_STALE_HOURS)
    or the latest heartbeat reports ok=false — at most once per
    CONSULT_RUNNER_ALARM_COOLDOWN_HOURS (cooldown persisted in the same
    heartbeat file). Returns True iff an alarm was sent."""
    heartbeat = _read_consult_runner_heartbeat()
    at = consultations._parse_iso(heartbeat.get('at'))
    stale = at is None or (now - at) >= timedelta(hours=CONSULT_RUNNER_HEARTBEAT_STALE_HOURS)
    failed = heartbeat.get('ok') is False
    if not (stale or failed):
        return False

    last_alarm = consultations._parse_iso(heartbeat.get('last_runner_alarm_at'))
    if last_alarm and (now - last_alarm) < timedelta(hours=CONSULT_RUNNER_ALARM_COOLDOWN_HOURS):
        return False

    if at is None:
        age = 'no heartbeat received yet'
    else:
        age = f'{(now - at).total_seconds() / 3600:.1f}h old'
    detail = heartbeat.get('detail') or ('runner reported ok=false' if failed else '')

    subject, body = build_consult_runner_alarm_email(detail=detail, age=age)
    sent = True
    if NOTIFICATION_EMAIL:
        sent = _send_email(NOTIFICATION_EMAIL, subject, body, brand=DEFAULT_BRAND)
    else:
        logger.critical(f"CONSULT RUNNER ALARM: {subject}\n{body}")

    if sent:
        heartbeat['last_runner_alarm_at'] = now.isoformat()
        _write_consult_runner_heartbeat(heartbeat)
    return sent


@app.route('/api/consult/<order_id>/op', methods=['POST'])
@limiter.limit("20/minute")
def consult_operator_op(order_id):
    """Matti's only lever until a review surface exists (§5): {call_at} |
    {close: reason} | {retry: true} | {deliver_endure: {plan_of_action_md}},
    via X-Cron-Secret. The coach's needs_attention email includes the curl
    one-liners for this."""
    secret = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not hmac.compare_digest(secret, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    if not any(k in data for k in ('call_at', 'close', 'retry', 'deliver_endure')):
        return jsonify({'error': 'call_at, close, retry, or deliver_endure is required'}), 400

    applied = []

    def _mutate(r):
        if data.get('call_at'):
            r['call_at'] = str(data['call_at'])
            consultations.append_timeline(r, 'call_set', r['call_at'])
            applied.append('call_at')
        if data.get('close'):
            r['status'] = 'closed'
            r['closed_reason'] = str(data['close'])
            consultations.append_timeline(r, 'closed', str(data['close']))
            applied.append('close')
        if data.get('retry'):
            r['status'] = 'open'
            analysis = r.setdefault('analysis', {})
            analysis['claimed_by'] = None
            analysis['lease_expires_at'] = None
            analysis['error'] = None
            consultations.append_timeline(r, 'error', 'operator retry')
            applied.append('retry')
        if isinstance(data.get('deliver_endure'), dict):
            plan_of_action_md = str(data['deliver_endure'].get('plan_of_action_md') or '')
            r['endure'] = {
                'requested_at': consultations._now_iso(),
                'plan_of_action_md': plan_of_action_md,
                'delivered_at': None,
                'result': None,
            }
            consultations.append_timeline(r, 'endure_requested')
            applied.append('deliver_endure')

    try:
        record = consultations.update_record(DELIVERIES_DIR, order_id, _mutate)
    except consultations.ConsultationError:
        return jsonify({'error': 'not found'}), 404

    if not applied:
        return jsonify({'error': 'no recognized operation'}), 400

    return jsonify({'status': 'ok', 'applied': applied, 'record_status': record['status']})


# =============================================================================
# LIFECYCLE TOUCHPOINTS — plan-aware anti-churn emails
#
# Unlike the fixed day-1/3/7 FOLLOWUP_SEQUENCE, these are computed from the
# athlete's actual plan calendar (plan_dates.yaml): FTP-rescale offer after
# the testing week, reassurance at the first recovery week, a mid-plan
# survey, B-race debriefs, race-week checklist, and the post-race
# survey + coaching offer. All reply-driven: responses land in the coach
# inbox and become coaching-funnel conversations.
# =============================================================================

def compute_touchpoints(plan_dates: dict, first_name: str, race_name: str) -> list:
    """Compute the lifecycle touchpoint schedule for one athlete's plan.

    Returns a list of {'date': 'YYYY-MM-DD', 'key': str, 'subject': str,
    'body': str}, sorted by date. Dates come from plan_dates.yaml — the
    same calendar that drives workout generation, so touches always match
    the plan the athlete is actually riding.
    """
    from datetime import timedelta as _td

    weeks = plan_dates.get('weeks', [])
    if not weeks:
        return []

    def _d(s):
        return datetime.strptime(s, '%Y-%m-%d')

    def _iso(dt):
        return dt.strftime('%Y-%m-%d')

    touches = []
    plan_start = plan_dates.get('plan_start', '')
    race_date = plan_dates.get('race_date', '')

    # 1. Setup check — day 2 of the plan
    if plan_start:
        touches.append({
            'date': _iso(_d(plan_start) + _td(days=1)),
            'key': 'setup_check',
            'subject': 'Quick check — did everything load OK?',
            'body': (
                f"Hey {first_name},\n\n"
                "You're one day into the plan. Quick check: did the workouts "
                "load into TrainingPeaks OK, and did the first session sync "
                "to your head unit?\n\n"
                "If anything looks off, just hit reply and I'll sort it out "
                "today.\n\nMatti\nGravel God Cycling"
            ),
        })

    # 2. FTP rescale offer — end of week 1 (testing week)
    if weeks:
        w1_sunday = weeks[0].get('sunday', '')
        if w1_sunday:
            touches.append({
                'date': w1_sunday,
                'key': 'ftp_rescale',
                'subject': 'Got your test results? Reply and I\'ll rescale your plan',
                'body': (
                    f"Hey {first_name},\n\n"
                    "Week 1 testing is done. If your FTP came out different "
                    "from what you put in the questionnaire, reply with the "
                    "new number and I'll rescale every remaining workout in "
                    "your plan to match. Takes me minutes, keeps every "
                    "interval honest.\n\n"
                    "This is the difference between a static plan and one "
                    "that adapts with you — use it.\n\nMatti"
                ),
            })

    # 3. First recovery week — reassurance
    for w in weeks:
        if w.get('is_recovery_week'):
            touches.append({
                'date': w.get('monday', ''),
                'key': 'recovery_note',
                'subject': 'This week is supposed to feel easy',
                'body': (
                    f"Hey {first_name},\n\n"
                    "You just hit your first recovery week. The volume drop "
                    "is intentional — this is where your body banks the "
                    "fitness from the last block. Don't add workouts, don't "
                    "extend rides. Feeling fresh by Sunday IS the workout.\n\n"
                    "If you're NOT feeling recovered by end of week, reply "
                    "and tell me — that's signal worth acting on.\n\nMatti"
                ),
            })
            break

    # 4. Mid-plan survey — ~45% through
    if len(weeks) >= 6:
        mid_week = weeks[max(1, round(len(weeks) * 0.45)) - 1]
        touches.append({
            'date': mid_week.get('monday', ''),
            'key': 'midplan_survey',
            'subject': f'Halfway check-in — 3 quick questions',
            'body': (
                f"Hey {first_name},\n\n"
                "You're around the halfway mark. Three questions — just hit "
                "reply with one-line answers:\n\n"
                "1. Are you finishing the hard days, or surviving them?\n"
                "2. Is the plan too hard, too easy, or about right?\n"
                "3. What's getting in the way, if anything?\n\n"
                "I read every reply and adjust plans when the answers call "
                "for it.\n\nMatti"
            ),
        })

    # 5. B-race debriefs — day after each B-race
    seen_b = set()
    for w in weeks:
        b = w.get('b_race')
        if b and b.get('date') and b['date'] not in seen_b:
            seen_b.add(b['date'])
            touches.append({
                'date': _iso(_d(b['date']) + _td(days=1)),
                'key': f"b_debrief_{b['date']}",
                'subject': f"How did {b.get('name', 'the race')} go?",
                'body': (
                    f"Hey {first_name},\n\n"
                    f"How was {b.get('name', 'the race')}? Reply with the "
                    "short version — result, how the legs felt, anything "
                    "that surprised you. If something's off, there's still "
                    "time to tune the final block before "
                    f"{race_name}.\n\nMatti"
                ),
            })

    # 6. Race-week checklist — Monday of race week
    race_week_monday = plan_dates.get('race_week_monday', '')
    if race_week_monday:
        touches.append({
            'date': race_week_monday,
            'key': 'race_week',
            'subject': f'Race week. Here\'s your checklist.',
            'body': (
                f"Hey {first_name},\n\n"
                f"It's race week for {race_name}. The work is done — "
                "nothing you do this week makes you fitter, plenty makes "
                "you slower. Checklist:\n\n"
                "- Follow the taper exactly. Openers sharpen, they don't train\n"
                "- Fueling: practice nothing new on race day; your race-day "
                "carb targets are in your fueling plan\n"
                "- Equipment check Saturday ride: tires, sealant, bolts, bags\n"
                "- Sleep is the priority. Wednesday night matters more than "
                "the night before\n\n"
                "Go get it. Reply if anything feels off.\n\nMatti"
            ),
        })

    # 7. Post-race — survey + coaching bridge
    if race_date:
        touches.append({
            'date': _iso(_d(race_date) + _td(days=1)),
            'key': 'postrace',
            'subject': f'How did {race_name} go?',
            'body': (
                f"Hey {first_name},\n\n"
                f"You did it. However {race_name} went, I want to hear it — "
                "reply with:\n\n"
                "1. Your result (and how it compared to your goal)\n"
                "2. The single best and worst moment of the day\n"
                "3. Would you recommend this plan to a riding buddy? "
                "(honest answer)\n\n"
                "And if this race lit the fire for the next one: my coaching "
                "roster has a spot open, and plan customers who join within "
                "two weeks get their first month's plan adjustments built "
                "off everything we just learned about you. Reply 'coaching' "
                "and I'll send details.\n\nMatti"
            ),
        })

    touches = [t for t in touches if t.get('date')]
    touches.sort(key=lambda t: t['date'])
    return touches


def process_touchpoint_emails():
    """Send lifecycle touchpoints due today. Returns stats dict.

    Stateless: recomputes each athlete's schedule from plan_dates.yaml on
    every run and dedupes via the followup sent-log (key = 'tp:<key>').
    """
    log_dir = Path(DATA_DIR) / '.logs'
    if not log_dir.exists():
        return {'checked': 0, 'sent': 0, 'errors': 0}

    sent = _get_sent_followups()
    today = datetime.utcnow().strftime('%Y-%m-%d')
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
    stats = {'checked': 0, 'sent': 0, 'errors': 0}

    for log_file in sorted(log_dir.glob('20*.jsonl')):
        for line in log_file.read_text().strip().split('\n'):
            if not line:
                continue
            try:
                order = json.loads(line)
            except json.JSONDecodeError:
                continue
            if order.get('product_type') != 'training_plan':
                continue
            if not order.get('success', True):
                continue

            athlete_id = order.get('athlete_id', '')
            email = order.get('email', '')
            name = order.get('name', '')
            order_id = order.get('order_id', '')
            if not athlete_id or not email or not order_id:
                continue

            plan_dates_path = (Path(ATHLETES_DIR)
                               / athlete_id.replace('_', '-')
                               / 'plan_dates.yaml')
            if not plan_dates_path.exists():
                plan_dates_path = Path(ATHLETES_DIR) / athlete_id / 'plan_dates.yaml'
            if not plan_dates_path.exists():
                continue

            try:
                with open(plan_dates_path) as f:
                    plan_dates = yaml.safe_load(f) or {}
            except (OSError, yaml.YAMLError):
                continue

            stats['checked'] += 1
            first_name = name.split()[0] if name else 'there'
            race_name = order.get('race_name', 'your race')

            for touch in compute_touchpoints(plan_dates, first_name, race_name):
                # Due today (or yesterday — 1-day catch-up window)
                if touch['date'] not in (today, yesterday):
                    continue
                dedupe_key = (order_id, f"tp:{touch['key']}")
                if dedupe_key in sent:
                    continue
                try:
                    _send_email(
                        to=email,
                        subject=touch['subject'],
                        body=touch['body'],
                        reply_to=NOTIFICATION_EMAIL or None,
                    )
                    _mark_followup_sent(order_id, f"tp:{touch['key']}", email)
                    sent.add(dedupe_key)
                    stats['sent'] += 1
                    logger.info(
                        f"Touchpoint {touch['key']} sent to "
                        f"{_mask_email(email)} (order {order_id})"
                    )
                except Exception as e:
                    stats['errors'] += 1
                    logger.error(f"Touchpoint send failed: {e}")

    return stats


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 2)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 2)


def _coaching_funnel_projection(case: dict) -> dict:
    """Return analytics-only booleans/timings; never project athlete PII."""
    events = case.get('analytics_events') or []
    event_names = {event.get('event_name') for event in events}
    verifications = case.get('verifications') or {}
    checkout = case.get('checkout') or {}
    payment = (case.get('receipts') or {}).get('stripe_payment') or {}
    onboarding = case.get('onboarding_materials') or {}
    billing = case.get('billing') or {}
    submitted_at = _parse_utc((case.get('source') or {}).get('submitted_at'))
    paid_at = _parse_utc(payment.get('confirmed_at'))
    active_event = next(
        (event for event in events
         if event.get('event_name') == 'coaching_active'), None)
    active_at = _parse_utc((active_event or {}).get('occurred_at'))

    def verified(gate, status):
        return (verifications.get(gate) or {}).get('status') == status

    return {
        'brand': normalize_brand(case.get('brand')),
        'tier': str(case.get('tier') or 'unknown'),
        'submitted_at': submitted_at,
        'stages': {
            'applications': True,
            'fit_approved': verified('coach_fit', 'approved'),
            'terms_signed': (
                verified('coaching_agreement', 'signed') and
                verified('data_consent', 'signed')),
            'checkout_created': bool(checkout.get('session_id')),
            'checkout_expired': (
                bool(checkout.get('expired_at')) or
                'coaching_checkout_expired' in event_names),
            'recovery_sent': (
                bool(checkout.get('recovery_sent_at')) or
                'coaching_checkout_recovery_sent' in event_names),
            'payment_confirmed': bool(payment),
            'billing_healthy': (
                bool(payment) and
                str(billing.get('standing') or 'healthy') in
                ('healthy', 'trialing', 'canceling_at_period_end')),
            'billing_attention': str(billing.get('standing') or '') in (
                'past_due', 'action_required', 'unpaid', 'incomplete', 'paused'),
            'subscription_ended': str(billing.get('standing') or '') in (
                'ended', 'canceled'),
            'checkout_recovered': (
                bool(payment.get('recovered_from')) or
                'coaching_checkout_recovered' in event_names),
            'trainingpeaks_connected': verified(
                'trainingpeaks_connection', 'verified'),
            'context_sealed': verified('athlete_context', 'sealed'),
            'plan_approved': verified('coach_plan_approval', 'approved'),
            'onboarding_delivered': bool(onboarding.get('delivered_at')),
            'active': case.get('state') == 'ACTIVE',
        },
        'application_to_payment_hours': (
            (paid_at - submitted_at).total_seconds() / 3600
            if paid_at and submitted_at and paid_at >= submitted_at else None),
        'application_to_active_hours': (
            (active_at - submitted_at).total_seconds() / 3600
            if active_at and submitted_at and active_at >= submitted_at else None),
    }


def _aggregate_coaching_funnel(projections: list[dict]) -> dict:
    stage_names = (
        'applications', 'fit_approved', 'terms_signed', 'checkout_created',
        'checkout_expired', 'recovery_sent', 'payment_confirmed',
        'billing_healthy', 'billing_attention', 'subscription_ended',
        'checkout_recovered', 'trainingpeaks_connected', 'context_sealed',
        'plan_approved', 'onboarding_delivered', 'active')
    counts = {
        stage: sum(1 for item in projections if item['stages'][stage])
        for stage in stage_names
    }
    applications = counts['applications']
    conversion = {
        stage: round((counts[stage] / applications) * 100, 1)
        if applications else 0.0
        for stage in stage_names if stage != 'applications'
    }
    abandoned = sum(
        1 for item in projections
        if item['stages']['checkout_expired'] and
        not item['stages']['payment_confirmed'])
    return {
        'stage_counts': counts,
        'conversion_percent_from_application': conversion,
        'abandoned_checkout_cases': abandoned,
        'median_hours': {
            'application_to_payment': _median([
                item['application_to_payment_hours'] for item in projections
                if item['application_to_payment_hours'] is not None]),
            'application_to_active': _median([
                item['application_to_active_hours'] for item in projections
                if item['application_to_active_hours'] is not None]),
        },
    }


_COACHING_ONBOARDING_REMINDER_MILESTONES = (
    (0, 'welcome_setup_check',
     'Confirm welcome delivery, TrainingPeaks connection, and kickoff booking'),
    (2, 'early_friction_check',
     'Review setup blockers, comments, schedule access, and unanswered questions'),
    (7, 'first_week_review',
     'Review the first week of execution and propose any onboarding adjustment'),
    (14, 'adaptation_check',
     'Review adherence, recovery, communication fit, and emerging constraints'),
    (28, 'ramp_completion_review',
     'Complete the first-30-day review before marking the ramp complete'),
)


def _suggest_coaching_onboarding_reminders(case: dict,
                                           now: datetime | None = None) -> list[dict]:
    """Create approval-only reminders; never send athlete communications."""
    payment = (case.get('receipts') or {}).get('stripe_payment') or {}
    onboarding = case.get('onboarding_materials') or {}
    anchor = _parse_utc(
        onboarding.get('delivered_at') or payment.get('confirmed_at'))
    if not anchor:
        return []
    now = now or datetime.now(timezone.utc)
    existing = {
        str(item.get('milestone') or '')
        for item in (case.get('onboarding_reminders') or [])
    }
    created = []
    for day, milestone, action in _COACHING_ONBOARDING_REMINDER_MILESTONES:
        due_at = anchor + timedelta(days=day)
        if milestone in existing or due_at > now:
            continue
        reminder = {
            'schema': 'coaching_onboarding_reminder/v1',
            'reminder_id': hashlib.sha256(
                f"{case.get('case_id')}\0{milestone}".encode()).hexdigest()[:24],
            'milestone': milestone,
            'day': day,
            'due_at': due_at.isoformat(),
            'suggested_at': now.isoformat(),
            'status': 'suggested',
            'action': action,
            'channel': 'coach_review_queue',
            'requires_coach_approval': True,
            'automatic_send': False,
        }
        case.setdefault('onboarding_reminders', []).append(reminder)
        _record_coaching_event(
            case, 'coaching_reminder_suggested', reminder['reminder_id'],
            details={'status': milestone}, occurred_at=now.isoformat())
        created.append(reminder)
    return created


@app.route('/api/coaching-funnel-report', methods=['GET'])
@limiter.limit("10/minute")
def coaching_funnel_report():
    """Private, aggregate onboarding funnel without names, emails, or case IDs."""
    supplied = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not supplied or not hmac.compare_digest(supplied, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        days = int(request.args.get('days', '30'))
    except (TypeError, ValueError):
        return jsonify({'error': 'days must be an integer from 1 to 730'}), 400
    if not 1 <= days <= 730:
        return jsonify({'error': 'days must be an integer from 1 to 730'}), 400

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    root = Path(DATA_DIR) / 'coaching_intakes'
    projections = []
    if root.exists():
        for path in root.glob('*.json'):
            try:
                case = json.loads(path.read_text())
                projected = _coaching_funnel_projection(case)
                if projected['submitted_at'] and projected['submitted_at'] >= cutoff:
                    projections.append(projected)
            except (OSError, ValueError, TypeError):
                logger.warning(f'Could not project coaching funnel case {path.name}')

    groups = {}
    for item in projections:
        key = f"{item['brand']}:{item['tier']}"
        groups.setdefault(key, []).append(item)
    return jsonify({
        'schema': 'coaching_funnel_report/v1',
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'window_days': days,
        'all_brands': _aggregate_coaching_funnel(projections),
        'by_brand_and_tier': {
            key: _aggregate_coaching_funnel(items)
            for key, items in sorted(groups.items())
        },
        'privacy': 'aggregate_only_no_athlete_pii_or_case_ids',
    })


@app.route('/api/cron/coaching-onboarding-reminders', methods=['POST'])
@limiter.limit("5/minute")
def cron_coaching_onboarding_reminders():
    """Populate a deduplicated coach-review queue without sending anything."""
    supplied = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not supplied or not hmac.compare_digest(supplied, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401
    created = []
    scanned = 0
    for case in _iter_coaching_intakes() or ():
        scanned += 1
        suggestions = _suggest_coaching_onboarding_reminders(case)
        if not suggestions:
            continue
        _write_coaching_intake(case)
        created.extend({
            'case_id': case.get('case_id'),
            'milestone': item['milestone'],
            'due_at': item['due_at'],
        } for item in suggestions)
    logger.info(json.dumps({
        'message': 'coaching_onboarding_reminders',
        'scanned': scanned,
        'suggested': len(created),
        'automatic_send': False,
    }, sort_keys=True))
    return jsonify({
        'schema': 'coaching_onboarding_reminder_run/v1',
        'status': 'ok',
        'scanned': scanned,
        'suggested': len(created),
        'reminders': created,
        'automatic_send': False,
    })


def _stripe_list_items(result) -> list:
    """Normalize Stripe SDK ListObject and dict-shaped test responses.

    Stripe's ListObject exposes provider results on ``.data`` while also
    implementing a mapping-like ``get`` that is not reliable across SDK
    generations. Prefer the documented attribute and keep dict compatibility
    for tests and older adapters.
    """
    data = getattr(result, 'data', None)
    if data is not None:
        return list(data)
    if isinstance(result, dict):
        return list(result.get('data') or [])
    return []


def _coaching_canary_result() -> tuple[dict, int]:
    """Run read-only provider/config checks plus a disposable volume probe."""
    checked_at = datetime.now(timezone.utc).isoformat()
    checks = []

    def add(name, passed, detail=''):
        checks.append({
            'name': name, 'passed': bool(passed),
            'detail': str(detail)[:240],
        })

    add('coaching_intake_secret_configured', bool(COACHING_INTAKE_SECRET))
    add('stripe_secret_configured', bool(STRIPE_SECRET_KEY))
    add('stripe_webhook_secret_configured', bool(STRIPE_WEBHOOK_SECRET))
    add('resend_delivery_configured', bool(RESEND_API_KEY and NOTIFICATION_EMAIL))
    add('coaching_booking_url_verified_shape',
        COACHING_BOOKING_URL.startswith('https://'))

    synthetic_adult_case = {
        'case_id': '00000000-0000-4000-8000-000000000000',
        'athlete': {'is_minor': False},
        'questionnaire': {'age': '40'},
    }
    esign_readiness = _coaching_esign_readiness(synthetic_adult_case)
    add('signwell_esign_configuration',
        esign_readiness.get('provider') == 'signwell' and
        esign_readiness.get('status') == 'ready',
        ', '.join(esign_readiness.get('blockers') or []))
    if esign_readiness.get('provider') == 'signwell' and SIGNWELL_API_KEY:
        try:
            signwell = SignWellClient(SIGNWELL_API_KEY)
            account = signwell.get_account()
            add('signwell_account_readback', isinstance(account, dict) and bool(account),
                'authenticated readback succeeded' if account else 'empty account response')
            if SIGNWELL_SYNTHETIC_TEMPLATE_ID:
                template = signwell.get_template(SIGNWELL_SYNTHETIC_TEMPLATE_ID)
                field_types = {
                    str(field.get('type') or '').lower()
                    for group in (template.get('fields') or [])
                    for field in group
                    if isinstance(field, dict)
                }
                metadata = template.get('metadata') or {}
                synthetic_ok = (
                    'SYNTHETIC TEST ONLY' in str(template.get('name') or '') and
                    str(metadata.get('legal_effect') or '') == 'none' and
                    {'signature', 'date'}.issubset(field_types)
                )
                add('signwell_synthetic_template_readback', synthetic_ok,
                    'identity, no-legal-effect metadata, and fields verified'
                    if synthetic_ok else 'synthetic template contract mismatch')
            else:
                add('signwell_synthetic_template_readback', False,
                    'SIGNWELL_SYNTHETIC_TEMPLATE_ID is not configured')
        except SignWellError as exc:
            add('signwell_account_readback', False,
                f'provider readback failed: {type(exc).__name__}')
            add('signwell_synthetic_template_readback', False,
                f'provider readback failed: {type(exc).__name__}')
    else:
        add('signwell_account_readback', False,
            'not attempted because SignWell provider/API key is unavailable')
        add('signwell_synthetic_template_readback', False,
            'not attempted because SignWell provider/API key is unavailable')

    for brand, cfg in sorted(BRANDS.items()):
        coaching = cfg.get('coaching') or {}
        tiers = coaching.get('tiers') or {}
        add(f'{brand}_core_offer_registry',
            coaching.get('enabled') is True and
            set(tiers) == {'min', 'mid', 'max'} and
            coaching.get('billing_period_days') == 28 and
            coaching.get('setup_fee_waiver_mode') == 'case_by_case_private')
        add(f'{brand}_server_analytics_configured', bool(
            cfg.get('ga4_measurement_id') and cfg.get('ga4_mp_api_secret')))
        for tier in ('min', 'mid', 'max'):
            ok, reason = _verify_coaching_checkout_contract(brand, tier)
            add(f'{brand}_{tier}_stripe_contract', ok, reason)
        waiver_ok, waiver_reason = _verify_coaching_checkout_contract(
            brand, 'mid', setup_fee_waived=True)
        add(f'{brand}_private_setup_waiver_contract',
            waiver_ok, waiver_reason)

    try:
        endpoints_obj = stripe.WebhookEndpoint.list(limit=100)
        endpoints = _stripe_list_items(endpoints_obj)
        required = {
            'checkout.session.completed', 'checkout.session.expired',
            'invoice.paid', 'invoice.payment_failed',
            'invoice.payment_action_required',
            'customer.subscription.updated', 'customer.subscription.deleted',
            'customer.subscription.paused', 'customer.subscription.resumed',
        }
        webhook_ok = False
        for endpoint in endpoints:
            data = (endpoint._to_dict_recursive()
                    if hasattr(endpoint, '_to_dict_recursive') else dict(endpoint))
            enabled = set(data.get('enabled_events') or [])
            if (data.get('status') == 'enabled' and
                    str(data.get('url') or '').rstrip('/').endswith('/webhook/stripe') and
                    ('*' in enabled or required.issubset(enabled))):
                webhook_ok = True
                break
        add('stripe_webhook_events_enabled', webhook_ok)
    except (AttributeError, TypeError, stripe.error.StripeError) as exc:
        add('stripe_webhook_events_enabled', False,
            f'provider readback failed: {type(exc).__name__}')

    try:
        configs_obj = stripe.billing_portal.Configuration.list(limit=100)
        configs = _stripe_list_items(configs_obj)
        portal_ok = False
        for config in configs:
            data = (config._to_dict_recursive()
                    if hasattr(config, '_to_dict_recursive') else dict(config))
            features = data.get('features') or {}
            if (data.get('active') is True and
                    (features.get('subscription_cancel') or {}).get('enabled') is True and
                    (features.get('payment_method_update') or {}).get('enabled') is True):
                portal_ok = True
                break
        add('stripe_customer_portal_configured', portal_ok)
    except (AttributeError, TypeError, stripe.error.StripeError) as exc:
        add('stripe_customer_portal_configured', False,
            f'provider readback failed: {type(exc).__name__}')

    receipt_root = Path(DATA_DIR) / '.canary' / 'coaching'
    probe_path = receipt_root / f'.probe-{uuid.uuid4()}.json'
    try:
        receipt_root.mkdir(parents=True, exist_ok=True)
        probe_payload = {'schema': 'coaching_volume_probe/v1', 'checked_at': checked_at}
        probe_path.write_text(json.dumps(probe_payload))
        add('persistent_volume_round_trip',
            json.loads(probe_path.read_text()) == probe_payload)
    except (OSError, ValueError) as exc:
        add('persistent_volume_round_trip', False, type(exc).__name__)
    finally:
        try:
            probe_path.unlink(missing_ok=True)
        except OSError:
            pass

    passed = all(check['passed'] for check in checks)
    result = {
        'schema': 'coaching_onboarding_canary/v1',
        'checked_at': checked_at,
        'status': 'ok' if passed else 'failed',
        'summary': {
            'passed': sum(1 for check in checks if check['passed']),
            'failed': sum(1 for check in checks if not check['passed']),
        },
        'checks': checks,
        'side_effects': (
            'no case, e-sign request, checkout, charge, email, or '
            'TrainingPeaks write'),
    }
    try:
        receipt_path = receipt_root / (
            checked_at.replace(':', '').replace('+00:00', 'Z') + '.json')
        receipt_path.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    except OSError:
        result['status'] = 'failed'
        result['summary']['failed'] += 1
        result['checks'].append({
            'name': 'durable_canary_receipt', 'passed': False,
            'detail': 'Could not persist the canary receipt',
        })
    return result, (200 if result['status'] == 'ok' else 503)


@app.route('/api/coaching-canary', methods=['POST'])
@limiter.limit("10/minute")
def coaching_canary():
    """Synthetic edge-to-backend canary authenticated by the intake Worker."""
    supplied = request.headers.get('X-Coaching-Intake-Secret', '')
    if not COACHING_INTAKE_SECRET:
        return jsonify({'error': 'Coaching intake is not configured'}), 503
    if not supplied or not hmac.compare_digest(supplied, COACHING_INTAKE_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401
    result, status = _coaching_canary_result()
    logger_method = logger.info if status == 200 else logger.error
    logger_method(json.dumps({
        'message': 'coaching_onboarding_canary',
        'status': result['status'],
        'summary': result['summary'],
    }, sort_keys=True))
    return jsonify(result), status


@app.route('/api/intel-stats', methods=['GET'])
@limiter.limit("10/minute")
def intel_stats():
    """Windowed commerce ground truth for the Morning Intel report.

    The report previously inferred orders from GA4 events; this endpoint
    exposes the actual ledger (/data/.logs) — orders WITH fulfillment
    outcomes, cart-recovery sends, and questionnaire starts — so a paying
    customer whose pipeline failed is never invisible.

    Secured by the same X-Cron-Secret as the cron endpoints.
    """
    secret = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not hmac.compare_digest(secret, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401

    if 'limit' in request.args:
        return jsonify({'error': 'limit is not supported; use hours'}), 400
    raw_hours = request.args.get('hours', '24')
    try:
        hours = int(raw_hours)
    except (TypeError, ValueError):
        return jsonify({'error': 'hours must be an integer from 1 to 720'}), 400
    if hours < 1 or hours > 720:
        return jsonify({'error': 'hours must be an integer from 1 to 720'}), 400

    from datetime import timedelta as _td
    now = datetime.now()
    cutoff = (now - _td(hours=hours)).isoformat()
    log_dir = Path(DATA_DIR) / '.logs'
    months = []
    cursor = (now - _td(hours=hours)).replace(day=1)
    final_month = now.strftime('%Y-%m')
    while True:
        months.append(cursor.strftime('%Y-%m'))
        if cursor.strftime('%Y-%m') == final_month:
            break
        cursor = (cursor.replace(day=28) + _td(days=4)).replace(day=1)

    def _monitor(email):
        e = (email or '').lower()
        return (not e or 'monitor' in e or 'healthcheck' in e
                or 'gravelgodcoaching@' in e or 'example.com' in e)

    orders, recoveries = [], []
    for m in months:
        f = log_dir / f'{m}.jsonl'
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            try:
                e = json.loads(line)
            except (ValueError, TypeError):
                continue
            if (e.get('timestamp') or '') < cutoff or _monitor(e.get('email')):
                continue
            if e.get('product_type') == 'cart_recovery' or 'recovery_url_sent' in e:
                recoveries.append({'id': e.get('order_id') or e.get('intake_id') or '',
                                   'timestamp': e.get('timestamp'),
                                   'email': e.get('email'),
                                   'product': e.get('original_product')})
            else:
                orders.append({'id': e.get('order_id') or e.get('intake_id') or '',
                               'timestamp': e.get('timestamp'),
                               'product_type': e.get('product_type'),
                               'email': e.get('email'),
                               'name': e.get('name'),
                               'success': e.get('success'),
                               'error': (e.get('error') or '')[:200] or None})

    q_starts = 0
    for m in months:
        f = log_dir / f'questionnaire-starts-{m}.jsonl'
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            try:
                e = json.loads(line)
            except (ValueError, TypeError):
                continue
            if (e.get('timestamp') or e.get('ts') or '') >= cutoff and \
                    not _monitor(e.get('email')) and e.get('src') != 'health-check':
                q_starts += 1

    orders.sort(key=lambda item: (item.get('timestamp') or '', item.get('id') or ''))
    recoveries.sort(key=lambda item: (item.get('timestamp') or '', item.get('id') or ''))
    coaching_cutoff = datetime.now(timezone.utc) - _td(hours=hours)
    coaching_projections = []
    for case in _iter_coaching_intakes() or ():
        projected = _coaching_funnel_projection(case)
        if (projected.get('submitted_at') and
                projected['submitted_at'] >= coaching_cutoff):
            coaching_projections.append(projected)
    return jsonify({
        'window_hours': hours,
        'orders': orders,
        'failed_orders': [o for o in orders if o.get('success') is False],
        'recoveries': recoveries,
        'questionnaire_starts': q_starts,
        'coaching_onboarding': _aggregate_coaching_funnel(
            coaching_projections),
    })


@app.route('/api/cron/followup-emails', methods=['POST'])
@limiter.limit("5/minute")
def cron_followup_emails():
    """Daily cron endpoint — send follow-up emails for recent orders.

    Secured by CRON_SECRET header. Call daily from an external scheduler.
    """
    secret = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not hmac.compare_digest(secret, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401

    try:
        stats = process_followup_emails()
        logger.info(f"Follow-up cron complete: {stats}")
        tp_stats = process_touchpoint_emails()
        logger.info(f"Touchpoint cron complete: {tp_stats}")
        consult_stats = process_consult_followups()
        logger.info(f"Consult follow-up cron complete: {consult_stats}")
        return jsonify({'status': 'ok', **stats,
                        'touchpoints': tp_stats,
                        'consult': consult_stats})
    except Exception as e:
        logger.exception(f"Follow-up cron error: {e}")
        return jsonify({'error': 'Internal error'}), 500


@app.route('/api/cron/state-audit', methods=['POST'])
@limiter.limit("5/minute")
def cron_state_audit():
    """Audit the Railway persistent fulfilment root through cron auth."""
    secret = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not hmac.compare_digest(secret, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict) or _has_client_timestamp(data):
        return jsonify({'error': 'JSON body without client timestamps is required'}), 400
    max_age_days = data.get('max_age_days', 3)
    if (not isinstance(max_age_days, int) or isinstance(max_age_days, bool)
            or not 1 <= max_age_days <= 30):
        return jsonify({'error': 'max_age_days must be an integer from 1 to 30'}), 400
    try:
        from tools.audit_fulfillment_states import build_audit_artifact
        artifact = build_audit_artifact(
            Path(DELIVERIES_DIR) / 'orders', max_age_days=max_age_days)
        logger.info(
            'Fulfillment state audit: %s',
            json.dumps(artifact, sort_keys=True, separators=(',', ':')),
        )
        status = 500 if artifact['summary']['critical'] else 200
        return jsonify(artifact), status
    except Exception:
        logger.exception('Fulfillment state audit execution failed')
        return jsonify({'error': 'Internal error'}), 500


@app.route('/api/cron/stripe-reconciliation', methods=['POST'])
@limiter.limit("2/minute")
def cron_stripe_reconciliation():
    """Return a PII-free, read-only Stripe revenue reconciliation receipt."""
    supplied = request.headers.get('X-Cron-Secret', '')
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured'}), 503
    if not supplied or not hmac.compare_digest(supplied, CRON_SECRET):
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'JSON body is required'}), 400
    try:
        start, end = parse_reconciliation_window(
            data.get('start_date'), data.get('end_date'))
        receipt = build_stripe_revenue_receipt(
            stripe, start, end, record_key_secret=CRON_SECRET,
            offer_price_ids={
                'training_plan': tuple(TRAINING_PLAN_PRICE_IDS.values()),
                'coaching': (
                    *COACHING_PRICE_IDS.values(), COACHING_SETUP_FEE_PRICE_ID),
                'consulting': (CONSULTING_PRICE_ID,),
                'consult_addon': (CONSULT_PLAN_ADDON_PRICE_ID,),
            })
    except ProviderRevenueError as exc:
        return jsonify({'error': str(exc)}), 400
    except stripe.error.StripeError as exc:
        logger.error(
            'Stripe reconciliation provider read failed: %s',
            type(exc).__name__)
        return jsonify({'error': 'Payment provider read failed'}), 502
    except Exception:
        logger.exception('Stripe reconciliation execution failed')
        return jsonify({'error': 'Internal error'}), 500
    summary = receipt.get('controls', {})
    logger.info(json.dumps({
        'message': 'stripe_reconciliation_complete',
        'period': receipt['period'],
        'successful_charges': summary.get('successful_charges', {}),
        'succeeded_refunds': summary.get('succeeded_refunds', {}),
        'paid_payouts': summary.get('paid_payouts', {}),
    }, sort_keys=True))
    return jsonify(receipt)


# =============================================================================
# ENGINE — deterministic block generation for Endure Labs (Convergence Phase 1)
# =============================================================================

@app.route('/api/training-plan-preview', methods=['POST', 'OPTIONS'])
@limiter.limit("20/minute")
def training_plan_preview():
    """Public, sanitized TrainingPeaks-calendar preview for the three sites."""
    if request.method == 'OPTIONS':
        return '', 204
    if not PUBLIC_PLAN_PREVIEW_ENABLED:
        return jsonify({
            'error': 'preview_unavailable',
            'message': 'The live plan preview is being updated.',
        }), 503
    if (request.content_length is not None
            and request.content_length > PUBLIC_PLAN_PREVIEW_MAX_BYTES):
        return jsonify({'error': 'invalid_request',
                        'message': 'Request body is too large.'}), 413

    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({'error': 'invalid_request',
                        'message': 'Request body must be JSON.'}), 400

    try:
        from engine_preview_provider import (
            engine_version as preview_engine_version,
            generate_preview_source,
            voice_version as preview_voice_version,
        )
        result, cache_hit = build_public_preview(
            payload,
            provider=generate_preview_source,
            engine_version=preview_engine_version(),
            voice_version=preview_voice_version(),
        )
    except PreviewContractError as exc:
        return jsonify({'error': 'invalid_request', 'message': str(exc)}), 400
    except PreviewProviderUnavailable:
        logger.warning('Public plan preview provider is unavailable')
        return jsonify({
            'error': 'preview_unavailable',
            'message': 'The live plan preview is being updated.',
        }), 503
    except Exception:
        logger.exception('Public plan preview generation failed')
        return jsonify({'error': 'preview_failed',
                        'message': 'Preview generation failed.'}), 500

    response = jsonify(result)
    response.headers['Cache-Control'] = 'public, max-age=300, s-maxage=900'
    response.headers['X-Preview-Cache'] = 'HIT' if cache_hit else 'MISS'
    response.headers['Vary'] = 'Origin'
    return response

@app.route('/engine/block', methods=['POST'])
@limiter.limit("60/minute")
def engine_block():
    """POST /engine/block — deterministic training-block generation.

    Exposes the block-builder core so Endure Labs generates blocks in <1s
    instead of a 30s LLM call. Contract is FROZEN (see engine_adapter.py):
    - Auth: X-Engine-Secret vs ENGINE_SHARED_SECRET (503 unset, 401 mismatch)
    - 400 invalid request (with field errors)
    - 422 compliance gate CRITICAL failure
    - 500 unexpected

    The {phase, weeks, start_date} for each block comes from POST
    /engine/season (see engine_season.py): it types a whole season off
    calculate_plan_dates and emits a `blocks` array whose entries feed
    straight into this endpoint.

    ADDITIVE July 2026: each response week carries a structured `strength`
    object (sessions + avoidSameDayAs) alongside the unchanged
    `strengthProtocol` prose string — see engine_adapter._structured_strength
    for the shape. No existing fields or request validation changed.

    ADDITIVE July 2026 (calendar truth): optional `block.week_descriptors`
    lets the caller pass /engine/season week shapes (types + races) straight
    in — descriptor types override the internal load/recovery rhythm and
    races get the pipeline's B-race mini-taper overlay (race day + openers).
    Omitted → byte-identical to before. See the week-descriptors block
    comment in engine_adapter.py.
    """
    secret = request.headers.get('X-Engine-Secret', '')
    expected = os.environ.get('ENGINE_SHARED_SECRET', '')
    if not expected:
        return jsonify({'error': 'ENGINE_SHARED_SECRET not configured'}), 503
    if not hmac.compare_digest(secret, expected):
        return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({'error': 'invalid_request',
                        'fields': {'body': 'Request body must be JSON'}}), 400

    try:
        from engine_adapter import (
            validate_request as engine_validate,
            generate_block as engine_generate,
            ComplianceFailure,
        )
    except Exception as e:
        logger.exception(f"Engine adapter import failed: {e}")
        return jsonify({'error': 'Internal error'}), 500

    params, field_errors = engine_validate(payload)
    if field_errors:
        return jsonify({'error': 'invalid_request', 'fields': field_errors}), 400

    try:
        result = engine_generate(params)
    except ComplianceFailure as cf:
        logger.warning(
            f"Engine block compliance gate failed: {cf.compliance['violations']}")
        return jsonify({'error': 'compliance_failed',
                        'compliance': cf.compliance}), 422
    except Exception as e:
        logger.exception(f"Engine block generation failed: {e}")
        return jsonify({'error': 'Internal error'}), 500

    logger.info(
        f"Engine block generated: phase={params['phase']} weeks={params['weeks']} "
        f"archetype={params['archetype']} methodology={params['methodology']} "
        f"in {result['engine']['generated_ms']}ms")
    return jsonify(result)


@app.route('/engine/season', methods=['POST'])
@limiter.limit("60/minute")
def engine_season():
    """POST /engine/season — deterministic season/periodization planning.

    Exposes the pipeline's season-planning brain (calculate_plan_dates, the
    SINGLE SOURCE OF TRUTH for phases, recovery weeks, taper, race week, and
    the B/C-race mini-taper overlay). Given an athlete, a race schedule (>=1
    A-race), and a start date, it returns a week-by-week season with phases
    and 2-4-week blocks that feed straight into /engine/block. Contract is
    FROZEN (see engine_season.py):
    - Auth: X-Engine-Secret vs ENGINE_SHARED_SECRET (503 unset, 401 mismatch)
    - 400 invalid request (with field errors)
    - 500 unexpected
    """
    secret = request.headers.get('X-Engine-Secret', '')
    expected = os.environ.get('ENGINE_SHARED_SECRET', '')
    if not expected:
        return jsonify({'error': 'ENGINE_SHARED_SECRET not configured'}), 503
    if not hmac.compare_digest(secret, expected):
        return jsonify({'error': 'Unauthorized'}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({'error': 'invalid_request',
                        'fields': {'body': 'Request body must be JSON'}}), 400

    try:
        from engine_season import (
            validate_request as season_validate,
            generate_season as season_generate,
            SeasonBuildError,
        )
    except Exception as e:
        logger.exception(f"Engine season import failed: {e}")
        return jsonify({'error': 'Internal error'}), 500

    params, field_errors = season_validate(payload)
    if field_errors:
        return jsonify({'error': 'invalid_request', 'fields': field_errors}), 400

    try:
        result = season_generate(params)
    except SeasonBuildError as sbe:
        logger.warning(f"Engine season build rejected: {sbe.fields}")
        return jsonify({'error': 'invalid_request', 'fields': sbe.fields}), 400
    except Exception as e:
        logger.exception(f"Engine season generation failed: {e}")
        return jsonify({'error': 'Internal error'}), 500

    logger.info(
        f"Engine season generated: weeks={len(result['weeks'])} "
        f"blocks={len(result['blocks'])} anchor={params['anchor']['name']} "
        f"methodology={params['methodology']} "
        f"in {result['engine']['generated_ms']}ms")
    return jsonify(result)


# =============================================================================
# STARTUP
# =============================================================================

# Clean up stale intake files on startup
try:
    cleanup_stale_intakes()
except Exception as e:
    logger.warning(f"Intake cleanup on startup failed: {e}")

# Schema-v1 authority is quarantined eagerly. Lazy lookup remains only as a
# crash-recovery safety net for files that appear after process startup.
try:
    _startup_migration = migrate_all_v1_states()
    if any(_startup_migration.values()):
        logger.warning(f"Startup legacy state migration: {_startup_migration}")
except Exception as e:
    logger.error(f"Startup legacy state migration failed closed: {e}")

# Crash durability: retry jobs orphaned by a restart mid-generation.
# Only touches queued/running records older than JOB_STUCK_AFTER_MINUTES,
# so a fresh deploy doesn't double-run anything actively in flight.
try:
    _startup_sweep = sweep_stuck_jobs()
    if _startup_sweep.get('retried') or _startup_sweep.get('failed'):
        logger.warning(f"Startup job sweep: {_startup_sweep}")
except Exception as e:
    logger.error(f"Startup job sweep failed: {e}")

# Fail closed if the image omitted files the offline apply-contract build reads.
# A missing schema has failed every real order since Phase 3 (APPLY_CONTRACT_INVALID).
_packaging_ok, _packaging = _runtime_packaging_ok()
_packaging_paths = {name: str(path) for name, path in _required_runtime_paths().items()}
if _packaging_ok:
    logger.info("Runtime packaging check OK: %s", _packaging_paths)
else:
    _missing = [name for name, present in _packaging.items() if not present]
    logger.critical(
        "RUNTIME PACKAGING GAP: required files missing from the image: %s. "
        "Offline apply-contract will fail closed with APPLY_CONTRACT_INVALID. "
        "paths=%s",
        _missing, _packaging_paths,
    )


# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
