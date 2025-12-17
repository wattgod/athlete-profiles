# Athlete OS Questionnaire - Complete Questions & Logic

## Overview
- **Total Sections:** 16
- **Total Questions:** ~60+
- **Estimated Completion Time:** 25 minutes
- **Form Type:** Comprehensive coaching intake

---

## SECTION 1: BASIC INFORMATION

### 1.1 Full Name
- **Type:** Text input
- **Required:** Yes
- **Logic:** Used to generate `athlete_id` (lowercase, hyphenated)
- **Field Name:** `name`

### 1.2 Email Address
- **Type:** Email input
- **Required:** Yes
- **Logic:** Primary contact, used for notifications and rate limiting
- **Field Name:** `email`

### 1.3 Phone Number
- **Type:** Text input
- **Required:** No
- **Placeholder:** "(555) 123-4567"
- **Field Name:** `phone`

### 1.4 Birthday
- **Type:** Date picker
- **Required:** No
- **Logic:** Age calculated automatically from birthday in `create_profile_from_form.py`
- **Field Name:** `birthday`

---

## SECTION 2: RACING GOALS & SUCCESS METRICS

### 2.1 Do you have racing goals?
- **Type:** Radio buttons (Yes/No)
- **Required:** Yes
- **Options:** `yes`, `no`
- **Logic:** 
  - If `yes` → Shows conditional fields 2.2 and 2.3
  - If `no` → Hides conditional fields
- **Field Name:** `has_racing_goals`

### 2.2 If so, list your race(s) (CONDITIONAL)
- **Type:** Textarea (3 rows)
- **Required:** Yes (only if 2.1 = `yes`)
- **Placeholder:** "e.g., Unbound Gravel 200 (June 7), Mid South (March 15)"
- **Logic:** 
  - Only shown if 2.1 = `yes`
  - Parsed in `create_profile_from_form.py` using `parse_race_list()` function
  - Extracts race names and dates using regex patterns
- **Field Name:** `race_list`

### 2.3 What does success look like in those races? (CONDITIONAL)
- **Type:** Textarea (3 rows)
- **Required:** Yes (only if 2.1 = `yes`)
- **Placeholder:** "e.g., Top 25% finish, Complete without bonking, Podium in age group"
- **Logic:** Only shown if 2.1 = `yes`
- **Field Name:** `success_looks_like`

### 2.4 What holds you back the most from achieving your goals?
- **Type:** Textarea (3 rows)
- **Required:** No
- **Placeholder:** "e.g., Time management, Lack of consistency, Injury concerns"
- **Field Name:** `obstacles`

### 2.5 List any training goals you might have
- **Type:** Textarea (3 rows)
- **Required:** No
- **Placeholder:** "e.g., PR local Strava segment; increase FTP by X watts"
- **Field Name:** `training_goals`

### 2.6 Anything else I should know about your goals?
- **Type:** Textarea (3 rows)
- **Required:** No
- **Field Name:** `goals_anything_else`

### 2.7 I'm training for...
- **Type:** Dropdown select
- **Required:** Yes
- **Options:**
  - `specific_race` - Specific race(s)
  - `general_fitness` - General fitness
  - `base_building` - Base building
  - `off_season` - Off-season maintenance
  - `return_from_injury` - Return from injury
  - `performance_improvement` - Performance improvement
- **Logic:** Converted via `convert_primary_goal()` function
- **Field Name:** `primary_goal`

---

## SECTION 3: TRAINING HISTORY & ASSESSMENT

### 3.1 Summarize your training history
- **Type:** Textarea (4 rows)
- **Required:** Yes
- **Placeholder:** "e.g., 5 years road cycling, 2 years gravel racing, 3 years structured training with coach"
- **Field Name:** `training_summary`

### 3.2 Years Cycling
- **Type:** Dropdown select
- **Required:** No
- **Options:**
  - `0-2` - 0-2 years
  - `3-5` - 3-5 years
  - `6-10` - 6-10 years
  - `10+` - 10+ years
- **Field Name:** `years_cycling`

