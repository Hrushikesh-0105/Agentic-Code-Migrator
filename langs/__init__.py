"""Language registry: everything the pipeline knows about a programming language, as data.

Every layer reads it instead of hard-coding a language:
  Layer 0      which tree-sitter grammar parses a source, which node types are functions / classes / imports
  Layer 2      naming, the neutral -> target type map, the error idiom, how code is laid out in files, idiom
               notes and per-pair feature rules for the prompts (see docs/prompting-survey.md for why these
               are data), comment syntax (slots, buggy-line markers)
  sandbox      the image, how to build a tree, how the differential harness reaches a function, the project's
               own test command
Standard library only (Layer 0 imports it inside the analysis container too).

    from langs import get, pair
    get("rust").types["list"]          -> "Vec<{0}>"
    pair("python3", "go").rules        -> feature-mapping rules for that direction
"""
from .catalog import LANGUAGES
from .model import Language, Pair
from .pairs import pair_rules

_BY_NAME = {}
for _l in LANGUAGES:
    for _n in (_l.id, _l.name, *_l.aliases):
        _BY_NAME[_n.lower()] = _l


def get(name):
    """A language by id, display name or alias ("python3", "Python", "C++", "cpp", "C#", "csharp", ...)."""
    try:
        return _BY_NAME[str(name).lower()]
    except KeyError:
        raise KeyError(f"unknown language {name!r}; known: {sorted({l.id for l in LANGUAGES})}") from None


def pair(source, target):
    s, t = get(source), get(target)
    return Pair(s, t, pair_rules(s, t))


def all_languages():
    return list(LANGUAGES)


__all__ = ["Language", "Pair", "get", "pair", "all_languages", "LANGUAGES"]
