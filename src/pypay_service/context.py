from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .config import ServiceConfig
from .db import Database
from .gateways import GatewayRegistry
from .notifier import Notifier
from .security import SecretBox


@dataclass(frozen=True)
class AppContext:
    config: ServiceConfig
    db: Database
    box: SecretBox
    gateways: GatewayRegistry
    notifier: Notifier
    clock: Callable[[], datetime]
