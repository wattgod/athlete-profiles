# Improvement backlog — 2026-09-06

**Quality 0.47** · avg coach 5.62/10 · contract pass 50% · load 12.88/plan · 8 critical issue types

Ranked recurring issues (frequency × severity). Fix top-down; each fix must keep tests green AND raise the quality score.

### 1. [critical] ×1  (gravel/masters_returner)
> Wrong discipline content — 'Road Race Strategy' and 'Category 5 to Category 1 Pathway' sections appear in the contents and (presumably) the body of a GRAVEL plan. Cat 5–1 is a USA Cycling road racing classification ladder that is entirely irrelevant to a gravel finisher event. This is embarrassing and will confuse or mislead the customer.

### 2. [critical] ×1  (gravel/masters_returner)
> Wrong discipline content — 'Road Skills' section listed in the contents is road-racing framing. For a gravel race in Mexico City the relevant skills are gravel-specific: loose surface cornering, traction management, mixed-terrain pacing, possibly altitude considerations (Mexico City sits at ~2,240 m). Sending road skills content to a gravel athlete undermines credibility.

### 3. [critical] ×1  (gravel/masters_returner)
> Zone Distribution check is flagged FAIL in the preview. The guide prescribes ~65% easy (Z1-2) volume, but the actual per-week workout distribution must be verified against this claim. A FAIL on this automated check means the calendar workouts do not match the stated methodology distribution — the guide text cannot go out contradicting the calendar.

### 4. [critical] ×1  (mtb/weekend_warrior)
> 'Gravel Skills' appears as a named section in the table of contents and presumably in the body — this athlete is racing Iceman Cometh, a mountain bike race, not a gravel event. Gravel-specific cornering and skills content is wrong-discipline material and is embarrassing to send to an MTB racer.

### 5. [critical] ×1  (mtb/weekend_warrior)
> The fueling section references a 2.68-hour estimated race duration (consistent with the JSON), but the guide text does not surface the calculated 59 g/hr carbohydrate target anywhere in the truncated nutrition section — the athlete's personalised fuelling number is missing from the plan they will actually read.

### 6. [critical] ×1  (mtb/ambitious_first_timer)
> Road-specific sections included for an MTB athlete: the guide contains 'Road Skills,' 'Road Race Strategy,' and a 'Category 5 to Category 1 Pathway' section. These are entirely wrong for an MTB Gran Fondo competitor and will confuse or embarrass the customer.

### 7. [critical] ×1  (gravel/time_crunched_parent)
> 'Category 5 to Category 1 Pathway' and 'Road Race Strategy' sections appear in the table of contents for a gravel athlete — these are road-racing constructs (USA Cycling upgrade points, Cat 5-1 categories) that have no relevance to a gravel gran fondo. This is a template bleed-through that will confuse and embarrass.

### 8. [critical] ×1  (gravel/time_crunched_parent)
> Goal field renders as 'Compete' in the 'Your Goals & Blindspots' section but the athlete's stated goal is 'podium' at an A-priority race. The plan should reflect the competitive intent; generic language undercuts the athlete's motivation and signals the system didn't read her questionnaire.

### 9. [major] ×1  (road/masters_returner)
> Zone Distribution check FAILED in preview. The guide text does not visibly correct or explain this — if zone distribution is wrong in the actual calendar workouts (e.g. too much time above Z2 in Base, or insufficient easy volume relative to the stated ~65% easy target), sending the plan as-is embeds a methodology contradiction. This must be resolved in the calendar before sending.

### 10. [major] ×1  (road/masters_returner)
> 'Road Race Strategy' and 'Category 5 to Category 1 Pathway' are listed as guide sections. These are entirely inappropriate for a 57-year-old masters athlete whose goal is to finish a gran fondo — a mass-participation timed event, not a licensed road race. Category racing progression content could actively mislead or confuse this athlete.

### 11. [major] ×1  (gravel/masters_returner)
> Weight listed as '158 lbs / 71.7 kg' appears in the profile card, but the athlete's weight does not exist in the provided JSON — it reads as a hardcoded or hallucinated value. If this was pulled from the questionnaire it should be verifiable; if not, using a made-up weight is a data-integrity failure that could also invalidate the post-ride protein/carb recovery calculations.

### 12. [major] ×1  (gravel/masters_returner)
> Weekly Volume automated check returned WARN but the guide text never surfaces or explains this warning to the athlete. For a masters returner, silently passing a flagged volume concern is a coaching liability — the plan should either adjust the volume or explicitly note the constraint and how it was handled.

