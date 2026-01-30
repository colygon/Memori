r"""
 __  __                           _
|  \/  | ___ _ __ ___   ___  _ __(_)
| |\/| |/ _ \ '_ ` _ \ / _ \| '__| |
| |  | |  __/ | | | | | (_) | |  | |
|_|  |_|\___|_| |_| |_|\___/|_|  |_|
                 perfectam memoriam
                      memorilabs.ai
"""

import logging
import time
from dataclasses import dataclass

from sqlalchemy.exc import OperationalError

from memori._config import Config
from memori._logging import truncate
from memori.embeddings import embed_texts
from memori.search import search_facts as search_facts_api
from memori.search._types import FactSearchResult, SearchDebug, SearchCandidate

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.05


class Recall:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _resolve_entity_id(self, entity_id: int | None) -> int | None:
        if entity_id is not None:
            return entity_id

        if self.config.entity_id is None:
            logger.debug("Recall aborted - no entity_id configured")
            return None

        entity_id = self.config.storage.driver.entity.create(self.config.entity_id)
        logger.debug("Entity ID resolved: %s", entity_id)
        if entity_id is None:
            logger.debug("Recall aborted - entity_id is None after resolution")
        return entity_id

    def _resolve_limit(self, limit: int | None) -> int:
        return self.config.recall_facts_limit if limit is None else limit

    def _embed_query(self, query: str) -> list[float]:
        logger.debug("Generating query embedding")
        embeddings_config = self.config.embeddings
        return embed_texts(
            query,
            model=embeddings_config.model,
        )[0]

    def _search_with_retries(
        self,
        *,
        entity_id: int,
        query: str,
        query_embedding: list[float],
        limit: int,
        debug: bool = False,
    ) -> list[FactSearchResult] | tuple[list[FactSearchResult], SearchDebug | None]:
        facts: list[FactSearchResult] = []
        debug_info: SearchDebug | None = None
        for attempt in range(MAX_RETRIES):
            try:
                logger.debug(
                    f"Executing search_facts - entity_id: {entity_id}, limit: {limit}, embeddings_limit: {self.config.recall_embeddings_limit}"
                )
                if debug:
                    facts, debug_info = search_facts_api(
                        self.config.storage.driver.entity_fact,
                        entity_id,
                        query_embedding,
                        limit,
                        self.config.recall_embeddings_limit,
                        query_text=query,
                        debug=True,
                    )
                else:
                    facts = search_facts_api(
                        self.config.storage.driver.entity_fact,
                        entity_id,
                        query_embedding,
                        limit,
                        self.config.recall_embeddings_limit,
                        query_text=query,
                    )
                logger.debug("Recall complete - found %d facts", len(facts))
                break
            except OperationalError as e:
                if "restart transaction" in str(e) and attempt < MAX_RETRIES - 1:
                    logger.debug(
                        "Retry attempt %d due to OperationalError", attempt + 1
                    )
                    time.sleep(RETRY_BACKOFF_BASE * (2**attempt))
                    continue
                raise

        if debug:
            return facts, debug_info
        return facts

    def search_facts(
        self,
        query: str,
        limit: int | None = None,
        entity_id: int | None = None,
        debug: bool = False,
    ) -> list[FactSearchResult] | "RecallResult":
        logger.debug(
            "Recall started - query: %s (%d chars), limit: %s",
            truncate(query, 50),
            len(query),
            limit,
        )

        if self.config.storage is None or self.config.storage.driver is None:
            logger.debug("Recall aborted - storage not configured")
            if debug:
                return RecallResult(
                    facts=[],
                    debug=RecallDebug(
                        status="skipped",
                        message="storage not configured",
                        entity_id=None,
                        query=query,
                        limit=limit or self.config.recall_facts_limit,
                        embeddings_limit=self.config.recall_embeddings_limit,
                        timing=RecallTiming(0.0, 0.0, 0.0),
                        candidates=[],
                    ),
                )
            return []

        entity_id = self._resolve_entity_id(entity_id)
        if entity_id is None:
            if debug:
                return RecallResult(
                    facts=[],
                    debug=RecallDebug(
                        status="skipped",
                        message="entity_id not configured",
                        entity_id=None,
                        query=query,
                        limit=limit or self.config.recall_facts_limit,
                        embeddings_limit=self.config.recall_embeddings_limit,
                        timing=RecallTiming(0.0, 0.0, 0.0),
                        candidates=[],
                    ),
                )
            return []

        limit = self._resolve_limit(limit)
        start_time = time.perf_counter()
        embed_start = time.perf_counter()
        query_embedding = self._embed_query(query)
        embed_ms = (time.perf_counter() - embed_start) * 1000

        search_start = time.perf_counter()
        if debug:
            facts, search_debug = self._search_with_retries(
                entity_id=entity_id,
                query=query,
                query_embedding=query_embedding,
                limit=limit,
                debug=True,
            )
        else:
            facts = self._search_with_retries(
                entity_id=entity_id,
                query=query,
                query_embedding=query_embedding,
                limit=limit,
            )
            search_debug = None
        search_ms = (time.perf_counter() - search_start) * 1000
        total_ms = (time.perf_counter() - start_time) * 1000

        if not debug:
            return facts

        return RecallResult(
            facts=facts,
            debug=RecallDebug(
                status="ok",
                message=None,
                entity_id=entity_id,
                query=query,
                limit=limit,
                embeddings_limit=self.config.recall_embeddings_limit,
                timing=RecallTiming(embed_ms, search_ms, total_ms),
                candidates=search_debug.candidates if search_debug else [],
            ),
        )


@dataclass(frozen=True)
class RecallTiming:
    embedding_ms: float
    search_ms: float
    total_ms: float


@dataclass(frozen=True)
class RecallDebug:
    status: str
    message: str | None
    entity_id: int | None
    query: str
    limit: int
    embeddings_limit: int
    timing: RecallTiming
    candidates: list[SearchCandidate]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "message": self.message,
            "entity_id": self.entity_id,
            "query": self.query,
            "limit": self.limit,
            "embeddings_limit": self.embeddings_limit,
            "timing": {
                "embedding_ms": self.timing.embedding_ms,
                "search_ms": self.timing.search_ms,
                "total_ms": self.timing.total_ms,
            },
            "candidates": [
                {
                    "id": c.id,
                    "content": c.content,
                    "similarity": c.similarity,
                    "rank_score": c.rank_score,
                    "selected": c.selected,
                }
                for c in self.candidates
            ],
        }


@dataclass(frozen=True)
class RecallResult:
    facts: list[FactSearchResult]
    debug: RecallDebug
