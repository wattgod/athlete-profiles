# Athlete Intake Data Flow Documentation

## Overview
This document explains what happens to athlete intake data from form submission to final profile creation.

## Data Flow Steps

### 1. Form Submission
- **Location**: `https://wattgod.github.io/athlete-profiles/athlete-questionnaire.html`
- **Action**: Athlete fills out comprehensive intake form
- **Data**: Form data is collected and sent as JSON

### 2. Webhook Processing
- **Endpoint**: `https://nameless-brook-9c7d.gravelgodcoaching.workers.dev`
- **Service**: Cloudflare Worker
- **Actions**:
  - Validates form data (required fields, email format, disposable email check)
  - Generates athlete ID from email
  - Triggers GitHub Actions workflow via `repository_dispatch` event
- **Response**: Returns success message with athlete_id

### 3. GitHub Actions Workflow
- **Workflow File**: `.github/workflows/athlete-intake.yml`
- **Trigger**: `repository_dispatch` event type `athlete-intake`
- **Steps**:

#### 3.1 Extract Payload
- Extracts email, athlete_id, coaching_tier, name from webhook payload
- Saves full form data to `/tmp/form_data.json`

#### 3.2 Create Athlete Directory
- Creates directory: `athletes/{athlete_id}/`
- Copies form data JSON to athlete directory

#### 3.3 Create Profile from Form
- **Script**: `athletes/scripts/create_profile_from_form.py`
- Converts form JSON to `profile.yaml` format
- Maps form fields to profile structure
- Handles race events (A/B/C priority)
- Sets defaults for missing fields

#### 3.4 Validate Profile
- **Script**: `athletes/scripts/validate_profile.py`
- Validates required fields
- Checks data types and formats
- Validates race dates (must be future)
- Returns errors/warnings

#### 3.5 Derive Classifications
- **Script**: `athletes/scripts/derive_classifications.py`
- Calculates training tier (ayahuasca/finisher/compete/podium)
- Determines starting phase
- Calculates plan duration in weeks
- Sets exercise exclusions based on injuries

#### 3.6 Build Weekly Structure
- **Script**: `athletes/scripts/build_weekly_structure.py`
- Creates custom weekly schedule based on availability
- Assigns key training days
- Respects time constraints per day

#### 3.7 Generate Unified Plan
- **Script**: `athletes/scripts/generate_athlete_plan.py`
- Generates training plan using unified system
- Creates ZWO workout files
- Generates training calendar
- Includes strength workouts

#### 3.8 Generate Athlete Guide
- **Script**: `athletes/scripts/generate_athlete_guide.py`
- Creates personalized HTML guide
- Includes nutrition targets
- Training plan overview

#### 3.9 Commit to Repository
- Commits all generated files to `athletes/{athlete_id}/`
- Files include:
  - `profile.yaml` - Full athlete profile
  - `derived.yaml` - Auto-calculated values
  - `form_data.json` - Original form submission
  - `plans/` - Training plans with ZWO files
  - `guide.html` - Personalized guide

### 4. Email Notifications

#### 4.1 Welcome Email to Athlete
- **Trigger**: On successful workflow completion
- **Recipient**: Athlete's email from form
- **Content**:
  - Confirmation of intake received
  - Selected coaching tier
  - **Next Steps**:
    1. Connect to TrainingPeaks Coach Account
    2. Subscribe to coaching package (tier-specific link)
    3. Schedule first meeting (calendar link)
- **Links Included**:
  - TrainingPeaks: https://home.trainingpeaks.com/attachtocoach?sharedKey=2OTEPC6BXNVQU
  - Gravel Min: https://checkout.trainingpeaks.com/product/8a7865f1-c387-43b0-8b03-9efb6a15ebd5
  - Gravel Mid: https://checkout.trainingpeaks.com/product/9ea94d4d-2fea-4d40-af6a-7b9566391c3d
  - Gravel Max: https://checkout.trainingpeaks.com/product/7623fdc6-859d-4676-9567-f196028053a5
  - Calendar: https://calendar.app.google/E282ZtBJAFBXYdYJ6

#### 4.2 Admin Notification
- **Trigger**: On successful workflow completion
- **Recipient**: `gravelgodcoaching@gmail.com`
- **Content**:
  - Athlete name, email, ID
  - Coaching tier
  - Link to profile in repository
  - Link to workflow run

#### 4.3 Failure Notifications
- **To Athlete**: If workflow fails, sends error notification
- **To Admin**: If workflow fails, sends alert with error details

## Data Storage

### Repository Structure
```
athlete-profiles/
└── athletes/
    └── {athlete_id}/
        ├── profile.yaml          # Complete profile
        ├── derived.yaml          # Auto-calculated values
        ├── form_data.json        # Original form submission
        ├── plans/
        │   └── {race_name}/
        │       ├── workouts/     # ZWO files
        │       ├── calendar/     # Training calendar
        │       └── guide.html    # Personalized guide
        └── history/              # Update history
```

## Security & Privacy

- Form data is stored in private GitHub repository
- Email addresses are used for athlete IDs (sanitized)
- All data is committed to version control for tracking
- Webhook validates origin and blocks disposable emails

## Troubleshooting

### Workflow Fails
- Check GitHub Actions logs
- Verify form data has all required fields
- Check validation errors in profile

### Email Not Sent
- Verify EMAIL_USERNAME and EMAIL_PASSWORD secrets are set
- Check Gmail App Password is correct
- Verify 2FA is enabled on Gmail account

### Profile Not Created
- Check athlete directory was created
- Verify scripts have correct permissions
- Check for Python errors in workflow logs

