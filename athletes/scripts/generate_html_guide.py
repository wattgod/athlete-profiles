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

        /* RACE TIMELINE */
        .race-timeline {{
            position: relative;
            padding: 20px 0;
            margin: 24px 0;
        }}

        .timeline-track {{
            position: relative;
            height: 60px;
            background: linear-gradient(to right, #f0f0f0 0%, #e0e0e0 100%);
            border: 2px solid var(--gg-border);
            margin-bottom: 20px;
        }}

        .timeline-labels {{
            display: flex;
            justify-content: space-between;
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--gg-muted);
            margin-bottom: 8px;
        }}

        .timeline-event {{
            position: absolute;
            top: -8px;
            transform: translateX(-50%);
            text-align: center;
        }}

        .timeline-event .marker {{
            width: 24px;
            height: 24px;
            border: 3px solid #000;
            border-radius: 50%;
            background: #fff;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 11px;
            font-weight: 700;
            margin: 0 auto 4px auto;
        }}

        .timeline-event.a-event .marker {{
            background: #000;
            color: #fff;
            width: 32px;
            height: 32px;
            font-size: 14px;
        }}

        .timeline-event.b-event .marker {{
            background: #666;
            color: #fff;
        }}

        .timeline-event.c-event .marker {{
            background: #fff;
            color: #000;
        }}

        .timeline-event .event-name {{
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            max-width: 80px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}

        .timeline-event .event-date {{
            font-size: 9px;
            color: var(--gg-muted);
        }}

        .event-legend {{
            display: flex;
            gap: 24px;
            justify-content: center;
            margin-top: 16px;
            font-size: 11px;
        }}

        .event-legend span {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .legend-dot {{
            width: 12px;
            height: 12px;
            border: 2px solid #000;
            border-radius: 50%;
        }}

        .legend-dot.a {{ background: #000; }}
        .legend-dot.b {{ background: #666; }}
        .legend-dot.c {{ background: #fff; }}

        /* GOALS */
        .goal-card {{
            border: 2px solid var(--gg-border);
            padding: 16px 20px;
            margin: 12px 0;
            display: flex;
            gap: 16px;
            align-items: flex-start;
        }}

        .goal-icon {{
            font-size: 24px;
            flex-shrink: 0;
        }}

        .goal-content h4 {{
            margin: 0 0 4px 0;
        }}

        .goal-content p {{
            margin: 0;
            color: var(--gg-muted);
            font-size: 13px;
        }}

        /* PHILOSOPHY */
        .philosophy-framework {{
            border: 2px solid var(--gg-border);
            padding: 24px;
            margin: 20px 0;
            background: var(--gg-soft);
        }}

        .philosophy-diagram {{
            display: flex;
            justify-content: center;
            gap: 8px;
            margin: 20px 0;
        }}

        .philosophy-bar {{
            height: 40px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}

        .bar-easy {{
            background: #e0e0e0;
            border: 2px solid #000;
        }}

        .bar-hard {{
            background: #000;
            color: #fff;
        }}

        /* BLINDSPOTS */
        .blindspot {{
            border: 2px solid var(--gg-border);
            border-left-width: 8px;
            padding: 16px 20px;
            margin: 16px 0;
            background: #fff;
        }}

        .blindspot.high {{
            border-left-color: #000;
            background: #f5f5f5;
        }}

        .blindspot.medium {{
            border-left-color: #666;
        }}

        .blindspot.low {{
            border-left-color: #ccc;
        }}

        .blindspot-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
        }}

        .blindspot-header h4 {{
            margin: 0;
        }}

        .blindspot-severity {{
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            padding: 2px 8px;
            border: 1px solid currentColor;
        }}

        /* ATP TABLE - IMPROVED READABILITY */
        .atp-container {{
            margin: 24px 0;
        }}

        .atp-week {{
            border: 2px solid var(--gg-border);
            margin-bottom: -2px;
        }}

        .atp-week-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 16px;
            background: var(--gg-soft);
            cursor: pointer;
            user-select: none;
        }}

        .atp-week-header:hover {{
            background: #e8e8e8;
        }}

        .atp-week-title {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}

        .atp-week-num {{
            font-weight: 700;
            font-size: 14px;
        }}

        .atp-week-phase {{
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            padding: 2px 8px;
            background: #000;
            color: #fff;
        }}

        .atp-week-phase.base {{ background: #888; }}
        .atp-week-phase.build {{ background: #444; }}
        .atp-week-phase.peak {{ background: #000; }}
        .atp-week-phase.taper {{ background: #aaa; color: #000; }}

        .atp-week-meta {{
            display: flex;
            gap: 16px;
            font-size: 11px;
            color: var(--gg-muted);
        }}

        .atp-week-toggle {{
            font-size: 18px;
            font-weight: 700;
            transition: transform 0.2s;
        }}

        .atp-week.open .atp-week-toggle {{
            transform: rotate(45deg);
        }}

        .atp-week-content {{
            display: none;
            padding: 16px 20px;
            border-top: 1px solid var(--gg-border);
        }}

        .atp-week.open .atp-week-content {{
            display: block;
        }}

        .atp-workouts {{
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 8px;
            margin: 16px 0;
        }}

        .atp-day {{
            border: 2px solid var(--gg-border);
            padding: 10px;
            font-size: 12px;
            min-height: 90px;
            background: #fff;
            cursor: pointer;
            transition: all 0.15s ease;
        }}

        .atp-day:hover {{
            background: #f5f5f5;
            transform: translateY(-2px);
            box-shadow: 2px 2px 0 #000;
        }}

        .atp-day-name {{
            font-weight: 700;
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 0.08em;
            margin-bottom: 8px;
            padding-bottom: 6px;
            border-bottom: 1px solid #ddd;
            color: #333;
        }}

        .atp-day.key-day {{
            background: #000;
            color: #fff;
        }}

        .atp-day.key-day .atp-day-name {{
            color: #fff;
            border-bottom-color: #444;
        }}

        .atp-day.key-day .atp-workout-item {{
            color: #fff;
        }}

        .atp-day.strength-day {{
            background: #f0f0f0;
            border-style: dashed;
        }}

        .atp-day.strength-day .atp-day-name {{
            color: #666;
        }}

        .atp-workout-item {{
            margin: 6px 0;
            font-size: 11px;
            font-weight: 500;
            color: #333;
        }}

        .atp-workout-item.strength {{
            color: #666;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 4px;
        }}

        .atp-workout-item.strength::before {{
            content: '💪';
            font-size: 12px;
        }}

        .atp-focus {{
            margin-top: 16px;
            padding-top: 12px;
            border-top: 1px dashed var(--gg-border);
            font-size: 13px;
        }}

        .atp-focus strong {{
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 0.05em;
        }}

        /* WORKOUT MODAL */
        .workout-modal {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.8);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }}

        .workout-modal.open {{
            display: flex;
        }}

        .workout-modal-content {{
            background: #fff;
            border: 3px solid #000;
            max-width: 600px;
            width: 90%;
            max-height: 80vh;
            overflow-y: auto;
            padding: 24px;
        }}

        .workout-modal-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
            padding-bottom: 12px;
            border-bottom: 2px solid #000;
        }}

        .workout-modal-header h3 {{
            margin: 0;
            font-size: 16px;
            text-transform: uppercase;
            letter-spacing: 0.1em;
        }}

        .workout-modal-close {{
            background: #000;
            color: #fff;
            border: none;
            width: 32px;
            height: 32px;
            font-size: 18px;
            cursor: pointer;
            font-family: inherit;
        }}

        .workout-modal-close:hover {{
            background: #333;
        }}

        .workout-detail {{
            margin: 12px 0;
        }}

        .workout-detail-label {{
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            color: #666;
            margin-bottom: 4px;
        }}

        /* RACE ACCORDION */
        .race-accordion {{
            margin: 20px 0;
        }}

        .race-accordion-item {{
            border: 2px solid var(--gg-border);
            margin-bottom: -2px;
        }}

        .race-accordion-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 16px 20px;
            cursor: pointer;
            background: #fff;
            transition: background 0.15s;
        }}

        .race-accordion-header:hover {{
            background: #f5f5f5;
        }}

        .race-accordion-title {{
            display: flex;
            align-items: center;
            gap: 16px;
        }}

        .race-priority {{
            width: 32px;
            height: 32px;
            border: 3px solid #000;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 14px;
        }}

        .race-priority.a {{ background: #000; color: #fff; }}
        .race-priority.b {{ background: #666; color: #fff; }}
        .race-priority.c {{ background: #fff; color: #000; }}

        .race-name {{
            font-weight: 600;
            font-size: 14px;
        }}

        .race-date {{
            font-size: 12px;
            color: #666;
        }}

        .race-accordion-toggle {{
            font-size: 20px;
            font-weight: 700;
            transition: transform 0.2s;
        }}

        .race-accordion-item.open .race-accordion-toggle {{
            transform: rotate(45deg);
        }}

        .race-accordion-content {{
            display: none;
            padding: 20px;
            border-top: 1px solid var(--gg-border);
            background: #f9f9f9;
        }}

        .race-accordion-item.open .race-accordion-content {{
            display: block;
        }}

        .race-details-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 16px;
        }}

        .race-detail {{
            background: #fff;
            border: 1px solid #ddd;
            padding: 12px;
        }}

        .race-detail-label {{
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: #666;
            margin-bottom: 4px;
        }}

        .race-detail-value {{
            font-size: 14px;
            font-weight: 600;
        }}

        /* NUTRITION SECTION */
        .nutrition-calc {{
            border: 2px solid var(--gg-border);
            padding: 20px;
            margin: 20px 0;
            background: var(--gg-soft);
        }}

        .nutrition-macros {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 12px;
            margin: 16px 0;
        }}

        .macro-box {{
            border: 2px solid var(--gg-border);
            padding: 16px;
            text-align: center;
            background: #fff;
        }}

        .macro-value {{
            font-size: 28px;
            font-weight: 700;
            display: block;
        }}

        .macro-unit {{
            font-size: 12px;
            color: #666;
        }}

        .macro-label {{
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            color: #666;
            margin-top: 4px;
        }}

        @media (max-width: 768px) {{
            .atp-workouts {{
                grid-template-columns: repeat(2, 1fr);
            }}
            .nutrition-macros {{
                grid-template-columns: repeat(2, 1fr);
            }}
        }}

        @media (max-width: 480px) {{
            .atp-workouts {{
                grid-template-columns: 1fr;
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
        sections.append(self._generate_race_timeline())
        sections.append(self._generate_your_goals())
        sections.append(self._generate_training_philosophy())
        sections.append(self._generate_blindspots())
        sections.append(self._generate_atp_table())
        sections.append(self._generate_your_weekly_schedule())
        sections.append(self._generate_phase_progression())
        sections.append(self._generate_training_fundamentals())
        sections.append(self._generate_training_zones())
        sections.append(self._generate_workout_execution())
        sections.append(self._generate_strength_program())
        sections.append(self._generate_nutrition_section())
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
            ("race-timeline", "Your Race Calendar"),
            ("your-goals", "Your Goals"),
            ("training-philosophy", "Your Training Philosophy"),
            ("blindspots", "Your Blindspots"),
            ("atp", "24-Week Training Plan"),
            ("your-schedule", "Your Weekly Schedule"),
            ("phase-progression", "Phase Progression"),
            ("training-fundamentals", "Training Fundamentals"),
            ("training-zones", "Training Zones"),
            ("workout-execution", "Workout Execution"),
            ("strength-program", "Your Strength Program"),
            ("nutrition", "Your Nutrition Targets"),
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
    
    def _generate_race_timeline(self) -> str:
        """Generate accordion-style race calendar showing A/B/C events."""
        target_race = self.profile.get('target_race', {})
        a_events = self.profile.get('a_events', [])
        b_events = self.profile.get('b_events', [])
        c_events = self.profile.get('c_events', [])
        
        # If no structured events, use target_race as A event
        if not a_events and target_race.get('name'):
            a_events = [{
                'name': target_race.get('name', 'A Race'),
                'date': target_race.get('date', ''),
                'distance': target_race.get('distance', target_race.get('distance_miles', '')),
                'distance_unit': target_race.get('distance_unit', 'miles'),
                'goal': target_race.get('goal_type', 'compete'),
                'elevation': target_race.get('elevation', ''),
                'notes': target_race.get('notes', '')
            }]
        
        # Build event list with dates
        all_events = []
        for e in a_events:
            all_events.append({**e, 'priority': 'A', 'priority_class': 'a'})
        for e in b_events:
            all_events.append({**e, 'priority': 'B', 'priority_class': 'b'})
        for e in c_events:
            all_events.append({**e, 'priority': 'C', 'priority_class': 'c'})
        
        # Sort by date
        def parse_date(d):
            try:
                return datetime.strptime(d.get('date', '2099-12-31'), '%Y-%m-%d')
            except:
                return datetime(2099, 12, 31)
        
        all_events.sort(key=parse_date)
        
        # Calculate weeks until race
        today = datetime.now()
        
        # Build accordion items
        accordion_items = []
        for i, e in enumerate(all_events):
            race_date = parse_date(e)
            weeks_out = max(0, (race_date - today).days // 7)
            
            # Format date nicely
            date_str = race_date.strftime('%B %d, %Y') if e.get('date') else 'TBD'
            
            # Goal description
            goal_desc = {
                'finish': 'Complete the distance',
                'compete': 'Race competitively (top 50%)',
                'podium': 'Podium finish (top 3 AG)',
                'pr': 'Personal record'
            }.get(e.get('goal', ''), e.get('goal', 'TBD'))
            
            open_class = 'open' if i == 0 else ''  # First one open by default
            
            accordion_items.append(f'''
            <div class="race-accordion-item {open_class}">
                <div class="race-accordion-header" onclick="this.parentElement.classList.toggle('open')">
                    <div class="race-accordion-title">
                        <div class="race-priority {e['priority_class']}">{e['priority']}</div>
                        <div>
                            <div class="race-name">{e.get('name', 'Race')}</div>
                            <div class="race-date">{date_str} · {weeks_out} weeks out</div>
                        </div>
                    </div>
                    <span class="race-accordion-toggle">+</span>
                </div>
                <div class="race-accordion-content">
                    <div class="race-details-grid">
                        <div class="race-detail">
                            <div class="race-detail-label">Distance</div>
                            <div class="race-detail-value">{e.get('distance', 'TBD')} {e.get('distance_unit', '')}</div>
                        </div>
                        <div class="race-detail">
                            <div class="race-detail-label">Goal</div>
                            <div class="race-detail-value">{goal_desc}</div>
                        </div>
                        <div class="race-detail">
                            <div class="race-detail-label">Priority</div>
                            <div class="race-detail-value">{e['priority']} Event — {'Peak form' if e['priority'] == 'A' else 'Important' if e['priority'] == 'B' else 'Training race'}</div>
                        </div>
                        <div class="race-detail">
                            <div class="race-detail-label">Taper</div>
                            <div class="race-detail-value">{'Full 2-week taper' if e['priority'] == 'A' else '1-week mini-taper' if e['priority'] == 'B' else 'None — train through'}</div>
                        </div>
                    </div>
                    {f'<p style="margin-top: 12px; font-size: 13px; color: #666;"><strong>Notes:</strong> {e.get("notes", "")}</p>' if e.get('notes') else ''}
                </div>
            </div>
            ''')
        
        accordion_html = "\n".join(accordion_items) if accordion_items else '<p>No race events defined. Add your target races to personalize this section.</p>'
        
        return f'''
<section id="race-timeline">
    <h2>2 · Your Race Calendar</h2>
    
    <p>Click any race to expand details. A events are your primary targets — everything else serves these races.</p>
    
    <div class="race-accordion">
{accordion_html}
    </div>
    
    <div class="callout info">
        <h4>The Universal Race Framework</h4>
        <p><strong>A Events (1-2/year):</strong> You peak for these. Full taper. Best form. Everything else serves these races.</p>
        <p><strong>B Events (3-5/year):</strong> Important, but you don't compromise A event prep. Shorter taper, maybe not fully peaked.</p>
        <p><strong>C Events (unlimited):</strong> Training opportunities. Show up undertrained on purpose. Use them to practice race execution, test equipment, build experience.</p>
    </div>
</section>
'''
    
    def _generate_your_goals(self) -> str:
        """Generate section showing athlete's stated goals."""
        target_race = self.profile.get('target_race', {})
        primary_goal = self.profile.get('primary_goal', 'specific_race')
        goal_type = target_race.get('goal_type', 'compete')
        
        # Additional goals from profile
        other_goals = self.profile.get('other_goals', [])
        
        # Infer goals from profile data
        inferred_goals = []
        
        # Time goal
        target_time = target_race.get('target_time')
        if target_time:
            inferred_goals.append({
                'icon': '⏱',
                'title': f'Finish in {target_time}',
                'desc': 'Time-based goal for your A event'
            })
        
        # Goal type
        goal_descriptions = {
            'finish': ('🏁', 'Complete the Distance', 'Cross that finish line. Everything else is gravy.'),
            'compete': ('🎯', 'Race Competitively', 'Finish in the top half of your age group. Proper racing.'),
            'podium': ('🏆', 'Podium Finish', 'Top 3 in age group. Elite performance.'),
            'pr': ('📈', 'Personal Record', 'Beat your previous best time or performance.')
        }
        
        if goal_type in goal_descriptions:
            icon, title, desc = goal_descriptions[goal_type]
            inferred_goals.append({'icon': icon, 'title': title, 'desc': desc})
        
        # FTP goal
        ftp = self.profile.get('fitness_markers', {}).get('ftp_watts')
        weight = self.profile.get('fitness_markers', {}).get('weight_kg')
        if ftp and weight:
            current_wpkg = ftp / weight
            target_wpkg = current_wpkg * 1.1  # 10% improvement
            inferred_goals.append({
                'icon': '⚡',
                'title': f'Improve W/kg from {current_wpkg:.1f} → {target_wpkg:.1f}',
                'desc': 'Realistic fitness progression over the plan'
            })
        
        # Strength goal
        strength_bg = self.profile.get('training_history', {}).get('strength_background', '')
        if strength_bg in ['none', 'beginner']:
            inferred_goals.append({
                'icon': '💪',
                'title': 'Build Strength Foundation',
                'desc': 'Establish consistent strength habit and movement competency'
            })
        elif strength_bg == 'intermediate':
            inferred_goals.append({
                'icon': '💪',
                'title': 'Develop Cycling-Specific Power',
                'desc': 'Convert gym strength into on-bike performance'
            })
        
        # Build goal cards
        goal_cards = []
        for g in inferred_goals:
            goal_cards.append(f'''
            <div class="goal-card">
                <div class="goal-icon">{g['icon']}</div>
                <div class="goal-content">
                    <h4>{g['title']}</h4>
                    <p>{g['desc']}</p>
                </div>
            </div>
            ''')
        
        goals_html = "\n".join(goal_cards) if goal_cards else '<p>No specific goals defined.</p>'
        
        return f'''
<section id="your-goals">
    <h2>3 · Your Goals</h2>
    
    <p>Based on your intake, here's what you're training for:</p>
    
{goals_html}
    
    <div class="callout tip">
        <h4>Goal Hierarchy</h4>
        <p>When goals conflict, prioritize in this order:</p>
        <ol>
            <li><strong>Health</strong> — No goal is worth injury</li>
            <li><strong>Consistency</strong> — Showing up beats heroics</li>
            <li><strong>A Event Performance</strong> — The main target</li>
            <li><strong>Secondary Goals</strong> — Nice to have</li>
        </ol>
    </div>
</section>
'''
    
    def _generate_training_philosophy(self) -> str:
        """Generate training philosophy explanation based on tier."""
        tier = self.derived.get('tier', 'compete').lower()
        
        # Philosophy varies by tier
        philosophies = {
            'ayahuasca': {
                'name': 'Minimum Effective Dose',
                'easy_pct': 85,
                'hard_pct': 15,
                'desc': 'Maximum results from minimum time investment. Every workout counts.',
                'principles': [
                    'Quality over quantity — no junk miles',
                    'Two key sessions per week, everything else easy',
                    'Strength training is mandatory, not optional',
                    'Recovery is the priority between sessions'
                ]
            },
            'finisher': {
                'name': 'Polarized Foundation',
                'easy_pct': 80,
                'hard_pct': 20,
                'desc': 'Build aerobic base with strategic intensity. Classic endurance approach.',
                'principles': [
                    '80% easy / 20% hard intensity distribution',
                    'Long rides build the engine',
                    'Two quality sessions per week',
                    'Strength supports, doesn\'t replace, bike work'
                ]
            },
            'compete': {
                'name': 'Polarized Performance',
                'easy_pct': 80,
                'hard_pct': 20,
                'desc': 'Proven approach for competitive endurance athletes. Easy truly easy, hard truly hard.',
                'principles': [
                    'Strict 80/20 polarized distribution',
                    'Three key sessions in peak weeks',
                    'Race-specific intervals in Build phase',
                    'Integrated strength for power and resilience'
                ]
            },
            'podium': {
                'name': 'Elite Periodization',
                'easy_pct': 75,
                'hard_pct': 25,
                'desc': 'High-volume foundation with aggressive intensity blocks. For serious competitors.',
                'principles': [
                    'Higher training load requires precise recovery',
                    'Block periodization for specific adaptations',
                    'Race simulation and heat/altitude prep',
                    'Strength maintains power-to-weight'
                ]
            }
        }
        
        p = philosophies.get(tier, philosophies['compete'])
        
        easy_width = p['easy_pct'] * 3  # Scale for visual
        hard_width = p['hard_pct'] * 3
        
        return f'''
<section id="training-philosophy">
    <h2>4 · Your Training Philosophy</h2>
    
    <p>Your plan follows <strong>{p['name']}</strong> — the approach best suited to your tier ({tier.upper()}) and goals.</p>
    
    <div class="philosophy-framework">
        <h3 style="margin-top: 0;">Intensity Distribution</h3>
        
        <div class="philosophy-diagram">
            <div class="philosophy-bar bar-easy" style="width: {easy_width}px;">EASY {p['easy_pct']}%</div>
            <div class="philosophy-bar bar-hard" style="width: {hard_width}px;">HARD {p['hard_pct']}%</div>
        </div>
        
        <p style="text-align: center; font-size: 13px; color: var(--gg-muted);">{p['desc']}</p>
    </div>
    
    <h3>Core Principles</h3>
    <ol>
        {"".join([f"<li>{principle}</li>" for principle in p['principles']])}
    </ol>
    
    <h3>Why This Works</h3>
    
    <p><strong>The science:</strong> Decades of research on elite endurance athletes consistently shows that ~80% easy / ~20% hard produces better results than "moderate" training (threshold grinding). The easy work builds aerobic capacity without accumulating fatigue. The hard work drives specific adaptations.</p>
    
    <p><strong>The mistake most people make:</strong> Going too hard on easy days (turning Z2 into Z3) and not hard enough on hard days (surviving intervals instead of executing them). This creates a "grey zone" that's too hard to recover from but not hard enough to adapt to.</p>
    
    <div class="callout alert">
        <h4>The Hard Truth</h4>
        <p>Easy days should feel embarrassingly easy. If you're proud of how hard you went on an "easy" day, you did it wrong. Save that energy for the sessions that matter.</p>
    </div>
</section>
'''
    
    def _generate_blindspots(self) -> str:
        """Generate personalized blindspots based on profile analysis."""
        blindspots = []
        
        # Analyze profile for potential blindspots
        
        # 1. Recovery capacity
        sleep_quality = self.profile.get('health_factors', {}).get('sleep_quality', '')
        sleep_hours = self.profile.get('health_factors', {}).get('sleep_hours_avg', 7)
        if sleep_quality in ['poor', 'fair'] or (isinstance(sleep_hours, (int, float)) and sleep_hours < 7):
            blindspots.append({
                'severity': 'high',
                'title': 'Recovery Deficit',
                'issue': f'You reported {sleep_quality} sleep quality and ~{sleep_hours} hours/night.',
                'risk': 'Poor sleep limits adaptation and increases injury risk. You\'ll accumulate fatigue faster than you recover from it.',
                'action': 'Prioritize sleep above all else. Consider reducing training volume by 10-15% until sleep improves. No amount of training overcomes poor recovery.'
            })
        
        # 2. Stress load
        stress_level = self.profile.get('health_factors', {}).get('stress_level', '')
        if stress_level in ['high', 'very_high']:
            blindspots.append({
                'severity': 'high',
                'title': 'Life Stress Overload',
                'issue': f'You reported {stress_level.replace("_", " ")} stress levels.',
                'risk': 'Training is a stressor. Life stress + training stress = total stress. High total stress leads to overtraining, illness, and burnout.',
                'action': 'Monitor HRV and resting heart rate closely. Be willing to skip hard sessions during stressful weeks. The plan adapts to you, not vice versa.'
            })
        
        # 3. Strength background
        strength_bg = self.profile.get('training_history', {}).get('strength_background', '')
        if strength_bg in ['none', 'beginner']:
            blindspots.append({
                'severity': 'medium',
                'title': 'Movement Quality Gap',
                'issue': f'You\'re new to structured strength training ({strength_bg}).',
                'risk': 'Poor movement patterns lead to injury, especially under fatigue. Strength exercises performed incorrectly do more harm than good.',
                'action': 'Watch EVERY video demo before attempting exercises. Start lighter than you think necessary. Master movement before adding load. If something hurts, stop.'
            })
        
        # 4. Injury history
        injuries = self.profile.get('injury_history', {}).get('current_injuries', [])
        past_injuries = self.profile.get('injury_history', {}).get('past_injuries', [])
        if injuries or past_injuries:
            injury_areas = set()
            for inj in injuries + past_injuries:
                if isinstance(inj, dict):
                    injury_areas.add(inj.get('area', 'unknown'))
                elif isinstance(inj, str):
                    injury_areas.add(inj)
            
            if injury_areas:
                blindspots.append({
                    'severity': 'high' if injuries else 'medium',
                    'title': 'Injury Management Required',
                    'issue': f'History of issues with: {", ".join(injury_areas)}.',
                    'risk': 'Past injuries often become recurring injuries. The tissue is weaker and the movement pattern may be compromised.',
                    'action': 'Modified exercises are provided in your plan. If pain returns, stop immediately and consult a professional. Prevention >>> treatment.'
                })
        
        # 5. Limited equipment
        equipment_tier = self.derived.get('equipment_tier', '')
        if equipment_tier == 'minimal':
            blindspots.append({
                'severity': 'low',
                'title': 'Equipment Limitations',
                'issue': 'You have minimal strength equipment available.',
                'risk': 'Some exercises will require substitutions. Progression may plateau earlier without heavier loads.',
                'action': 'Bodyweight progressions can take you far. Consider adding a single kettlebell or adjustable dumbbells for more options. Resistance bands are cheap and versatile.'
            })
        
        # 6. Schedule constraints
        weekly_avail = self.profile.get('weekly_availability', {})
        total_hours = weekly_avail.get('total_hours_available', 0)
        if isinstance(total_hours, (int, float)) and total_hours < 8:
            blindspots.append({
                'severity': 'medium',
                'title': 'Time-Crunched Reality',
                'issue': f'You have ~{total_hours} hours/week available for training.',
                'risk': 'Limited time means every session must count. There\'s less margin for error or missed workouts.',
                'action': 'Prioritize ruthlessly. Never skip a key session. Be willing to shorten easy rides. Indoor training is your friend for time efficiency.'
            })
        
        # 7. Age considerations
        age = self._calculate_age()
        if age and age >= 45:
            blindspots.append({
                'severity': 'medium',
                'title': 'Masters Recovery Window',
                'issue': f'At {age}, recovery physiology has changed.',
                'risk': 'What worked at 25 doesn\'t work at 45+. Ignoring this leads to persistent fatigue, illness, and injury.',
                'action': 'Extra rest day every 2-3 weeks. Sleep becomes even more critical. Strength training is mandatory for maintaining fast-twitch fibers.'
            })
        
        # 8. Underfueling risk
        nutrition = self.profile.get('nutrition', {})
        daily_carbs = nutrition.get('daily_carbs_g_per_kg', 0)
        fueling_during = nutrition.get('fuels_during_rides', '')
        eating_disorder_history = nutrition.get('eating_disorder_history', False)
        weight_loss_goal = self.profile.get('weight_loss_goal', False)
        
        # Check multiple underfueling signals
        underfueling_signals = []
        if isinstance(daily_carbs, (int, float)) and daily_carbs > 0 and daily_carbs < 3:
            underfueling_signals.append(f'low daily carb intake ({daily_carbs}g/kg)')
        if fueling_during in ['never', 'rarely']:
            underfueling_signals.append('rarely fueling during rides')
        if eating_disorder_history:
            underfueling_signals.append('history of disordered eating')
        if weight_loss_goal:
            underfueling_signals.append('active weight loss goal during training')
        
        # Also check training hours vs reported eating
        cycling_hours = self.profile.get('weekly_availability', {}).get('cycling_hours_target', 0)
        if isinstance(cycling_hours, (int, float)) and cycling_hours > 10 and not fueling_during:
            underfueling_signals.append('high training volume without mentioned fueling strategy')
        
        if underfueling_signals:
            blindspots.append({
                'severity': 'high' if eating_disorder_history or len(underfueling_signals) >= 2 else 'medium',
                'title': 'Underfueling Risk',
                'issue': f'Signals detected: {"; ".join(underfueling_signals)}.',
                'risk': 'Chronic underfueling leads to RED-S (Relative Energy Deficiency in Sport): hormonal disruption, bone loss, immune suppression, declining performance despite hard training. It\'s the silent killer of endurance careers.',
                'action': 'Fuel FOR the work, not against it. Minimum 3g carbs/kg daily, more on hard days. Eat before/during/after key sessions. Weight loss and peak performance don\'t happen simultaneously.'
            })
        
        # 9. Alcohol consumption
        alcohol = self.profile.get('lifestyle', {}).get('alcohol_drinks_per_week', 0)
        if not alcohol:
            alcohol = nutrition.get('alcohol_drinks_per_week', 0)
        
        if isinstance(alcohol, (int, float)):
            if alcohol >= 14:
                blindspots.append({
                    'severity': 'high',
                    'title': 'Alcohol Impact on Recovery',
                    'issue': f'You reported ~{int(alcohol)} drinks per week.',
                    'risk': 'Alcohol directly impairs protein synthesis, disrupts sleep architecture, dehydrates, and blunts growth hormone release. At this level, you\'re significantly compromising adaptation.',
                    'action': 'Consider cutting to <7 drinks/week during heavy training blocks. Never drink within 3 hours of sleep. Avoid alcohol on nights before key sessions. This single change could be your biggest performance gain.'
                })
            elif alcohol >= 7:
                blindspots.append({
                    'severity': 'medium',
                    'title': 'Alcohol Affecting Recovery',
                    'issue': f'You reported ~{int(alcohol)} drinks per week.',
                    'risk': 'Even moderate alcohol consumption impairs sleep quality and recovery. One drink after a hard workout can reduce protein synthesis by 24%.',
                    'action': 'Front-load drinking to early week if social. Keep nights before hard sessions alcohol-free. Consider "dry training blocks" during Build and Peak phases.'
                })
        
        # 10. Caffeine
        caffeine = self.profile.get('lifestyle', {}).get('caffeine_mg_per_day', 0)
        if not caffeine:
            caffeine = nutrition.get('caffeine_mg_per_day', 0)
        # Also check cups of coffee as proxy
        coffee_cups = self.profile.get('lifestyle', {}).get('coffee_cups_per_day', 0)
        if not caffeine and coffee_cups:
            caffeine = coffee_cups * 95  # ~95mg per cup
        
        if isinstance(caffeine, (int, float)):
            if caffeine >= 500:
                blindspots.append({
                    'severity': 'high',
                    'title': 'Caffeine Dependency',
                    'issue': f'You\'re consuming ~{int(caffeine)}mg caffeine daily ({int(caffeine/95)} cups of coffee equivalent).',
                    'risk': 'At this level, caffeine no longer provides performance benefits—you\'re just maintaining baseline. Sleep is likely compromised. You\'ve lost the ability to use caffeine strategically on race day.',
                    'action': 'Gradually reduce to <300mg/day. Cut off caffeine by 2pm. Consider a 2-week caffeine reset before your A event so race-day caffeine actually works.'
                })
            elif caffeine >= 400:
                blindspots.append({
                    'severity': 'medium',
                    'title': 'Caffeine Tolerance',
                    'issue': f'You\'re at ~{int(caffeine)}mg caffeine daily.',
                    'risk': 'You\'re near the ceiling where additional caffeine stops helping. Sleep quality may be subtly compromised even if you "fall asleep fine."',
                    'action': 'Keep afternoon caffeine minimal. Consider reducing intake 4-6 weeks before A event to restore sensitivity for race-day boost.'
                })
        
        # Build blindspot cards
        if not blindspots:
            blindspots.append({
                'severity': 'low',
                'title': 'No Major Blindspots Identified',
                'issue': 'Your profile doesn\'t reveal obvious risk factors.',
                'risk': 'This doesn\'t mean you\'re invincible. Stay vigilant for the unexpected.',
                'action': 'Monitor your body\'s signals. If something feels off, investigate before it becomes a problem.'
            })
        
        blindspot_cards = []
        for bs in blindspots:
            blindspot_cards.append(f'''
            <div class="blindspot {bs['severity']}">
                <div class="blindspot-header">
                    <h4>{bs['title']}</h4>
                    <span class="blindspot-severity">{bs['severity']}</span>
                </div>
                <p><strong>What we noticed:</strong> {bs['issue']}</p>
                <p><strong>The risk:</strong> {bs['risk']}</p>
                <p><strong>Your action:</strong> {bs['action']}</p>
            </div>
            ''')
        
        return f'''
<section id="blindspots">
    <h2>5 · Your Blindspots</h2>
    
    <p>Based on your intake, these are potential risks to be aware of. Forewarned is forearmed.</p>
    
{"".join(blindspot_cards)}
    
    <div class="callout info">
        <h4>Why This Section Exists</h4>
        <p>Everyone has blindspots. The difference between successful athletes and injured/burned-out ones is often awareness of their vulnerabilities, not raw talent or work ethic.</p>
        <p>Review this section monthly. Your blindspots can change as life circumstances shift.</p>
    </div>
</section>
'''
    
    def _generate_atp_table(self) -> str:
        """Generate interactive Annual Training Plan table."""
        plan_weeks = self.derived.get('plan_weeks', 12)
        
        # Determine phases based on plan length
        if plan_weeks >= 20:
            phase_ranges = [
                (1, 4, 'Base', 'base'),
                (5, 12, 'Build', 'build'),
                (13, 18, 'Peak', 'peak'),
                (19, plan_weeks, 'Taper', 'taper')
            ]
        elif plan_weeks >= 12:
            phase_ranges = [
                (1, 3, 'Base', 'base'),
                (4, 7, 'Build', 'build'),
                (8, 10, 'Peak', 'peak'),
                (11, plan_weeks, 'Taper', 'taper')
            ]
        else:
            phase_ranges = [
                (1, 2, 'Base', 'base'),
                (3, 5, 'Build', 'build'),
                (6, plan_weeks - 1, 'Peak', 'peak'),
                (plan_weeks, plan_weeks, 'Taper', 'taper')
            ]
        
        def get_phase(week):
            for start, end, name, cls in phase_ranges:
                if start <= week <= end:
                    return name, cls
            return 'Build', 'build'
        
        # Week descriptions
        week_focuses = {
            'Base': [
                'Building aerobic foundation. Long Z2 rides establish mitochondrial density.',
                'Movement quality in strength. Learn the patterns before adding load.',
                'Establishing rhythm and consistency. Show up, do the work.',
                'Progressive volume increase. Each week slightly more than the last.'
            ],
            'Build': [
                'Adding race-specific intensity. G-Spot intervals introduce discomfort.',
                'Strength shifts to heavier loads. Building max strength.',
                'Volume peaks. This is the hardest training block.',
                'Recovery week every 3-4 weeks. Absorb the training.',
                'Race simulation workouts. Practice execution under fatigue.',
                'Final volume push before taper begins.'
            ],
            'Peak': [
                'Highest intensity, slightly reduced volume.',
                'Race pace work. Dialing in the exact effort you\'ll use.',
                'Strength shifts to power. Fast, explosive movements.',
                'Last hard weeks. Trust the fitness you\'ve built.'
            ],
            'Taper': [
                'Volume drops significantly. Intensity stays sharp.',
                'Strength maintains, doesn\'t build. Don\'t lose adaptations.',
                'Freshening up. The hay is in the barn.',
                'Race week. Execute the plan.'
            ]
        }
        
        # Build week rows
        week_rows = []
        for week in range(1, plan_weeks + 1):
            phase_name, phase_class = get_phase(week)
            
            # Get focus text
            phase_week_num = week - [r[0] for r in phase_ranges if r[2] == phase_name][0]
            focuses = week_focuses.get(phase_name, [])
            focus_text = focuses[phase_week_num % len(focuses)] if focuses else 'Progressive training.'
            
            # Determine if recovery week (every 4th week typically)
            is_recovery = week % 4 == 0 and phase_name not in ['Taper']
            volume_label = 'Recovery' if is_recovery else ['Low', 'Medium', 'High', 'Peak'][min(3, (week % 4))]
            
            # Generate day structure
            days_html = self._generate_atp_week_days(week, phase_name)
            
            week_rows.append(f'''
            <div class="atp-week" data-week="{week}">
                <div class="atp-week-header" onclick="this.parentElement.classList.toggle('open')">
                    <div class="atp-week-title">
                        <span class="atp-week-num">Week {week}</span>
                        <span class="atp-week-phase {phase_class}">{phase_name}</span>
                    </div>
                    <div class="atp-week-meta">
                        <span>{volume_label}</span>
                    </div>
                    <span class="atp-week-toggle">+</span>
                </div>
                <div class="atp-week-content">
{days_html}
                    <div class="atp-focus">
                        <strong>Focus:</strong> {focus_text}
                    </div>
                </div>
            </div>
            ''')
        
        return f'''
<section id="atp">
    <h2>6 · {plan_weeks}-Week Training Plan</h2>
    
    <p>Click any week to expand and see the workout structure. This is your high-level roadmap.</p>
    
    <div class="atp-container">
{"".join(week_rows)}
    </div>
    
    <div class="callout tip">
        <h4>How to Read This</h4>
        <ul>
            <li><strong>Phase badges</strong> show where you are in periodization</li>
            <li><strong>Volume labels</strong> indicate training load (Recovery weeks every ~4 weeks)</li>
            <li><strong>KEY days</strong> are your priority sessions — never skip these</li>
            <li>Actual workouts are in your ZWO files and calendar</li>
        </ul>
    </div>
</section>

<script>
// Auto-open current week based on plan start date
document.addEventListener('DOMContentLoaded', function() {{
    // Could calculate current week and auto-open it
    // For now, leave all collapsed
}});
</script>
'''
    
    def _generate_atp_week_days(self, week: int, phase: str) -> str:
        """Generate the day-by-day structure for an ATP week with clickable days."""
        
        # Use weekly structure if available
        if self.weekly_structure:
            days = self.weekly_structure.get('days', {})
            day_order = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
            
            day_cells = []
            for day_name in day_order:
                schedule = days.get(day_name, {})
                am = schedule.get('am')
                pm = schedule.get('pm')
                is_key = schedule.get('is_key_day', False)
                is_strength = am == 'strength' or pm == 'strength'
                
                workouts = []
                if am:
                    workout_class = 'strength' if am == 'strength' else ''
                    workouts.append(f'<div class="atp-workout-item {workout_class}">{am.replace("_", " ").title()}</div>')
                if pm:
                    workout_class = 'strength' if pm == 'strength' else ''
                    workouts.append(f'<div class="atp-workout-item {workout_class}">{pm.replace("_", " ").title()} (PM)</div>')
                
                if not workouts:
                    workouts.append('<div class="atp-workout-item" style="color: #999;">Rest</div>')
                
                # Determine day class
                day_class = 'key-day' if is_key else ('strength-day' if is_strength else '')
                
                # Create modal data
                modal_data = f'data-week="{week}" data-day="{day_name}" data-phase="{phase}" data-am="{am or ""}" data-pm="{pm or ""}"'
                
                day_cells.append(f'''
                    <div class="atp-day {day_class}" {modal_data} onclick="showWorkoutModal(this)">
                        <div class="atp-day-name">{day_name[:3].upper()}</div>
                        {"".join(workouts)}
                    </div>
                ''')
            
            return f'''
                    <div class="atp-workouts">
{"".join(day_cells)}
                    </div>
            '''
        
        # Fallback: generic structure based on phase
        generic_days = {
            'Base': ['Rest', 'Easy Ride', 'Strength', 'Tempo', 'Strength', 'Long Ride', 'Easy Ride'],
            'Build': ['Strength', 'Intervals', 'Easy Ride', 'Strength', 'Easy Ride', 'Long Ride', 'Recovery'],
            'Peak': ['Strength', 'VO2 Intervals', 'Easy Ride', 'Race Pace', 'Rest', 'Long Ride', 'Easy Ride'],
            'Taper': ['Strength', 'Openers', 'Rest', 'Easy Ride', 'Rest', 'Race/Easy', 'Rest']
        }
        
        day_names = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']
        full_day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        workouts = generic_days.get(phase, generic_days['Build'])
        key_days = [1, 3, 5] if phase in ['Build', 'Peak'] else [3, 5]
        
        day_cells = []
        for i, (name, full_name, workout) in enumerate(zip(day_names, full_day_names, workouts)):
            is_key = i in key_days and workout not in ['Rest', 'Recovery', 'Easy Ride']
            is_strength = 'Strength' in workout
            
            day_class = 'key-day' if is_key else ('strength-day' if is_strength else '')
            workout_class = 'strength' if is_strength else ''
            
            modal_data = f'data-week="{week}" data-day="{full_name}" data-phase="{phase}" data-am="{workout}" data-pm=""'
            
            day_cells.append(f'''
                <div class="atp-day {day_class}" {modal_data} onclick="showWorkoutModal(this)">
                    <div class="atp-day-name">{name}</div>
                    <div class="atp-workout-item {workout_class}">{workout}</div>
                </div>
            ''')
        
        return f'''
                    <div class="atp-workouts">
{"".join(day_cells)}
                    </div>
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
    
    def _generate_nutrition_section(self) -> str:
        """Generate personalized nutrition targets based on athlete questionnaire data."""
        
        # Pull from questionnaire data - multiple fallback sources
        weight_kg = (
            self.profile.get('weight_kg') or 
            self.profile.get('fitness_markers', {}).get('weight_kg') or 
            70
        )
        
        height_cm = (
            self.profile.get('height_cm') or 
            self.profile.get('demographics', {}).get('height_cm') or
            175
        )
        
        ftp = (
            self.profile.get('fitness_markers', {}).get('ftp_watts') or
            self.profile.get('ftp_watts') or 
            200
        )
        
        cycling_hours = (
            self.profile.get('weekly_availability', {}).get('cycling_hours_target') or
            self.profile.get('training_history', {}).get('current_weekly_hours') or
            10
        )
        
        age = self._calculate_age() or self.profile.get('health_factors', {}).get('age') or 35
        sex = self.profile.get('sex', 'male').lower()
        
        # Activity level from lifestyle questionnaire
        active_job = self.profile.get('lifestyle', {}).get('active_job', False)
        activity_level = 'moderately_active' if active_job else 'sedentary'
        
        # Weight goal affects calorie target
        weight_goal = self.profile.get('lifestyle', {}).get('weight_goal', 'maintain')
        
        # Calculate BMR using Mifflin-St Jeor equation
        if sex == 'male':
            bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) + 5
        else:
            bmr = (10 * weight_kg) + (6.25 * height_cm) - (5 * age) - 161
        
        # Activity multiplier for non-training expenditure
        activity_multipliers = {
            'sedentary': 1.2,
            'lightly_active': 1.375,
            'moderately_active': 1.55,
            'very_active': 1.725
        }
        base_tdee = bmr * activity_multipliers.get(activity_level, 1.2)
        
        # Average daily training expenditure
        avg_daily_hours = cycling_hours / 7
        intensity_factor = 0.7  # Typical training IF
        avg_power = ftp * intensity_factor
        cycling_kcal = (avg_power / 75) * 5 * avg_daily_hours * 60
        
        # Total daily expenditure
        daily_kcal = base_tdee + cycling_kcal
        
        # Adjust for weight goal
        if weight_goal == 'lose_slow':
            daily_kcal -= 300  # Modest deficit
        elif weight_goal == 'lose_fast':
            daily_kcal -= 500  # More aggressive (not recommended during hard training)
        elif weight_goal == 'gain':
            daily_kcal += 300  # Slight surplus
        
        # Standard endurance athlete macro split based on training volume
        # CHO: 5-8g/kg based on training volume
        if cycling_hours <= 6:
            cho_per_kg = 4.5
        elif cycling_hours <= 10:
            cho_per_kg = 5.5
        elif cycling_hours <= 15:
            cho_per_kg = 6.5
        else:
            cho_per_kg = 7.5
        
        cho_grams = int(weight_kg * cho_per_kg)
        
        # Protein: Higher for weight loss, older athletes, or high volume
        pro_per_kg = 1.8
        if weight_goal in ['lose_slow', 'lose_fast']:
            pro_per_kg = 2.0  # Higher protein preserves muscle during deficit
        if age >= 50:
            pro_per_kg = max(pro_per_kg, 2.0)  # Masters need more protein
        
        pro_grams = int(weight_kg * pro_per_kg)
        
        # Fat: Minimum 0.8g/kg, remainder of calories
        min_fat = int(weight_kg * 0.8)
        remaining_kcal = daily_kcal - (cho_grams * 4) - (pro_grams * 4)
        fat_grams = max(min_fat, int(remaining_kcal / 9))
        
        # Recalculate total for accuracy
        total_kcal = int((cho_grams * 4) + (pro_grams * 4) + (fat_grams * 9))
        
        # Percentages
        cho_pct = int(cho_grams * 4 / total_kcal * 100)
        pro_pct = int(pro_grams * 4 / total_kcal * 100)
        fat_pct = int(fat_grams * 9 / total_kcal * 100)
        
        # Training day adjustments
        hard_day_kcal = int(total_kcal * 1.15)
        hard_day_cho = int(cho_grams * 1.2)
        easy_day_kcal = int(total_kcal * 0.9)
        easy_day_cho = int(cho_grams * 0.8)
        
        # Weight goal notes
        weight_note = ""
        if weight_goal == 'lose_slow':
            weight_note = "Includes 300 kcal deficit for gradual weight loss."
        elif weight_goal == 'lose_fast':
            weight_note = "⚠️ 500 kcal deficit — monitor energy and recovery closely."
        elif weight_goal == 'gain':
            weight_note = "Includes 300 kcal surplus for muscle building."
        
        return f'''
<section id="nutrition">
    <h2>Your Nutrition Targets</h2>
    
    <p>Calculated from your questionnaire data:</p>
    
    <div class="nutrition-calc" style="margin-bottom: 20px;">
        <h3 style="margin-top: 0; font-size: 12px; color: #666;">Your Stats</h3>
        <table style="margin: 0; font-size: 13px;">
            <tbody>
                <tr><td style="border: none; padding: 4px 16px 4px 0;"><strong>Weight:</strong></td><td style="border: none;">{weight_kg} kg</td></tr>
                <tr><td style="border: none; padding: 4px 16px 4px 0;"><strong>Height:</strong></td><td style="border: none;">{height_cm} cm</td></tr>
                <tr><td style="border: none; padding: 4px 16px 4px 0;"><strong>Age:</strong></td><td style="border: none;">{age} years</td></tr>
                <tr><td style="border: none; padding: 4px 16px 4px 0;"><strong>Sex:</strong></td><td style="border: none;">{sex.title()}</td></tr>
                <tr><td style="border: none; padding: 4px 16px 4px 0;"><strong>FTP:</strong></td><td style="border: none;">{ftp}W ({ftp/weight_kg:.2f} W/kg)</td></tr>
                <tr><td style="border: none; padding: 4px 16px 4px 0;"><strong>Training Volume:</strong></td><td style="border: none;">~{cycling_hours} hrs/week</td></tr>
                <tr><td style="border: none; padding: 4px 16px 4px 0;"><strong>Daily Activity:</strong></td><td style="border: none;">{activity_level.replace('_', ' ').title()}</td></tr>
                <tr><td style="border: none; padding: 4px 16px 4px 0;"><strong>Weight Goal:</strong></td><td style="border: none;">{weight_goal.replace('_', ' ').title()}</td></tr>
            </tbody>
        </table>
    </div>
    
    <div class="nutrition-calc">
        <h3 style="margin-top: 0;">Daily Targets — Average Training Day</h3>
        {f'<p style="font-size: 12px; color: #666; margin-bottom: 16px;">{weight_note}</p>' if weight_note else ''}
        <div class="nutrition-macros">
            <div class="macro-box">
                <span class="macro-value">{total_kcal}</span>
                <span class="macro-unit">kcal</span>
                <div class="macro-label">Total Calories</div>
            </div>
            <div class="macro-box">
                <span class="macro-value">{cho_grams}</span>
                <span class="macro-unit">g ({cho_pct}%)</span>
                <div class="macro-label">Carbs</div>
            </div>
            <div class="macro-box">
                <span class="macro-value">{pro_grams}</span>
                <span class="macro-unit">g ({pro_pct}%)</span>
                <div class="macro-label">Protein</div>
            </div>
            <div class="macro-box">
                <span class="macro-value">{fat_grams}</span>
                <span class="macro-unit">g ({fat_pct}%)</span>
                <div class="macro-label">Fat</div>
            </div>
        </div>
        <p style="font-size: 11px; color: #999; margin-top: 12px; text-align: center;">
            BMR: {int(bmr)} kcal | Base TDEE: {int(base_tdee)} kcal | Training: +{int(cycling_kcal)} kcal/day avg
        </p>
    </div>
    
    <h3>Nutrient Timing</h3>
    
    <p><strong>When you eat matters as much as what you eat.</strong> Aligned with Chapter 8: Fueling & Hydration from your training plan guide.</p>
    
    <div class="callout info">
        <h4>Pre-Workout Nutrition</h4>
        <p><strong>For Hard Sessions (threshold, VO2max, intervals):</strong></p>
        <table style="margin: 12px 0;">
            <thead>
                <tr>
                    <th>Timing</th>
                    <th>What to Eat</th>
                    <th>Amount</th>
                    <th>Examples</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td><strong>2-3 Hours Before</strong></td>
                    <td>Moderate carbs + light protein</td>
                    <td>1-2g carbs/kg<br>({int(weight_kg * 1)}-{int(weight_kg * 2)}g)</td>
                    <td>Oatmeal + banana + honey<br>Toast + peanut butter</td>
                </tr>
                <tr>
                    <td><strong>15-30 Min Before</strong><br><span style="font-size: 10px; color: #666;">Optional</span></td>
                    <td>Fast-digesting carbs</td>
                    <td>20-30g total</td>
                    <td>Banana, sports drink, energy gel</td>
                </tr>
            </tbody>
        </table>
        <p><strong>For Easy Sessions (Z2 endurance):</strong></p>
        <ul style="margin: 8px 0;">
            <li>Eat normally, don't stress timing</li>
            <li>Can even train fasted if under 90 minutes</li>
            <li>Flexible approach — no strict rules</li>
        </ul>
        <p style="font-size: 11px; margin-top: 8px; color: #666;"><strong>Rule:</strong> Hard sessions need fuel. Easy sessions are flexible.</p>
    </div>
    
    <div class="callout tip">
        <h4>During Training — The 60-80g Per Hour Rule</h4>
        <p>For any ride <strong>over 90 minutes at moderate-to-high intensity (Z3+)</strong>, you need <strong>60-80g of carbohydrates per hour.</strong></p>
        <p style="font-size: 12px; color: #666;">Your gut can absorb ~60g glucose/hour. Add fructose (different transporters) to reach 90g. Sweet spot: 70-75g/hour.</p>
        
        <table style="margin: 12px 0;">
            <thead>
                <tr>
                    <th>Session Type</th>
                    <th>Duration</th>
                    <th>Carbs/Hour</th>
                    <th>When to Start</th>
                    <th>What to Use</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td>Z2 Endurance</td>
                    <td>2-4 hours</td>
                    <td>40-60g</td>
                    <td>After 60 min</td>
                    <td>Real food: PB&J, bananas, bars</td>
                </tr>
                <tr>
                    <td>Tempo/G-Spot</td>
                    <td>2-3 hours</td>
                    <td>60-80g</td>
                    <td>Start at 30-45 min</td>
                    <td>Mix: liquids + solids</td>
                </tr>
                <tr>
                    <td>Threshold/VO2max</td>
                    <td>60-90 min</td>
                    <td>Pre-workout sufficient<br>+ 1 gel mid-session</td>
                    <td>Between efforts</td>
                    <td>Gel or sports drink</td>
                </tr>
                <tr>
                    <td>Race/Long hard</td>
                    <td>&gt;90 min</td>
                    <td>60-90g</td>
                    <td>Start at 30 min</td>
                    <td>Mix: drinks + gels/chews<br>(2:1 glucose:fructose if &gt;60g)</td>
                </tr>
            </tbody>
        </table>
        <p style="font-size: 11px; margin-top: 8px; color: #666;"><strong>Critical:</strong> Start fueling at 30 minutes, not 60. By the time you feel hungry, you're already behind. Set a timer.</p>
    </div>
    
    <div class="callout alert">
        <h4>Post-Workout Recovery</h4>
        <p><strong>Only needed if:</strong> Workout was long (2.5+ hours) AND hard, AND you have another hard session within 24-36 hours.</p>
        
        <table style="margin: 12px 0;">
            <thead>
                <tr>
                    <th>Timing</th>
                    <th>Protein</th>
                    <th>Carbs</th>
                    <th>Examples</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td><strong>Within 30 minutes</strong><br><span style="font-size: 10px; color: #666;">Critical window</span></td>
                    <td>20-30g</td>
                    <td>1-1.5g/kg<br>({int(weight_kg * 1)}-{int(weight_kg * 1.5)}g)</td>
                    <td>Recovery shake, chocolate milk</td>
                </tr>
                <tr>
                    <td><strong>Within 1-2 hours</strong></td>
                    <td>Full meal</td>
                    <td>Full meal</td>
                    <td>Protein + carbs + vegetables</td>
                </tr>
            </tbody>
        </table>
        
        <p><strong>If workout was easy, short, or next hard session is 48+ hours away:</strong></p>
        <ul style="margin: 8px 0;">
            <li>Just eat your next meal normally</li>
            <li>Recovery nutrition is optional</li>
            <li>Don't overthink it</li>
        </ul>
        
        <p style="font-size: 11px; margin-top: 8px; color: #666;"><strong>Rule:</strong> The more frequently you train hard, the more critical recovery nutrition becomes. If training once per day with easy sessions, skip the fancy protocols and just eat dinner.</p>
    </div>
    
    <h4>Sample Timing Schedule (Hard Session)</h4>
    
    <div class="timeline">
        <div class="timeline-item">
            <div class="timeline-time">2-3 Hours Before</div>
            <p><strong>Pre-workout meal:</strong> {int(weight_kg * 1)}-{int(weight_kg * 2)}g carbs + light protein. Low fiber, low fat. Oatmeal + banana + honey, or toast + peanut butter.</p>
        </div>
        <div class="timeline-item">
            <div class="timeline-time">15-30 Minutes Before</div>
            <p><strong>Quick snack (optional):</strong> 20-30g fast carbs. Banana or gel. Skip if you ate well 2-3 hours prior.</p>
        </div>
        <div class="timeline-item">
            <div class="timeline-time">During Session (if &gt;90 min)</div>
            <p><strong>Start at 30 minutes:</strong> 60-80g carbs/hour. Set a timer. Mix liquids + solids. Use 2:1 glucose:fructose if exceeding 60g/hour.</p>
        </div>
        <div class="timeline-item">
            <div class="timeline-time">Within 30 Minutes After</div>
            <p><strong>Recovery (if long/hard + training again within 24-36hrs):</strong> 20-30g protein + {int(weight_kg * 1)}-{int(weight_kg * 1.5)}g carbs. Recovery shake or chocolate milk. Window is smaller than you think.</p>
        </div>
        <div class="timeline-item">
            <div class="timeline-time">Within 1-2 Hours After</div>
            <p><strong>Full meal:</strong> Balanced meal with protein, carbs, vegetables. Continue normal eating pattern throughout the day.</p>
        </div>
    </div>
    
    <p style="font-size: 12px; color: #666; margin-top: 16px;"><strong>Note:</strong> For easy sessions (&lt;90 min Z2), timing is flexible. Eat normally, or even train fasted. Recovery nutrition is optional unless you're training twice per day.</p>
    
    <div class="callout tip">
        <h4>Common Timing Mistakes</h4>
        <ul style="margin: 8px 0;">
            <li><strong>Waiting until hungry to fuel</strong> — By then it's too late. Set a timer.</li>
            <li><strong>Skipping pre-workout meal</strong> — You'll bonk mid-session. Eat 2-3 hours before.</li>
            <li><strong>Not eating post-workout</strong> — Recovery window closes fast. Eat within 60 minutes.</li>
            <li><strong>Trying new foods on race day</strong> — Test everything in training first.</li>
            <li><strong>Overthinking it</strong> — Simple carbs before/during, protein+carbs after. That's 90% of it.</li>
        </ul>
    </div>
    
    <h3>Day-Type Adjustments</h3>
    <table>
        <thead>
            <tr>
                <th>Day Type</th>
                <th>Calories</th>
                <th>Carbs</th>
                <th>Notes</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td><strong>Hard / Key Session</strong></td>
                <td>{hard_day_kcal} kcal</td>
                <td>{hard_day_cho}g</td>
                <td>Front-load carbs before and during session</td>
            </tr>
            <tr>
                <td><strong>Average Training</strong></td>
                <td>{total_kcal} kcal</td>
                <td>{cho_grams}g</td>
                <td>Steady intake throughout day</td>
            </tr>
            <tr>
                <td><strong>Easy / Recovery</strong></td>
                <td>{easy_day_kcal} kcal</td>
                <td>{easy_day_cho}g</td>
                <td>Slight reduction, maintain protein</td>
            </tr>
            <tr>
                <td><strong>Rest Day</strong></td>
                <td>{int(total_kcal * 0.8)} kcal</td>
                <td>{int(cho_grams * 0.6)}g</td>
                <td>Reduce carbs, maintain protein for recovery</td>
            </tr>
        </tbody>
    </table>
    
    <h3>During Training</h3>
    <table>
        <thead>
            <tr>
                <th>Session Type</th>
                <th>Carbs/Hour</th>
                <th>Timing</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td>&lt;90 min easy</td>
                <td>Optional (0-30g)</td>
                <td>Water is fine</td>
            </tr>
            <tr>
                <td>90 min - 2 hrs</td>
                <td>30-60g</td>
                <td>Start at 30 min</td>
            </tr>
            <tr>
                <td>2-4 hrs</td>
                <td>60-75g</td>
                <td>Every 20 min</td>
            </tr>
            <tr>
                <td>Race / 4+ hrs</td>
                <td>80-100g</td>
                <td>Every 15-20 min</td>
            </tr>
        </tbody>
    </table>
    
    <div class="callout tip">
        <h4>Fuel the Work</h4>
        <p><strong>Common mistake:</strong> Eating less to lose weight during hard training blocks.</p>
        <p><strong>Reality:</strong> Underfueling impairs adaptation, increases injury risk, and tanks performance. Eat for the work you're doing. Weight management happens in easy phases, not build phases.</p>
    </div>
</section>
'''
    
    def _generate_footer(self) -> str:
        first_name = self._get_first_name()
        plan_weeks = self.derived.get('plan_weeks', 12)
        
        # Add workout modal
        modal_html = '''
<!-- Workout Detail Modal -->
<div class="workout-modal" id="workoutModal" onclick="if(event.target === this) closeWorkoutModal()">
    <div class="workout-modal-content">
        <div class="workout-modal-header">
            <h3 id="modalTitle">Workout Details</h3>
            <button class="workout-modal-close" onclick="closeWorkoutModal()">×</button>
        </div>
        <div id="modalBody">
            <!-- Populated by JavaScript -->
        </div>
    </div>
</div>

<script>
// Workout descriptions by type
const workoutDescriptions = {
    'strength': {
        title: 'Strength Session',
        duration: '45-60 min',
        description: 'Full-body strength workout targeting cycling-specific muscle groups.',
        instructions: [
            'Watch video demos before new exercises',
            'Complete warm-up activation circuit first',
            'Rest 60-120s between sets (longer for heavy sets)',
            'Stop if form breaks down',
            'Log weights for progressive overload tracking'
        ],
        file: 'Check your ZWO files for the specific workout'
    },
    'intervals': {
        title: 'Interval Session',
        duration: '60-90 min',
        description: 'Structured intensity work targeting specific energy systems.',
        instructions: [
            '15-20 min warm-up before first interval',
            'Hit target power, don\\'t exceed it',
            'If you can\\'t complete reps, reduce target by 5%',
            'Full recovery between sets',
            'Easy spin cool-down'
        ],
        zones: 'Varies by phase — check specific workout'
    },
    'easy_ride': {
        title: 'Easy / Recovery Ride',
        duration: '45-90 min',
        description: 'Truly easy spinning to promote recovery without adding fatigue.',
        instructions: [
            'Zone 1-2 only — this should feel embarrassingly easy',
            'Conversation pace throughout',
            'No Strava segments, no chasing groups',
            'Purpose is recovery, not fitness',
            'If HR drifts into Z3, slow down'
        ],
        zones: 'Z1-Z2 (<75% FTP)'
    },
    'long_ride': {
        title: 'Long Endurance Ride',
        duration: '2.5-5+ hours',
        description: 'Building aerobic base and time-in-saddle.',
        instructions: [
            'Mostly Z2 with natural terrain variation',
            'Practice race nutrition (target 60-80g carbs/hour)',
            'Include some Z3 efforts if terrain demands',
            'Build mental fortitude for long efforts',
            'This is where endurance adaptations happen'
        ],
        zones: 'Z2 primary (65-75% FTP), Z3 ok on climbs'
    },
    'tempo': {
        title: 'Tempo Ride',
        duration: '60-90 min',
        description: 'Sustained moderate effort building muscular endurance.',
        instructions: [
            'Main set at 76-87% FTP (Z3)',
            'Should feel comfortably hard',
            'Can talk in short sentences',
            'Don\\'t let it creep into threshold',
            'Good for building fatigue resistance'
        ],
        zones: 'Z3 (76-87% FTP)'
    },
    'rest': {
        title: 'Rest Day',
        duration: '0 min',
        description: 'Complete rest — no training.',
        instructions: [
            'No cycling (not even "easy")',
            'Light walking/stretching is fine',
            'Focus on sleep and nutrition',
            'This is where adaptation happens',
            'Don\\'t feel guilty — rest IS training'
        ]
    }
};

function showWorkoutModal(element) {
    const week = element.dataset.week;
    const day = element.dataset.day;
    const phase = element.dataset.phase;
    const am = element.dataset.am;
    const pm = element.dataset.pm;
    
    const modal = document.getElementById('workoutModal');
    const title = document.getElementById('modalTitle');
    const body = document.getElementById('modalBody');
    
    // Get workout info
    const workoutType = (am || 'rest').toLowerCase().replace(/ /g, '_');
    const workout = workoutDescriptions[workoutType] || workoutDescriptions['easy_ride'];
    
    title.textContent = `Week ${week} · ${day.charAt(0).toUpperCase() + day.slice(1)} · ${workout.title}`;
    
    let html = `
        <div class="workout-detail">
            <div class="workout-detail-label">Phase</div>
            <div>${phase}</div>
        </div>
        <div class="workout-detail">
            <div class="workout-detail-label">Duration</div>
            <div>${workout.duration}</div>
        </div>
        <div class="workout-detail">
            <div class="workout-detail-label">Description</div>
            <div>${workout.description}</div>
        </div>
    `;
    
    if (workout.zones) {
        html += `
        <div class="workout-detail">
            <div class="workout-detail-label">Target Zones</div>
            <div>${workout.zones}</div>
        </div>
        `;
    }
    
    html += `
        <div class="workout-detail">
            <div class="workout-detail-label">Execution Notes</div>
            <ul style="margin: 8px 0 0 0; padding-left: 20px;">
                ${workout.instructions.map(i => `<li>${i}</li>`).join('')}
            </ul>
        </div>
    `;
    
    if (pm) {
        html += `
        <div class="workout-detail" style="margin-top: 16px; padding-top: 12px; border-top: 1px dashed #ddd;">
            <div class="workout-detail-label">PM Session</div>
            <div>${pm.replace(/_/g, ' ')}</div>
        </div>
        `;
    }
    
    body.innerHTML = html;
    modal.classList.add('open');
}

function closeWorkoutModal() {
    document.getElementById('workoutModal').classList.remove('open');
}

// Close on Escape key
document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeWorkoutModal();
});
</script>
'''
        
        return f'''
{modal_html}

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

