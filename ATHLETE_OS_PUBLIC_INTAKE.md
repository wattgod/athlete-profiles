# Athlete OS Public Intake System

## Overview

This system allows athletes to submit a questionnaire via a web form, which automatically triggers GitHub Actions to generate a personalized training plan.

## Architecture

```
Web Form (HTML)
    ↓
Webhook Service (Zapier/Make.com/Cloudflare Worker)
    ↓
GitHub Actions (repository_dispatch)
    ↓
Validate → Create Profile → Generate Plan → Email Notification
```

## Components

### 1. Web Form
**Location**: `docs/athlete-questionnaire.html`

- Neo-brutalist styling matching survey.html
- Collects all required Athlete OS data
- Includes honeypot field for bot detection
- Client-side validation
- Submits to webhook endpoint

### 2. GitHub Actions Workflow
**Location**: `.github/workflows/athlete-intake.yml`

**Triggers**:
- `repository_dispatch` (from webhook)
- `workflow_dispatch` (manual testing)

**Steps**:
1. Validate submission (email, rate limits, required fields)
2. Create athlete profile from form data
3. Generate plan (validate → derive → build → generate)
4. Commit generated plan
5. Send email notification

### 3. Validation Script
**Location**: `athletes/scripts/validate_submission.py`

**Validations**:
- Email format and disposable provider check
- Rate limiting (max 5 submissions per email per day)
- Required fields present
- Age range (18-99)
- Weekly hours range (0-40)
- At least 3 days available for training

**Rate Limiting**:
- Stores submissions in `.github/rate-limits.json`
- Tracks by email and date
- Auto-cleans entries older than 7 days

### 4. Profile Creator
**Location**: `athletes/scripts/create_profile_from_form.py`

**Converts**:
- Form JSON → profile.yaml format
- Maps form fields to profile structure
- Generates athlete_id from name
- Sets defaults for optional fields

## Setup Instructions

### Step 1: Deploy Web Form

1. Copy `docs/athlete-questionnaire.html` to your web server
2. Update webhook URL in form JavaScript:
   ```javascript
   const webhookUrl = 'https://your-webhook-endpoint.com/athlete-intake';
   ```

### Step 2: Set Up Webhook Service

**Option A: Zapier/Make.com (Recommended for speed)**

1. Create new Zap/Make scenario
2. Trigger: Webhook (POST)
3. Action: GitHub - Repository Dispatch
   - Repository: `your-username/athlete-profiles`
   - Event Type: `athlete-intake`
   - Client Payload:
     ```json
     {
       "email": "{{email}}",
       "athlete_id": "{{athlete_id}}",
       "data": "{{form_data}}"
     }
     ```

**Option B: Cloudflare Worker (More control)**

See `webhook-worker-example.js` for reference implementation.

### Step 3: Configure GitHub Secrets

Add to repository secrets:
- `EMAIL_USERNAME`: Gmail address for notifications
- `EMAIL_PASSWORD`: Gmail app password

### Step 4: Test Workflow

```bash
# Manual test via workflow_dispatch
gh workflow run athlete-intake.yml \
  -f email=test@example.com \
  -f athlete_id=test-athlete
```

## Form Fields Mapping

| Form Field | Profile Field | Notes |
|------------|---------------|-------|
| `name` | `name` | Required |
| `email` | `email` | Required, validated |
| `age` | `health_factors.age` | Optional |
| `primary_goal` | `primary_goal` | Converted to enum |
| `race_name` | `target_race.name` | If specific_race |
| `race_date` | `target_race.date` | If specific_race |
| `years_cycling` | `training_history.years_cycling` | Enum |
| `years_structured` | `training_history.years_structured` | Number |
| `current_ftp` | `fitness_markers.ftp_watts` | Optional |
| `weekly_volume` | `training_history.current_weekly_hours` | Parsed range |
| `{day}_available` | `preferred_days.{day}.availability` | Boolean → enum |
| `{day}_time` | `preferred_days.{day}.time_slots` | Converted to am/pm |
| `{day}_duration` | `preferred_days.{day}.max_duration_min` | Minutes |
| `equipment` | `strength_equipment[]` | Checkboxes → array |
| `current_injuries` | `injury_history.current_injuries[]` | Text → structured |
| `limitations` | `movement_limitations` | Checkboxes → dict |
| `communication_style` | `coaching_style.autonomy_preference` | Mapped |
| `workout_length` | `strength_preferences.preferred_session_length` | Mapped |
| `strength_interest` | `strength_preferences.experience_level` | Mapped |

