"""fpl init - interactive setup for settings.yaml."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

import click
import httpx
from dotenv import dotenv_values, set_key
from rich.table import Table

from fpl_cli.cli._banner import show_banner
from fpl_cli.cli._context import _user_config_dir, console, resolve_format


class StatusDisplay(NamedTuple):
    text: str
    colour: str


def _settings_file() -> Path:
    return _user_config_dir() / "settings.yaml"


def _env_file() -> Path:
    return _user_config_dir() / ".env"


# Pattern: direct-api

_CLASSIC_FIELDS = (
    ("classic_entry_id", "Classic entry ID (your team page URL: fantasy.premierleague.com/entry/YOUR_ID/...)"),
    ("classic_league_id", "Classic league ID (your league page URL: fantasy.premierleague.com/leagues/YOUR_ID/...)"),
)

_DRAFT_FIELDS = (
    ("draft_entry_id", "Draft entry ID (your team page URL: draft.premierleague.com/entry/YOUR_ID/...)"),
)

_DRAFT_API = "https://draft.premierleague.com/api/entry/{id}/public"


def _fetch_draft_league_id(entry_id: int) -> int | None:
    """Derive draft_league_id from the draft API's entry endpoint."""
    try:
        resp = httpx.get(_DRAFT_API.format(id=entry_id), timeout=10)
        resp.raise_for_status()
        league_set = resp.json()["entry"]["league_set"]
        return league_set[0] if league_set else None
    except (httpx.HTTPError, KeyError, IndexError):
        return None


def _prompt_penalty(existing_rules: list[dict[str, Any]], rule_type: str, rule: dict[str, Any]) -> None:
    """Prompt for penalty text and add to rule dict if non-empty."""
    default = _rule_field(existing_rules, rule_type, "penalty", "")
    penalty = click.prompt("    Penalty (e.g. £5)", default=default, show_default=bool(default))
    if penalty:
        rule["penalty"] = penalty


def _prompt_fines_config(fmt: str, existing: dict[str, Any]) -> dict[str, Any]:
    """Prompt for fines configuration and return the fines settings dict."""
    fines: dict[str, Any] = {}
    existing_classic = existing.get("classic", [])
    existing_draft = existing.get("draft", [])

    if fmt in ("classic", "both"):
        classic_rules: list[dict[str, Any]] = []
        if click.confirm("  Last-place fine (classic)?", default=_has_rule(existing_classic, "last-place")):
            rule: dict[str, Any] = {"type": "last-place"}
            _prompt_penalty(existing_classic, "last-place", rule)
            classic_rules.append(rule)

        if click.confirm("  Below-threshold fine (classic)?", default=_has_rule(existing_classic, "below-threshold")):
            default_threshold = _rule_field(existing_classic, "below-threshold", "threshold", 25)
            threshold = click.prompt("    Points threshold", type=int, default=default_threshold)
            rule = {"type": "below-threshold", "threshold": threshold}
            _prompt_penalty(existing_classic, "below-threshold", rule)
            classic_rules.append(rule)

        if click.confirm("  Red card fine (classic)?", default=_has_rule(existing_classic, "red-card")):
            rule = {"type": "red-card"}
            _prompt_penalty(existing_classic, "red-card", rule)
            classic_rules.append(rule)

        if classic_rules:
            fines["classic"] = classic_rules

    if fmt in ("draft", "both"):
        draft_rules: list[dict[str, Any]] = []
        if click.confirm("  Last-place fine (draft)?", default=_has_rule(existing_draft, "last-place")):
            rule = {"type": "last-place"}
            _prompt_penalty(existing_draft, "last-place", rule)
            draft_rules.append(rule)

        if click.confirm("  Below-threshold fine (draft)?", default=_has_rule(existing_draft, "below-threshold")):
            default_threshold = _rule_field(existing_draft, "below-threshold", "threshold", 25)
            threshold = click.prompt("    Points threshold", type=int, default=default_threshold)
            rule = {"type": "below-threshold", "threshold": threshold}
            _prompt_penalty(existing_draft, "below-threshold", rule)
            draft_rules.append(rule)

        if draft_rules:
            fines["draft"] = draft_rules

    if fines:
        escalation = click.prompt(
            "  Escalation note (optional, e.g. 'Fines double each GW')",
            default=existing.get("escalation_note", ""),
            show_default=False,
        )
        if escalation:
            fines["escalation_note"] = escalation

    return fines


