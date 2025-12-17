#!/usr/bin/env python3
"""
Create Profile from Form Data
Converts form JSON data to profile.yaml format.
"""

import json
import sys
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, Any
import yaml


def generate_athlete_id(name: str) -> str:
    """Generate athlete ID from name."""
    # Convert to lowercase, replace spaces with hyphens, remove special chars
    athlete_id = re.sub(r'[^a-z0-9-]', '', name.lower().replace(' ', '-'))
    # Remove multiple consecutive hyphens
    athlete_id = re.sub(r'-+', '-', athlete_id)
    # Remove leading/trailing hyphens
    athlete_id = athlete_id.strip('-')
    return athlete_id


def convert_primary_goal(form_goal: str) -> str:
    """Convert form goal to profile format."""
    mapping = {
        'Complete first gravel century': 'specific_race',
        'Top 25% finish': 'specific_race',
        'Podium': 'specific_race',
        'Win': 'specific_race',
        'General fitness': 'general_fitness',
        'Base building': 'base_building',
        'Return from injury': 'return_from_injury'
    }
    return mapping.get(form_goal, form_goal.lower().replace(' ', '_'))


def convert_goal_type(form_goal: str) -> str:
    """Extract goal type from form goal."""
    if 'Top 25%' in form_goal or 'Podium' in form_goal or 'Win' in form_goal:
        if 'Win' in form_goal:
            return 'podium'
        elif 'Podium' in form_goal:
            return 'podium'
        else:
            return 'compete'
    return 'finish'


def parse_weekly_volume(volume: str) -> tuple[int, int]:
    """Parse weekly volume string to min/max hours."""
    if not volume:
        return 0, 0
    
    if volume.endswith('+'):
        min_hours = int(volume.replace('+', ''))
        return min_hours, 40
    
    if '-' in volume:
        parts = volume.split('-')
        return int(parts[0]), int(parts[1])
    
    return int(volume), int(volume)


def convert_day_availability(form_data: Dict, day: str) -> Dict:
    """Convert day availability from form to profile format."""
    available = form_data.get(f'{day}_available', False)
    time = form_data.get(f'{day}_time', '')
    duration = form_data.get(f'{day}_duration', '')
    
    if not available:
        return {
            'availability': 'unavailable',
            'time_slots': [],
            'max_duration_min': 0,
            'is_key_day_ok': False
        }
    
    # Convert time to time_slots
    time_slots = []
    if time in ['early_morning', 'morning']:
        time_slots = ['am']
    elif time in ['afternoon', 'evening']:
        time_slots = ['pm']
    elif time == 'flexible':
        time_slots = ['am', 'pm']
    else:
        time_slots = ['am']  # Default
    
    # Convert duration
    max_duration = int(duration) if duration else 60
    
    return {
        'availability': 'available',
        'time_slots': time_slots,
        'max_duration_min': max_duration,
        'is_key_day_ok': True  # Default to true if available
    }


def convert_equipment(form_data: Dict) -> list:
    """Convert equipment checkboxes to profile format."""
    equipment_map = {
        'smart_trainer': 'smart_trainer',
        'dumb_trainer_pm': 'power_meter_bike',
        'outdoor_pm': 'power_meter_bike',
        'no_pm': None,  # Not equipment, just info
        'gym_membership': 'gym_membership',
        'home_gym': 'dumbbells',  # Assume DB/KB
        'pull_up_bar': 'pull_up_bar',
        'resistance_bands': 'resistance_bands'
    }
    
    equipment = form_data.get('equipment', [])
    if not isinstance(equipment, list):
        equipment = [equipment] if equipment else []
    
    strength_equipment = []
    for eq in equipment:
        mapped = equipment_map.get(eq)
        if mapped:
            strength_equipment.append(mapped)
    
    return strength_equipment