## Security Features

### 1. Honeypot Field
- Hidden field named `website`
- Bots typically fill all fields
- If filled, submission rejected

### 2. Email Validation
- Format validation
- Disposable provider check
- Rate limiting

### 3. Rate Limiting
- Max 5 submissions per email per day
- Tracked in `.github/rate-limits.json`
- Auto-cleanup of old entries

### 4. Data Validation
- Required fields check
- Age range (18-99)
- Hours range (0-40)
- Schedule validation (min 3 days)

## Testing Checklist

- [ ] Form loads and displays correctly
- [ ] All fields validate on client side
- [ ] Honeypot catches bots
- [ ] Email validation works
- [ ] Rate limiting prevents spam
- [ ] Webhook triggers GitHub Actions
- [ ] Validation script runs successfully
- [ ] Profile created correctly
- [ ] Plan generation completes
- [ ] Email notification sent
- [ ] Error handling works

## Manual Testing

### Test Form Submission

```bash
# Create test profile manually
python3 athletes/scripts/create_profile_from_form.py \
  --athlete-id test-athlete \
  --data '{"name":"Test User","email":"test@example.com","primary_goal":"specific_race","race_name":"Test Race","race_date":"2026-06-07","race_distance":100,"race_distance_unit":"miles","years_cycling":"3-5","years_structured":2,"weekly_volume":"9-12","monday_available":true,"monday_time":"morning","monday_duration":"90","tuesday_available":true,"tuesday_time":"evening","tuesday_duration":"60","wednesday_available":true,"wednesday_time":"morning","wednesday_duration":"45"}'

# Run workflow
python3 athletes/scripts/validate_profile.py test-athlete
python3 athletes/scripts/derive_classifications.py test-athlete
python3 athletes/scripts/build_weekly_structure.py test-athlete
python3 athletes/scripts/generate_athlete_plan.py test-athlete
```

### Test Rate Limiting

```bash
# Submit 6 times quickly
for i in {1..6}; do
  python3 athletes/scripts/validate_submission.py \
    --email "test@example.com" \
    --data '{"name":"Test"}'
done
# 6th should fail
```

## Troubleshooting

### Form submission fails
- Check webhook URL is correct
- Verify webhook service is active
- Check browser console for errors

### GitHub Actions fails
- Check workflow logs
- Verify secrets are set
- Check Python dependencies installed

### Email not sent
- Verify EMAIL_USERNAME and EMAIL_PASSWORD secrets
- Check Gmail app password is correct
- Verify SMTP settings

### Rate limit issues
- Check `.github/rate-limits.json` exists
- Verify date format is correct
- Manually clean old entries if needed

## Future Enhancements

1. **reCAPTCHA Integration** - Add before public launch
2. **Payment Integration** - For paid tier
3. **Enhanced Validation** - More sophisticated injury parsing
4. **Race ID Auto-Detection** - Match race name to known races
5. **Email Templates** - Customizable notification emails
6. **Webhook Retry Logic** - Handle transient failures
7. **Submission Queue** - Handle high volume

## Files Modified/Created

- `docs/athlete-questionnaire.html` - Web form
- `.github/workflows/athlete-intake.yml` - GitHub Actions workflow
- `athletes/scripts/validate_submission.py` - Validation script
- `athletes/scripts/create_profile_from_form.py` - Profile creator
- `.github/rate-limits.json` - Rate limit tracking (git-ignored)

## Success Criteria

✅ Form accessible at public URL  
✅ Submit form → triggers GitHub Actions  
✅ Validation prevents spam/abuse  
✅ Email sent to athlete with plan access link  
✅ Process completes in <5 minutes from submission  

---

**Status**: Ready for testing. Deploy webhook service and test end-to-end workflow.

