"""Regression coverage for Task P: recovery after simulations and plan variety."""

import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from block_chain import (build_plan_from_calendar,
                         protect_post_simulation_recovery,
                         pre_simulation_strength_block_days)
from block_compliance import (r01_no_back_to_back_intensity,
                              r03_recovery_tss_ceiling)
from generate_athlete_package import (race_day_tss_from_emitted_minutes,
                                      place_strength_days)
from workout_mapper import endurance_focus_title


def _day(day, name, role, duration, tss, **extra):
    return {'day': day, 'name': name, 'role': role,
            'duration': duration, 'tss': tss, **extra}


def test_r01_detects_a_hard_session_across_the_sunday_monday_seam():
    weeks = [
        {'plan_week': 1, 'days': [
            _day('Sun', 'Act Race Simulation', 'long_ride', 250, 250,
                 act_simulation={'dress_rehearsal': True}),
        ]},
        {'plan_week': 2, 'days': [
            _day('Mon', 'Thirty-Fifteens', 'intensity', 55, 70),
        ]},
    ]

    passed, message = r01_no_back_to_back_intensity(weeks)

    assert not passed
    assert 'W1 Sun→W2 Mon' in message


def test_post_simulation_day_is_easy_and_displaced_sharpener_moves_to_interval_day():
    plan = {'weeks': [
        {'plan_week': 1, 'days': [
            _day('Sun', 'Act Race Simulation', 'long_ride', 250, 250,
                 act_simulation={'dress_rehearsal': True}),
        ]},
        {'plan_week': 2, 'days': [
            _day('Mon', 'Thirty-Fifteens', 'intensity', 55, 70),
            _day('Tue', 'OFF', 'off', 0, 0),
            _day('Thu', 'Endurance', 'filler', 70, 55),
        ]},
    ]}

    protected = protect_post_simulation_recovery(plan, ['Thu'])
    monday = plan['weeks'][1]['days'][0]
    thursday = plan['weeks'][1]['days'][2]

    assert protected == {(2, 'Mon')}
    assert monday['post_sim_recovery'] is True
    assert monday['name'] == 'Endurance'
    assert monday['role'] == 'filler'
    assert thursday['name'] == 'Thirty-Fifteens'
    assert thursday['role'] == 'intensity'
    assert r01_no_back_to_back_intensity(plan['weeks'])[0]


def test_post_simulation_off_day_stays_off():
    plan = {'weeks': [
        {'plan_week': 1, 'days': [
            _day('Sun', 'Act Race Simulation', 'long_ride', 250, 250,
                 act_simulation={'dress_rehearsal': True}),
        ]},
        {'plan_week': 2, 'days': [
            _day('Mon', 'OFF', 'off', 0, 0),
            _day('Thu', 'Endurance', 'filler', 70, 55),
        ]},
    ]}

    protected = protect_post_simulation_recovery(plan, ['Thu'])
    monday = plan['weeks'][1]['days'][0]

    assert protected == {(2, 'Mon')}
    assert monday['post_sim_recovery'] is True
    assert monday['name'] == 'OFF'
    assert monday['role'] == 'off'
    assert monday['duration'] == 0


def test_pre_simulation_strength_block_days_flags_the_day_before_dress_rehearsal():
    """Regression: verified live, loaded strength (Power B -- Bulgarians +
    trap-bar triples) landed on the Saturday immediately before a Sunday
    Act 2 dress rehearsal -- the plan's biggest day -- while Act 1 got a
    3-day strength buffer. The day before an Act-class simulation must be
    flagged for strength placement to avoid."""
    plan = {'weeks': [
        {'plan_week': 1, 'days': [
            _day('Mon', 'Threshold', 'intensity', 60, 70),
            _day('Tue', 'OFF', 'off', 0, 0),
            _day('Wed', 'Endurance', 'filler', 70, 55),
            _day('Thu', 'Thirty-Fifteens', 'intensity', 55, 70),
            _day('Fri', 'OFF', 'off', 0, 0),
            _day('Sat', 'Endurance', 'filler', 90, 60),
            _day('Sun', 'Act Race Simulation', 'long_ride', 250, 245,
                 act_simulation={'dress_rehearsal': True}, is_dress_rehearsal=True),
        ]},
    ]}

    blocked = pre_simulation_strength_block_days(plan)

    assert blocked == {(1, 'Sat')}
    # An easy bike day the day before is untouched -- only strength cares.
    assert plan['weeks'][0]['days'][5]['name'] == 'Endurance'


