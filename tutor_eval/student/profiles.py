import json
from pathlib import Path

import yaml


def load_kg(kg_path: Path) -> dict:
    with open(kg_path) as f:
        return json.load(f)


def load_profiles(profiles_path: Path) -> list[dict]:
    with open(profiles_path) as f:
        data = yaml.safe_load(f)
    return data.get("profiles", [])


def get_profile(profiles_path: Path, name: str) -> dict:
    profiles = load_profiles(profiles_path)
    for p in profiles:
        if p.get("name") == name:
            return p
    available = [p.get("name") for p in profiles]
    raise ValueError(f"Profile {name!r} not found. Available: {available}")
