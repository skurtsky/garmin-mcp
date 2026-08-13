# TEMPORARY DIAGNOSTIC — remove before merge.
import json
from garmin_client import get_client

MULTISPORT_ID = 23901177014  # Lac Meech Sprint Triathlon


def test_diag_multisport_parent_shape():
    client = get_client()
    raw = client.get_activity(MULTISPORT_ID)
    meta = raw.get('metadataDTO') or {}
    info = {
        'top_level_keys': sorted(raw.keys()),
        'metadata_keys': sorted(meta.keys()),
        'child_ish_top': {k: v for k, v in raw.items() if 'child' in k.lower()},
        'child_ish_meta': {k: v for k, v in meta.items() if 'child' in k.lower()},
        'isMultiSportParent': raw.get('isMultiSportParent'),
        'activityTypeDTO': raw.get('activityTypeDTO'),
    }
    raise AssertionError("DIAG>>> " + json.dumps(info, default=str, indent=2))