### 3.3 Years of Structured Training
- **Type:** Number input
- **Required:** No
- **Min:** 0
- **Max:** 50
- **Field Name:** `years_structured`

### 3.4 Rate your level in your primary sport
- **Type:** Rating scale (1-5 radio buttons)
- **Required:** Yes
- **Options:** 1, 2, 3, 4, 5
- **Help Text:** "1 = Beginner, 3 = Intermediate, 5 = Advanced/Elite"
- **Field Name:** `primary_sport_level`

### 3.5 List your strengths
- **Type:** Textarea (3 rows)
- **Required:** No
- **Placeholder:** "e.g., Climbing, Mental toughness, Consistency, Technical skills"
- **Field Name:** `strengths`

### 3.6 List your weaknesses
- **Type:** Textarea (3 rows)
- **Required:** No
- **Placeholder:** "e.g., Sprinting, Time management, Recovery, Technical descents"
- **Field Name:** `weaknesses`

### 3.7 Current FTP (watts) - Optional
- **Type:** Number input
- **Required:** No
- **Min:** 0
- **Logic:** If provided, sets `ftp_date` to current date
- **Field Name:** `current_ftp`

### 3.8 How Many Hours a Week Do You Train (On Average)?
- **Type:** Dropdown select
- **Required:** No
- **Options:**
  - `0-2` - 0-2 hours
  - `3-5` - 3-5 hours
  - `6-8` - 6-8 hours
  - `9-12` - 9-12 hours
  - `13-15` - 13-15 hours
  - `16-20` - 16-20 hours
  - `21+` - 21+ hours
- **Logic:** Parsed via `parse_weekly_volume()` to get min/max hours
- **Field Name:** `weekly_volume`

### 3.9 How many times did you race last year?
- **Type:** Number input
- **Required:** No
- **Min:** 0
- **Field Name:** `races_last_year`

### 3.10 Does Weather Limit Your Training?
- **Type:** Radio buttons (Yes/No)
- **Required:** No
- **Options:** `yes`, `no`
- **Logic:** If `yes` → Shows conditional field 3.11
- **Field Name:** `weather_limits`

### 3.11 If Yes, please describe (CONDITIONAL)
- **Type:** Textarea (2 rows)
- **Required:** No
- **Placeholder:** "e.g., Heat affects performance significantly, avoid training in extreme cold"
- **Logic:** Only shown if 3.10 = `yes`
- **Field Name:** `weather_description`

---

## SECTION 4: STRENGTH & MOBILITY

### 4.1 Do You Strength Train?
- **Type:** Radio buttons (Yes/No)
- **Required:** No
- **Options:** `yes`, `no`
- **Logic:** 
  - If `yes` → Shows conditional field 4.2
  - Affects `strength_background` in profile (beginner if `no`, intermediate if `yes`)
- **Field Name:** `strength_trains`

### 4.2 If yes, describe your routine (CONDITIONAL)
- **Type:** Textarea (3 rows)
- **Required:** No
- **Placeholder:** "e.g., 2x/week gym - squats, deadlifts, core work"
- **Logic:** Only shown if 4.1 = `yes`
- **Field Name:** `strength_routine`

### 4.3 Rate Your Mobility
- **Type:** Rating scale (1-5 radio buttons)
- **Required:** Yes
- **Options:** 1, 2, 3, 4, 5
- **Help Text:** "1 = Very limited, 3 = Average, 5 = Excellent"
- **Field Name:** `mobility_rating`

---

## SECTION 5: TRAINING LOG & DEVICES

### 5.1 Do you keep a training log?
- **Type:** Radio buttons (Yes/No)
- **Required:** No
- **Options:** `yes`, `no`
- **Field Name:** `keeps_log`

### 5.2 What devices do you use?
- **Type:** Checkboxes (multiple selection)
- **Required:** No
- **Options:**
  - `garmin` - Garmin
  - `wahoo` - Wahoo
  - `power_meter` - Power Meter
  - `hr_monitor` - Heart Rate Monitor
  - `smart_trainer` - Smart Trainer
  - `strava` - Strava
  - `trainingpeaks` - TrainingPeaks
  - `other` - Other
