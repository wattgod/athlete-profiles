#!/usr/bin/env python3
"""
Validate Athlete Submission
Validates form data before creating athlete profile.
"""

import json
import sys
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List

# Disposable email providers (common ones)
DISPOSABLE_EMAIL_PROVIDERS = [
    '10minutemail.com',
    'guerrillamail.com',
    'tempmail.com',
    'mailinator.com',
    'throwaway.email',
    'getnada.com',
    'mohmal.com',
    'fakeinbox.com',
    'trashmail.com',
    'maildrop.cc'
]

# Rate limit file
RATE_LIMIT_FILE = Path('.github/rate-limits.json')


def is_disposable_email(email: str) -> bool:
    """Check if email is from disposable provider."""
    domain = email.split('@')[1].lower() if '@' in email else ''
    return domain in DISPOSABLE_EMAIL_PROVIDERS


def check_rate_limit(email: str, max_per_day: int = 5) -> bool:
    """Check if email has exceeded rate limit."""
    if not RATE_LIMIT_FILE.exists():
        return True  # No limit file, allow
    
    try:
        with open(RATE_LIMIT_FILE, 'r') as f:
            rate_limits = json.load(f)
    except:
        return True  # Error reading, allow
    
    today = datetime.now().strftime('%Y-%m-%d')
    email_key = email.lower()
    
    if email_key not in rate_limits:
        return True  # New email, allow
    
    submissions = rate_limits[email_key].get(today, [])
    
    if len(submissions) >= max_per_day:
        return False  # Rate limit exceeded
    
    return True  # Under limit


def record_submission(email: str):
    """Record submission for rate limiting."""
    RATE_LIMIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    if RATE_LIMIT_FILE.exists():
        with open(RATE_LIMIT_FILE, 'r') as f:
            rate_limits = json.load(f)
    else:
        rate_limits = {}
    
    today = datetime.now().strftime('%Y-%m-%d')
    email_key = email.lower()
    
    if email_key not in rate_limits:
        rate_limits[email_key] = {}
    
    if today not in rate_limits[email_key]:
        rate_limits[email_key][today] = []
    
    rate_limits[email_key][today].append(datetime.now().isoformat())
    
    # Clean up old entries (older than 7 days)
    cutoff_date = datetime.now().replace(day=datetime.now().day - 7)
    for email_addr in list(rate_limits.keys()):
        for date in list(rate_limits[email_addr].keys()):
            if datetime.strptime(date, '%Y-%m-%d') < cutoff_date:
                del rate_limits[email_addr][date]
        if not rate_limits[email_addr]:
            del rate_limits[email_addr]
    
    with open(RATE_LIMIT_FILE, 'w') as f:
        json.dump(rate_limits, f, indent=2)


def validate_email(email: str) -> tuple[bool, str]:
    """Validate email format and provider."""
    if not email:
        return False, "Email is required"
    
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
        return False, "Invalid email format"
    
    if is_disposable_email(email):
        return False, "Disposable email providers are not allowed"
    
    return True, ""


def validate_age(age: int) -> tuple[bool, str]:
    """Validate age."""
    if age and (age < 18 or age > 99):
        return False, "Age must be between 18 and 99"
    return True, ""


def validate_weekly_hours(hours: int) -> tuple[bool, str]:
    """Validate weekly training hours."""
    if hours and (hours < 0 or hours > 40):
        return False, "Weekly hours must be between 0 and 40"
    return True, ""


def validate_required_fields(data: Dict) -> tuple[bool, List[str]]:
    """Validate all required fields are present."""
    errors = []
    
    required = ['name', 'email', 'primary_goal']
    
    for field in required:
        if not data.get(field):
            errors.append(f"Missing required field: {field}")
    
    # If primary_goal is specific_race, require race info
    if data.get('primary_goal') == 'specific_race':
        if not data.get('race_name') or not data.get('race_date'):
            errors.append("Race name and date required for specific race goal")
    
    return len(errors) == 0, errors


def validate_schedule(data: Dict) -> tuple[bool, str]:
    """Validate at least 3 days available for training."""
    days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    available_days = 0
    
    for day in days:
        if data.get(f'{day}_available'):
            available_days += 1
    
    if available_days < 3:
        return False, "At least 3 days per week must be available for training"
    
    return True, ""


def validate_submission(email: str, data_str: str) -> tuple[bool, List[str]]:
    """
    Validate athlete submission.
    
    Returns: (is_valid, errors)
    """
    errors = []
    
    # Parse data
    try:
        if data_str:
            data = json.loads(data_str) if isinstance(data_str, str) else data_str
        else:
            data = {}
    except json.JSONDecodeError:
        return False, ["Invalid JSON data"]
    
    # Validate email
    email_valid, email_error = validate_email(email)
    if not email_valid:
        errors.append(email_error)
    
    # Check rate limit
    if not check_rate_limit(email):
        errors.append("Rate limit exceeded. Maximum 5 submissions per day.")
    
    # Validate required fields
    required_valid, required_errors = validate_required_fields(data)
    if not required_valid:
        errors.extend(required_errors)
    
    # Validate age
    if 'age' in data and data['age']:
        age_valid, age_error = validate_age(int(data['age']))
        if not age_valid:
            errors.append(age_error)
    
    # Validate weekly hours
    if 'weekly_volume' in data:
        # Parse volume range (e.g., "9-12" -> use max)
        volume = data['weekly_volume']
        if isinstance(volume, str) and '-' in volume:
            max_hours = int(volume.split('-')[1]) if volume.split('-')[1] else 40
        elif isinstance(volume, str) and volume.endswith('+'):
            max_hours = 40
        else:
            max_hours = int(volume) if volume else 0
        
        hours_valid, hours_error = validate_weekly_hours(max_hours)
        if not hours_valid:
            errors.append(hours_error)
    
    # Validate schedule
    schedule_valid, schedule_error = validate_schedule(data)
    if not schedule_valid:
        errors.append(schedule_error)
    
    # If all valid, record submission
    if not errors:
        record_submission(email)
    
    return len(errors) == 0, errors


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Validate athlete submission')
    parser.add_argument('--email', required=True, help='Athlete email')
    parser.add_argument('--data', required=True, help='Form data as JSON string')
    
    args = parser.parse_args()
    
    is_valid, errors = validate_submission(args.email, args.data)
    
    if not is_valid:
        print("Validation failed:")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)
    
    print("✅ Validation passed")
    sys.exit(0)


if __name__ == '__main__':
    main()

