from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SearchEndpoint:
    name: str
    url: str
    version: int
    base_params: dict[str, str]

    def build_params(self, query: str, page: int) -> dict[str, str]:
        params = dict(self.base_params)
        params["query"] = query
        params["page"] = str(page)
        return params


_COMMON_PARAMS = {
    "ab_testing": "false",
    "appType": "1",
    "curr": "rub",
    "dest": "-1257786",
    "lang": "ru",
    "resultset": "catalog",
    # We need top cheapest list, so request price-ascending feed by default.
    "sort": "priceup",
    "spp": "30",
    "suppressSpellcheck": "false",
}

# WB endpoints are volatile; keep several versions for graceful fallback.
SEARCH_ENDPOINTS: list[SearchEndpoint] = [
    SearchEndpoint(
        name="exactmatch_v14",
        url="https://search.wb.ru/exactmatch/ru/common/v14/search",
        version=14,
        base_params=_COMMON_PARAMS,
    ),
    SearchEndpoint(
        name="exactmatch_v10",
        url="https://search.wb.ru/exactmatch/ru/common/v10/search",
        version=10,
        base_params=_COMMON_PARAMS,
    ),
    SearchEndpoint(
        name="exactmatch_v9",
        url="https://search.wb.ru/exactmatch/ru/common/v9/search",
        version=9,
        base_params=_COMMON_PARAMS,
    ),
]
