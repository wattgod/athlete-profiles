# Athlete Questionnaire Analysis & Justification

## Current Questionnaire Questions (in athlete-questionnaire.html)

### SECTION 1: Basic Information
1. **Full Name** - Required
   - **Justification**: Needed to create athlete profile and personalize communication
   
2. **Email Address** - Required
   - **Justification**: Primary contact method, required for plan delivery and notifications
   
3. **Age** - Optional
   - **Justification**: Health factors, recovery capacity estimation, age-appropriate training intensity
   
4. **Primary Goal** - Required
   - **Justification**: Determines plan structure (specific race vs. general fitness), affects phase alignment and volume

### SECTION 2: Training History
5. **Years Cycling** - Optional
   - **Justification**: Experience level affects starting phase (may skip "Learn to Lift" for experienced athletes)
   
6. **Years of Structured Training** - Optional
   - **Justification**: Training background affects tier classification and plan complexity
   
7. **Current FTP (watts)** - Optional
   - **Justification**: Fitness marker for intensity prescription, w/kg calculation if weight provided
   
8. **Typical Weekly Volume (Last 3 Months)** - Optional
   - **Justification**: Current capacity assessment, determines tier (ayahuasca/finisher/compete/podium)

### SECTION 3: Weekly Schedule
9. **Day-by-day availability** (Mon-Sun) - Required
   - **Justification**: Critical for custom weekly structure generation, ensures plan fits actual schedule
   
10. **Preferred time slots** (per day) - Required if available
    - **Justification**: AM/PM preference affects workout placement (e.g., AM strength, PM intervals)
    
11. **Available duration** (per day) - Required if available
    - **Justification**: Max session length constraints, prevents scheduling 3hr rides on 1hr days

### SECTION 4: Equipment Access
12. **Equipment checkboxes** (Smart trainer, Power meter, Gym, etc.) - Optional
    - **Justification**: Filters exercise selection, determines equipment tier (minimal/moderate/full)

### SECTION 5: Injuries & Limitations
13. **Current Injuries** - Optional
    - **Justification**: Auto-generates exercise exclusions, prevents prescribing harmful movements
    
14. **Past Injuries Affecting Movement** - Optional
    - **Justification**: Historical context for movement limitations, informs exercise substitutions
    
15. **Movement Limitations** (checkboxes) - Optional
    - **Justification**: Quick assessment of common limitations (squat depth, shoulder mobility, etc.)

### SECTION 6: Preferences
16. **Communication Style** - Optional
    - **Justification**: Affects guide tone and coaching approach (direct vs. encouraging)
    
17. **Workout Length Preference** - Optional
    - **Justification**: Influences session structure (shorter/intense vs. longer/easier)
    
18. **Strength Training Interest** - Optional
    - **Justification**: Determines strength frequency (not interested = 0x, eager = 2-3x/week)

### SECTION 7: Target Race
19. **Race Name** - Conditional (if specific_race goal)
    - **Justification**: Race-specific customization (Unbound = hip stability, Leadville = climbing power)
    
20. **Race Date** - Conditional (if specific_race goal)
    - **Justification**: Calculates plan weeks (race date - start date), determines phase distribution
    
21. **Race Distance** - Conditional (if specific_race goal)
    - **Justification**: Affects training volume and race-specific workout emphasis

---

## Missing Questions (from your existing questionnaire)

### High Priority - Should Add

1. **Training Goals Beyond Racing** (e.g., PR segments, FTP targets)
   - **Justification**: Personalizes plan beyond race goals, adds motivation context
   - **Where**: Add to SECTION 2 (Training History)

2. **Training Strengths & Weaknesses**
   - **Justification**: Identifies areas to emphasize vs. develop, informs exercise selection
   - **Where**: Add to SECTION 2 (Training History)

3. **Sleep Quality & Hours**
   - **Justification**: Recovery capacity assessment, affects volume recommendations
   - **Where**: Add to SECTION 5 (Health) or new Health section

4. **Work Schedule & Stress Level**
   - **Justification**: Life stress affects recovery, determines realistic training load
   - **Where**: Add to SECTION 3 (Availability) or new Lifestyle section

