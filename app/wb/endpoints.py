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
    "appType": "1",
    "curr": "rub",
    "dest": "-1257786",
    "lang": "ru",
    "locale": "ru",
    "resultset": "catalog",
    # 2026-05-18: u-search.wb.ru/v18 returns 0 products with sort=priceup.
    # Use popular (verified HTTP 200 + populated products). The /top10
    # handler sorts client-side by price anyway via min_price_rub filter.
    "sort": "popular",
    "spp": "30",
    "suppressSpellcheck": "false",
    "inheritFilters": "false",
}

# WB updated search endpoints in 2026: old search.wb.ru/.../v9-v14 returns 400/429.
# New: u-search.wb.ru/.../v18/search. Keep older variants as fallback.
SEARCH_ENDPOINTS: list[SearchEndpoint] = [
    SearchEndpoint(
        name="u_search_v18",
        url="https://u-search.wb.ru/exactmatch/ru/common/v18/search",
        version=18,
        base_params=_COMMON_PARAMS,
    ),
    SearchEndpoint(
        name="u_search_v17",
        url="https://u-search.wb.ru/exactmatch/ru/common/v17/search",
        version=17,
        base_params=_COMMON_PARAMS,
    ),
    # Legacy fallbacks — may 400/429 in 2026 but keep for backward compat
    SearchEndpoint(
        name="exactmatch_v14",
        url="https://search.wb.ru/exactmatch/ru/common/v14/search",
        version=14,
        base_params={**_COMMON_PARAMS, "ab_testing": "false"},
    ),
]
