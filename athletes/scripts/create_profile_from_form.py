#!/usr/bin/env python3
"""
Create Profile from Form Data
Converts comprehensive coaching intake form JSON data to profile.yaml format.
"""

import json
import sys
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List
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
        'Specific race(s)': 'specific_race',
        'General fitness': 'general_fitness',
        'Base building': 'base_building',
        'Off-season maintenance': 'off_season',
        'Return from injury': 'return_from_injury',
        'Performance improvement': 'performance_improvement'
    }
    return mapping.get(form_goal, form_goal.lower().replace(' ', '_'))


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


def convert_devices(form_data: Dict) -> list:
    """Convert device checkboxes to list."""
    devices = form_data.get('devices', [])
    if not isinstance(devices, list):
        devices = [devices] if devices else []
    return devices


def convert_health_conditions(form_data: Dict) -> list:
    """Convert health condition checkboxes to list."""
    conditions = form_data.get('health_conditions', [])
    if not isinstance(conditions, list):
        conditions = [conditions] if conditions else []
    # Remove 'none' if other conditions selected
    if len(conditions) > 1 and 'none' in conditions:
        conditions.remove('none')
    return conditions


def convert_diet_styles(form_data: Dict) -> list:
    """Convert diet style checkboxes to list."""
    diets = form_data.get('diet_styles', [])
    if not isinstance(diets, list):
        diets = [diets] if diets else []
    return diets


def parse_race_list(race_list: str) -> List[Dict]:
    """Parse race list text into structured format."""
    if not race_list:
        return []
    
    races = []
    # Simple parsing - split by newline or comma
    for line in race_list.split('\n'):
        line = line.strip()
        if not line:
            continue
        
        # Try to extract date if present (e.g., "Unbound Gravel 200 (June 7)")
        race_name = line
        race_date = None
        
        # Look for date patterns
        date_patterns = [
            r'\(([A-Za-z]+ \d+)\)',  # (June 7)
            r'(\d{4}-\d{2}-\d{2})',  # 2024-06-07
            r'([A-Za-z]+ \d{1,2},? \d{4})',  # June 7, 2024
        ]
        
        for pattern in date_patterns:
            match = re.search(pattern, line)
            if match:
                race_date = match.group(1)
                race_name = re.sub(pattern, '', line).strip('()').strip()
                break
        
        races.append({
            'name': race_name,
            'date': race_date,
            'priority': 'A'  # Default to A priority
        })
    
    return races


def parse_b_priority_events(b_events: str) -> List[Dict]:
    """Parse B-priority events text."""
    if not b_events:
        return []
    
    events = []
    for line in b_events.split('\n'):
        line = line.strip()
        if line:
            events.append({
                'name': line,
                'priority': 'B'
            })
    
    return events