5. **Time Management Challenges**
   - **Justification**: Affects plan structure (time-crunched vs. high-volume approach)
   - **Where**: Add to SECTION 3 (Availability)

6. **Training Log/Platform Usage**
   - **Justification**: Determines delivery format (TrainingPeaks vs. other platforms)
   - **Where**: Add to SECTION 6 (Preferences) or new Platform section

### Medium Priority - Consider Adding

7. **Weather Limitations**
   - **Justification**: Indoor/outdoor preference, affects workout design
   - **Where**: Add to SECTION 4 (Equipment) or SECTION 3

8. **Group Ride Frequency & Importance**
   - **Justification**: Social training preferences, may affect workout scheduling
   - **Where**: Add to SECTION 6 (Preferences)

9. **Previous Coaching Experience**
   - **Justification**: Sets expectations, identifies what worked/didn't work
   - **Where**: Add to SECTION 6 (Preferences)

10. **Diet/Fueling Strategy**
    - **Justification**: Performance optimization, but lower priority for initial plan
    - **Where**: New section or omit (can be follow-up)

11. **Bike Fit & Pain Issues**
    - **Justification**: Injury prevention, but overlaps with injuries section
    - **Where**: Add to SECTION 5 (Injuries) or merge

### Low Priority - Can Omit or Follow-up

12. **Medications/Supplements**
    - **Justification**: Important for health, but may be sensitive/private
    - **Where**: Optional in Health section or follow-up conversation

13. **Relationships/Personal Commitments**
    - **Justification**: Affects schedule, but already captured in weekly availability
    - **Where**: Optional text field in Availability section

14. **Caffeine/Alcohol Intake**
    - **Justification**: Performance optimization, but not critical for plan generation
    - **Where**: Follow-up or omit

15. **Secondary Races (B-priority)**
    - **Justification**: Useful for periodization, but can be added later
    - **Where**: Add to SECTION 7 (Target Race) as optional field

---

## Recommended Additions

### Must Add (Critical for Personalization)
1. Training goals (beyond racing)
2. Strengths & weaknesses
3. Sleep quality/hours
4. Work schedule/stress
5. Time management challenges
6. Training platform preference

### Should Add (Improves Personalization)
7. Weather limitations
8. Group ride preferences
9. Previous coaching experience
10. Bike fit/pain issues

### Nice to Have (Can Add Later)
11. Diet/fueling
12. Medications
13. Secondary races
14. Personal commitments (detailed)

---

## Question Organization Recommendation

**Current Structure** (7 sections) is good, but could be enhanced:

1. **Basics & Goals** - Keep as is, add training goals
2. **Training History** - Add strengths/weaknesses, racing frequency
3. **Availability & Schedule** - Add work schedule, stress, time management
4. **Equipment & Environment** - Add weather limitations
5. **Health & Limitations** - Add sleep, bike fit/pain
6. **Preferences** - Add platform, group rides, coaching experience
7. **Target Race** - Add secondary races

**Or restructure to 8 sections:**
1. Basics & Goals
2. Training History & Experience
3. Availability & Schedule
4. Equipment & Environment
5. Health & Limitations
6. Lifestyle & Stress
7. Preferences & Communication
8. Target Race(s)

---

## Justification Summary

**Why each current question exists:**
- **Name/Email**: Required for profile creation and delivery
- **Goal**: Determines plan type and structure
- **Training History**: Classifies tier and starting phase
- **Weekly Schedule**: Generates custom weekly structure (core differentiator)
- **Equipment**: Filters exercise selection
- **Injuries**: Auto-excludes harmful exercises
- **Preferences**: Personalizes approach and communication
- **Race Info**: Calculates plan duration and race-specific customization

**Why missing questions matter:**
- **Strengths/Weaknesses**: Better exercise selection and emphasis
- **Sleep/Stress**: More accurate recovery capacity assessment
- **Time Management**: Better plan structure (time-crunched vs. high-volume)
- **Platform**: Correct delivery format
- **Training Goals**: Additional motivation and personalization

The current questionnaire covers the **minimum viable** for plan generation, but adding the high-priority questions would significantly improve personalization quality.

