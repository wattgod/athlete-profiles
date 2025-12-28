#!/usr/bin/env python3
"""
Generate Neo-Brutalist Athlete Dashboard

Creates a comprehensive dashboard showing all relevant coaching information
for an athlete in Neo-Brutalist style.
"""

import yaml
import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional


def format_date(date_str: Optional[str]) -> str:
    """Format date string for display."""
    if not date_str:
        return "N/A"
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%B %d, %Y")
    except:
        return date_str


def format_value(value, default="—") -> str:
    """Format a value for display."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return "YES" if value else "NO"
    if isinstance(value, list):
        if not value:
            return default
        return ", ".join(str(v).replace("_", " ").title() for v in value)
    return str(value)


def generate_dashboard(athlete_id: str) -> Path:
    """
    Generate comprehensive Neo-Brutalist dashboard for athlete.
    """
    # Load data
    profile_path = Path(f"athletes/{athlete_id}/profile.yaml")
    derived_path = Path(f"athletes/{athlete_id}/derived.yaml")
    weekly_structure_path = Path(f"athletes/{athlete_id}/weekly_structure.yaml")
    
    with open(profile_path, 'r') as f:
        profile = yaml.safe_load(f)
    
    derived = {}
    if derived_path.exists():
        with open(derived_path, 'r') as f:
            derived = yaml.safe_load(f)
    
    weekly_structure = {}
    if weekly_structure_path.exists():
        with open(weekly_structure_path, 'r') as f:
            weekly_structure = yaml.safe_load(f)
    
    # Get current plan
    plan_config_path = Path(f"athletes/{athlete_id}/plans/current/plan_config.yaml")
    plan_config = {}
    if plan_config_path.exists():
        with open(plan_config_path, 'r') as f:
            plan_config = yaml.safe_load(f)
    
    name = profile.get('name', athlete_id)
    email = profile.get('email', '')
    target_race = profile.get("target_race", {})
    fitness = profile.get("fitness_markers", {})
    training_history = profile.get("training_history", {})
    weekly_availability = profile.get("weekly_availability", {})
    preferred_days = profile.get("preferred_days", {})
    health = profile.get("health_factors", {})
    equipment = profile.get("strength_equipment", [])
    cycling_equipment = profile.get("cycling_equipment", {})
    
    # Generate HTML
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{name} - Coaching Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Sometype+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg: #ffffff;
            --fg: #000000;
            --border: #000000;
            --muted: #666666;
            --soft: #f5f5f5;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            background: var(--bg);
            color: var(--fg);
            font-family: "Sometype Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 14px;
            line-height: 1.6;
            padding: 24px;
            max-width: 1400px;
            margin: 0 auto;
        }}

        /* HEADER */
        .header {{
            border: 3px solid var(--border);
            padding: 24px;
            margin-bottom: 32px;
            background: var(--soft);
        }}

        .header h1 {{
            font-size: 32px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.2em;
            margin-bottom: 8px;
        }}

        .header-meta {{
            display: flex;
            flex-wrap: wrap;
            gap: 16px;
            margin-top: 16px;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.1em;
        }}

        .header-meta span {{
            border: 2px solid var(--border);
            padding: 6px 12px;
            background: var(--bg);
        }}

        /* GRID LAYOUT */
        .dashboard-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 24px;
            margin-bottom: 32px;
        }}

        /* CARD STYLES */
        .card {{
            border: 3px solid var(--border);
            padding: 24px;
            background: var(--bg);
        }}

        .card-header {{
            font-size: 18px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.15em;
            margin-bottom: 20px;
            padding-bottom: 12px;
            border-bottom: 2px solid var(--border);
        }}

        .card-content {{
            font-size: 14px;
        }}

        /* KEY-VALUE PAIRS */
        .kv-row {{
            display: flex;
            justify-content: space-between;
            padding: 8px 0;
            border-bottom: 1px solid var(--border);
        }}

        .kv-row:last-child {{
            border-bottom: none;
        }}

        .kv-key {{
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--muted);
        }}

        .kv-value {{
            font-weight: 500;
            text-align: right;
        }}

        /* TABLE STYLES */
        .table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 16px;
        }}

        .table th {{
            border: 2px solid var(--border);
            padding: 12px;
            text-align: left;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            font-size: 12px;
            background: var(--soft);
        }}

        .table td {{
            border: 2px solid var(--border);
            padding: 12px;
            border-top: none;
        }}

        .table tr:first-child td {{
            border-top: 2px solid var(--border);
        }}

        /* BADGES */
        .badge {{
            display: inline-block;
            border: 2px solid var(--border);
            padding: 4px 10px;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            font-weight: 600;
            margin: 2px;
        }}

        .badge-key {{
            background: var(--fg);
            color: var(--bg);
        }}

        /* FULL WIDTH CARDS */
        .card-full {{
            grid-column: 1 / -1;
        }}

        /* WEEKLY SCHEDULE */
        .schedule-day {{
            display: grid;
            grid-template-columns: 120px 1fr;
            gap: 16px;
            padding: 12px 0;
            border-bottom: 1px solid var(--border);
        }}

        .schedule-day:last-child {{
            border-bottom: none;
        }}

        .day-name {{
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.1em;
        }}

        .day-content {{
            display: flex;
            flex-direction: column;
            gap: 4px;
        }}

        .workout-type {{
            font-weight: 600;
        }}

        .workout-notes {{
            font-size: 12px;
            color: var(--muted);
        }}

        @media (max-width: 768px) {{
            .dashboard-grid {{
                grid-template-columns: 1fr;
            }}
            
            body {{
                padding: 16px;
            }}
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>{name.upper()}</h1>
        <div class="header-meta">
            <span>ID: {athlete_id}</span>
            <span>EMAIL: {email}</span>
            <span>TIER: {derived.get('tier', 'N/A').upper()}</span>
            <span>PLAN: {plan_config.get('plan_weeks', 'N/A')} WEEKS</span>
        </div>
    </div>

    <div class="dashboard-grid">
        <!-- TARGET RACE -->
        <div class="card">
            <div class="card-header">TARGET RACE</div>
            <div class="card-content">
                <div class="kv-row">
                    <span class="kv-key">Race</span>
                    <span class="kv-value">{format_value(target_race.get('name'))}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Date</span>
                    <span class="kv-value">{format_date(target_race.get('date'))}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Distance</span>
                    <span class="kv-value">{format_value(target_race.get('distance_miles'))} MILES</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Goal</span>
                    <span class="kv-value">{format_value(target_race.get('goal_type')).upper()}</span>
                </div>
            </div>
        </div>

        <!-- FITNESS MARKERS -->
        <div class="card">
            <div class="card-header">FITNESS MARKERS</div>
            <div class="card-content">
                <div class="kv-row">
                    <span class="kv-key">FTP</span>
                    <span class="kv-value">{format_value(fitness.get('ftp_watts'))} W</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">FTP Date</span>
                    <span class="kv-value">{format_date(fitness.get('ftp_date'))}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Weight</span>
                    <span class="kv-value">{format_value(fitness.get('weight_kg'))} KG</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">W/KG</span>
                    <span class="kv-value">{format_value(fitness.get('w_kg'))}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Resting HR</span>
                    <span class="kv-value">{format_value(fitness.get('resting_hr'))} BPM</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Max HR</span>
                    <span class="kv-value">{format_value(fitness.get('max_hr'))} BPM</span>
                </div>
            </div>
        </div>

        <!-- TRAINING HISTORY -->
        <div class="card">
            <div class="card-header">TRAINING HISTORY</div>
            <div class="card-content">
                <div class="kv-row">
                    <span class="kv-key">Years Cycling</span>
                    <span class="kv-value">{format_value(training_history.get('years_cycling'))}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Years Structured</span>
                    <span class="kv-value">{format_value(training_history.get('years_structured'))}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Strength Background</span>
                    <span class="kv-value">{format_value(training_history.get('strength_background')).upper()}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Peak Weekly Hours</span>
                    <span class="kv-value">{format_value(training_history.get('highest_weekly_hours'))} H</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Current Weekly Hours</span>
                    <span class="kv-value">{format_value(training_history.get('current_weekly_hours'))} H</span>
                </div>
            </div>
        </div>

        <!-- PLAN INFO -->
        <div class="card">
            <div class="card-header">CURRENT PLAN</div>
            <div class="card-content">
                <div class="kv-row">
                    <span class="kv-key">Tier</span>
                    <span class="kv-value">{format_value(derived.get('tier')).upper()}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Plan Weeks</span>
                    <span class="kv-value">{format_value(derived.get('plan_weeks'))}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Strength Frequency</span>
                    <span class="kv-value">{format_value(derived.get('strength_frequency'))}X/WEEK</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Equipment Tier</span>
                    <span class="kv-value">{format_value(derived.get('equipment_tier')).upper()}</span>
                </div>
            </div>
        </div>

        <!-- WEEKLY AVAILABILITY -->
        <div class="card">
            <div class="card-header">WEEKLY AVAILABILITY</div>
            <div class="card-content">
                <div class="kv-row">
                    <span class="kv-key">Total Hours</span>
                    <span class="kv-value">{format_value(weekly_availability.get('total_hours_available'))} H</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Cycling Target</span>
                    <span class="kv-value">{format_value(weekly_availability.get('cycling_hours_target'))} H</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Strength Max</span>
                    <span class="kv-value">{format_value(weekly_availability.get('strength_sessions_max'))} SESSIONS</span>
                </div>
            </div>
        </div>

        <!-- HEALTH FACTORS -->
        <div class="card">
            <div class="card-header">HEALTH FACTORS</div>
            <div class="card-content">
                <div class="kv-row">
                    <span class="kv-key">Age</span>
                    <span class="kv-value">{format_value(health.get('age'))}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Sleep Quality</span>
                    <span class="kv-value">{format_value(health.get('sleep_quality')).upper()}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Sleep Hours</span>
                    <span class="kv-value">{format_value(health.get('sleep_hours_avg'))} H</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Stress Level</span>
                    <span class="kv-value">{format_value(health.get('stress_level')).upper()}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Recovery Capacity</span>
                    <span class="kv-value">{format_value(health.get('recovery_capacity')).upper()}</span>
                </div>
            </div>
        </div>

        <!-- EQUIPMENT -->
        <div class="card">
            <div class="card-header">STRENGTH EQUIPMENT</div>
            <div class="card-content">
                {format_equipment_list(equipment)}
            </div>
        </div>

        <!-- CYCLING EQUIPMENT -->
        <div class="card">
            <div class="card-header">CYCLING EQUIPMENT</div>
            <div class="card-content">
                <div class="kv-row">
                    <span class="kv-key">Smart Trainer</span>
                    <span class="kv-value">{format_value(cycling_equipment.get('smart_trainer'))}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Power Meter</span>
                    <span class="kv-value">{format_value(cycling_equipment.get('power_meter_bike'))}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">HR Monitor</span>
                    <span class="kv-value">{format_value(cycling_equipment.get('hr_monitor'))}</span>
                </div>
                <div class="kv-row">
                    <span class="kv-key">Indoor Setup</span>
                    <span class="kv-value">{format_value(cycling_equipment.get('indoor_setup')).upper()}</span>
                </div>
            </div>
        </div>

        <!-- WEEKLY SCHEDULE -->
        <div class="card card-full">
            <div class="card-header">WEEKLY SCHEDULE</div>
            <div class="card-content">
                {format_weekly_schedule(weekly_structure.get('days', {}))}
            </div>
        </div>

        <!-- KEY DAYS & STRENGTH DAYS -->
        <div class="card">
            <div class="card-header">KEY DAYS</div>
            <div class="card-content">
                {format_day_list(derived.get('key_day_candidates', []))}
            </div>
        </div>

        <div class="card">
            <div class="card-header">STRENGTH DAYS</div>
            <div class="card-content">
                {format_day_list(derived.get('strength_day_candidates', []))}
            </div>
        </div>

        <!-- EXERCISE EXCLUSIONS -->
        {format_exclusions_card(derived.get('exercise_exclusions', []))}
    </div>

    <div style="text-align: center; margin-top: 48px; padding-top: 24px; border-top: 3px solid var(--border); font-size: 12px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--muted);">
        GENERATED {datetime.now().strftime('%B %d, %Y AT %H:%M').upper()}
    </div>
</body>
</html>'''

    # Write dashboard
    dashboard_path = Path(f"athletes/{athlete_id}/dashboard.html")
    dashboard_path.parent.mkdir(parents=True, exist_ok=True)
    with open(dashboard_path, 'w') as f:
        f.write(html)
    
    return dashboard_path


def format_equipment_list(equipment: List[str]) -> str:
    """Format equipment list."""
    if not equipment:
        return '<div class="kv-value">BODYWEIGHT ONLY</div>'
    
    items = []
    for item in equipment:
        items.append(f'<span class="badge">{item.replace("_", " ").upper()}</span>')
    return '<div>' + ' '.join(items) + '</div>'


def format_weekly_schedule(days: Dict) -> str:
    """Format weekly schedule."""
    if not days:
        return '<div class="kv-value">NO SCHEDULE AVAILABLE</div>'
    
    day_order = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
    schedule_html = []
    
    for day in day_order:
        if day not in days:
            continue
        
        day_data = days[day]
        day_name = day.upper()
        key_badge = '<span class="badge badge-key">KEY</span>' if day_data.get('is_key_day') else ''
        
        am = day_data.get('am') or ''
        pm = day_data.get('pm') or ''
        
        if am and pm:
            workout = f"{am.upper()} (AM) + {pm.upper()} (PM)"
        elif am:
            workout = am.upper()
        elif pm:
            workout = f"{pm.upper()} (PM)"
        else:
            workout = "REST"
        
        notes = day_data.get('notes', '')
        max_duration = day_data.get('max_duration', '')
        
        schedule_html.append(f'''
            <div class="schedule-day">
                <div class="day-name">{day_name} {key_badge}</div>
                <div class="day-content">
                    <div class="workout-type">{workout}</div>
                    {f'<div class="workout-notes">{notes}</div>' if notes else ''}
                    {f'<div class="workout-notes">MAX: {max_duration} MIN</div>' if max_duration else ''}
                </div>
            </div>
        ''')
    
    return '\n'.join(schedule_html)


def format_day_list(days: List[str]) -> str:
    """Format list of days."""
    if not days:
        return '<div class="kv-value">NONE</div>'
    
    badges = [f'<span class="badge">{day.upper()}</span>' for day in days]
    return '<div>' + ' '.join(badges) + '</div>'


def format_exclusions_card(exclusions: List[str]) -> str:
    """Format exercise exclusions card."""
    if not exclusions:
        return ''
    
    items = [f'<span class="badge" style="text-decoration: line-through;">{ex.upper()}</span>' for ex in exclusions]
    return f'''
        <div class="card">
            <div class="card-header">EXERCISE EXCLUSIONS</div>
            <div class="card-content">
                <div>{' '.join(items)}</div>
            </div>
        </div>
    '''


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python generate_dashboard.py <athlete_id>")
        sys.exit(1)
    
    athlete_id = sys.argv[1]
    
    try:
        dashboard_path = generate_dashboard(athlete_id)
        print(f"✅ Dashboard generated: {dashboard_path}")
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