- **Logic:** Converted via `convert_devices()` function to list
- **Field Name:** `devices` (array)

---

## SECTION 6: WEEKLY SCHEDULE

### 6.1-6.7 Day-by-Day Availability (Monday through Sunday)
For each day (Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday):

#### 6.X.1 Can train?
- **Type:** Checkbox
- **Required:** No
- **Logic:** 
  - If checked → Enables time and duration fields
  - If unchecked → Disables time and duration fields
- **Field Name:** `{day}_available` (e.g., `monday_available`)

#### 6.X.2 Time
- **Type:** Dropdown select
- **Required:** No (disabled until "Can train" is checked)
- **Options:**
  - `early_morning` - Early morning
  - `morning` - Morning
  - `midday` - Midday
  - `afternoon` - Afternoon
  - `evening` - Evening
  - `flexible` - Flexible
- **Logic:** 
  - Converted to `time_slots` array in profile:
    - `early_morning` or `morning` → `['am']`
    - `afternoon` or `evening` → `['pm']`
    - `flexible` → `['am', 'pm']`
- **Field Name:** `{day}_time` (e.g., `monday_time`)

#### 6.X.3 Duration
- **Type:** Dropdown select
- **Required:** No (disabled until "Can train" is checked)
- **Options:**
  - `30` - 30 min
  - `45` - 45 min
  - `60` - 1 hr
  - `90` - 1.5 hr
  - `120` - 2 hr
  - `150` - 2.5 hr
  - `180` - 3 hr
  - `240` - 4 hr+
- **Logic:** Converted to `max_duration_min` in profile
- **Field Name:** `{day}_duration` (e.g., `monday_duration`)

**Conversion Logic (in `convert_day_availability()`):**
- If not available → `availability: 'unavailable'`, `time_slots: []`, `max_duration_min: 0`, `is_key_day_ok: false`
- If available → `availability: 'available'`, `time_slots` from time field, `max_duration_min` from duration, `is_key_day_ok: true`

---

## SECTION 7: WORK & LIFE BALANCE

### 7.1 Do You Work?
- **Type:** Radio buttons (Yes/No)
- **Required:** No
- **Options:** `yes`, `no`
- **Logic:** If `yes` → Shows conditional fields 7.2 and 7.3
- **Field Name:** `works`

### 7.2 If yes, how many hours per week? (CONDITIONAL)
- **Type:** Number input
- **Required:** No
- **Min:** 0
- **Max:** 80
- **Logic:** Only shown if 7.1 = `yes`
- **Field Name:** `work_hours`

### 7.3 Rate Your Job's Stress Level (CONDITIONAL)
- **Type:** Rating scale (1-5 radio buttons)
- **Required:** No
- **Options:** 1, 2, 3, 4, 5
- **Help Text:** "1 = Low stress, 3 = Moderate, 5 = Very high stress"
- **Logic:** Only shown if 7.1 = `yes`
- **Field Name:** `job_stress`

### 7.4 Preferred Day(s) Off
- **Type:** Checkboxes (multiple selection)
- **Required:** No
- **Options:** Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday
- **Field Name:** `preferred_off_days` (array)

### 7.5 Preferred Long Day
- **Type:** Dropdown select
- **Required:** No
- **Options:** Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday
- **Field Name:** `preferred_long_day`

### 7.6 What is your typical wake time?
- **Type:** Time picker
- **Required:** No
- **Field Name:** `wake_time`

### 7.7 What is your typical bed time?
- **Type:** Time picker
- **Required:** No
- **Field Name:** `bed_time`

### 7.8 How many hours of sleep do you average per night?
- **Type:** Number input
- **Required:** No
- **Min:** 4
- **Max:** 12
- **Step:** 0.5
- **Field Name:** `sleep_hours`

### 7.9 Relationships
- **Type:** Textarea (2 rows)
- **Required:** No
- **Placeholder:** "e.g., Married, 2 kids (ages 3, 5)"
- **Field Name:** `relationships`