def test_strength_relocates_off_the_day_before_a_dress_rehearsal():
    """Regression: a week with a Sunday act-sim and Saturday strength in the
    naive layout must relocate strength to Thu or earlier once the pre-sim
    day is blocked, using place_strength_days' own candidate selection."""
    plan = {'weeks': [
        {'plan_week': 1, 'days': [
            _day('Mon', 'Threshold', 'intensity', 60, 70),
            _day('Tue', 'OFF', 'off', 0, 0),
            _day('Wed', 'Endurance', 'filler', 70, 55),
            _day('Thu', 'Thirty-Fifteens', 'intensity', 55, 70),
            _day('Fri', 'OFF', 'off', 0, 0),
            _day('Sat', 'Endurance', 'filler', 90, 60),
            _day('Sun', 'Act Race Simulation', 'long_ride', 250, 245,
                 act_simulation={'dress_rehearsal': True}, is_dress_rehearsal=True),
        ]},
    ]}
    protected_days = {day for week, day in pre_simulation_strength_block_days(plan)
                      if week == 1}
    assert protected_days == {'Sat'}

    def is_available(day):
        return day != 'Sun'  # Sunday is the long/act-sim day

    naive = place_strength_days(is_available, 1, strength_only_abbrevs=['Sat'])
    assert naive == ['Sat']  # confirms the naive layout really does land on Saturday

    relocated = place_strength_days(is_available, 1, blocked_days=protected_days,
                                    strength_only_abbrevs=['Sat'])
    assert relocated and relocated[0] != 'Sat'
    assert relocated[0] in ('Mon', 'Tue', 'Wed', 'Thu')


def test_strength_avoids_vo2_intensity_day_when_an_easy_day_is_available():
    """AE-8.4 (sol programming review 2026-08-24, major 9): the default
    coach-preferred strength pair (Tue/Thu) collides directly with the
    default intensity_1/intensity_2 slot days, stacking strength onto VO2
    days. avoid_days must sink an eligible-but-intensity day behind every
    non-intensity candidate."""
    def is_available(day):
        return day in ('Tue', 'Wed', 'Thu', 'Fri')

    placed = place_strength_days(is_available, 1, avoid_days={'Tue', 'Thu'})
    assert placed == ['Wed']


def test_strength_falls_back_to_an_avoided_day_to_keep_weekly_frequency():
    """avoid_days is a soft preference, never a hard block -- if nothing
    else can satisfy the requested session count, the avoided (intensity)
    day is still used rather than silently dropping the session."""
    def is_available(day):
        return day in ('Tue', 'Thu')

    placed = place_strength_days(is_available, 2, avoid_days={'Tue', 'Thu'})
    assert sorted(placed) == ['Thu', 'Tue']


def test_strength_never_lands_on_a_hard_blocked_test_day_even_without_avoid():
    """Test days (FTP Test / Anaerobic Test) are a hard block, not a soft
    avoid -- unlike VO2/intensity days, there is no frequency-preserving
    fallback onto them (AE-8.4's morning-primer exception does not apply
    to test days)."""
    def is_available(day):
        return day in ('Tue', 'Thu')

    placed = place_strength_days(is_available, 2, blocked_days={'Tue', 'Thu'})
    assert placed == []


def test_strength_drops_for_the_week_when_no_relocation_slot_fits():
    """Relocation is preferred, but when the pre-sim day is the only
    available day, the session must drop for that week rather than
    silently keeping the blocked placement."""
    def is_available(day):
        return day == 'Sat'  # only the blocked day is available at all

    relocated = place_strength_days(is_available, 1, blocked_days={'Sat'},
                                    strength_only_abbrevs=['Sat'])
    assert relocated == []


def test_recovery_floor_uses_preceding_load_weeks_and_stays_in_house_band():
    descriptors = [
        {'plan_week': 1, 'phase': 'base', 'week_type': 'load'},
        {'plan_week': 2, 'phase': 'base', 'week_type': 'load'},
        {'plan_week': 3, 'phase': 'base', 'week_type': 'load'},
        {'plan_week': 4, 'phase': 'base', 'week_type': 'recovery'},
    ]
    plan = build_plan_from_calendar(
        descriptors, archetype='specialist', max_intensity=2,
        off_days=['Tue'], long_ride_day='Sun', hours_per_week=10,
    )
    loads = [week['total_tss'] for week in plan['weeks'][:3]]
    recovery = plan['weeks'][3]['total_tss']
    ratio = recovery / (sum(loads) / len(loads))

    assert 0.50 <= ratio <= 0.65
    assert r03_recovery_tss_ceiling(plan['weeks'])[0]


