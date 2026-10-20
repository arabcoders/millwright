from __future__ import annotations

from pycountry import languages


def normalize_language(code: str, *, title: str | None = None) -> str:
    # Code fields take precedence over names. The name "En" would otherwise
    # shadow English's ISO 639-1 code "en" in the registry's generic lookup.
    value = code.strip().lower()
    for field in ("alpha_2", "alpha_3", "bibliographic"):
        language = languages.get(**{field: value})
        if language is not None:
            return language.alpha_3
    try:
        return languages.lookup(code.strip()).alpha_3
    except LookupError:
        # Some media uses invalid tags with an exact language name as its title.
        # Resolve that metadata through the same ISO database, without fuzzy guesses.
        if title:
            try:
                return languages.lookup(title.strip()).alpha_3
            except LookupError:
                pass
        # Preserve unrecognized metadata rather than inventing a language.
        return code
