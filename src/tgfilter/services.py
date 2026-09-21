"""Service container: assembled once in Application.post_init, attached to bot_data['services']."""
from __future__ import annotations

from dataclasses import dataclass

from .channel_fetch import ChannelFetcher
from .config import Settings
from .jev import JevClient
from .llm import TemplateCompiler
from .pipeline import Pipeline
from .store import Store


@dataclass
class Services:
    settings: Settings
    store: Store
    fetcher: ChannelFetcher
    jev: JevClient
    compiler: TemplateCompiler | None
    pipeline: Pipeline
