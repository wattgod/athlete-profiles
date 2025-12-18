#!/usr/bin/env python3
"""
Generate Athlete Guide

Creates comprehensive personalized training guide for athlete based on their profile and plan.
"""

import yaml
import sys
import json
from pathlib import Path
from datetime import datetime
from typing import Dict


PHASE_DETAILS = {
    "Learn to Lift": {
        "focus": "Movement quality and neuromuscular adaptation",
        "rpe": "5-6/10",
        "rest": "60-90 seconds between sets",
        "reps": "Higher reps (10-15) with lighter loads",
        "tips": [
            "Focus on perfect form over weight",
            "Watch the video demos before each exercise",
            "If something doesn't feel right, regress to an easier variation",
            "You should feel challenged but not crushed"
        ]
    },
    "Lift Heavy Sh*t": {
        "focus": "Maximum strength development",
        "rpe": "7-8/10",
        "rest": "2-3 minutes between heavy sets",
        "reps": "Lower reps (4-8) with heavier loads",
        "tips": [
            "Progressive overload: add weight when you can complete all reps with good form",
            "If form breaks down, reduce weight",
            "This phase builds the strength foundation for power later",
            "Expect some delayed onset muscle soreness (DOMS)"
        ]
    },
    "Lift Fast": {
        "focus": "Power conversion - moving weight quickly",
        "rpe": "7-8/10 effort, but submaximal weight",
        "rest": "2-3 minutes for full neural recovery",
        "reps": "Lower reps (3-6) with focus on SPEED",
        "tips": [
            "Move the weight as fast as possible with control",
            "If you can't move it fast, reduce the weight",
            "Quality > quantity - stop when speed drops",
            "This phase converts your strength into cycling power"
        ]
    },
    "Don't Lose It": {
        "focus": "Maintain adaptations with minimal fatigue",
        "rpe": "5-6/10",
        "rest": "As needed",
        "reps": "Moderate reps (6-10) with moderate weight",
        "tips": [
            "Just enough stimulus to maintain, not build",
            "You should feel refreshed after these sessions",
            "This preserves your strength while prioritizing cycling freshness",
            "Never feel crushed going into race week"
        ]
    }
}


def get_phase_for_week(week: int, plan_weeks: int) -> str:
    """Determine which strength phase a week falls into."""
    if plan_weeks >= 20:
        if week <= 6:
            return "Learn to Lift"
        elif week <= 12:
            return "Lift Heavy Sh*t"
        elif week <= 18:
            return "Lift Fast"
        else:
            return "Don't Lose It"
    elif plan_weeks >= 12:
        if week <= 4:
            return "Learn to Lift"
        elif week <= 8:
            return "Lift Heavy Sh*t"
        elif week <= 10:
            return "Lift Fast"
        else:
            return "Don't Lose It"
    else:
        if week <= 2:
            return "Learn to Lift"
        elif week <= 4:
            return "Lift Heavy Sh*t"
        elif week <= plan_weeks - 1:
            return "Lift Fast"
        else:
            return "Don't Lose It"


