"""Browser automation to scrape sell prices from FPL website."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime

import keyring

from fpl_cli.paths import user_data_dir

logger = logging.getLogger(__name__)


@dataclass
class PlayerSellPrice:
    """A player's name and sell price."""
    name: str
    sell_price: float  # In millions (e.g., 8.2 = £8.2m)
    position: str = ""
    purchase_price: float = 0.0
    element_id: int | None = None

    @property
    def profit_loss(self) -> float:
        """P/L vs purchase price. Returns 0.0 if purchase_price unknown."""
        if self.purchase_price == 0.0:
            return 0.0
        return self.sell_price - self.purchase_price


@dataclass
class TeamFinances:
    """Team financial state from FPL transfers page."""
    bank: float  # In millions
    free_transfers: int
    squad: list[PlayerSellPrice] = field(default_factory=list)
    total_value: float = 0.0
    scraped_at: str = ""
    extraction_errors: list[str] = field(default_factory=list)

    @property
    def is_suspect(self) -> bool:
        """Whether this data looks like a failed or partial scrape."""
        if not self.squad and self.bank == 0.0:
            return True
        if 0 < len(self.squad) < 11:
            return True
        if self.total_value == 0.0:
            return True
        return False

    @property
    def warnings(self) -> list[str]:
        """Human-readable list of what looks off about this data."""
        msgs = []
        if not self.squad and self.bank == 0.0:
            msgs.append("No squad data and bank is £0.0m - scrape likely failed completely")
        elif not self.squad:
            msgs.append("No squad data extracted")
        if 0 < len(self.squad) < 11:
            msgs.append(f"Only {len(self.squad)} players extracted (expected 15)")
        if self.total_value == 0.0:
            msgs.append("Total squad value is £0.0m")
        if self.extraction_errors:
            for err in self.extraction_errors:
                msgs.append(f"Extraction error: {err}")
        return msgs

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "bank": self.bank,
            "free_transfers": self.free_transfers,
            "squad": [
                {
                    "name": p.name,
                    "sell_price": p.sell_price,
                    "position": p.position,
                    "purchase_price": p.purchase_price,
                    "element_id": p.element_id,
                }
                for p in self.squad
            ],
            "total_value": self.total_value,
            "scraped_at": self.scraped_at,
            "extraction_errors": self.extraction_errors,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TeamFinances:
        """Create from dictionary."""
        squad = [
            PlayerSellPrice(
                name=p["name"],
                sell_price=p["sell_price"],
                position=p.get("position", ""),
                purchase_price=p.get("purchase_price", 0.0),
                element_id=p.get("element_id"),
            )
            for p in data.get("squad", [])
        ]
        return cls(
            bank=data.get("bank", 0.0),
            free_transfers=data.get("free_transfers", 0),
            squad=squad,
            total_value=data.get("total_value", 0.0),
            scraped_at=data.get("scraped_at", ""),
            extraction_errors=data.get("extraction_errors", []),
        )


CACHE_FILE = user_data_dir() / "team_finances.json"


def save_cache(finances: TeamFinances) -> None:
    """Save finances to cache file."""
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(finances.to_dict(), f, indent=2)


def load_cache() -> TeamFinances | None:
    """Load finances from cache file if it exists."""
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return TeamFinances.from_dict(data)
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def cache_age_hours() -> float | None:
    """Get age of cache in hours, or None if no cache."""
    if not CACHE_FILE.exists():
        return None
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        scraped_at = datetime.fromisoformat(data.get("scraped_at", ""))
        age = datetime.now() - scraped_at
        return age.total_seconds() / 3600
    except (ValueError, AttributeError, OSError, TypeError) as e:
        logger.debug("Failed to parse cache age: %s", e)
        return None


