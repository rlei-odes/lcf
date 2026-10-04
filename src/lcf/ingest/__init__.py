"""Turning files into material a question can be asked of.

A pure package, like `spec/` and `engine/`: bytes and text in, dataclasses out.
No database, no HTTP, no object storage, and `tests/test_boundaries.py` asserts
it stays that way. That is what keeps the parser, the chunker and the ranker
testable with a bytestring and an assertion — which is the majority of what can
go wrong in here.

Only the data types are re-exported here. The functions stay on their modules on
purpose: `parse.parse`, `chunk.chunk_source`, `language.detect`. Lifting
`parse.parse` to `lcf.ingest.parse` would shadow the submodule of the same name,
so `from lcf.ingest import parse` would hand a caller the function where it
expected the module — which fails at the first attribute access rather than at
import, a long way from the line that caused it.
"""

from lcf.ingest.chunk import Chunk
from lcf.ingest.commands import COMMAND_KINDS, Command
from lcf.ingest.parse import Attachment, Image, Parsed, ParseFailed, Unit

__all__ = [
    "COMMAND_KINDS",
    "Attachment",
    "Chunk",
    "Command",
    "Image",
    "ParseFailed",
    "Parsed",
    "Unit",
]
