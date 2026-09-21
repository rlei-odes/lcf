"""YAML in, YAML out.

ruamel rather than PyYAML because specs are meant to be git-tracked, and losing a
rule builder's comments on every export would make that useless.
"""

import io
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from lcf.spec.models import DocTypeSpec

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 100
_yaml.indent(mapping=2, sequence=4, offset=2)


def to_data(text: str) -> Any:
    """YAML text to plain Python, with no opinion about whether it is a spec.

    Split out from `parse` so a caller can tell "that is not YAML" apart from
    "that is YAML but not a spec" — two different things to say to whoever is
    editing it.
    """
    return _to_plain(_yaml.load(text))


def parse(text: str) -> DocTypeSpec:
    """Parse YAML text into a spec. Raises pydantic.ValidationError on bad shape."""
    return DocTypeSpec.model_validate(to_data(text))


def dump_data(data: Any) -> str:
    """Serialise raw spec data — a draft, which need not be valid yet."""
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def load(path: str | Path) -> DocTypeSpec:
    return parse(Path(path).read_text(encoding="utf-8"))


def dump(spec: DocTypeSpec) -> str:
    """Serialise a spec back to YAML.

    Round-trips through the *model*, not the source text: comments in a file we
    never parsed cannot be recovered. Equality of the reloaded model is the
    property we guarantee, and the one the tests assert.
    """
    return dump_data(spec.to_dict())


def _to_plain(data: Any) -> Any:
    """ruamel returns its own mapping/sequence types; Pydantic is happier with plain ones."""
    if isinstance(data, dict):
        return {str(k): _to_plain(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_to_plain(v) for v in data]
    return data
