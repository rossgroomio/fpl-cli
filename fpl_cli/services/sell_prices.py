"""Cached sell prices brought forward to today's prices (#426).

The scraper caches FPL's own sell price for each squad player, read from the
logged-in `my-team` endpoint, the only live source for squad, bank and free
transfers. Prices move overnight, so a cached sell price goes stale on the
first price change after the scrape, while the rest stays right until the
next transfer. The sell-on rule is plain arithmetic on the purchase price and
today's price, so `reprice_squad` recomputes it, but only for a player whose
cached figure the rule reproduces at the price stored with the scrape. A
player it cannot check keeps the scraped price, and the warning builders here
say so in the same words whichever command shows it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fpl_cli.models.player import Player
    from fpl_cli.scraper.fpl_prices import PlayerSellPrice, TeamFinances

# `metadata.warnings` code for sell prices left as scraped that may be out of date.
AS_SCRAPED = "sell_prices_as_scraped"

# `metadata.warnings` code for an OUT player `transfer-eval` cannot find in the cached squad.
PLAYER_NOT_CACHED = "sell_prices_player_not_cached"

_REFRESH = "Re-run `fpl squad sell-prices --refresh`"


def selling_price(purchase: int, current: int) -> int:
    """FPL's sell price for a player bought at *purchase* and now priced at *current*.

    Both in £0.1m units. A fall is taken in full; of a rise the seller keeps
    half, rounded down, since the 50% sell-on fee rounds up (`docs/fpl-rules.md`).
    """
    if current <= purchase:
        return current
    return purchase + (current - purchase) // 2


def _tenths(millions: float) -> int:
    return round(millions * 10)


def _sell_price_today(row: PlayerSellPrice, player: Player | None) -> float | None:
    """*row*'s sell price at *player*'s price today, or None if it cannot be vouched for."""
    if player is None or not row.is_player(player) or row.price_at_scrape is None:
        return None
    today = player.now_cost
    at_scrape = _tenths(row.price_at_scrape)
    if today == at_scrape:
        return row.scraped_sell_price
    # The rule is trusted for this row only if it reproduces what FPL itself
    # said at the scrape. A purchase price of 0 is unknown.
    purchase = _tenths(row.purchase_price)
    if purchase and selling_price(purchase, at_scrape) == _tenths(row.scraped_sell_price):
        return selling_price(purchase, today) / 10
    return None


@dataclass
class Repricing:
    """A cached squad brought forward to today's prices by `reprice_squad`."""
    finances: TeamFinances
    # Rows whose sell price changed with a price change since the scrape.
    updated: list[PlayerSellPrice] = field(default_factory=list)
    # Rows left as scraped because they could not be checked: they may be out of date.
    unverified: list[PlayerSellPrice] = field(default_factory=list)

    def notes(self) -> list[str]:
        """The line naming each sell price a price change moved, for stderr."""
        if not self.updated:
            return []
        moves = ", ".join(
            f"{p.name} £{p.scraped_sell_price:.1f}m → £{p.sell_price:.1f}m"
            for p in self.updated
        )
        count = len(self.updated)
        return [
            f"{count} sell price{'s' if count != 1 else ''} updated for price changes"
            f" since the scrape: {moves}"
        ]

    def warnings(self) -> list[dict[str, str]]:
        """The `metadata.warnings` entry naming the squad's unverified sell prices."""
        if not self.unverified:
            return []
        names = ", ".join(p.name for p in self.unverified)
        return [{
            "code": AS_SCRAPED,
            "message": (
                f"{len(self.unverified)} of {len(self.finances.squad)} sell prices could not"
                " be checked against today's prices and are as scraped, so may be out of"
                f" date: {names}. {_REFRESH}."
            ),
        }]


def reprice_squad(finances: TeamFinances, players: Iterable[Player]) -> Repricing:
    """Recompute each cached sell price from its purchase price and today's price.

    A row the rule cannot be checked on keeps its scraped price and is listed
    in `unverified`: a scrape-time check that fails, an unknown purchase price,
    a player missing from *players*, or a cache scraped before scrape-time
    prices were stored. The squad, bank and free transfers are left as
    scraped. Saving the result is harmless, since `TeamFinances.to_dict`
    writes each row's scraped figure, but it is meant as a view for this run.
    """
    by_id = {p.id: p for p in players}
    squad: list[PlayerSellPrice] = []
    updated: list[PlayerSellPrice] = []
    unverified: list[PlayerSellPrice] = []
    for row in finances.squad:
        player = by_id.get(row.element_id) if row.element_id is not None else None
        today = _sell_price_today(row, player)
        scraped = row.scraped_sell_price
        repriced = replace(row, sell_price=scraped if today is None else today, _scraped=scraped)
        squad.append(repriced)
        if today is None:
            unverified.append(repriced)
        elif _tenths(today) != _tenths(scraped):
            updated.append(repriced)
    total_value = sum(p.sell_price for p in squad) + finances.bank
    return Repricing(replace(finances, squad=squad, total_value=total_value), updated, unverified)


def out_player_warnings(repricing: Repricing, out_player: Player) -> list[dict[str, str]]:
    """`transfer-eval`'s warning when the OUT player's sell price is missing or unchecked.

    Affordability is built on that one price, so the reader is told when it
    is absent (not in the cached squad, or a cache with no player ids to
    match on) or as scraped and possibly out of date.
    """
    name = out_player.web_name
    if not any(p.is_player(out_player) for p in repricing.finances.squad):
        return [{
            "code": PLAYER_NOT_CACHED,
            "message": (
                f"{name} could not be matched to the squad in the sell-price cache, so"
                " affordability is not shown. If the squad has changed since the scrape,"
                " or the scrape could not read player ids, re-run"
                " `fpl squad sell-prices --refresh`."
            ),
        }]
    if any(p.is_player(out_player) for p in repricing.unverified):
        return [{
            "code": AS_SCRAPED,
            "message": (
                f"{name}'s sell price could not be checked against today's price and is as"
                f" scraped, so the affordability figures may be out of date. {_REFRESH}."
            ),
        }]
    return []


def carried_warnings(warnings: Sequence[object]) -> list[dict[str, str]]:
    """The `sell_prices_as_scraped` entries from a sell-prices payload's `metadata.warnings`.

    `fpl allocate --sell-prices` budgets on the file's sell prices, so it
    repeats these in its own output rather than budgeting silently on a price
    the file itself says may be out of date.
    """
    return [
        {"code": AS_SCRAPED, "message": str(w.get("message", ""))}
        for w in warnings
        if isinstance(w, dict) and w.get("code") == AS_SCRAPED
    ]