### 7.10 Other time commitments we should know about?
- **Type:** Textarea (2 rows)
- **Required:** No
- **Placeholder:** "e.g., Kids' sports 3x/week, Volunteer work, Travel for work"
- **Field Name:** `time_commitments`

### 7.11 Is Time Management a challenge for you?
- **Type:** Radio buttons (Yes/No)
- **Required:** No
- **Options:** `yes`, `no`
- **Field Name:** `time_management_challenge`

---

## SECTION 8: EQUIPMENT ACCESS

### 8.1 Equipment Access
- **Type:** Checkboxes (multiple selection)
- **Required:** No
- **Options:**
  - `smart_trainer` - Smart Trainer
  - `dumb_trainer_pm` - Dumb Trainer + Power Meter
  - `outdoor_pm` - Outdoor Only + Power Meter
  - `no_pm` - No Power Meter
  - `gym_membership` - Gym Membership
  - `home_gym` - Home Gym (DB/KB)
  - `pull_up_bar` - Pull-up Bar
  - `resistance_bands` - Resistance Bands
- **Logic:** 
  - Converted via `convert_equipment()` function
  - Maps to `strength_equipment` array in profile
  - `smart_trainer` → `cycling_equipment.smart_trainer: true`
  - `dumb_trainer_pm` or `outdoor_pm` → `cycling_equipment.power_meter_bike: true`
  - `gym_membership` → `strength_equipment: ['gym_membership']`
  - `home_gym` → `strength_equipment: ['dumbbells']`
  - `pull_up_bar` → `strength_equipment: ['pull_up_bar']`
  - `resistance_bands` → `strength_equipment: ['resistance_bands']`
- **Field Name:** `equipment` (array)

---

## SECTION 9: HEALTH & MEDICATIONS

### 9.1 Please list any medications, drugs, or nutritional supplements
- **Type:** Textarea (3 rows)
- **Required:** No
- **Placeholder:** "e.g., Multivitamin, Fish oil, Prescription medications"
- **Field Name:** `medications`

### 9.2 Check all that apply to you:
- **Type:** Checkboxes (multiple selection)
- **Required:** No
- **Options:**
  - `high_blood_pressure` - High Blood Pressure
  - `diabetes` - Diabetes
  - `asthma` - Asthma
  - `heart_condition` - Heart Condition
  - `arthritis` - Arthritis
  - `none` - None of the above
- **Logic:** 
  - Converted via `convert_health_conditions()` function
  - Removes 'none' if other conditions selected
  - Joins into comma-separated string for `medical_conditions`
- **Field Name:** `health_conditions` (array)

### 9.3 Anything else related to your health we should be aware of?
- **Type:** Textarea (3 rows)
- **Required:** No
- **Placeholder:** "e.g., Exercise-induced asthma, Previous heart surgery"
- **Field Name:** `health_anything_else`

### 9.4 Current Injuries
- **Type:** Textarea (3 rows)
- **Required:** No
- **Placeholder:** "Describe any current injuries..."
- **Logic:** 
  - If provided → `coming_off_injury: true` in profile
  - Converted to structured injury object in `injury_history.current_injuries`
- **Field Name:** `current_injuries`

### 9.5 Past Injuries Affecting Movement
- **Type:** Textarea (3 rows)
- **Required:** No
- **Placeholder:** "Describe past injuries that still affect you..."
- **Logic:** Converted to structured injury object in `injury_history.past_injuries`
- **Field Name:** `past_injuries`

### 9.6 Movement Limitations
- **Type:** Checkboxes (multiple selection)
- **Required:** No
- **Options:**
  - `deep_squat_painful` - Deep squat painful
  - `single_leg_balance` - Single-leg balance difficult
  - `pushups_shoulders` - Push-ups hurt shoulders
  - `hip_mobility` - Hip mobility issues
  - `lower_back` - Lower back sensitivity