def generate_athlete_guide(athlete_id: str, plan_dir: Path) -> Path:
    """
    Generate comprehensive personalized training guide for athlete.
    """
    # Load data
    profile_path = Path(f"athletes/{athlete_id}/profile.yaml")
    derived_path = Path(f"athletes/{athlete_id}/derived.yaml")
    config_path = plan_dir / "plan_config.yaml"
    summary_path = plan_dir / "plan_summary.json"
    
    with open(profile_path, 'r') as f:
        profile = yaml.safe_load(f)
    
    with open(derived_path, 'r') as f:
        derived = yaml.safe_load(f)
    
    with open(config_path, 'r') as f:
        plan_config = yaml.safe_load(f)
    
    plan_summary = {}
    if summary_path.exists():
        with open(summary_path, 'r') as f:
            plan_summary = json.load(f)
    
    name = profile.get('name', athlete_id)
    first_name = name.split()[0] if name else athlete_id
    target_race = profile.get("target_race", {})
    race_name = target_race.get('name', 'your race')
    
    guide_lines = []
    
    # =====================================================================
    # HEADER
    # =====================================================================
    guide_lines.append(f"# {first_name}'s Training Plan: {race_name}")
    guide_lines.append("")
    guide_lines.append(f"*Generated {datetime.now().strftime('%B %d, %Y')}*")
    guide_lines.append("")
    guide_lines.append("---")
    guide_lines.append("")
    
    # =====================================================================
    # TL;DR SUMMARY
    # =====================================================================
    guide_lines.append("## Quick Reference")
    guide_lines.append("")
    guide_lines.append(f"**Race**: {race_name}")
    guide_lines.append(f"**Date**: {target_race.get('date', 'TBD')}")
    guide_lines.append(f"**Goal**: {target_race.get('goal_type', 'finish').title()}")
    guide_lines.append(f"**Plan Length**: {derived['plan_weeks']} weeks")
    guide_lines.append(f"**Tier**: {derived['tier'].title()}")
    guide_lines.append(f"**Strength Sessions**: {derived['strength_frequency']}x/week")
    guide_lines.append("")
    
    # =====================================================================
    # YOUR WEEKLY STRUCTURE
    # =====================================================================
    guide_lines.append("## Your Weekly Structure")
    guide_lines.append("")
    guide_lines.append("This is YOUR schedule based on your availability:")
    guide_lines.append("")
    guide_lines.append("| Day | Workout | Notes |")
    guide_lines.append("|-----|---------|-------|")
    
    weekly_structure_path = Path(f"athletes/{athlete_id}/weekly_structure.yaml")
    if weekly_structure_path.exists():
        with open(weekly_structure_path, 'r') as f:
            weekly_structure = yaml.safe_load(f)
        
        for day, schedule in weekly_structure.get("days", {}).items():
            am = schedule.get("am") or ""
            pm = schedule.get("pm") or ""
            key = " 🔑" if schedule.get("is_key_day") else ""
            
            if am and pm:
                workout = f"{am} (AM) + {pm} (PM)"
            elif am:
                workout = am
            elif pm:
                workout = f"{pm} (PM)"
            else:
                workout = "Rest"
            
            notes = "**Key session**" if schedule.get("is_key_day") else schedule.get("notes", "")
            guide_lines.append(f"| {day.title()}{key} | {workout} | {notes} |")
    
    guide_lines.append("")
    
    # Key days explanation
    key_days = derived.get("key_day_candidates", [])
    strength_days = derived.get("strength_day_candidates", [])
    
    guide_lines.append("### Priority Order")
    guide_lines.append("")
    guide_lines.append("When life gets in the way, prioritize in this order:")
    guide_lines.append("")
    guide_lines.append("1. **Key cycling sessions** (🔑 days) — These drive fitness gains")
    guide_lines.append("2. **Long ride** — Builds endurance foundation")
    guide_lines.append("3. **Strength sessions** — Injury prevention + power")
    guide_lines.append("4. **Easy rides** — Recovery, can be shortened or skipped")
    guide_lines.append("")
    
    # =====================================================================
    # PHASE-BY-PHASE GUIDE
    # =====================================================================
    guide_lines.append("---")
    guide_lines.append("")
    guide_lines.append("## Phase-by-Phase Guide")
    guide_lines.append("")
    
    plan_weeks = derived['plan_weeks']
    
    for phase_name, details in PHASE_DETAILS.items():
        # Determine which weeks this phase covers
        weeks_in_phase = []
        for w in range(1, plan_weeks + 1):
            if get_phase_for_week(w, plan_weeks) == phase_name:
                weeks_in_phase.append(w)
        
        if not weeks_in_phase:
            continue
        
        week_range = f"Weeks {weeks_in_phase[0]}-{weeks_in_phase[-1]}" if len(weeks_in_phase) > 1 else f"Week {weeks_in_phase[0]}"
        
        guide_lines.append(f"### {phase_name}")
        guide_lines.append(f"*{week_range}*")
        guide_lines.append("")
        guide_lines.append(f"**Focus**: {details['focus']}")
        guide_lines.append(f"**Effort**: {details['rpe']}")
        guide_lines.append(f"**Rest Between Sets**: {details['rest']}")
        guide_lines.append(f"**Rep Range**: {details['reps']}")
        guide_lines.append("")
        guide_lines.append("**Tips for this phase:**")
        for tip in details['tips']:
            guide_lines.append(f"- {tip}")
        guide_lines.append("")
    
    # =====================================================================
    # STRENGTH SESSION INSTRUCTIONS
    # =====================================================================
    guide_lines.append("---")
    guide_lines.append("")
    guide_lines.append("## How to Execute Strength Sessions")
    guide_lines.append("")
    guide_lines.append("### Before You Start")
    guide_lines.append("")
    guide_lines.append("1. **Watch the video demos** — Each exercise has a link. Watch it first.")
    guide_lines.append("2. **Warm up** — 5 minutes easy cardio + the activation exercises in the workout")
    guide_lines.append("3. **Have equipment ready** — Minimize rest time searching for weights")
    guide_lines.append("")
    guide_lines.append("### During the Workout")
    guide_lines.append("")
    guide_lines.append("- **Follow the prescribed order** — Exercises are sequenced intentionally")
    guide_lines.append("- **Use the rest periods** — Don't rush. Strength needs recovery between sets")
    guide_lines.append("- **Log your weights** — Track what you lift so you can progress")
    guide_lines.append("- **Stop before failure** — Leave 1-2 reps in the tank")
    guide_lines.append("")
    guide_lines.append("### Weight Selection")
    guide_lines.append("")
    guide_lines.append("| If you can do... | Weight is... |")
    guide_lines.append("|-------------------|--------------|")
    guide_lines.append("| 3+ more reps than prescribed | Too light — increase next set |")
    guide_lines.append("| Exactly prescribed reps | Perfect — maintain or increase slightly |")
    guide_lines.append("| Fewer than prescribed | Too heavy — reduce weight |")
    guide_lines.append("| Form breaks down | Way too heavy — ego check, reduce significantly |")
    guide_lines.append("")
    
    # =====================================================================
    # EXERCISE MODIFICATIONS
    # =====================================================================
    exclusions = derived.get("exercise_exclusions", [])
    if exclusions:
        guide_lines.append("### Your Exercise Modifications")
        guide_lines.append("")
        guide_lines.append("Based on your injury history and movement limitations, ")
        guide_lines.append("these exercises have been excluded from your plan:")
        guide_lines.append("")
        for exclusion in exclusions:
            guide_lines.append(f"- ~~{exclusion}~~")
        guide_lines.append("")
        guide_lines.append("Substitute exercises are provided in your workouts.")
        guide_lines.append("")
    
    # =====================================================================
    # IMPORTING TO TRAININGPEAKS
    # =====================================================================
    guide_lines.append("---")
    guide_lines.append("")
    guide_lines.append("## Importing Workouts to TrainingPeaks")
    guide_lines.append("")
    guide_lines.append("### Step 1: Download Your ZWO Files")
    guide_lines.append("")
    guide_lines.append("Your strength workouts are in the `workouts/` folder:")
    guide_lines.append("```")
    guide_lines.append("workouts/")
    guide_lines.append("├── W01_STR_Learn_to_Lift_A.zwo")
    guide_lines.append("├── W01_STR_Learn_to_Lift_B.zwo")
    guide_lines.append("├── W02_STR_Learn_to_Lift_A.zwo")
    guide_lines.append("└── ... (one file per session)")
    guide_lines.append("```")
    guide_lines.append("")
    guide_lines.append("### Step 2: Import to TrainingPeaks")
    guide_lines.append("")
    guide_lines.append("1. Log into TrainingPeaks")
    guide_lines.append("2. Go to your calendar")
    guide_lines.append("3. Click **+ Add Workout** on the appropriate day")
    guide_lines.append("4. Select **Import from File**")
    guide_lines.append("5. Choose the ZWO file for that week/session")
    guide_lines.append("6. The workout will appear with full instructions and video links")
    guide_lines.append("")
    guide_lines.append("### Naming Convention")
    guide_lines.append("")
    guide_lines.append("`W##_STR_Phase_Session.zwo`")
    guide_lines.append("")
    guide_lines.append("- `W##` = Week number")
    guide_lines.append("- `STR` = Strength workout")
    guide_lines.append("- `Phase` = Learn to Lift, Lift Heavy Sh*t, Lift Fast, Don't Lose It")
    guide_lines.append("- `Session` = A (first session) or B (second session)")
    guide_lines.append("")
    
    # =====================================================================
    # WHAT IF I MISS A WORKOUT?
    # =====================================================================
    guide_lines.append("---")
    guide_lines.append("")
    guide_lines.append("## What If I Miss a Workout?")
    guide_lines.append("")
    guide_lines.append("### Missed a Strength Session")
    guide_lines.append("")
    guide_lines.append("- **Same week**: Move it to the next available day (avoid day before key cycling)")
    guide_lines.append("- **Already past**: Skip it. Don't double up. Move on to next week's session.")
    guide_lines.append("- **Multiple weeks**: Just pick up where you are in the plan. Don't try to catch up.")
    guide_lines.append("")
    guide_lines.append("### Missed a Key Cycling Session")
    guide_lines.append("")
    guide_lines.append("- **Same week**: Try to fit it in, even shortened")
    guide_lines.append("- **Already past**: Note it happened, don't try to make it up")
    guide_lines.append("- **Multiple sessions**: Consider if something needs to change (schedule, life, etc.)")
    guide_lines.append("")
    guide_lines.append("### Feeling Fatigued?")
    guide_lines.append("")
    guide_lines.append("| Fatigue Level | Strength Adjustment | Cycling Adjustment |")
    guide_lines.append("|---------------|---------------------|-------------------|")
    guide_lines.append("| Legs tired | Do upper body focus | Reduce intensity, keep duration |")
    guide_lines.append("| Generally exhausted | Skip or do mobility only | Easy spin or rest |")
    guide_lines.append("| Sick | Rest completely | Rest completely |")
    guide_lines.append("| Minor niggle | Modify around it | Modify around it |")
    guide_lines.append("")
    
    # =====================================================================
    # RACE-SPECIFIC NOTES
    # =====================================================================
    if plan_summary.get("strength_customization"):
        guide_lines.append("---")
        guide_lines.append("")
        guide_lines.append(f"## {race_name}-Specific Training")
        guide_lines.append("")
        
        customization = plan_summary["strength_customization"]
        if customization.get("notes"):
            guide_lines.append(customization["notes"])
            guide_lines.append("")
        
        if customization.get("emphasized_exercises"):
            guide_lines.append("### Key Exercises for This Race")
            guide_lines.append("")
            guide_lines.append("Your plan emphasizes these movements:")
            guide_lines.append("")
            for ex in customization["emphasized_exercises"]:
                guide_lines.append(f"- {ex}")
            guide_lines.append("")
    
    # =====================================================================
    # YOUR EQUIPMENT
    # =====================================================================
    guide_lines.append("---")
    guide_lines.append("")
    guide_lines.append("## Your Equipment")
    guide_lines.append("")
    equipment = profile.get("strength_equipment", [])
    if equipment:
        guide_lines.append("Your workouts are designed for:")
        guide_lines.append("")
        for item in equipment:
            guide_lines.append(f"- {item.replace('_', ' ').title()}")
    else:
        guide_lines.append("Your workouts use **bodyweight only**.")
    guide_lines.append("")
    guide_lines.append("If you gain access to more equipment, let your coach know to update your plan.")
    guide_lines.append("")
    
    # =====================================================================
    # FINAL NOTES
    # =====================================================================
    guide_lines.append("---")
    guide_lines.append("")
    guide_lines.append("## Questions?")
    guide_lines.append("")
    guide_lines.append("- **Technical issues**: Check the workout file names match the week you're in")
    guide_lines.append("- **Exercise substitutions**: Message your coach with the specific movement")
    guide_lines.append("- **Schedule changes**: Update your profile and we'll regenerate")
    guide_lines.append("- **Something hurts**: Stop. Message your coach before continuing.")
    guide_lines.append("")
    guide_lines.append("---")
    guide_lines.append("")
    guide_lines.append(f"*Let's get after it, {first_name}.*")
    guide_lines.append("")
    
    # Write guide
    guide_path = plan_dir / "guide.md"
    with open(guide_path, 'w') as f:
        f.write("\n".join(guide_lines))
    
    # Also write to current/
    current_dir = Path(f"athletes/{athlete_id}/plans/current")
    current_dir.mkdir(parents=True, exist_ok=True)
    current_guide = current_dir / "guide.md"
    with open(current_guide, 'w') as f:
        f.write("\n".join(guide_lines))
    
    return guide_path


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python generate_athlete_guide.py <athlete_id> [plan_dir]")
        sys.exit(1)
    
    athlete_id = sys.argv[1]
    
    if len(sys.argv) >= 3:
        plan_dir = Path(sys.argv[2])
    else:
        # Find most recent plan
        plans_dir = Path(f"athletes/{athlete_id}/plans")
        if not plans_dir.exists():
            print(f"Error: No plans found for {athlete_id}")
            sys.exit(1)
        
        plan_dirs = [p for p in plans_dir.iterdir() if p.is_dir() and p.name != "current"]
        if not plan_dirs:
            print(f"Error: No plans found for {athlete_id}")
            sys.exit(1)
        
        plan_dir = sorted(plan_dirs, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    
    try:
        guide_path = generate_athlete_guide(athlete_id, plan_dir)
        print(f"✅ Guide generated: {guide_path}")
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
