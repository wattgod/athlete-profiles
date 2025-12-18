#!/usr/bin/env python3
"""
Generate Personalized HTML Training Guide

Creates a comprehensive, neo-brutalist styled training guide
customized to the athlete's profile, race, and plan.
"""

import yaml
import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional


# =============================================================================
# NEO-BRUTALIST HTML TEMPLATE
# =============================================================================

HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - Training Guide</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Sometype+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --gg-bg: #ffffff;
            --gg-fg: #111111;
            --gg-border: #000000;
            --gg-muted: #666666;
            --gg-soft: #f5f5f5;
            --gg-accent: #000000;
        }}

        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            padding: 32px 20px 64px 20px;
            max-width: 900px;
            margin: 0 auto;
            background: var(--gg-bg);
            color: var(--gg-fg);
            font-family: "Sometype Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 14px;
            line-height: 1.7;
            letter-spacing: 0.01em;
        }}

        /* HEADER */
        .guide-header {{
            border-bottom: 3px solid var(--gg-border);
            padding-bottom: 16px;
            margin-bottom: 32px;
        }}

        .guide-header h1 {{
            font-size: 28px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.15em;
            margin: 0 0 8px 0;
        }}

        .guide-subtitle {{
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            color: var(--gg-muted);
            margin: 0 0 12px 0;
        }}

        .guide-meta {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }}

        .guide-meta span {{
            display: inline-block;
            border: 1px solid var(--gg-border);
            padding: 4px 10px;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.1em;
        }}

        /* TABLE OF CONTENTS */
        .toc {{
            border: 2px solid var(--gg-border);
            padding: 20px 24px;
            margin-bottom: 40px;
            background: var(--gg-soft);
        }}

        .toc h2 {{
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.15em;
            margin: 0 0 16px 0;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--gg-border);
        }}

        .toc ol {{
            margin: 0;
            padding-left: 20px;
        }}

        .toc li {{
            margin-bottom: 6px;
        }}

        .toc a {{
            color: var(--gg-fg);
            text-decoration: none;
        }}

        .toc a:hover {{
            text-decoration: underline;
        }}

        /* SECTIONS */
        section {{
            margin-bottom: 48px;
        }}

        h2 {{
            font-size: 18px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            margin: 0 0 20px 0;
            padding-bottom: 8px;
            border-bottom: 2px solid var(--gg-border);
        }}

        h3 {{
            font-size: 13px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            color: var(--gg-muted);
            margin: 24px 0 12px 0;
        }}

        h4 {{
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin: 20px 0 10px 0;
        }}

        p {{
            margin: 0 0 16px 0;
        }}

        strong {{
            font-weight: 600;
        }}

        ul, ol {{
            margin: 0 0 16px 0;
            padding-left: 24px;
        }}

        li {{
            margin-bottom: 6px;
        }}

        /* CALLOUT BOXES */
        .callout {{
            border: 2px solid var(--gg-border);
            border-left-width: 6px;
            padding: 16px 20px;
            margin: 20px 0;
            background: var(--gg-soft);
        }}

        .callout.alert {{
            border-left-color: #000;
            background: #f0f0f0;
        }}

        .callout.info {{
            border-left-color: #666;
        }}

        .callout.tip {{
            border-style: dashed;
        }}

        .callout h4 {{
            margin-top: 0;
        }}

        /* TABLES */
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 16px 0 24px 0;
            font-size: 13px;
        }}

        th, td {{
            border: 1px solid var(--gg-border);
            padding: 8px 12px;
            text-align: left;
            vertical-align: top;
        }}

        th {{
            background: #f0f0f0;
            font-weight: 600;
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 0.08em;
        }}

        tbody tr:nth-child(even) {{
            background: #fafafa;
        }}

        /* KEY INDICATOR */
        .key-day {{
            display: inline-block;
            background: #000;
            color: #fff;
            padding: 2px 6px;
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-left: 6px;
        }}

        /* PHASE CARDS */
        .phase-card {{
            border: 2px solid var(--gg-border);
            margin: 16px 0;
            background: #fff;
        }}

        .phase-card-header {{
            background: #000;
            color: #fff;
            padding: 12px 16px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            font-size: 13px;
        }}

        .phase-card-body {{
            padding: 16px;
        }}

        .phase-card-body p {{
            margin: 0 0 8px 0;
        }}

        .phase-card-body ul {{
            margin: 8px 0 0 0;
        }}

        /* QUICK STATS */
        .quick-stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 12px;
            margin: 20px 0;
        }}

        .stat-box {{
            border: 2px solid var(--gg-border);
            padding: 16px;
            text-align: center;
        }}

        .stat-value {{
            font-size: 24px;
            font-weight: 700;
            display: block;
            margin-bottom: 4px;
        }}

        .stat-label {{
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            color: var(--gg-muted);
        }}

        /* CHECKLIST */
        .checklist {{
            list-style: none;
            padding: 0;
        }}

        .checklist li {{
            display: flex;
            align-items: flex-start;
            gap: 10px;
            margin-bottom: 8px;
        }}

        .checklist input[type="checkbox"] {{
            width: 18px;
            height: 18px;
            margin-top: 2px;
            cursor: pointer;
        }}

        /* ZONE TABLE */
        .zone-row-gspot {{
            background: #e8f5e9 !important;
        }}

        /* TIMELINE */
        .timeline {{
            border-left: 3px solid var(--gg-border);
            margin: 20px 0 20px 12px;
            padding-left: 24px;
        }}

        .timeline-item {{
            position: relative;
            margin-bottom: 20px;
        }}

        .timeline-item::before {{
            content: '';
            position: absolute;
            left: -30px;
            top: 4px;
            width: 12px;
            height: 12px;
            background: #000;
            border-radius: 50%;
        }}

        .timeline-time {{
            font-weight: 600;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}

        /* DECISION TREE */
        .decision-tree {{
            border: 2px solid var(--gg-border);
            padding: 20px;
            margin: 20px 0;
        }}

        .decision-tree h4 {{
            margin-top: 0;
            padding-bottom: 8px;
            border-bottom: 1px solid var(--gg-border);
        }}

        .decision-tree ol {{
            margin-bottom: 0;
        }}

        /* FOOTER */
        .guide-footer {{
            border-top: 3px solid var(--gg-border);
            padding-top: 24px;
            margin-top: 48px;
            text-align: center;
        }}

        .guide-footer p {{
            font-size: 16px;
            font-weight: 600;
        }}

        /* PRINT STYLES */
        @media print {{
            body {{
                font-size: 12px;
            }}
            .toc {{
                page-break-after: always;
            }}
            section {{
                page-break-inside: avoid;
            }}
        }}

        /* RESPONSIVE */
        @media (max-width: 600px) {{
            .guide-header h1 {{
                font-size: 22px;
            }}
            .quick-stats {{
                grid-template-columns: 1fr 1fr;
            }}
        }}
    </style>
</head>
<body>
{content}
</body>
</html>
'''


# =============================================================================
# GUIDE GENERATOR CLASS
# =============================================================================

class GuideGenerator:
    def __init__(self, athlete_id: str):
        self.athlete_id = athlete_id
        self.profile = None
        self.derived = None
        self.weekly_structure = None
        self.plan_config = None
        self.plan_summary = None
        
        self._load_data()
    
    def _load_data(self):
        """Load all athlete data files."""
        base_path = Path(f"athletes/{self.athlete_id}")
        
        # Load profile
        with open(base_path / "profile.yaml", 'r') as f:
            self.profile = yaml.safe_load(f)
        
        # Load derived
        with open(base_path / "derived.yaml", 'r') as f:
            self.derived = yaml.safe_load(f)
        
        # Load weekly structure if exists
        ws_path = base_path / "weekly_structure.yaml"
        if ws_path.exists():
            with open(ws_path, 'r') as f:
                self.weekly_structure = yaml.safe_load(f)
        
        # Load plan config if exists
        plans_dir = base_path / "plans"
        if plans_dir.exists():
            plan_dirs = [p for p in plans_dir.iterdir() if p.is_dir() and p.name != "current"]
            if plan_dirs:
                latest_plan = sorted(plan_dirs, key=lambda p: p.stat().st_mtime, reverse=True)[0]
                
                config_path = latest_plan / "plan_config.yaml"
                if config_path.exists():
                    with open(config_path, 'r') as f:
                        self.plan_config = yaml.safe_load(f)
                
                summary_path = latest_plan / "plan_summary.json"
                if summary_path.exists():
                    with open(summary_path, 'r') as f:
                        self.plan_summary = json.load(f)
    
    def _get_var(self, key: str, default: str = "") -> str:
        """Get a variable from profile or derived data."""
        # Check profile first
        if '.' in key:
            parts = key.split('.')
            data = self.profile
            for part in parts:
                if data and isinstance(data, dict):
                    data = data.get(part)
                else:
                    data = None
            if data:
                return str(data)
        
        # Direct lookups
        if key in self.profile:
            return str(self.profile[key])
        if key in self.derived:
            return str(self.derived[key])
        
        return default
    
    def _get_athlete_name(self) -> str:
        return self.profile.get('name', self.athlete_id)
    
    def _get_first_name(self) -> str:
        name = self._get_athlete_name()
        return name.split()[0] if name else self.athlete_id
    
    def _get_race_name(self) -> str:
        return self.profile.get('target_race', {}).get('name', 'Gravel Race')
    
    def _get_race_date(self) -> str:
        return self.profile.get('target_race', {}).get('date', 'TBD')
    
    def _get_tier(self) -> str:
        return self.derived.get('tier', 'compete').upper()
    
    def _get_tier_hours(self) -> tuple:
        tier = self.derived.get('tier', 'compete').lower()
        hours = {
            'ayahuasca': (4, 8),
            'finisher': (8, 12),
            'compete': (12, 18),
            'podium': (18, 25)
        }
        return hours.get(tier, (10, 15))
    
    def _calculate_age(self) -> Optional[int]:
        birthday = self.profile.get('birthday')
        if birthday:
            try:
                birth = datetime.strptime(birthday, "%Y-%m-%d")
                today = datetime.now()
                return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
            except:
                pass
        return None
    
    def _is_masters(self) -> bool:
        age = self._calculate_age()
        return age is not None and age >= 50
    
    def _is_female(self) -> bool:
        return self.profile.get('sex', '').lower() == 'female'
    
    def generate(self) -> str:
        """Generate the complete HTML guide."""
        sections = []
        
        # Header
        sections.append(self._generate_header())
        
        # Table of Contents
        sections.append(self._generate_toc())
        
        # Sections
        sections.append(self._generate_quick_reference())
        sections.append(self._generate_your_weekly_schedule())
        sections.append(self._generate_phase_progression())
        sections.append(self._generate_training_fundamentals())
        sections.append(self._generate_training_zones())
        sections.append(self._generate_workout_execution())
        sections.append(self._generate_strength_program())
        sections.append(self._generate_fueling_hydration())
        sections.append(self._generate_mental_training())
        sections.append(self._generate_race_tactics())
        sections.append(self._generate_race_week())
        
        # Conditional sections
        if self._is_masters():
            sections.append(self._generate_masters_section())
        
        if self._is_female():
            sections.append(self._generate_women_section())
        
        sections.append(self._generate_faq())
        sections.append(self._generate_footer())
        
        content = "\n\n".join(sections)
        
        return HTML_TEMPLATE.format(
            title=f"{self._get_race_name()} - {self._get_first_name()}",
            content=content
        )
    
    # =========================================================================
    # SECTION GENERATORS
    # =========================================================================
    
    def _generate_header(self) -> str:
        hours_min, hours_max = self._get_tier_hours()
        plan_weeks = self.derived.get('plan_weeks', 12)
        strength_freq = self.derived.get('strength_frequency', 2)
        
        return f'''
<header class="guide-header">
    <h1>{self._get_race_name()}</h1>
    <p class="guide-subtitle">{self._get_tier()} · {plan_weeks}-Week Training Plan · {self._get_first_name()}</p>
    <div class="guide-meta">
        <span>{hours_min}-{hours_max} hours/week</span>
        <span>{plan_weeks} weeks</span>
        <span>Strength {strength_freq}x/week</span>
        <span>Race: {self._get_race_date()}</span>
    </div>
</header>
'''
    
    def _generate_toc(self) -> str:
        toc_items = [
            ("quick-reference", "Quick Reference"),
            ("your-schedule", "Your Weekly Schedule"),
            ("phase-progression", "Phase Progression"),
            ("training-fundamentals", "Training Fundamentals"),
            ("training-zones", "Training Zones"),
            ("workout-execution", "Workout Execution"),
            ("strength-program", "Your Strength Program"),
            ("fueling", "Fueling & Hydration"),
            ("mental-training", "Mental Training"),
            ("race-tactics", f"Race Tactics for {self._get_race_name()}"),
            ("race-week", "Race Week Protocol"),
        ]
        
        if self._is_masters():
            toc_items.append(("masters", "Masters-Specific Considerations"))
        if self._is_female():
            toc_items.append(("women", "Women-Specific Considerations"))
        
        toc_items.append(("faq", "FAQ"))
        
        items_html = "\n".join([
            f'<li><a href="#{id}">{title}</a></li>'
            for id, title in toc_items
        ])
        
        return f'''
<nav class="toc">
    <h2>Contents</h2>
    <ol>
{items_html}
    </ol>
</nav>
'''
    
    def _generate_quick_reference(self) -> str:
        hours_min, hours_max = self._get_tier_hours()
        plan_weeks = self.derived.get('plan_weeks', 12)
        strength_freq = self.derived.get('strength_frequency', 2)
        
        target_race = self.profile.get('target_race', {})
        
        return f'''
<section id="quick-reference">
    <h2>1 · Quick Reference</h2>
    
    <div class="quick-stats">
        <div class="stat-box">
            <span class="stat-value">{self._get_race_name()}</span>
            <span class="stat-label">Target Race</span>
        </div>
        <div class="stat-box">
            <span class="stat-value">{self._get_race_date()}</span>
            <span class="stat-label">Race Date</span>
        </div>
        <div class="stat-box">
            <span class="stat-value">{target_race.get('goal_type', 'Compete').title()}</span>
            <span class="stat-label">Goal</span>
        </div>
        <div class="stat-box">
            <span class="stat-value">{plan_weeks}</span>
            <span class="stat-label">Weeks</span>
        </div>
        <div class="stat-box">
            <span class="stat-value">{self._get_tier()}</span>
            <span class="stat-label">Tier</span>
        </div>
        <div class="stat-box">
            <span class="stat-value">{hours_min}-{hours_max}</span>
            <span class="stat-label">Hours/Week</span>
        </div>
        <div class="stat-box">
            <span class="stat-value">{strength_freq}x</span>
            <span class="stat-label">Strength/Week</span>
        </div>
    </div>
    
    <div class="callout info">
        <h4>What This Plan Does</h4>
        <p>This plan coordinates your cycling and strength training into a unified system. Phases are aligned so you're not peaking in both simultaneously. Strength sessions are scheduled to avoid interfering with key cycling sessions.</p>
        <p><strong>Your job:</strong> Execute the workouts. Recover properly. Trust the process.</p>
    </div>
</section>
'''
    
    def _generate_your_weekly_schedule(self) -> str:
        if not self.weekly_structure:
            return '<section id="your-schedule"><h2>2 · Your Weekly Schedule</h2><p>Weekly structure not yet generated.</p></section>'
        
        days = self.weekly_structure.get('days', {})
        rows = []
        
        for day_name in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']:
            schedule = days.get(day_name, {})
            am = schedule.get('am') or '—'
            pm = schedule.get('pm') or '—'
            is_key = schedule.get('is_key_day', False)
            notes = schedule.get('notes', '')
            
            key_badge = '<span class="key-day">KEY</span>' if is_key else ''
            
            rows.append(f'''
                <tr>
                    <td><strong>{day_name.title()}</strong>{key_badge}</td>
                    <td>{am}</td>
                    <td>{pm}</td>
                    <td>{notes}</td>
                </tr>
            ''')
        
        rows_html = "\n".join(rows)
        
        key_days = self.derived.get('key_day_candidates', [])
        strength_days = self.derived.get('strength_day_candidates', [])
        
        return f'''
<section id="your-schedule">
    <h2>2 · Your Weekly Schedule</h2>
    
    <p>This is <strong>YOUR</strong> schedule based on your availability and preferences:</p>
    
    <table>
        <thead>
            <tr>
                <th>Day</th>
                <th>AM</th>
                <th>PM</th>
                <th>Notes</th>
            </tr>
        </thead>
        <tbody>
{rows_html}
        </tbody>
    </table>
    
    <h3>Priority Order</h3>
    <p>When life gets in the way, prioritize in this order:</p>
    <ol>
        <li><strong>Key cycling sessions</strong> — These drive fitness gains</li>
        <li><strong>Long ride</strong> — Builds endurance foundation</li>
        <li><strong>Strength sessions</strong> — Injury prevention + power</li>
        <li><strong>Easy rides</strong> — Recovery, can be shortened or skipped</li>
    </ol>
    
    <div class="callout tip">
        <h4>Your Key Days</h4>
        <p><strong>Key cycling:</strong> {', '.join([d.title() for d in key_days]) or 'TBD'}</p>
        <p><strong>Strength days:</strong> {', '.join([d.title() for d in strength_days]) or 'TBD'}</p>
    </div>
</section>
'''
    
    def _generate_phase_progression(self) -> str:
        plan_weeks = self.derived.get('plan_weeks', 12)
        
        # Calculate phase weeks based on plan length
        if plan_weeks >= 20:
            phases = [
                ("Base", "1-4", "Learn to Lift"),
                ("Build", "5-12", "Lift Heavy Sh*t"),
                ("Peak", "13-18", "Lift Fast"),
                ("Taper", "19-20", "Don't Lose It"),
            ]
        elif plan_weeks >= 12:
            phases = [
                ("Base", "1-3", "Learn to Lift"),
                ("Build", "4-7", "Lift Heavy Sh*t"),
                ("Peak", "8-10", "Lift Fast"),
                ("Taper", "11-12", "Don't Lose It"),
            ]
        else:
            phases = [
                ("Base", "1-2", "Learn to Lift"),
                ("Build", "3-4", "Lift Heavy Sh*t"),
                ("Peak", f"5-{plan_weeks-1}", "Lift Fast"),
                ("Taper", f"{plan_weeks}", "Don't Lose It"),
            ]
        
        phase_cards = []
        for cycling_phase, weeks, strength_phase in phases:
            phase_cards.append(f'''
<div class="phase-card">
    <div class="phase-card-header">{cycling_phase} Phase — Weeks {weeks}</div>
    <div class="phase-card-body">
        <p><strong>Cycling:</strong> {self._get_cycling_phase_desc(cycling_phase)}</p>
        <p><strong>Strength:</strong> {strength_phase}</p>
    </div>
</div>
''')
        
        return f'''
<section id="phase-progression">
    <h2>3 · Your {plan_weeks}-Week Phase Progression</h2>
    
    <p>Your training progresses through four coordinated phases. Cycling and strength are aligned so you're not double-peaking.</p>
    
    {"".join(phase_cards)}
    
    <div class="callout alert">
        <h4>Why Phase Alignment Matters</h4>
        <p>Most training plans treat cycling and strength separately. You end up building max strength while also doing your highest cycling volume—a recipe for overtraining.</p>
        <p>This plan coordinates them: when cycling load is highest (Build/Peak), strength shifts to power and maintenance. When cycling is easier (Base), strength builds foundation.</p>
    </div>
</section>
'''
    
    def _get_cycling_phase_desc(self, phase: str) -> str:
        descs = {
            "Base": "Building aerobic foundation. Long Z2 rides. Establishing rhythm.",
            "Build": "Adding intensity. Race-specific fitness. G-Spot work.",
            "Peak": "Maximum training load. Race simulation. Proving readiness.",
            "Taper": "Reducing volume, maintaining intensity. Arriving fresh."
        }
        return descs.get(phase, "Progressive training.")
    
    def _generate_training_fundamentals(self) -> str:
        return '''
<section id="training-fundamentals">
    <h2>4 · Training Fundamentals</h2>
    
    <p>Before executing workouts, understand how training works at a mechanical level.</p>
    
    <h3>The Adaptation Cycle</h3>
    
    <div class="timeline">
        <div class="timeline-item">
            <div class="timeline-time">Step 1: Stress</div>
            <p>You apply training stress—a workout that exceeds your current capacity. Muscle fibers develop microtears. Glycogen depletes. Your body registers this as a problem to solve.</p>
        </div>
        <div class="timeline-item">
            <div class="timeline-time">Step 2: Fatigue</div>
            <p>Immediately after, you're weaker than before. This is normal. Fatigue is the signal that triggers adaptation.</p>
        </div>
        <div class="timeline-item">
            <div class="timeline-time">Step 3: Recovery</div>
            <p>Given adequate rest, nutrition, and time, your body repairs: muscle fibers rebuild, mitochondria multiply, capillary density increases.</p>
        </div>
        <div class="timeline-item">
            <div class="timeline-time">Step 4: Supercompensation</div>
            <p>Your body doesn't just return to baseline—it overshoots. You're now stronger than before.</p>
        </div>
        <div class="timeline-item">
            <div class="timeline-time">Step 5: Repeat</div>
            <p>Apply slightly larger stress. The cycle repeats. Over weeks, these small adaptations compound into meaningful fitness gains.</p>
        </div>
    </div>
    
    <h3>The Practical Rules</h3>
    <ol>
        <li><strong>Training stress must be adequate but not excessive</strong> — Hard enough to trigger adaptation. Not so hard you can't recover.</li>
        <li><strong>Recovery is training</strong> — Sleep, nutrition, stress management. This is where adaptation happens.</li>
        <li><strong>Consistency compounds</strong> — Ten weeks of steady training beats four weeks of heroics followed by burnout.</li>
        <li><strong>Patience is required</strong> — Meaningful adaptation takes weeks and months, not days.</li>
    </ol>
</section>
'''
    
    def _generate_training_zones(self) -> str:
        return '''
<section id="training-zones">
    <h2>5 · Training Zones</h2>
    
    <p>Zones quantify intensity. But the end goal of measuring intensity is to help you <strong>develop a feeling for intensity</strong>.</p>
    
    <table>
        <thead>
            <tr>
                <th>Zone</th>
                <th>Name</th>
                <th>% FTP</th>
                <th>Feel</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td><strong>Z1</strong></td>
                <td>Active Recovery</td>
                <td>&lt;55%</td>
                <td>Very easy. Full conversation possible. Doesn't feel like training.</td>
            </tr>
            <tr>
                <td><strong>Z2</strong></td>
                <td>Endurance</td>
                <td>56-75%</td>
                <td>All-day pace. Can chat freely. <strong>Most of your training lives here.</strong></td>
            </tr>
            <tr>
                <td><strong>Z3</strong></td>
                <td>Tempo</td>
                <td>76-87%</td>
                <td>Comfortably hard. Talking in short sentences.</td>
            </tr>
            <tr class="zone-row-gspot">
                <td><strong>G-Spot</strong></td>
                <td>Gravel Race Pace</td>
                <td>88-92%</td>
                <td>Uncomfortably sustainable. Hard enough to hurt, easy enough to repeat.</td>
            </tr>
            <tr>
                <td><strong>Z4</strong></td>
                <td>Threshold</td>
                <td>93-105%</td>
                <td>Hard, controlled. Can only say a few words.</td>
            </tr>
            <tr>
                <td><strong>Z5</strong></td>
                <td>VO2max</td>
                <td>106-120%</td>
                <td>Very hard. Near maximum. Speech impossible.</td>
            </tr>
            <tr>
                <td><strong>Z6</strong></td>
                <td>Anaerobic</td>
                <td>121-150%</td>
                <td>All-out. 30 seconds to 3 minutes max.</td>
            </tr>
        </tbody>
    </table>
    
    <div class="callout alert">
        <h4>The Most Common Mistake</h4>
        <p><strong>Easy means easy.</strong> Most people train too hard on easy days. Z2 should feel genuinely conversational. If you're breathing hard, you're in Z3.</p>
        <p>Fix this. It's the most common training mistake.</p>
    </div>
    
    <h3>When Devices and Body Conflict</h3>
    <p>Power meters can lie (bad calibration, stale FTP). Heart rate can be misleading (heat, dehydration, caffeine, illness).</p>
    <p><strong>Your body doesn't lie.</strong> If 90% FTP feels like 9/10 today when it should feel like 7/10, something's wrong. Trust your body.</p>
</section>
'''
    
    def _generate_workout_execution(self) -> str:
        return '''
<section id="workout-execution">
    <h2>6 · Workout Execution</h2>
    
    <p>There's a massive gap between what's written on the plan and what actually happens. This section teaches you how to close that gap.</p>
    
    <h3>Universal Rules</h3>
    
    <h4>1. Warm Up Properly</h4>
    <p>For intensity sessions: 15-20 minutes Z1→Z2→Z3. Include 3×1 min at Z3-Z4 to "open the legs." 2-3 minutes easy spin before first work interval.</p>
    
    <h4>2. Do the Actual Workout</h4>
    <p>Execute what's prescribed. Not more. Not less. Adding volume or intensity might feel productive, but it accumulates fatigue and ruins tomorrow's workout.</p>
    
    <h4>3. Chase Time-in-Zone, Not Hero Intervals</h4>
    <p>The goal is highest average power across the entire set, not crushing the first interval then dying.</p>
    
    <table>
        <thead>
            <tr>
                <th>Bad Execution</th>
                <th>Good Execution</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td>
                    Interval 1: 320W (way too hard)<br>
                    Interval 2: 290W (struggling)<br>
                    Interval 3: 270W (barely hanging on)<br>
                    Interval 4: Failed<br>
                    <strong>Total: 3 intervals, 293W avg</strong>
                </td>
                <td>
                    Interval 1: 300W (controlled)<br>
                    Interval 2: 300W (harder but doable)<br>
                    Interval 3: 295W (hardest one)<br>
                    Interval 4: 295W (finished strong)<br>
                    <strong>Total: 4 intervals, 297.5W avg</strong>
                </td>
            </tr>
        </tbody>
    </table>
    
    <h4>4. Stop If Power Drops >10%</h4>
    <p>Quality beats quantity. Four quality intervals at 300W beats six degraded intervals averaging 270W.</p>
    
    <h3>Indoor vs Outdoor</h3>
    <p><strong>Ride indoors:</strong> Interval sessions, short workouts (&lt;90 min), bad weather, time-crunched days.</p>
    <p><strong>Ride outdoors:</strong> Long endurance rides (2+ hours), skills practice, mental freshness, race-specific terrain.</p>
</section>
'''
    
    def _generate_strength_program(self) -> str:
        strength_freq = self.derived.get('strength_frequency', 2)
        exclusions = self.derived.get('exercise_exclusions', [])
        equipment = self.profile.get('strength_equipment', [])
        
        exclusions_html = ""
        if exclusions:
            exclusions_html = f'''
    <h3>Your Exercise Modifications</h3>
    <p>Based on your profile, these exercises have been excluded:</p>
    <ul>
        {"".join([f"<li><s>{ex}</s></li>" for ex in exclusions])}
    </ul>
    <p>Substitute exercises are provided in your workouts.</p>
'''
        
        equipment_html = ""
        if equipment:
            equipment_html = f'''
    <h3>Your Equipment</h3>
    <p>Workouts are designed for:</p>
    <ul>
        {"".join([f"<li>{eq.replace('_', ' ').title()}</li>" for eq in equipment])}
    </ul>
'''
        
        return f'''
<section id="strength-program">
    <h2>7 · Your Strength Program</h2>
    
    <p>Your plan includes <strong>{strength_freq}x/week</strong> strength sessions coordinated with your cycling.</p>
    
    <h3>Phase-by-Phase Guide</h3>
    
    <div class="phase-card">
        <div class="phase-card-header">Learn to Lift — Foundation Phase</div>
        <div class="phase-card-body">
            <p><strong>Focus:</strong> Movement quality and neuromuscular adaptation</p>
            <p><strong>Effort:</strong> 5-6/10 &nbsp;|&nbsp; <strong>Rest:</strong> 60-90 seconds &nbsp;|&nbsp; <strong>Reps:</strong> 10-15</p>
            <ul>
                <li>Focus on perfect form over weight</li>
                <li>Watch video demos before each exercise</li>
                <li>You should feel challenged but not crushed</li>
            </ul>
        </div>
    </div>
    
    <div class="phase-card">
        <div class="phase-card-header">Lift Heavy Sh*t — Max Strength Phase</div>
        <div class="phase-card-body">
            <p><strong>Focus:</strong> Maximum strength development</p>
            <p><strong>Effort:</strong> 7-8/10 &nbsp;|&nbsp; <strong>Rest:</strong> 2-3 minutes &nbsp;|&nbsp; <strong>Reps:</strong> 4-8</p>
            <ul>
                <li>Progressive overload: add weight when you complete all reps with good form</li>
                <li>If form breaks down, reduce weight</li>
                <li>Expect some DOMS (delayed onset muscle soreness)</li>
            </ul>
        </div>
    </div>
    
    <div class="phase-card">
        <div class="phase-card-header">Lift Fast — Power Conversion Phase</div>
        <div class="phase-card-body">
            <p><strong>Focus:</strong> Moving weight quickly</p>
            <p><strong>Effort:</strong> 7-8/10 &nbsp;|&nbsp; <strong>Rest:</strong> 2-3 minutes &nbsp;|&nbsp; <strong>Reps:</strong> 3-6</p>
            <ul>
                <li>Move the weight as fast as possible with control</li>
                <li>If you can't move it fast, reduce the weight</li>
                <li>This converts your strength into cycling power</li>
            </ul>
        </div>
    </div>
    
    <div class="phase-card">
        <div class="phase-card-header">Don't Lose It — Maintenance Phase</div>
        <div class="phase-card-body">
            <p><strong>Focus:</strong> Maintain adaptations with minimal fatigue</p>
            <p><strong>Effort:</strong> 5-6/10 &nbsp;|&nbsp; <strong>Rest:</strong> As needed &nbsp;|&nbsp; <strong>Reps:</strong> 6-10</p>
            <ul>
                <li>Just enough stimulus to maintain, not build</li>
                <li>You should feel refreshed after these sessions</li>
                <li>Never feel crushed going into race week</li>
            </ul>
        </div>
    </div>
    
{exclusions_html}
{equipment_html}
    
    <h3>How to Execute Strength Sessions</h3>
    <ol>
        <li><strong>Watch the video demos</strong> — Each exercise has a link. Watch it first.</li>
        <li><strong>Warm up</strong> — 5 minutes easy cardio + activation exercises in the workout</li>
        <li><strong>Follow the prescribed order</strong> — Exercises are sequenced intentionally</li>
        <li><strong>Use the rest periods</strong> — Strength needs recovery between sets</li>
        <li><strong>Log your weights</strong> — Track what you lift so you can progress</li>
        <li><strong>Stop before failure</strong> — Leave 1-2 reps in the tank</li>
    </ol>
    
    <h3>Weight Selection</h3>
    <table>
        <thead>
            <tr>
                <th>If you can do...</th>
                <th>Weight is...</th>
            </tr>
        </thead>
        <tbody>
            <tr><td>3+ more reps than prescribed</td><td>Too light — increase next set</td></tr>
            <tr><td>Exactly prescribed reps</td><td>Perfect — maintain or increase slightly</td></tr>
            <tr><td>Fewer than prescribed</td><td>Too heavy — reduce weight</td></tr>
            <tr><td>Form breaks down</td><td>Way too heavy — ego check, reduce significantly</td></tr>
        </tbody>
    </table>
</section>
'''
    
    def _generate_fueling_hydration(self) -> str:
        return '''
<section id="fueling">
    <h2>8 · Fueling & Hydration</h2>
    
    <p>You can have perfect training, a dialed bike, and excellent pacing strategy. None of it matters if you run out of fuel halfway through.</p>
    
    <h3>Quick Reference</h3>
    <table>
        <thead>
            <tr>
                <th>Scenario</th>
                <th>Carbs</th>
                <th>Fluid</th>
            </tr>
        </thead>
        <tbody>
            <tr><td>Training &lt;2 hours</td><td>30-45g/hour</td><td>500-750ml/hour</td></tr>
            <tr><td>Training 2-4 hours</td><td>45-60g/hour</td><td>500-750ml/hour</td></tr>
            <tr><td>Long ride 4-6 hours</td><td>60-75g/hour</td><td>500-750ml/hour</td></tr>
            <tr><td>Race day</td><td>60-90g/hour</td><td>500-750ml/hour</td></tr>
            <tr><td>Hot conditions (&gt;80°F)</td><td>60-90g/hour</td><td>750-1000ml/hour</td></tr>
        </tbody>
    </table>
    
    <h3>Daily Nutrition</h3>
    <ul>
        <li><strong>Protein:</strong> 1.6-2.2g per kg bodyweight</li>
        <li><strong>Carbs:</strong> 3-7g per kg (depends on training volume)</li>
        <li><strong>Fat:</strong> 0.8-1.2g per kg bodyweight</li>
    </ul>
    
    <h3>Race Day Fueling</h3>
    <p><strong>Pre-race (3-4 hours before):</strong> 2-3g carbs per kg. Familiar foods only.</p>
    <p><strong>During race:</strong> Start fueling at 30 minutes. 70-80g carbs per hour. <strong>Set a timer.</strong></p>
    
    <div class="callout alert">
        <h4>When Your Stomach Rebels</h4>
        <ol>
            <li>Back off intensity for 5-10 minutes</li>
            <li>Switch to liquid calories temporarily</li>
            <li>Small sips, not big gulps</li>
            <li>Don't panic and stop eating entirely—you'll bonk</li>
        </ol>
    </div>
    
    <h3>Train Your Gut</h3>
    <p>Your gut is trainable. If you never eat during training rides, your gut won't tolerate eating during races. Practice fueling on every long ride.</p>
</section>
'''
    
    def _generate_mental_training(self) -> str:
        return '''
<section id="mental-training">
    <h2>9 · Mental Training</h2>
    
    <p>Physical training builds the engine. Mental training determines whether you use that engine when things get hard.</p>
    
    <h3>6-2-7 Breathing Technique</h3>
    <p><strong>The pattern:</strong> Inhale 6 seconds, hold 2 seconds, exhale 7 seconds.</p>
    <p>The key is the exhale is longer than inhale—this triggers the calming response.</p>
    <p><strong>Use it for:</strong> Pre-race anxiety, mid-race panic, after a bad section.</p>
    
    <h3>Performance Statements</h3>
    <p>Pre-planned phrases that replace negative self-talk:</p>
    
    <table>
        <thead>
            <tr>
                <th>Type</th>
                <th>Examples</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td><strong>Technical cues</strong></td>
                <td>"Smooth pedal stroke" • "Relax your shoulders" • "Light hands"</td>
            </tr>
            <tr>
                <td><strong>Pain responses</strong></td>
                <td>"This is supposed to be hard" • "Pain is temporary, quitting is permanent"</td>
            </tr>
            <tr>
                <td><strong>Process statements</strong></td>
                <td>"Just get to the next aid station" • "One more climb" • "Next mile marker"</td>
            </tr>
        </tbody>
    </table>
    
    <h3>Personal Highlight Reel</h3>
    <p>Build a mental movie you can play to access confidence:</p>
    <ol>
        <li><strong>Scene 1:</strong> A past moment when you overcame something difficult</li>
        <li><strong>Scene 2:</strong> A future crucial moment in this race—see yourself executing perfectly</li>
        <li><strong>Scene 3:</strong> Crossing the finish line—in full sensory detail</li>
    </ol>
    <p>Practice until you can trigger the confident feeling on demand.</p>
</section>
'''
    
    def _generate_race_tactics(self) -> str:
        race_name = self._get_race_name()
        
        return f'''
<section id="race-tactics">
    <h2>10 · Race Tactics for {race_name}</h2>
    
    <p>Every long gravel race follows a predictable three-act structure.</p>
    
    <h3>The Three Acts</h3>
    
    <table>
        <thead>
            <tr>
                <th>Phase</th>
                <th>When</th>
                <th>What Happens</th>
                <th>Your Job</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td><strong>Act 1: The Madness</strong></td>
                <td>0-2 hours</td>
                <td>Chaos. Fresh legs + nervous energy. Attacks fly. Groups form/shatter.</td>
                <td>Survive. Don't chase. Find sustainable group. Eat. Drink.</td>
            </tr>
            <tr>
                <td><strong>Act 2: False Dawn</strong></td>
                <td>2-6 hours</td>
                <td>Order returns. Groups stabilize. Can feel deceptively easy.</td>
                <td>Stay disciplined on nutrition. Contribute to paceline but no hero pulls.</td>
            </tr>
            <tr>
                <td><strong>Act 3: The Piper</strong></td>
                <td>Final 2-4 hours</td>
                <td>The bill comes due. Under-fueled riders bonk. Under-prepared cramp.</td>
                <td>Maintain YOUR pace while others lose theirs. This is where you move up.</td>
            </tr>
        </tbody>
    </table>
    
    <h3>Decision Trees</h3>
    
    <div class="decision-tree">
        <h4>Flat Tire Protocol</h4>
        <ol>
            <li>Stay calm—this is expected, not a crisis</li>
            <li>Check if sealant is working (spin wheel, look for bubbles)</li>
            <li>If hole visible, insert plug immediately</li>
            <li>If plug fails or sidewall cut, replace tube</li>
            <li>Resume at Z2 for 2-3 minutes to settle back in</li>
        </ol>
    </div>
    
    <div class="decision-tree">
        <h4>Dropped from Group</h4>
        <ol>
            <li>Don't panic—emotional response costs more energy</li>
            <li>Assess: were you overextended or did they surge?</li>
            <li>Find YOUR sustainable pace</li>
            <li>Look for riders at similar pace within 30-60 seconds</li>
            <li>If solo, accept it—focus on YOUR race</li>
        </ol>
    </div>
    
    <div class="decision-tree">
        <h4>Bonking Protocol</h4>
        <ol>
            <li><strong>STOP IMMEDIATELY</strong>—don't try to push through</li>
            <li>Consume 2-3 gels or 200-300 calories FAST</li>
            <li>Wait 15-20 minutes MINIMUM</li>
            <li>Resume at Z1-Z2 pace ONLY</li>
            <li>Fuel aggressively for next hour</li>
        </ol>
    </div>
</section>
'''
    
    def _generate_race_week(self) -> str:
        return '''
<section id="race-week">
    <h2>11 · Race Week Protocol</h2>
    
    <p>By race week, the training is done. You can't add fitness—you can only preserve what you've built or add dumb fatigue through poor decisions.</p>
    
    <h3>Race Morning Timeline</h3>
    
    <div class="timeline">
        <div class="timeline-item">
            <div class="timeline-time">3-4 Hours Before</div>
            <p>Wake up. Eat familiar, high-carb, low-fiber breakfast. Target 1-2g carbs per kg.</p>
        </div>
        <div class="timeline-item">
            <div class="timeline-time">2 Hours Before</div>
            <p>Arrive at venue. Set up bike and gear. Use bathroom. Begin sipping fluids.</p>
        </div>
        <div class="timeline-item">
            <div class="timeline-time">1 Hour Before</div>
            <p>Final bike check: tire pressure, brakes, shifting. Short warm-up spin. Start pre-race nutrition (100-200 cal carbs).</p>
        </div>
        <div class="timeline-item">
            <div class="timeline-time">30 Minutes Before</div>
            <p>Run through highlight reel visualization. Review performance statements. Begin settling mind.</p>
        </div>
        <div class="timeline-item">
            <div class="timeline-time">10 Minutes Before</div>
            <p>6-2-7 breathing. Find your spot. Check nutrition is accessible.</p>
        </div>
        <div class="timeline-item">
            <div class="timeline-time">Start</div>
            <p>Controlled effort. Find sustainable rhythm. First gel at 20 minutes, not 60.</p>
        </div>
    </div>
    
    <div class="callout tip">
        <h4>Race Week Rule</h4>
        <p>Less is more. You've done the work. Now let your body absorb it. Show up fresh, not fatigued from last-minute training.</p>
    </div>
</section>
'''
    
    def _generate_masters_section(self) -> str:
        age = self._calculate_age()
        return f'''
<section id="masters">
    <h2>12 · Masters-Specific Considerations</h2>
    
    <p>At {age}, your physiology has changed in ways that demand training modifications—but these changes don't mandate decline.</p>
    
    <h3>The Physiology Is Real, But Trainable</h3>
    <ul>
        <li>VO2max drops ~5% per decade in trained athletes (vs 10% in sedentary)</li>
        <li>Maximum heart rate drops ~1 bpm per year</li>
        <li>Recovery takes longer—48 hours may become 72 hours</li>
        <li>Muscle fibers (especially Type II fast-twitch) decline without strength training</li>
    </ul>
    
    <h3>Training Modifications</h3>
    <ul>
        <li><strong>Recovery weeks:</strong> Every 2-3 weeks instead of every 4</li>
        <li><strong>Intensity distribution:</strong> 80/20 polarized (easy truly easy, hard truly hard)</li>
        <li><strong>Strength training:</strong> Mandatory, not optional—preserves fast-twitch fibers</li>
        <li><strong>Sleep:</strong> Aim for 7-9 hours. Growth hormone released during deep sleep.</li>
        <li><strong>Protein:</strong> 1.6-1.8g/kg daily (older muscles need more)</li>
    </ul>
    
    <h3>When to Skip vs Push Through</h3>
    <p><strong>Skip/modify when:</strong> Resting HR elevated >10%, HRV suppressed multiple days, sleep poor 2+ nights, muscle soreness beyond 48 hours.</p>
    <p><strong>Push through when:</strong> General fatigue but metrics normal, not mentally eager but physically recovered, tiredness improves during warmup.</p>
    
    <div class="callout info">
        <h4>The Mindset Shift</h4>
        <p>The correct mindset isn't training harder—it's training with greater precision. Recovery windows expand, so each stimulus must count. Injury consequences multiply because healing takes longer.</p>
    </div>
</section>
'''
    
    def _generate_women_section(self) -> str:
        return '''
<section id="women">
    <h2>13 · Women-Specific Considerations</h2>
    
    <p><strong>Women aren't small men.</strong> Your physiology differs in ways that affect training, recovery, fueling, and performance.</p>
    
    <h3>Menstrual Cycle and Training</h3>
    <table>
        <thead>
            <tr>
                <th>Phase</th>
                <th>Days</th>
                <th>Training Approach</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td><strong>Follicular</strong></td>
                <td>1-14</td>
                <td>Estrogen rises. Handle more intensity. Schedule hard workouts here.</td>
            </tr>
            <tr>
                <td><strong>Luteal</strong></td>
                <td>15-28</td>
                <td>Progesterone rises. Core temp increases. Prioritize endurance, recovery.</td>
            </tr>
            <tr>
                <td><strong>Menstruation</strong></td>
                <td>1-5</td>
                <td>Listen to your body. Extra rest day if needed. Don't force intensity.</td>
            </tr>
        </tbody>
    </table>
    
    <h3>Iron: The Critical Difference</h3>
    <ul>
        <li><strong>Target ferritin:</strong> >50 ng/mL (not just "normal" 15-150)</li>
        <li><strong>Get tested</strong> before starting this plan</li>
        <li><strong>If deficient:</strong> Supplement with vitamin C, avoid coffee/tea around supplements</li>
    </ul>
    
    <h3>Fueling Differences</h3>
    <ul>
        <li>During luteal phase, your body burns more carbs at rest—may need 10-15% more calories</li>
        <li>Under-fueling disrupts your cycle (amenorrhea = under-fueling, not fitness)</li>
        <li>GI distress may be more common during certain cycle phases</li>
    </ul>
    
    <h3>Heat and Hydration</h3>
    <ul>
        <li>Women sweat less per gland but core temperature rises faster</li>
        <li>During luteal phase: core temp already elevated, heat tolerance decreases</li>
        <li>Focus on cooling strategies (ice, cold water on skin) not just drinking more</li>
    </ul>
</section>
'''
    
    def _generate_faq(self) -> str:
        return '''
<section id="faq">
    <h2>14 · Frequently Asked Questions</h2>
    
    <h4>What if I miss a week of training?</h4>
    <p>One week won't kill you. Jump back in where the plan currently is—don't try to "make up" missed work. Forward progress only.</p>
    
    <h4>Can I do this plan entirely indoors?</h4>
    <p>Technically yes, but you're missing critical skills development. Do at least 30-40% outside, especially long rides.</p>
    
    <h4>What if my FTP changes mid-plan?</h4>
    <p>Test at Week 6-7 if curious. Only adjust zones if FTP changed by 5+ watts. Small fluctuations are noise.</p>
    
    <h4>How do I know if I'm overtraining?</h4>
    <p>Elevated resting heart rate, persistent fatigue, declining performance, irritability, poor sleep. If 3+ symptoms, take 2-3 days completely off.</p>
    
    <h4>What if I can't hit the prescribed watts?</h4>
    <p>Either FTP is set too high, or you're under-recovered. Take an extra rest day, retest FTP if needed.</p>
    
    <h4>Should I follow the plan exactly?</h4>
    <p>Follow as written. The order isn't random—hard days are spaced for optimal recovery. If you have a non-standard schedule, shift the entire week, don't rearrange individual workouts.</p>
    
    <h4>What if I get sick?</h4>
    <p>Above the neck (head cold): reduce intensity by one zone. Below the neck (chest, stomach): skip the workout entirely. Don't be a hero.</p>
</section>
'''
    
    def _generate_footer(self) -> str:
        first_name = self._get_first_name()
        plan_weeks = self.derived.get('plan_weeks', 12)
        
        return f'''
<footer class="guide-footer">
    <p>You have the plan.</p>
    <p>You understand how training works, how to execute the workouts, how to fuel and hydrate, how to manage your mental game, and how to approach race day.</p>
    <p><strong>Now do the work.</strong></p>
    <p>Not perfectly. Not heroically. Consistently. Intelligently. Over {plan_weeks} weeks.</p>
    <p>Show up for the workouts. Do them correctly. Recover properly. Trust the process.</p>
    <p style="font-size: 20px; margin-top: 32px;"><strong>Let's get after it, {first_name}.</strong></p>
    <p style="font-size: 11px; color: #666; margin-top: 24px;">Generated {datetime.now().strftime('%B %d, %Y')} • Gravel God Cycling</p>
</footer>
'''


# =============================================================================
# MAIN
# =============================================================================

def generate_html_guide(athlete_id: str, output_path: Optional[Path] = None) -> Path:
    """Generate HTML training guide for an athlete."""
    generator = GuideGenerator(athlete_id)
    html = generator.generate()
    
    if output_path is None:
        output_path = Path(f"athletes/{athlete_id}/plans/current/training_guide.html")
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w') as f:
        f.write(html)
    
    return output_path


def main():
    if len(sys.argv) < 2:
        print("Usage: python generate_html_guide.py <athlete_id> [output_path]")
        sys.exit(1)
    
    athlete_id = sys.argv[1]
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    
    try:
        path = generate_html_guide(athlete_id, output_path)
        print(f"✅ Training guide generated: {path}")
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