def test_cadence_skill_never_returns_to_introductory_level_in_later_phases():
    descriptors = [
        {'plan_week': 1, 'phase': 'base', 'week_type': 'load'},
        {'plan_week': 2, 'phase': 'base', 'week_type': 'load'},
        {'plan_week': 3, 'phase': 'base', 'week_type': 'recovery'},
        {'plan_week': 4, 'phase': 'peak', 'week_type': 'load'},
        {'plan_week': 5, 'phase': 'peak', 'week_type': 'load'},
        {'plan_week': 6, 'phase': 'taper', 'week_type': 'taper'},
    ]
    plan = build_plan_from_calendar(
        descriptors, archetype='specialist', max_intensity=2,
        off_days=['Tue'], long_ride_day='Sun', hours_per_week=10,
    )
    levels = [
        (week['plan_week'], day['level'])
        for week in plan['weeks'] for day in week['days']
        if day['name'] == 'Cadence Work'
    ]

    highest = 0
    for _, level in levels:
        assert level >= highest
        highest = max(highest, level)
    assert highest >= 2


def test_load_filler_pool_rotates_existing_workout_types():
    descriptors = [
        {'plan_week': number, 'phase': 'base', 'week_type': 'load'}
        for number in range(1, 4)
    ]
    plan = build_plan_from_calendar(
        descriptors, archetype='specialist', max_intensity=2,
        off_days=['Tue'], long_ride_day='Sun', hours_per_week=10,
    )
    filler_names = [
        day['name'] for week in plan['weeks'] for day in week['days']
        if day['role'] == 'filler'
    ]

    assert {'Endurance', 'Cadence Work', 'Endurance Blocks',
            'Taper Burst Endurance'} <= set(filler_names)
    assert max(Counter(filler_names).values()) <= 3


def test_endurance_focus_titles_rotate_without_a_fourth_identical_card():
    # The generator uses a monotonic variation offset for rendered Endurance
    # fillers. Six honest focus variants keep an 18-card plan at three or
    # fewer cards per title rather than restarting the old 70min generic card.
    titles = [endurance_focus_title(offset) for offset in range(18)]

    assert len(set(titles)) == 6
    assert max(Counter(titles).values()) <= 3


def test_race_day_description_tss_uses_emitted_free_ride_duration():
    # 5.2h rounds to an emitted 310-minute FreeRide, which is 218 TSS with
    # the shared parser's IF=0.65 estimate.  The old raw-hour calculation
    # yielded 220 while PlanIR correctly held 218.
    assert race_day_tss_from_emitted_minutes(310) == 218


# =============================================================================
# Coordinator finding 2026-08-24 (steve-wagner regen, real calendar): two
# ratified-rule violations produced by the B-race -1/-2 displacement/
# strength-avoidance fixes above (Task P / HEAD, "B-race protection, strength
# adjacency"). Fixture is the exact real calendar shape: FTP Test Tue Sep1,
# Dirt Diggler B-race Sat Sep5 (W1 -- its own -2 day collides with the
# testing week's default Anaerobic Test slot), Fool's Gold B-race Sat Sep12
# (W2, the immediately following week). Also covered at the plan_dates layer
# by test_plan_dates.py::test_b_race_easy_day_reserved_in_base_phase.
#   1. TEST GAP: the B-race -1/-2 displacement swap that evicted the
#      Anaerobic Test off its reserved Sep 3 Thursday had no notion of the
#      ratified >=2-day-after-FTP-Test rule (commit 6d3eda1 FIX3 heritage,
#      block_builder.py's testing-week template) and landed it on Sep 2 --
#      1 day after FTP Test, not >=2.
#   2. RACE-1 STRENGTH: a full strength session landed on Sep 4 (Dirt
#      Diggler's -1/opener day) alongside Openers -- the B-race -1/-2
#      reservation protected bike content on that day but strength
#      placement never consulted it.
# =============================================================================