- **Logic:** 
  - Converted to `movement_limitations` object:
    - `deep_squat_painful` → `deep_squat: 'painful'`
    - `single_leg_balance` → `single_leg_balance: 'limited'`
    - `pushups_shoulders` → `push_up_position: 'painful'`
    - `hip_mobility` or `lower_back` → `hip_hinge: 'limited'` or `'painful'`
- **Field Name:** `limitations` (array)

---

## SECTION 10: NUTRITION

### 10.1 Do you follow any specific diet styles?
- **Type:** Checkboxes (multiple selection)
- **Required:** No
- **Options:**
  - `none` - No specific diet
  - `vegetarian` - Vegetarian
  - `vegan` - Vegan
  - `keto` - Keto
  - `paleo` - Paleo
  - `mediterranean` - Mediterranean
  - `other` - Other
- **Logic:** Converted via `convert_diet_styles()` function to array
- **Field Name:** `diet_styles` (array)

### 10.2 Rate your fluid intake
- **Type:** Rating scale (1-5 radio buttons)
- **Required:** No
- **Options:** 1, 2, 3, 4, 5
- **Help Text:** "1 = Poor, 3 = Adequate, 5 = Excellent"
- **Field Name:** `fluid_intake`

### 10.3 Caffeine intake
- **Type:** Dropdown select
- **Required:** No
- **Options:**
  - `none` - None
  - `1-2` - 1-2 cups/day
  - `3-4` - 3-4 cups/day
  - `5+` - 5+ cups/day
- **Field Name:** `caffeine_intake`

### 10.4 Alcohol intake
- **Type:** Dropdown select
- **Required:** No
- **Options:**
  - `none` - None
  - `1-2` - 1-2 drinks/week
  - `3-5` - 3-5 drinks/week
  - `daily` - Daily
- **Field Name:** `alcohol_intake`

### 10.5 Any dietary restrictions? Describe.
- **Type:** Textarea (2 rows)
- **Required:** No
- **Placeholder:** "e.g., Gluten-free, Lactose intolerant, Food allergies"
- **Field Name:** `dietary_restrictions`

### 10.6 What's your fueling strategy when you train?
- **Type:** Textarea (3 rows)
- **Required:** No
- **Placeholder:** "e.g., Gels + sports drink for rides >90min, Water only for short rides"
- **Field Name:** `fueling_strategy`

### 10.7 Do you eat something after hard workouts? If yes, what?
- **Type:** Textarea (2 rows)
- **Required:** No
- **Placeholder:** "e.g., Protein shake within 30min, Full meal within 2 hours"
- **Field Name:** `post_workout_fuel`

---

## SECTION 11: BIKE FIT & PAIN

### 11.1 When was your last bike fit?
- **Type:** Text input
- **Required:** No
- **Placeholder:** "e.g., 2023-08-15 or 'Never'"
- **Field Name:** `last_bike_fit`

### 11.2 Do you get pain on the bike?
- **Type:** Radio buttons (Yes/No)
- **Required:** No
- **Options:** `yes`, `no`
- **Logic:** If `yes` → Shows conditional field 11.3
- **Field Name:** `bike_pain`

### 11.3 If yes, describe the pain (CONDITIONAL)
- **Type:** Textarea (3 rows)
- **Required:** No
- **Placeholder:** "e.g., Occasional lower back pain on 3+ hour rides, Knee pain when climbing"
- **Logic:** Only shown if 11.2 = `yes`
- **Field Name:** `pain_description`

---

## SECTION 12: SOCIAL TRAINING

### 12.1 How many times per week do you ride with others?
- **Type:** Number input
- **Required:** No
- **Min:** 0
- **Max:** 7
- **Logic:** If > 0 → `group_rides_available: true` in profile
- **Field Name:** `group_rides_per_week`

### 12.2 How important are group rides to you?
- **Type:** Rating scale (1-5 radio buttons)
- **Required:** No
- **Options:** 1, 2, 3, 4, 5
- **Help Text:** "1 = Not important, 3 = Somewhat important, 5 = Very important"
- **Field Name:** `group_ride_importance`

---

## SECTION 13: COACHING HISTORY