def _prompt_llm_config(existing: dict[str, Any]) -> dict[str, Any]:
    """Prompt for LLM provider configuration and return the llm settings dict."""
    from fpl_cli.api.providers import PROVIDER_NAMES, PROVIDERS

    provider_names = sorted(PROVIDER_NAMES)
    llm: dict[str, Any] = {}

    for role in ("research", "synthesis"):
        existing_role = existing.get(role, {})
        default_provider = existing_role.get("provider", "perplexity" if role == "research" else "anthropic")

        provider = click.prompt(
            f"  {role.title()} provider",
            type=click.Choice(provider_names, case_sensitive=False),
            default=default_provider,
        )

        provider_cls = PROVIDERS[provider]
        provider_unchanged = provider == existing_role.get("provider")
        default_model = (
            existing_role.get("model", provider_cls.DEFAULT_MODEL)
            if provider_unchanged
            else provider_cls.DEFAULT_MODEL
        )
        model = click.prompt(f"  {role.title()} model", default=default_model)

        role_cfg: dict[str, Any] = {"provider": provider, "model": model}

        # Prompt for base_url if openai provider
        if provider == "openai":
            existing_url = existing_role.get("base_url", "")
            if existing_url or click.confirm("  Custom base URL? (for Ollama, Groq, etc.)", default=bool(existing_url)):
                base_url = click.prompt("  Base URL", default=existing_url or "http://localhost:11434/v1")
                role_cfg["base_url"] = base_url

        # Check API key
        api_key_var = provider_cls.API_KEY_ENV_VAR
        if os.environ.get(api_key_var):
            click.echo(f"  ✓ {api_key_var} is set")
        else:
            click.echo(f"  Set {api_key_var} - get your key from {provider_cls.KEY_SETUP_URL}")

        # Warn about non-search providers for research role
        if role == "research" and provider != "perplexity":
            click.echo("  Note: this provider has no built-in web search. Research results may vary.")

        llm[role] = role_cfg

    return llm


def _has_rule(rules: list[dict[str, Any]], rule_type: str) -> bool:
    return any(r.get("type") == rule_type for r in rules)


def _rule_field(rules: list[dict[str, Any]], rule_type: str, field: str, default: Any = None) -> Any:
    for r in rules:
        if r.get("type") == rule_type:
            return r.get(field, default)
    return default


def _keyring_available() -> bool:
    """Check whether the system keyring backend is functional."""
    try:
        import keyring

        backend = keyring.get_keyring()
        return "fail" not in type(backend).__module__
    except ImportError:
        return False
    except Exception as exc:  # noqa: BLE001 — keyring backend unpredictable
        click.echo(f"  Keyring probe failed: {exc}", err=True)
        return False


def _mask_key(key: str) -> str:
    """Mask an API key for display, showing first/last 4 chars."""
    if len(key) > 8:
        return key[:4] + "..." + key[-4:]
    return "****"


# ---------------------------------------------------------------------------
# Tier functions
# ---------------------------------------------------------------------------