class FPLPriceScraper:
    """Scraper for FPL sell prices using Playwright browser automation."""

    FPL_HOME_URL = "https://fantasy.premierleague.com/"
    FPL_TRANSFERS_URL = "https://fantasy.premierleague.com/transfers"

    async def scrape(self, headless: bool = True) -> TeamFinances:
        """Scrape sell prices from FPL transfers page.

        Args:
            headless: Run browser in headless mode (default True)

        Returns:
            TeamFinances with sell prices, bank, and free transfers
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ImportError(
                "Playwright not installed. Run: pip install 'fpl-cli[scraper]'"
            ) from None

        email = os.getenv("FPL_EMAIL") or keyring.get_password("fpl-cli", "email")
        password = os.getenv("FPL_PASSWORD") or keyring.get_password("fpl-cli", "password")
        if not email or not password:
            raise ValueError(
                "FPL credentials required. Run `fpl init` or `fpl credentials set`,"
                " or set FPL_EMAIL and FPL_PASSWORD environment variables."
            )

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=headless)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36",
            )
            page = await context.new_page()

            # Intercept authenticated API responses before any navigation
            my_entry_response: dict | None = None

            async def capture_my_entry(response) -> None:
                nonlocal my_entry_response
                if "/api/my-team/" in response.url and response.status == 200:
                    try:
                        my_entry_response = await response.json()
                    except Exception as e:  # noqa: BLE001 — scraper resilience
                        logger.debug("Failed to capture API JSON: %s", e)

            page.on("response", capture_my_entry)

            try:
                # Login to FPL
                await self._login(page, email, password)

                # Navigate to transfers page - triggers /api/my-team/{id}/ call
                await page.goto(self.FPL_TRANSFERS_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)  # Wait for dynamic content to load

                # May need to accept cookies again on this domain
                await self._accept_cookies(page)
                await page.wait_for_timeout(1000)

                # Extract data
                finances = await self._extract_finances(page, my_entry_response)

                return finances

            finally:
                await browser.close()

    async def _login(self, page, email: str, password: str) -> None:
        """Log in to FPL website."""
        # Start at FPL home page
        await page.goto(self.FPL_HOME_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        # Accept cookies if dialog appears
        await self._accept_cookies(page)

        # Click Log in button to trigger login flow
        sign_in_selectors = [
            "a:has-text('Log in')",
            "button:has-text('Log in')",
            "a:has-text('Sign In')",
            "button:has-text('Sign In')",
            "[data-testid='sign-in']",
            "a[href*='login']",
        ]

        for selector in sign_in_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=3000):
                    await btn.click()
                    await page.wait_for_timeout(3000)
                    break
            except Exception:  # noqa: BLE001 — scraper resilience
                continue

        # Accept cookies on login page if needed
        await self._accept_cookies(page)

        # Wait for login form to load
        await page.wait_for_selector(
            "input[placeholder*='mail'], input[placeholder*='Mail']", timeout=10000
        )

        # Fill email field - look for placeholder "Email address"
        email_selectors = [
            "input[placeholder*='mail']",
            "input[placeholder*='Mail']",
            "input[type='email']",
            "input[name='email']",
            "input[name='login']",
        ]
        for selector in email_selectors:
            try:
                el = page.locator(selector).first
                if await el.is_visible(timeout=1000):
                    await el.fill(email)
                    break
            except Exception:  # noqa: BLE001 — scraper resilience
                continue

        # Fill password field
        password_selectors = [
            "input[placeholder*='assword']",
            "input[type='password']",
            "input[name='password']",
        ]
        for selector in password_selectors:
            try:
                el = page.locator(selector).first
                if await el.is_visible(timeout=1000):
                    await el.fill(password)
                    break
            except Exception:  # noqa: BLE001 — scraper resilience
                continue

        # Submit the login form
        await self._submit_login(page)

    async def _accept_cookies(self, page) -> None:
        """Accept cookie consent dialog if present."""
        cookie_selectors = [
            "button:has-text('Accept All Cookies')",
            "button:has-text('Accept all cookies')",
            "button:has-text('Accept All')",
            "button:has-text('Accept')",
            "[class*='accept']",
            "#onetrust-accept-btn-handler",  # OneTrust cookie banner
            "button[title*='Accept']",
            "[data-testid='accept-cookies']",
        ]

        for selector in cookie_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await page.wait_for_timeout(1000)
                    return
            except Exception:  # noqa: BLE001 — scraper resilience
                continue

    async def _submit_login(self, page) -> None:
        """Submit login form and wait for redirect."""
        # Click submit button
        submit_selectors = [
            "button[type='submit']",
            "button:has-text('Login')",
            "button:has-text('Sign In')",
            "input[type='submit']",
        ]

        for selector in submit_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    break
            except Exception:  # noqa: BLE001 — scraper resilience
                continue

        # Wait for redirect to FPL site
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        try:
            await page.wait_for_url("**/fantasy.premierleague.com/**", timeout=15000)
        except PlaywrightTimeout:
            # Check if we're still on login page (bad credentials)
            if "login" in page.url.lower():
                raise ValueError(
                    "Login failed - check your FPL_EMAIL and FPL_PASSWORD credentials"
                )
            # Otherwise we might be on another page, continue

    async def _extract_finances(self, page, my_entry_response: dict | None = None) -> TeamFinances:
        """Extract financial data - tries intercepted API data first, falls back to DOM."""
        if my_entry_response:
            finances = await self._extract_from_intercepted(page, my_entry_response)
            if finances and not finances.is_suspect:
                return finances

        return await self._extract_via_dom(page)

    async def _extract_from_intercepted(self, page, my_entry_response: dict) -> TeamFinances | None:
        """Build TeamFinances from intercepted /api/my-team/ response data."""
        try:
            bootstrap = await page.evaluate("""
                async () => {
                    const r = await fetch('/api/bootstrap-static/');
                    return await r.json();
                }
            """)
            elements = {e["id"]: e for e in bootstrap.get("elements", [])}
            element_types = {
                et["id"]: et["singular_name_short"]
                for et in bootstrap.get("element_types", [])
            }
        except Exception as e:  # noqa: BLE001 — scraper resilience
            logger.debug("Failed to fetch bootstrap data: %s", e)
            elements = {}
            element_types = {}

        picks = my_entry_response.get("picks", [])
        transfers = my_entry_response.get("transfers", {})
        bank = transfers.get("bank", 0) / 10.0
        free_transfers = transfers.get("limit", 0)

        squad = []
        for pick in picks:
            element_id = pick["element"]
            sell_price = pick["selling_price"] / 10.0
            purchase_price = pick.get("purchase_price", 0) / 10.0
            player = elements.get(element_id, {})
            name = player.get("web_name", f"Player {element_id}")
            pos_id = player.get("element_type", 0)
            position = element_types.get(pos_id, "")
            squad.append(PlayerSellPrice(
                name=name,
                sell_price=sell_price,
                position=position,
                purchase_price=purchase_price,
                element_id=element_id,
            ))

        if not squad:
            return None

        total_value = sum(p.sell_price for p in squad) + bank

        return TeamFinances(
            bank=bank,
            free_transfers=free_transfers,
            squad=squad,
            total_value=total_value,
            scraped_at=datetime.now().isoformat(),
        )

    async def _extract_via_dom(self, page) -> TeamFinances:
        """Fallback: extract financial data by parsing the transfers page DOM."""
        import re

        squad = []
        bank = 0.0
        free_transfers = 0
        errors: list[str] = []

        # Extract budget
        try:
            budget_els = await page.locator("text=Budget").all()
            for el in budget_els:
                parent = el.locator("../..")
                parent_text = await parent.text_content()
                if parent_text:
                    match = re.search(r"£(\d+\.?\d*)m", parent_text)
                    if match:
                        bank = float(match.group(1))
                        break
        except Exception as e:  # noqa: BLE001 — scraper resilience
            errors.append(f"Budget extraction failed: {e}")

        # Extract free transfers
        try:
            ft_els = await page.locator("text=Free Transfer").all()
            for el in ft_els:
                parent = el.locator("../..")
                parent_text = await parent.text_content()
                if parent_text:
                    match = re.search(r"Free\s*Transfers?\s*(\d+)", parent_text, re.IGNORECASE)
                    if match:
                        free_transfers = int(match.group(1))
                        break
        except Exception as e:  # noqa: BLE001 — scraper resilience
            errors.append(f"Free transfers extraction failed: {e}")

        # Extract players from pitch
        try:
            price_elements = await page.locator("text=/£\\d+\\.\\d+m/").all()

            for price_el in price_elements:
                try:
                    price_text = await price_el.text_content()
                    if not price_text:
                        continue

                    if "Budget" in (await price_el.locator("..").text_content() or ""):
                        continue

                    price_match = re.search(r"£(\d+\.?\d*)m", price_text)
                    if not price_match:
                        continue

                    sell_price = float(price_match.group(1))

                    name = None
                    for levels in range(1, 5):
                        parent_selector = "/".join([".."] * levels)
                        try:
                            parent = price_el.locator(parent_selector)
                            parent_text = await parent.text_content()

                            if parent_text and "Budget" not in parent_text:
                                match = re.search(
                                    r"£\d+\.?\d*m\s*([A-Za-zÀ-ÿ][\w\s\.\-\']*?)(?:\s*[A-Z]{3}\s*\([HA]\)|$)",
                                    parent_text
                                )
                                if match:
                                    name = match.group(1).strip()
                                    if name and len(name) > 1:
                                        break
                        except Exception as e:  # noqa: BLE001 — scraper resilience
                            logger.debug("Failed to match sell price for player: %s", e)
                            continue

                    excluded = ["budget", "cost", "pts", "reset", "play", "wildcard", "free hit"]
                    if name and name.lower() not in excluded:
                        squad.append(PlayerSellPrice(name=name, sell_price=sell_price))
                except Exception as e:  # noqa: BLE001 — scraper resilience
                    logger.debug("Failed to parse pitch section: %s", e)
                    continue

        except Exception as e:  # noqa: BLE001 — scraper resilience
            errors.append(f"Squad extraction failed: {e}")

        seen_names: set[str] = set()
        unique_squad = []
        for p in squad:
            if p.name not in seen_names and len(unique_squad) < 15:
                seen_names.add(p.name)
                unique_squad.append(p)
        squad = unique_squad

        total_value = sum(p.sell_price for p in squad) + bank

        return TeamFinances(
            bank=bank,
            free_transfers=free_transfers,
            squad=squad,
            total_value=total_value,
            scraped_at=datetime.now().isoformat(),
            extraction_errors=errors,
        )