### 13.1 Have you worked with a coach before?
- **Type:** Radio buttons (Yes/No)
- **Required:** No
- **Options:** `yes`, `no`
- **Logic:** If `yes` → Shows conditional field 13.2
- **Field Name:** `previous_coach`

### 13.2 If yes, what did you like or dislike about the experience? (CONDITIONAL)
- **Type:** Textarea (3 rows)
- **Required:** No
- **Placeholder:** "e.g., Liked structured plans, disliked lack of flexibility when life got busy"
- **Logic:** Only shown if 13.1 = `yes`
- **Field Name:** `coach_experience`

---

## SECTION 14: PREFERENCES

### 14.1 Communication Style
- **Type:** Dropdown select
- **Required:** No
- **Options:**
  - `direct` - Direct/no-BS
  - `encouraging` - Encouraging/supportive
  - `data_driven` - Data-driven/analytical
  - `flexible` - Flexible
- **Field Name:** `communication_style`

### 14.2 Workout Length Preference
- **Type:** Dropdown select
- **Required:** No
- **Options:**
  - `shorter` - Shorter/intense
  - `moderate` - Moderate
  - `longer` - Longer/easier
  - `no_preference` - No preference
- **Field Name:** `workout_length`

### 14.3 Strength Training Interest
- **Type:** Dropdown select
- **Required:** No
- **Options:**
  - `not_interested` - Not interested
  - `willing` - Willing if necessary
  - `eager` - Eager to include
- **Logic:** 
  - Affects `strength_sessions_max`:
    - `eager` → 2 sessions/week
    - `willing` → 1 session/week
    - `not_interested` → 0 sessions/week
  - Affects `strength_preferences.experience_level`:
    - `not_interested` → 'beginner'
    - Otherwise → 'intermediate'
- **Field Name:** `strength_interest`

---

## SECTION 15: TARGET RACE

### 15.1 Race Name
- **Type:** Text input
- **Required:** No
- **Placeholder:** "e.g., Unbound Gravel 200"
- **Logic:** 
  - Used if `race_list` (2.2) is empty
  - Combined with 15.2 and 15.3 to create `target_race` object
- **Field Name:** `race_name`

### 15.2 Race Date
- **Type:** Date picker
- **Required:** No
- **Logic:** Used to calculate plan weeks (race date - start date)
- **Field Name:** `race_date`

### 15.3 Race Distance
- **Type:** Number input + Unit dropdown
- **Required:** No
- **Number Min:** 0
- **Unit Options:** Miles, Km
- **Logic:** 
  - If unit is `miles` → Used directly
  - If unit is `km` → Converted to miles (km * 0.621371)
- **Field Names:** `race_distance`, `race_distance_unit`

### 15.4 Enter your 2-4 'B' priority Events
- **Type:** Textarea (3 rows)
- **Required:** No
- **Placeholder:** "e.g., Local gravel series (4 races), Century ride in fall"
- **Logic:** Parsed via `parse_b_priority_events()` function
- **Field Name:** `b_priority_events`

---

## SECTION 16: PERSONAL CONTEXT

### 16.1 The most important people in my life are...
- **Type:** Textarea (2 rows)
- **Required:** No
- **Placeholder:** "e.g., Wife Sarah, kids Emma and Jack"
- **Field Name:** `important_people`

### 16.2 Anything else you want to tell me before we get rolling?
- **Type:** Textarea (3 rows)
- **Required:** No
- **Field Name:** `anything_else`

### 16.3 Anything else I should know about your life that will/could affect your training?
- **Type:** Textarea (3 rows)
- **Required:** No
- **Placeholder:** "e.g., Work travel 1 week/month - need portable training options"
- **Field Name:** `life_affecting_training`

---

## CONDITIONAL FIELD LOGIC SUMMARY

### Fields that show/hide based on other answers:

1. **2.2 & 2.3** (Race list & Success metrics)
   - Show if: 2.1 (`has_racing_goals`) = `yes`
   - Hide if: 2.1 = `no`