def _tier_fpl_ids(data: dict[str, Any], fmt: str) -> None:
    """Tier 1 (Required): Collect FPL entry and league IDs."""
    existing_fpl = data.get("fpl", {}) or {}

    fpl_section: dict[str, int] = {}
    if fmt in ("classic", "both"):
        for key, description in _CLASSIC_FIELDS:
            default = existing_fpl.get(key) or None
            value = click.prompt(description, type=int, default=default)
            fpl_section[key] = value

    if fmt in ("draft", "both"):
        for key, description in _DRAFT_FIELDS:
            default = existing_fpl.get(key) or None
            entry_id = click.prompt(description, type=int, default=default)
            fpl_section[key] = entry_id

        league_id = _fetch_draft_league_id(fpl_section["draft_entry_id"])
        if league_id is not None:
            click.echo(f"Found draft league: {league_id}")
            fpl_section["draft_league_id"] = league_id
        else:
            click.echo("Could not auto-detect draft league ID.")
            league_default = existing_fpl.get("draft_league_id") or None
            fpl_section["draft_league_id"] = click.prompt(
                "Draft league ID", type=int, default=league_default
            )

    # Merge into data, preserving CommentedMap
    data.setdefault("fpl", {})

    # Format downgrade: remove IDs for deselected format
    if fmt == "classic":
        data["fpl"].pop("draft_entry_id", None)
        data["fpl"].pop("draft_league_id", None)
    elif fmt == "draft":
        data["fpl"].pop("classic_entry_id", None)
        data["fpl"].pop("classic_league_id", None)

    data["fpl"].update(fpl_section)


def _tier_custom_analysis(data: dict[str, Any]) -> None:
    """Tier 2 (Optional): Enable custom analysis features (scoring, rankings, recommendations)."""
    configured = data.get("custom_analysis") is not None

    if configured:
        current = bool(data.get("custom_analysis"))
        state = "enabled" if current else "disabled"
        prompt = f"Custom Analysis ({state}) - Reconfigure?"
    else:
        prompt = (
            "Enable Custom Analysis? (captain picks, transfer targets, value scores, "
            "Bayesian FDR - uses custom scoring algorithms under active development)"
        )

    if not click.confirm(prompt, default=False):
        return

    data["custom_analysis"] = click.confirm(
        "  Enable custom analysis features?",
        default=data.get("custom_analysis", False),
    )


def _tier_ai_features(data: dict[str, Any]) -> None:
    """Tier 3 (Optional): Configure LLM providers for AI-powered commands."""
    existing_llm = data.get("llm", {}) or {}
    configured = bool(data.get("llm"))

    if configured:
        prompt = "AI Features (configured) - Reconfigure?"
    else:
        prompt = "Configure AI Features? (unlocks review --summarise, preview --scout)"

    if not click.confirm(prompt, default=False):
        return

    llm_section = _prompt_llm_config(existing_llm)
    data.setdefault("llm", {})
    data["llm"].update(llm_section)


def _tier_league_table() -> None:
    """Tier 4 (Optional): Configure football-data.org API key for league standings."""
    env_values = dotenv_values(_env_file())
    existing_key = env_values.get("FOOTBALL_DATA_API_KEY", "") or ""
    configured = bool(existing_key)

    if configured:
        prompt = "football-data.org API key (configured) - Reconfigure?"
    else:
        prompt = "football-data.org API key? (adds real PL standings to your weekly review)"

    if not click.confirm(prompt, default=False):
        return

    if existing_key:
        click.echo(f"  Current key: {_mask_key(existing_key)}")

    key = click.prompt(
        "  API key (free at football-data.org/client/register)",
        default="",
        show_default=False,
    )

    key = key.strip()
    if not key and existing_key:
        # User pressed Enter without typing - keep existing key
        return

    if key:
        # Pre-create with restricted permissions to avoid world-readable window
        env = _env_file()
        if not env.exists():
            env.touch(mode=0o600)
        set_key(str(env), "FOOTBALL_DATA_API_KEY", key, encoding="utf-8")
        if os.name != "nt":
            env.chmod(0o600)