def create_profile_from_form(athlete_id: str, form_data: Dict) -> Dict:
    """Convert comprehensive form data to profile.yaml structure."""
    
    # Parse weekly volume
    min_hours, max_hours = parse_weekly_volume(form_data.get('weekly_volume', '0-2'))
    
    # Parse race list
    race_list_text = form_data.get('race_list', '')
    primary_races = parse_race_list(race_list_text)
    
    # Get primary race (first one or from race_name field)
    primary_race = None
    if primary_races:
        primary_race = primary_races[0]
    elif form_data.get('race_name'):
        primary_race = {
            'name': form_data.get('race_name', ''),
            'date': form_data.get('race_date', ''),
            'distance_miles': int(form_data.get('race_distance', 0)) if form_data.get('race_distance_unit') == 'miles' else int(form_data.get('race_distance', 0)) * 0.621371 if form_data.get('race_distance') else 0
        }
    
    # Parse B-priority events
    b_events = parse_b_priority_events(form_data.get('b_priority_events', ''))
    
    # Calculate age from birthday if provided
    age = form_data.get('age')
    if not age and form_data.get('birthday'):
        try:
            birth_date = datetime.strptime(form_data['birthday'], '%Y-%m-%d')
            age = (datetime.now() - birth_date).days // 365
        except:
            pass
    
    # Generate profile
    profile = {
        'name': form_data.get('name', ''),
        'email': form_data.get('email', ''),
        'phone': form_data.get('phone', ''),
        'athlete_id': athlete_id,
        'birthday': form_data.get('birthday'),
        
        'primary_goal': convert_primary_goal(form_data.get('primary_goal', '')),
        
        'target_race': {
            'name': primary_race.get('name', '') if primary_race else form_data.get('race_name', ''),
            'race_id': 'unbound_gravel_200',  # Default, could be enhanced with race matching
            'date': primary_race.get('date', '') if primary_race else form_data.get('race_date', ''),
            'distance_miles': primary_race.get('distance_miles', 0) if primary_race else (int(form_data.get('race_distance', 0)) if form_data.get('race_distance_unit') == 'miles' else int(form_data.get('race_distance', 0)) * 0.621371 if form_data.get('race_distance') else 0),
            'goal_type': 'compete'  # Default, could parse from success_looks_like
        } if (primary_race or form_data.get('race_name')) else None,
        
        'secondary_races': b_events + primary_races[1:] if len(primary_races) > 1 else b_events,
        
        'racing': {
            'has_goals': form_data.get('has_racing_goals') == 'yes',
            'race_list': race_list_text,
            'success_metrics': form_data.get('success_looks_like', ''),
            'obstacles': form_data.get('obstacles', ''),
            'training_goals': form_data.get('training_goals', ''),
            'goals_notes': form_data.get('goals_anything_else', '')
        },
        
        'training_history': {
            'summary': form_data.get('training_summary', ''),
            'years_cycling': form_data.get('years_cycling', '0-2'),
            'years_structured': int(form_data.get('years_structured', 0)),
            'strength_background': 'beginner' if form_data.get('strength_trains') == 'no' else 'intermediate',
            'highest_weekly_hours': max_hours,
            'current_weekly_hours': min_hours,
            'primary_sport_level': int(form_data.get('primary_sport_level', 3)),
            'strengths': form_data.get('strengths', ''),
            'weaknesses': form_data.get('weaknesses', ''),
            'races_last_year': int(form_data.get('races_last_year', 0)) if form_data.get('races_last_year') else None,
            'weather_limits': form_data.get('weather_limits') == 'yes',
            'weather_description': form_data.get('weather_description', '')
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
            'strength_sessions_max': 2 if form_data.get('strength_interest') == 'eager' else 1 if form_data.get('strength_interest') == 'willing' else 0
        },
        
        'preferred_days': {
            day: convert_day_availability(form_data, day)
            for day in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        },
        
        'schedule_constraints': {
            'work_schedule': '9-5' if form_data.get('works') == 'yes' else 'flexible',
            'travel_frequency': 'none',
            'family_commitments': form_data.get('time_commitments', ''),
            'seasonal_changes': '',
            'preferred_off_days': form_data.get('preferred_off_days', []),
            'preferred_long_day': form_data.get('preferred_long_day', '')
        },
        
        'cycling_equipment': {
            'smart_trainer': 'smart_trainer' in form_data.get('equipment', []),
            'power_meter_bike': 'dumb_trainer_pm' in form_data.get('equipment', []) or 'outdoor_pm' in form_data.get('equipment', []),
            'hr_monitor': 'hr_monitor' in form_data.get('devices', []),
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
            'current_injuries': [{
                'area': 'general',
                'severity': 'moderate',
                'affects_cycling': True,
                'affects_strength': True,
                'notes': form_data.get('current_injuries', '')
            }] if form_data.get('current_injuries') else [],
            'past_injuries': [{
                'area': 'general',
                'notes': form_data.get('past_injuries', '')
            }] if form_data.get('past_injuries') else []
        },
        
        'movement_limitations': {
            'deep_squat': 'painful' if 'deep_squat_painful' in form_data.get('limitations', []) else None,
            'single_leg_balance': 'limited' if 'single_leg_balance' in form_data.get('limitations', []) else None,
            'push_up_position': 'painful' if 'pushups_shoulders' in form_data.get('limitations', []) else None,
            'hip_hinge': 'limited' if 'hip_mobility' in form_data.get('limitations', []) or 'lower_back' in form_data.get('limitations', []) else None,
            'notes': ''
        },
        
        'health_factors': {
            'age': int(age) if age else None,
            'sleep_quality': 'good',  # Default, could calculate from sleep_hours
            'sleep_hours_avg': float(form_data.get('sleep_hours', 7)) if form_data.get('sleep_hours') else 7,
            'wake_time': form_data.get('wake_time', ''),
            'bed_time': form_data.get('bed_time', ''),
            'stress_level': 'moderate' if int(form_data.get('job_stress', 3)) <= 3 else 'high',
            'recovery_capacity': 'normal',  # Default
            'medical_conditions': ', '.join(convert_health_conditions(form_data)),
            'medications': form_data.get('medications', ''),
            'health_notes': form_data.get('health_anything_else', '')
        },
        
        'strength': {
            'currently_training': form_data.get('strength_trains') == 'yes',
            'routine': form_data.get('strength_routine', ''),
            'mobility_rating': int(form_data.get('mobility_rating', 3)) if form_data.get('mobility_rating') else 3
        },
        
        'devices': {
            'training_log': form_data.get('keeps_log') == 'yes',
            'devices': convert_devices(form_data)
        },
        
        'work': {
            'employed': form_data.get('works') == 'yes',
            'hours_per_week': int(form_data.get('work_hours', 0)) if form_data.get('work_hours') else None,
            'stress_level': int(form_data.get('job_stress', 3)) if form_data.get('job_stress') else None
        },
        
        'life_balance': {
            'relationships': form_data.get('relationships', ''),
            'time_commitments': form_data.get('time_commitments', ''),
            'time_management_challenge': form_data.get('time_management_challenge') == 'yes'
        },
        
        'nutrition': {
            'diet_styles': convert_diet_styles(form_data),
            'fluid_intake_rating': int(form_data.get('fluid_intake', 3)) if form_data.get('fluid_intake') else None,
            'caffeine': form_data.get('caffeine_intake', ''),
            'alcohol': form_data.get('alcohol_intake', ''),
            'restrictions': form_data.get('dietary_restrictions', ''),
            'training_fuel': form_data.get('fueling_strategy', ''),
            'post_workout': form_data.get('post_workout_fuel', '')
        },
        
        'bike': {
            'last_fit': form_data.get('last_bike_fit', ''),
            'pain': form_data.get('bike_pain') == 'yes',
            'pain_description': form_data.get('pain_description', '')
        },
        
        'social': {
            'group_rides_per_week': int(form_data.get('group_rides_per_week', 0)) if form_data.get('group_rides_per_week') else 0,
            'group_ride_importance': int(form_data.get('group_ride_importance', 3)) if form_data.get('group_ride_importance') else None
        },
        
        'coaching': {
            'previous_coach': form_data.get('previous_coach') == 'yes',
            'experience': form_data.get('coach_experience', '')
        },
        
        'personal': {
            'important_people': form_data.get('important_people', ''),
            'anything_else': form_data.get('anything_else', ''),
            'life_affecting_training': form_data.get('life_affecting_training', '')
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
            'group_rides_available': form_data.get('group_rides_per_week', 0) > 0,
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
            'autonomy_preference': 'general_guidance',
            'communication_style': form_data.get('communication_style', 'flexible')
        },
        
        'platforms': {
            'primary': 'trainingpeaks' if 'trainingpeaks' in form_data.get('devices', []) else 'strava' if 'strava' in form_data.get('devices', []) else 'trainingpeaks',
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
            'current_commitments': form_data.get('time_commitments', '')
        }
    }
    
    # Remove None values and empty strings where appropriate
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
    print(f"   Sections: {len([k for k in profile.keys() if isinstance(profile[k], dict)])} sections populated")


if __name__ == '__main__':
    main()
