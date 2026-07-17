import pytest

from tools.challenges import (
    get_active_goals,
    get_earned_badges,
    get_adhoc_challenges,
)


# ── GOALS ─────────────────────────────────────────────────────────────────────

def test_get_active_goals_returns_list():
    result = get_active_goals()
    assert isinstance(result, list)


def test_get_active_goals_rejects_invalid_type():
    with pytest.raises(ValueError):
        get_active_goals('bogus')


@pytest.mark.parametrize('goal_type', ['active', 'future', 'past'])
def test_get_active_goals_accepts_valid_types(goal_type):
    result = get_active_goals(goal_type)
    assert isinstance(result, list)


def test_get_active_goals_entries_have_required_keys():
    result = get_active_goals('past')  # past is most likely to be populated
    expected = ('id', 'goal_category', 'goal_type', 'goal_type_name',
                'target_value', 'current_value', 'progress_pct',
                'start_date', 'end_date', 'create_date')
    for goal in result:
        for key in expected:
            assert key in goal, f"Missing key: {key}"


def test_get_active_goals_progress_pct_non_negative():
    for goal_type in ('active', 'future', 'past'):
        for goal in get_active_goals(goal_type):
            if goal['progress_pct'] is not None:
                assert goal['progress_pct'] >= 0


# ── BADGES ────────────────────────────────────────────────────────────────────

def test_get_earned_badges_returns_list():
    result = get_earned_badges()
    assert isinstance(result, list)


def test_get_earned_badges_entries_have_required_keys():
    result = get_earned_badges()
    expected = ('id', 'name', 'key', 'category_id', 'difficulty_id',
                'points', 'earned_date', 'times_earned')
    for badge in result:
        for key in expected:
            assert key in badge, f"Missing key: {key}"


def test_get_earned_badges_points_non_negative():
    for badge in get_earned_badges():
        if badge['points'] is not None:
            assert badge['points'] >= 0


# ── ADHOC CHALLENGES ──────────────────────────────────────────────────────────

def test_get_adhoc_challenges_returns_list():
    result = get_adhoc_challenges()
    assert isinstance(result, list)


def test_get_adhoc_challenges_respects_limit():
    result = get_adhoc_challenges(limit=5)
    assert isinstance(result, list)
    assert len(result) <= 5


def test_get_adhoc_challenges_entries_have_required_keys():
    result = get_adhoc_challenges()
    expected = ('id', 'uuid', 'name', 'description', 'activity_type_id',
                'start_date', 'end_date', 'user_ranking', 'num_players')
    for challenge in result:
        for key in expected:
            assert key in challenge, f"Missing key: {key}"