class TestBRaceTestGapAndStrengthAdjacency:
    @pytest.fixture(autouse=True)
    def _freeze_generation_clock(self, monkeypatch):
        # preferred_start is 2026-08-31. Without a frozen clock,
        # clamp_past_start refits Week 1 to monday_on_or_after(today) once
        # that Monday is in the past, so hardcoded Sep 3–5 assertions miss
        # the plan. Pin to a Tuesday before Week 1, matching other date
        # fixtures (test_tp_projection / test_plan_dates).
        monkeypatch.setenv('GG_FIXED_NOW', '2026-08-18')

    def _generate(self, tmp_path):
        import calculate_plan_dates as cpd
        from generate_athlete_package import generate_zwo_files

        plan_dates = cpd.calculate_plan_dates(
            '2026-10-11', plan_weeks=6, preferred_start='2026-08-31',
            b_events=[{'name': 'Dirt Diggler', 'date': '2026-09-05'},
                      {'name': "Fool's Gold", 'date': '2026-09-12'}])

        profile = {
            'name': 'Gap Test Sample', 'athlete_id': 'b-race-gap-sample',
            'target_race': {'name': 'Sanity Gravel Race', 'date': '2026-10-11',
                            'distance_miles': 60, 'discipline': 'gravel'},
            'fitness_markers': {'ftp_watts': 250, 'weight_kg': 75},
            'weekly_availability': {'cycling_hours_target': 8},
            'schedule_constraints': {'preferred_long_day': 'saturday',
                                     'preferred_off_days': ['sunday']},
            'preferred_days': {
                'monday': {'availability': 'available', 'is_key_day_ok': False, 'max_duration_min': 150},
                'tuesday': {'availability': 'available', 'is_key_day_ok': True, 'max_duration_min': 90},
                'wednesday': {'availability': 'available', 'is_key_day_ok': False, 'max_duration_min': 75},
                'thursday': {'availability': 'available', 'is_key_day_ok': True, 'max_duration_min': 90},
                'friday': {'availability': 'available', 'is_key_day_ok': False, 'max_duration_min': 75},
                'saturday': {'availability': 'available', 'is_key_day_ok': True,
                            'is_long_day': True, 'max_duration_min': 240},
                'sunday': {'availability': 'rest'},
            },
        }
        derived = {'plan_weeks': 6, 'ability_level': 'Intermediate'}
        methodology = {'methodology_id': 'polarized_80_20',
                       'configuration': {'intensity_distribution': {'z2': 0.80, 'z4': 0.15, 'z5': 0.05}}}

        athlete_dir = tmp_path / 'b-race-gap-sample'
        (athlete_dir / 'workouts').mkdir(parents=True)
        generate_zwo_files(athlete_dir, plan_dates, methodology, derived, profile)
        return athlete_dir, generate_zwo_files.last_naming_manifest

    def test_anaerobic_test_lands_at_least_two_days_after_ftp_test(self, tmp_path):
        _, manifest = self._generate(tmp_path)
        ftp_recs = [rec for rec in manifest.values()
                    if 'FTP_Test' in str(rec.get('filename_stem', ''))]
        anaerobic_recs = [rec for rec in manifest.values()
                          if 'Anaerobic_Test' in str(rec.get('filename_stem', ''))]
        assert ftp_recs, "test setup did not produce an FTP Test"
        assert anaerobic_recs, "test setup did not produce an Anaerobic Test"

        ftp_date = datetime.strptime(ftp_recs[0]['date'], '%Y-%m-%d')
        anaerobic_date = datetime.strptime(anaerobic_recs[0]['date'], '%Y-%m-%d')
        gap_days = (anaerobic_date - ftp_date).days
        assert gap_days >= 2, (
            f"Anaerobic Test landed {gap_days}d after FTP Test "
            f"({ftp_recs[0]['date']} -> {anaerobic_recs[0]['date']}) -- "
            "the ratified testing-week order rule requires >=2 days")

    def test_no_strength_on_any_b_race_opener_or_easy_day(self, tmp_path):
        _, manifest = self._generate(tmp_path)
        strength_recs = [rec for rec in manifest.values() if rec.get('tp_kind') == 'strength']
        assert strength_recs, "test setup did not place any strength sessions"

        # Dirt Diggler (Sat 2026-09-05): -1 = Sep 4, -2 = Sep 3.
        # Fool's Gold (Sat 2026-09-12): -1 = Sep 11, -2 = Sep 10.
        reserved_dates = {'2026-09-04', '2026-09-03', '2026-09-11', '2026-09-10'}
        manifest_dates = {rec.get('date') for rec in manifest.values()}
        assert reserved_dates <= manifest_dates, (
            "B-race reserved days missing from plan (clock clamp?): "
            f"{sorted(reserved_dates - manifest_dates)}")
        offenders = [rec for rec in strength_recs if rec.get('date') in reserved_dates]
        assert not offenders, (
            "strength session(s) landed on a B-race -1/-2 reserved day: "
            f"{[(r.get('date'), r.get('filename_stem')) for r in offenders]}")

    def test_openers_day_before_b_race_carries_no_strength(self, tmp_path):
        """race-1 = openers only, never a full lift alongside it -- the
        exact real defect (steve-wagner Sep 4: Openers + Strength_
        Foundation_Strength_B on the same day)."""
        _, manifest = self._generate(tmp_path)
        opener_day_recs = [rec for rec in manifest.values() if rec.get('date') == '2026-09-04']
        assert opener_day_recs
        assert any('Openers' in str(rec.get('filename_stem', '')) for rec in opener_day_recs)
        assert not any(rec.get('tp_kind') == 'strength' for rec in opener_day_recs)
