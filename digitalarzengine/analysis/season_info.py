from typing import Literal

SeasonType = Literal["winter", "spring", "summer", "autumn", "annual"]

def get_season_months(season: SeasonType) -> list[int]:
    seasons = {
        "winter": [12, 1, 2],
        "spring": [3, 4, 5],
        "summer": [6, 7, 8],
        "autumn": [9, 10, 11],
        "annual": list(range(1, 13)),
    }

    if season not in seasons:
        raise ValueError(f"Unknown season: {season}")

    return seasons[season]