def _tier_fines(data: dict[str, Any], fmt: str) -> None:
    """Tier 5 (Optional): Configure fines tracking rules."""
    existing_fines = data.get("fines", {}) or {}
    configured = bool(data.get("fines"))

    if configured:
        prompt = "Fines Tracking (configured) - Reconfigure?"
    else:
        prompt = "Configure Fines Tracking? (unlocks fines in status)"

    if not click.confirm(prompt, default=False):
        return

    fines_section = _prompt_fines_config(fmt, existing_fines)
    data.setdefault("fines", {})
    data["fines"].update(fines_section)


def _tier_fpl_login() -> None:
    """Tier 6 (Optional): Store FPL credentials in system keyring."""
    if not _keyring_available():
        return

    import keyring

    has_email = bool(keyring.get_password("fpl-cli", "email"))

    if has_email:
        prompt = "FPL Login (configured) - Reconfigure?"
    else:
        prompt = "Configure FPL Login? (unlocks squad sell-prices --refresh)"

    if not click.confirm(prompt, default=False):
        return

    existing_email = keyring.get_password("fpl-cli", "email") or ""
    email = click.prompt("  FPL email", default=existing_email)
    password = click.prompt(
        "  FPL password (press Enter to keep existing)" if existing_email else "  FPL password",
        default="",
        hide_input=True,
        show_default=False,
    )
    if email:
        keyring.set_password("fpl-cli", "email", email)
    if password:
        keyring.set_password("fpl-cli", "password", password)


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

_TIER_UNLOCKS = {
    "FPL IDs": "Core commands: status, squad, league, review, preview",
    "Custom Analysis": "captain, targets, differentials, value scores, Bayesian FDR",
    "AI Features": "review --summarise, preview --scout",
    "football-data.org": "Premier League table in review",
    "Fines Tracking": "Fines in status",
    "FPL Login": "squad sell-prices --refresh",
}


def _detect_fpl_ids_status(data: dict[str, Any]) -> StatusDisplay:
    """Detect FPL IDs status from in-memory data."""
    fpl = data.get("fpl", {})
    has_classic = bool(fpl.get("classic_entry_id"))
    has_draft = bool(fpl.get("draft_league_id"))
    if has_classic and has_draft:
        return StatusDisplay("Configured (both)", "green")
    if has_classic:
        return StatusDisplay("Configured (classic)", "green")
    if has_draft:
        return StatusDisplay("Configured (draft)", "green")
    return StatusDisplay("Not configured", "red")


def _detect_custom_analysis_status(data: dict[str, Any]) -> StatusDisplay:
    """Detect Custom Analysis status from in-memory data."""
    value = data.get("custom_analysis")
    if value is None:
        return StatusDisplay("Skipped", "dim")
    return StatusDisplay("Enabled", "green") if value else StatusDisplay("Disabled", "yellow")


def _detect_ai_status(data: dict[str, Any]) -> StatusDisplay:
    """Detect AI Features status from settings + env vars."""
    llm = data.get("llm", {})
    if not llm or (not llm.get("research") and not llm.get("synthesis")):
        return StatusDisplay("Skipped", "dim")

    # Check if required API keys are set (these come from shell env, not stale)
    from fpl_cli.api.providers import PROVIDERS

    missing_keys = []
    for role in ("research", "synthesis"):
        role_cfg = llm.get(role, {})
        provider_name = role_cfg.get("provider")
        if provider_name and provider_name in PROVIDERS:
            provider_cls = PROVIDERS[provider_name]
            if not os.environ.get(provider_cls.API_KEY_ENV_VAR):
                missing_keys.append(provider_cls.API_KEY_ENV_VAR)

    if missing_keys:
        unique = sorted(set(missing_keys))
        return StatusDisplay(f"Missing: {', '.join(unique)}", "yellow")
    return StatusDisplay("Configured", "green")


def _detect_league_table_status() -> StatusDisplay:
    """Detect League Table status from .env file (not os.environ - may be stale)."""
    env_values = dotenv_values(_env_file())
    if env_values.get("FOOTBALL_DATA_API_KEY"):
        return StatusDisplay("Configured", "green")
    return StatusDisplay("Skipped", "dim")