def convert_injuries(form_data: Dict) -> list:
    """Convert injury text to structured format."""
    injuries = []
    
    current = form_data.get('current_injuries', '')
    if current:
        # Simple parsing - could be enhanced
        injuries.append({
            'area': 'general',  # Would need NLP to extract
            'severity': 'moderate',
            'affects_cycling': True,
            'affects_strength': True,
            'notes': current
        })
    
    return injuries


def convert_limitations(form_data: Dict) -> Dict:
    """Convert movement limitations to profile format."""
    limitations = form_data.get('limitations', [])
    if not isinstance(limitations, list):
        limitations = [limitations] if limitations else []
    
    mapping = {
        'deep_squat_painful': ('deep_squat', 'painful'),
        'single_leg_balance': ('single_leg_balance', 'limited'),
        'pushups_shoulders': ('push_up_position', 'painful'),
        'hip_mobility': ('hip_hinge', 'limited'),
        'lower_back': ('hip_hinge', 'painful')
    }
    
    result = {}
    for lim in limitations:
        if lim in mapping:
            movement, level = mapping[lim]
            result[movement] = level
    
    return result


def create_profile_from_form(athlete_id: str, form_data: Dict) -> Dict:
    """Convert form data to profile.yaml structure."""
    
    # Parse weekly volume
    min_hours, max_hours = parse_weekly_volume(form_data.get('weekly_volume', '0-2'))
    
    # Generate profile
    profile = {
        'name': form_data.get('name', ''),
        'email': form_data.get('email', ''),
        'athlete_id': athlete_id,
        
        'primary_goal': convert_primary_goal(form_data.get('primary_goal', '')),
        
        'target_race': {
            'name': form_data.get('race_name', ''),
            'race_id': 'unbound_gravel_200',  # Default, could be enhanced
            'date': form_data.get('race_date', ''),
            'distance_miles': int(form_data.get('race_distance', 0)) if form_data.get('race_distance_unit') == 'miles' else int(form_data.get('race_distance', 0)) * 0.621371,
            'goal_type': convert_goal_type(form_data.get('primary_goal', ''))
        } if form_data.get('primary_goal') in ['Complete first gravel century', 'Top 25% finish', 'Podium', 'Win'] else None,
        
        'training_history': {
            'years_cycling': form_data.get('years_cycling', '0-2'),
            'years_structured': int(form_data.get('years_structured', 0)),
            'strength_background': 'beginner',  # Default, could ask in form
            'highest_weekly_hours': max_hours,
            'current_weekly_hours': min_hours
        },
        
        'fitness_markers': {
            'ftp_watts': int(form_data.get('current_ftp', 0)) if form_data.get('current_ftp') else None,
            'ftp_date': datetime.now().strftime('%Y-%m-%d') if form_data.get('current_ftp') else None,
            'weight_kg': None,
            'w_kg': None,
            'resting_hr': None,
            'max_hr': None
        },
        
        'recent_training': {
            'last_12_weeks': 'consistent',  # Default
            'current_phase': 'base',
            'coming_off_injury': bool(form_data.get('current_injuries')),
            'days_since_last_ride': None
        },
        
        'weekly_availability': {
            'total_hours_available': max_hours + 2,  # Add buffer for strength
            'cycling_hours_target': max_hours,
            'strength_sessions_max': 2 if form_data.get('strength_interest') == 'eager' else 1
        },
        
        'preferred_days': {
            day: convert_day_availability(form_data, day)
            for day in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        },
        
        'schedule_constraints': {
            'work_schedule': '9-5',  # Default
            'travel_frequency': 'none',
            'family_commitments': '',
            'seasonal_changes': ''
        },
        
        'cycling_equipment': {
            'smart_trainer': 'smart_trainer' in form_data.get('equipment', []),
            'power_meter_bike': 'dumb_trainer_pm' in form_data.get('equipment', []) or 'outdoor_pm' in form_data.get('equipment', []),
            'hr_monitor': True,  # Assume most have
            'indoor_setup': 'basic' if 'smart_trainer' in form_data.get('equipment', []) else 'none'
        },
        
        'strength_equipment': convert_equipment(form_data),
        
        'training_environment': {
            'primary_location': 'home' if 'home_gym' in form_data.get('equipment', []) else 'gym' if 'gym_membership' in form_data.get('equipment', []) else 'home',
            'gym_type': 'commercial' if 'gym_membership' in form_data.get('equipment', []) else 'home_gym' if 'home_gym' in form_data.get('equipment', []) else 'none',
            'outdoor_riding_access': 'good',  # Default
            'indoor_riding_tolerance': 'tolerate_it'  # Default
        },
        
        'injury_history': {
            'current_injuries': convert_injuries(form_data),
            'past_injuries': []
        },
        
        'movement_limitations': convert_limitations(form_data),
        
        'health_factors': {
            'age': int(form_data.get('age', 35)) if form_data.get('age') else None,
            'sleep_quality': 'good',  # Default
            'sleep_hours_avg': 7,  # Default
            'stress_level': 'moderate',  # Default
            'recovery_capacity': 'normal',  # Default
            'medical_conditions': '',
            'medications': ''
        },
        
        'methodology_preferences': {
            'polarized': 3,  # Default
            'pyramidal': 3,
            'threshold_focused': 3,
            'hiit_focused': 3,
            'high_volume': 3,
            'time_crunched': 3,
            'preferred_approach': 'polarized',
            'past_success_with': '',
            'past_failure_with': ''
        },
        
        'workout_preferences': {
            'longest_indoor_tolerable': 90,  # Default
            'preferred_interval_style': 'mixed',
            'music_or_entertainment': 'preferred',
            'group_rides_available': False,
            'outdoor_interval_friendly': False
        },
        
        'strength_preferences': {
            'experience_level': 'beginner' if form_data.get('strength_interest') == 'not_interested' else 'intermediate',
            'comfort_with_barbell': 'low',
            'comfort_with_kettlebells': 'moderate',
            'preferred_session_length': 45,
            'time_of_day': 'separate_day'
        },
        
        'coaching_style': {
            'communication_frequency': 'weekly',
            'feedback_detail': 'moderate',
            'accountability_need': 'moderate',
            'autonomy_preference': 'general_guidance'
        },
        
        'platforms': {
            'primary': 'trainingpeaks',
            'secondary': '',
            'calendar_integration': 'google'
        },
        
        'communication': {
            'preferred_method': 'email',
            'timezone': 'America/New_York',  # Default
            'best_time_to_reach': ''
        },
        
        'plan_start': {
            'preferred_start': datetime.now().strftime('%Y-%m-%d'),
            'current_commitments': ''
        }
    }
    
    # Remove None values
    if not profile['target_race']['name']:
        profile['target_race'] = None
    
    return profile


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Create profile from form data')
    parser.add_argument('--athlete-id', required=True, help='Athlete ID')
    parser.add_argument('--data', required=True, help='Form data as JSON string')
    
    args = parser.parse_args()
    
    # Parse data
    try:
        data = json.loads(args.data) if isinstance(args.data, str) else args.data
    except json.JSONDecodeError:
        print("Error: Invalid JSON data")
        sys.exit(1)
    
    # Generate athlete ID if not provided
    if not args.athlete_id:
        args.athlete_id = generate_athlete_id(data.get('name', 'athlete'))
    
    # Create profile
    profile = create_profile_from_form(args.athlete_id, data)
    
    # Create athlete directory
    athlete_dir = Path(f'athletes/{args.athlete_id}')
    athlete_dir.mkdir(parents=True, exist_ok=True)
    
    # Write profile.yaml
    profile_path = athlete_dir / 'profile.yaml'
    with open(profile_path, 'w') as f:
        yaml.dump(profile, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    
    print(f"✅ Profile created: {profile_path}")
    print(f"   Athlete ID: {args.athlete_id}")
    print(f"   Name: {profile['name']}")
    print(f"   Email: {profile['email']}")


if __name__ == '__main__':
    main()

