"""Adapters package for profiler."""

from profiler.adapters.base import BaseAdapter
from profiler.adapters.brreg import BrregEnhetAdapter
from profiler.adapters.doffin import DoffinAdapter
from profiler.adapters.gleif import GleifAdapter
from profiler.adapters.kunngjoring import BrregKunngjoringAdapter
from profiler.adapters.regnskap import BrregRegnskapAdapter
from profiler.adapters.roller import BrregRollerAdapter
from profiler.adapters.website import WebsiteAdapter
from profiler.adapters.wikidata import WikidataAdapter

__all__ = [
    "BaseAdapter",
    "BrregEnhetAdapter",
    "BrregRollerAdapter",
    "BrregRegnskapAdapter",
    "BrregKunngjoringAdapter",
    "WebsiteAdapter",
    "GleifAdapter",
    "WikidataAdapter",
    "DoffinAdapter",
]
