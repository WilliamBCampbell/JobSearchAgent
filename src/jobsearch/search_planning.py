"""Application-owned search presets and deduplication, independent of the scraper."""

DEFAULT_PROFILES = {
    "defaults": {
        "sites": ["indeed", "linkedin"],
        "location": "USA",
        "country_indeed": "usa",
        "is_remote": True,
        "results_wanted": 15,
        "hours_old": 168,
        "distance": 50,
        "fetch_description": True,
    },
    "profiles": {
        "software_dev": {
            "terms": [".NET software engineer", "AI engineer", "AI agent engineer"]
        },
        "solutions": {"terms": ["solutions engineer"]},
        "customer": {"terms": ["customer engineer"]},
        "sales": {"terms": ["sales engineer"]},
        "architecture": {"terms": ["solutions architect"]},
        "ai": {"terms": ["AI solutions engineer"]},
        "implementation": {
            "terms": ["implementation engineer", "integration engineer"]
        },
    },
}


def build_plan(args, config):
    settings = config["defaults"] | config["profiles"][args.profile]
    terms, sites = (
        args.term or settings.pop("terms"),
        args.sites or settings.pop("sites"),
    )
    settings.pop("terms", None)
    settings.pop("sites", None)
    for arg, key in (
        ("location", "location"),
        ("remote", "is_remote"),
        ("results", "results_wanted"),
        ("hours", "hours_old"),
        ("descriptions", "fetch_description"),
    ):
        if getattr(args, arg) is not None:
            settings[key] = getattr(args, arg)
    settings.update(description_format="markdown", verbose=2)
    return [
        settings | {"site_name": [site], "search_term": term}
        for term in terms
        for site in sites
    ]


def deduplicate(rows):
    unique = {}
    for index, row in enumerate(rows):
        identity = row.get("id") or row.get("job_url") or f"missing-id-{index}"
        key = (row.get("site"), identity)
        if key not in unique:
            unique[key] = row | {"search_terms": [row["search_term"]]}
        else:
            saved = unique[key]
            if row["search_term"] not in saved["search_terms"]:
                saved["search_terms"].append(row["search_term"])
            for field, value in row.items():
                if (
                    field not in ("search_term", "search_terms")
                    and (
                        field not in saved or saved[field] is None or saved[field] == ""
                    )
                    and value is not None
                    and value != ""
                ):
                    saved[field] = value
    return list(unique.values())
