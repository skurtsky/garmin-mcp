"""Build tools/data/garmin_exercise_meta.json from the exported exercise sheet.

Source: an export of
https://docs.google.com/spreadsheets/d/1OaqIaBhPk4xBnkqVPYvFpj2HZNk_ISX0zHgWfA0WXkQ
(several CSV sections, each starting with ``## Sheet name: <name>`` followed by
two header rows). Dev-only; the server never fetches the sheet at runtime:

    python scripts/build_exercise_db.py path/to/garmin_exercises_database.txt
"""
import csv
import io
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "tools" / "data" / "garmin_exercise_meta.json"
MUSCLE_FIRST, MUSCLE_LAST = "ABDUCTORS", "TRICEPS"
EQUIP_FIRST = "ANKLE_WEIGHT"


def parse_sections(text: str):
    section, lines = None, []
    for line in text.splitlines():
        if line.startswith("## Sheet name:"):
            if section:
                yield section, lines
            section, lines = line.split(":", 1)[1].strip(), []
        elif section is not None:
            lines.append(line)
    if section:
        yield section, lines


def build(text: str) -> list[dict]:
    records: dict[tuple[str, str], dict] = {}
    for sheet, lines in parse_sections(text):
        rows = list(csv.reader(io.StringIO("\n".join(lines))))
        header = rows[1]
        idx = {h: i for i, h in enumerate(header) if h}
        muscles = header[idx[MUSCLE_FIRST]: idx[MUSCLE_LAST] + 1]
        equipment = header[idx[EQUIP_FIRST]:] if EQUIP_FIRST in idx else []
        for row in rows[2:]:
            if len(row) < 4 or not row[idx["CATEGORY_GARMIN"]] or not row[idx["NAME_GARMIN"]]:
                continue
            key = (row[idx["CATEGORY_GARMIN"]], row[idx["NAME_GARMIN"]])
            if key in records:
                continue  # first sheet wins (Exercises is first)
            scores = {m: int(row[idx[m]]) for m in muscles if m in idx and row[idx[m]].strip().isdigit()}
            records[key] = {
                "name": row[idx["Name"]],
                "category": key[0],
                "garmin_name": key[1],
                "found": row[idx["FOUND"]].strip().upper() == "TRUE",
                "url": row[idx["URL"]] or None,
                "difficulty": row[idx["DIFFICULTY"]] or None,
                "muscles": {m: s for m, s in scores.items() if s > 0},
                "equipment": [e for e in equipment if e in idx and idx[e] < len(row) and row[idx[e]].strip() == "1"],
                "sheet": sheet,
            }
    return list(records.values())


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    records = build(Path(sys.argv[1]).read_text(encoding="utf-8"))
    OUT.write_text(json.dumps(records, separators=(",", ":"), ensure_ascii=False) + "\n")
    print(f"wrote {len(records)} exercises ({sum(r['found'] for r in records)} found) to {OUT}")


if __name__ == "__main__":
    main()