### 13. [major] ×1  (gravel/masters_returner)
> The off-day layout is contradictory: the guide states 'Off days: Sunday, Saturday, Wednesday' — that is THREE off days — yet it also says 'Your week has 4 training days, 2 of which are key sessions.' Three off days leaves only 4 riding days, which is internally consistent in count, but listing Saturday AND Sunday as off days for a gravel athlete whose long ride is a cornerstone is unusual and should be confirmed as intentional, not a generation error. More critically, listing Wednesday as an off day mid-week directly conflicts with 'Intervals: Mid-week' in the same at-a-glance box.

### 14. [major] ×1  (gravel/masters_returner)
> Taper Intensity is flagged WARN. The guide tells the athlete 'Volume drops sharply; short, sharp efforts keep the engine awake' in the Taper section, but the system check indicates intensity handling in the taper weeks may not meet the threshold — this needs to be confirmed against the actual calendar before sending.

### 15. [major] ×1  (gravel/masters_returner)
> Weekly Volume is flagged WARN. The guide promises 9 h/week, but the automated check did not pass cleanly. If any week materially undershoots or overshoots the athlete's stated 9-hour target, the guide text that references '9 hours per week' is misleading and could erode trust.

### 16. [major] ×1  (gravel/ambitious_first_timer)
> Zone Distribution check FAILED in preview. The guide describes ~75% Z1-Z2 as the pyramidal distribution but no corrective detail is given in the text. If the underlying calendar weeks violate the pyramidal ratio (too much Z3/Z4), the guide text is misleading about what the athlete will actually experience. This needs calendar-level resolution before sending.

### 17. [major] ×1  (gravel/ambitious_first_timer)
> Taper Intensity flagged WARN in preview. The guide text tells the athlete 'short, sharp efforts keep the engine awake' during taper but gives no concrete guardrails (e.g., no Z4+ > X minutes, keep intensity windows short). Given the warning flag, the taper section as written is too vague to safely override the concern.

### 18. [major] ×1  (mtb/weekend_warrior)
> The FTP test note says 'The test result sets ALL your training zones for the next 6 weeks' — but this is a 9-week plan with one test. A blanket 6-week figure is internally inconsistent and confusing; it should reference the actual plan duration or the period until the next scheduled test.

### 19. [major] ×1  (mtb/weekend_warrior)
> Preview flags 'Weekly Volume: WARN', 'Zone Distribution: WARN', and 'Taper Intensity: WARN' — three unresolved warnings that a coach would need to review before signing off. The guide text makes no acknowledgment or mitigation of these issues, so the plan may be delivering the wrong volume or taper stimulus without the athlete or coach being aware.

### 20. [major] ×1  (mtb/weekend_warrior)
> The 'FTP Test Frequency: WARN' preview flag combined with the guide's vague '6 weeks' language suggests the retest cadence for a 9-week plan has not been resolved — the athlete is left without clear guidance on whether or when to retest.

### 21. [minor] ×2  (mtb/ambitious_first_timer, mtb/weekend_warrior)
> Long ride duration range cited as '1.8-3.1 hours' in the Weekly Structure section — the upper bound of 3.1 hours exceeds the athlete's 7 h/week target meaningfully on a single day and may conflict with the Time-Crunched methodology's volume caps; this should be verified against the actual calendar.

### 22. [major] ×1  (mtb/ambitious_first_timer)
> FTP test note states 'The test result sets ALL your training zones for the next 6 weeks' — but this is a 9-week plan. The figure is inconsistent with the plan length and will undermine athlete trust in the document's accuracy.

### 23. [major] ×1  (mtb/ambitious_first_timer)
> The 'FTP Test Frequency' preview check returned WARN but no corrective action or athlete-facing explanation is visible in the guide. If there is an issue with test scheduling it needs to be resolved before sending, not silently flagged.

### 24. [major] ×1  (gravel/time_crunched_parent)
> Fueling section references 59 g carbs/hour and an estimated race duration of ~4.4 hours, but this number never appears in the truncated guide text and — more importantly — for a 96-mile gravel race with a podium goal for a 150 W FTP rider, ~4.4 hours is optimistic and the hourly carb figure (59 g) is below current evidence-based recommendations (80-90 g/h for efforts >2.5 h). Sending under-fueling guidance to a podium-seeking athlete is a meaningful error.

### 25. [major] ×1  (gravel/time_crunched_parent)
> Taper Intensity flagged WARN by the automated preview check but no visible resolution or explanation appears in the guide. Sending a plan with an unresolved taper-intensity warning risks the athlete peaking wrong for her A race.
