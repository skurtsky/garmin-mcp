# tools/challenges.py
from garmin_client import get_client

_VALID_GOAL_TYPES = {"active", "future", "past"}


def get_active_goals(goal_type: str = "active") -> list[dict]:
    """
    Get the athlete's goals (step / distance / activity goals) and progress.

    Args:
        goal_type: Which goals to fetch — 'active' (current, default),
                   'future' (upcoming), or 'past' (completed/expired).
    """
    if goal_type not in _VALID_GOAL_TYPES:
        raise ValueError(
            f"goal_type must be one of {sorted(_VALID_GOAL_TYPES)}, got {goal_type!r}"
        )

    client = get_client()
    raw = client.get_goals(status=goal_type) or []

    goals = []
    for g in raw:
        target = g.get('goalValue') or g.get('targetValue')
        # Garmin returns progress under a few different keys depending on goal
        # kind; fall back across the known variants.
        current = (g.get('currentValue')
                   if g.get('currentValue') is not None
                   else g.get('progressValue'))

        progress_pct = None
        if target and current is not None:
            progress_pct = round(current / target * 100, 1)

        goals.append({
            'id':             g.get('id'),
            'goal_category':  g.get('goalCategory') if 'goalCategory' in g else g.get('userGoalCategoryPK'),
            'goal_type':      g.get('goalType') if 'goalType' in g else g.get('userGoalTypePK'),
            'goal_type_name': g.get('goalTypeName') or g.get('goalTypeKey'),
            'target_value':   target,
            'current_value':  current,
            'progress_pct':   progress_pct,
            'start_date':     g.get('startDate'),
            'end_date':       g.get('endDate'),
            'create_date':    g.get('createDate'),
        })

    return goals


def get_earned_badges() -> list[dict]:
    """
    Get the athlete's earned challenge/achievement badges with the date
    earned, points, category and how many times each was earned.
    """
    client = get_client()
    raw = client.get_earned_badges() or []

    badges = []
    for b in raw:
        badges.append({
            'id':           b.get('badgeId'),
            'name':         b.get('badgeName'),
            'key':          b.get('badgeKey'),
            'category_id':  b.get('badgeCategoryId'),
            'difficulty_id': b.get('badgeDifficultyId'),
            'points':       b.get('badgePoints'),
            'earned_date':  b.get('badgeEarnedDate'),
            'times_earned': b.get('badgeEarnedNumber'),
        })

    return badges


def get_adhoc_challenges(limit: int = 100) -> list[dict]:
    """
    Get the athlete's ad-hoc / community challenges.

    Args:
        limit: Maximum number of challenges to return (default 100).
    """
    client = get_client()
    raw = client.get_adhoc_challenges(0, limit) or {}

    # The endpoint returns a dict wrapper; the challenge list lives under one of
    # a few keys depending on API version. Normalise to a plain list.
    if isinstance(raw, dict):
        entries = None
        for key in ('challenges', 'adHocChallenges', 'adhocChallenges'):
            if isinstance(raw.get(key), list):
                entries = raw[key]
                break
        if entries is None:
            # Fall back to the first list-valued field, else nothing.
            entries = next((v for v in raw.values() if isinstance(v, list)), [])
    elif isinstance(raw, list):
        entries = raw
    else:
        entries = []

    challenges = []
    for c in entries:
        players = c.get('players') if isinstance(c.get('players'), list) else None
        challenges.append({
            'id':               c.get('socialChallengeStatusId') or c.get('challengeId') or c.get('id'),
            'uuid':             c.get('uuid'),
            'name':             c.get('adHocChallengeName') or c.get('challengeName') or c.get('name'),
            'description':      c.get('adHocChallengeDesc') or c.get('description'),
            'activity_type_id': c.get('socialChallengeActivityTypeId'),
            'start_date':       c.get('startDate'),
            'end_date':         c.get('endDate'),
            'user_ranking':     c.get('userRanking'),
            'num_players':      len(players) if players is not None else None,
        })

    return challenges