2. **3.11** (Weather description)
   - Show if: 3.10 (`weather_limits`) = `yes`
   - Hide if: 3.10 = `no`

3. **4.2** (Strength routine)
   - Show if: 4.1 (`strength_trains`) = `yes`
   - Hide if: 4.1 = `no`

4. **7.2 & 7.3** (Work hours & Job stress)
   - Show if: 7.1 (`works`) = `yes`
   - Hide if: 7.1 = `no`

5. **11.3** (Pain description)
   - Show if: 11.2 (`bike_pain`) = `yes`
   - Hide if: 11.2 = `no`

6. **13.2** (Coach experience)
   - Show if: 13.1 (`previous_coach`) = `yes`
   - Hide if: 13.1 = `no`

### Fields that enable/disable based on other answers:

1. **6.X.2 & 6.X.3** (Time & Duration for each day)
   - Enabled if: 6.X.1 (`{day}_available`) checkbox is checked
   - Disabled if: 6.X.1 checkbox is unchecked

---

## DATA CONVERSION LOGIC

### Key Conversion Functions:

1. **`generate_athlete_id(name)`**
   - Converts name to lowercase, hyphenated ID
   - Removes special characters
   - Example: "John Doe" → "john-doe"

2. **`convert_primary_goal(form_goal)`**
   - Maps form dropdown values to profile format
   - Example: "Specific race(s)" → "specific_race"

3. **`parse_weekly_volume(volume)`**
   - Parses volume string to min/max hours tuple
   - Example: "9-12" → (9, 12)
   - Example: "21+" → (21, 40)

4. **`convert_day_availability(form_data, day)`**
   - Converts day availability to profile structure
   - Maps time to `time_slots` array
   - Maps duration to `max_duration_min`
   - Sets `availability` and `is_key_day_ok`

5. **`convert_equipment(form_data)`**
   - Maps equipment checkboxes to `strength_equipment` array
   - Also sets `cycling_equipment` boolean fields

6. **`convert_devices(form_data)`**
   - Converts device checkboxes to list
   - Used for `devices.devices` array

7. **`convert_health_conditions(form_data)`**
   - Converts health condition checkboxes to list
   - Removes 'none' if other conditions selected
   - Joins to comma-separated string

8. **`parse_race_list(race_list)`**
   - Parses race list textarea into structured format
   - Extracts race names and dates using regex
   - Returns list of race objects

9. **`parse_b_priority_events(b_events)`**
   - Parses B-priority events textarea
   - Returns list of event objects with priority 'B'

---

## PROFILE.YAML MAPPING

All form fields are mapped to the `profile.yaml` structure in `create_profile_from_form.py`. Key mappings:

- **Basic Info** → `name`, `email`, `phone`, `birthday`, `athlete_id`
- **Racing** → `racing` object (has_goals, race_list, success_metrics, etc.)
- **Training History** → `training_history` object
- **Weekly Schedule** → `preferred_days` object (7 days with availability, time_slots, max_duration_min)
- **Equipment** → `cycling_equipment` and `strength_equipment`
- **Health** → `injury_history`, `movement_limitations`, `health_factors`
- **Work/Life** → `work`, `life_balance` objects
- **Nutrition** → `nutrition` object
- **Bike** → `bike` object
- **Social** → `social` object
- **Coaching** → `coaching` object
- **Personal** → `personal` object
- **Preferences** → `coaching_style`, `methodology_preferences`, `workout_preferences`, `strength_preferences`

---

## FORM FEATURES

### Progress Tracking
- Real-time progress bar showing completion percentage
- Updates as user fills required fields
- Progress text changes based on completion level

### Save/Resume
- Save button stores form data to localStorage
- Resume button restores saved data
- Automatically detects if saved data exists

### Section Collapse
- Click section header to expand/collapse
- All sections start expanded
- Visual indicator (+/-) shows state

### Validation
- Required fields marked with red asterisk
- HTML5 validation on submit
- Honeypot field for bot detection

### Conditional Fields
- Show/hide based on radio button selections
- Smooth transitions
- Required fields in conditional sections only required when visible