def _detect_fines_status(data: dict[str, Any]) -> StatusDisplay:
    """Detect Fines Tracking status from in-memory data."""
    fines = data.get("fines", {})
    if fines and (fines.get("classic") or fines.get("draft")):
        return StatusDisplay("Configured", "green")
    return StatusDisplay("Skipped", "dim")


def _detect_fpl_login_status() -> StatusDisplay:
    """Detect FPL Login status from system keyring (live, no caching)."""
    try:
        import keyring

        if keyring.get_password("fpl-cli", "email"):
            return StatusDisplay("Configured", "green")
        return StatusDisplay("Skipped", "dim")
    except ImportError:
        return StatusDisplay("Keyring unavailable", "yellow")
    except Exception as exc:  # noqa: BLE001 — keyring backend unpredictable
        click.echo(f"  Keyring check failed: {exc}", err=True)
        return StatusDisplay("Keyring unavailable", "yellow")


def _render_summary_table(data: dict[str, Any]) -> None:
    """Display configuration summary after init completes."""
    table = Table(title="Setup Summary", show_header=True, header_style="bold")
    table.add_column("Tier", style="bold")
    table.add_column("Status")
    table.add_column("Unlocks", style="dim")

    detectors: list[tuple[str, StatusDisplay]] = [
        ("FPL IDs", _detect_fpl_ids_status(data)),
        ("Custom Analysis", _detect_custom_analysis_status(data)),
        ("AI Features", _detect_ai_status(data)),
        ("football-data.org", _detect_league_table_status()),
        ("Fines Tracking", _detect_fines_status(data)),
        ("FPL Login", _detect_fpl_login_status()),
    ]

    for tier_name, status in detectors:
        table.add_row(
            tier_name,
            f"[{status.colour}]{status.text}[/{status.colour}]",
            _TIER_UNLOCKS[tier_name],
        )

    console.print()
    console.print(table)


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------


@click.command("init")
def init_command() -> None:
    """Set up fpl-cli with your FPL IDs and optional features."""
    from ruamel.yaml import YAML, YAMLError

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True  # type: ignore[assignment]

    if _settings_file().exists():
        data = yaml.load(_settings_file().read_text(encoding="utf-8"))
        if data is None:
            data = {}
    else:
        data = {}

    show_banner()

    # --- Tier 1: FPL IDs (Required) ---
    current_format = resolve_format(data)
    fmt_default = current_format.value if current_format else "both"

    fmt_prompt = "Which FPL format do you play?"
    fmt = click.prompt(
        fmt_prompt,
        type=click.Choice(["classic", "draft", "both"], case_sensitive=False),
        default=fmt_default,
    )

    if fmt in ("classic", "both"):
        data["use_net_points"] = click.confirm(
            "Include transfer hits in gameweek points rankings? (affects league, review, fines)",
            default=data.get("use_net_points", False),
        )

    _tier_fpl_ids(data, fmt)

    # --- Tier 2: Custom Analysis (Optional) ---
    _tier_custom_analysis(data)

    # --- Tier 3: AI Features (Optional) ---
    _tier_ai_features(data)

    # --- Tier 4: League Table (Optional) ---
    _tier_league_table()

    # --- Tier 5: Fines Tracking (Optional) ---
    _tier_fines(data, fmt)

    # --- Tier 6: FPL Login (Optional) ---
    _tier_fpl_login()

    # Save settings.yaml atomically
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=_settings_file().parent,
        suffix=".yaml",
        delete=False,
    )
    try:
        yaml.dump(data, tmp)
        tmp.close()
        Path(tmp.name).replace(_settings_file())
        if os.name != "nt":
            _settings_file().chmod(0o600)
    except (OSError, YAMLError):
        Path(tmp.name).unlink(missing_ok=True)
        raise

    # Summary table
    _render_summary_table(data)

    click.echo(f"\nSettings saved to {_settings_file()}")
