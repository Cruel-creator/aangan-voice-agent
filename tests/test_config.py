from pathlib import Path

from app.config import Settings, load_rules

ROOT = Path(__file__).resolve().parent.parent


def test_rules_yaml_loads_into_typed_models():
    rules = load_rules()
    assert rules.timeline.too_soon_below_weeks < rules.timeline.tight_below_weeks
    assert rules.timezone == "Asia/Kolkata"


def test_env_example_lists_every_setting():
    listed = {
        line.split("=", 1)[0].strip()
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert {name.upper() for name in Settings.model_fields} <= listed


def test_blank_values_from_render_form_fall_back_to_defaults(monkeypatch):
    """Fields left empty in the hosting dashboard must not stop the app from booting."""
    for key in (
        "USD_INR_RATE",
        "TELEPHONY_COST_INR_PER_MIN",
        "FIXED_MONTHLY_COST_INR",
        "SMS_PROVIDER",
        "PUBLIC_BASE_URL",
        "TELEGRAM_BOT_TOKEN",
    ):
        monkeypatch.setenv(key, "")
    s = Settings()
    assert (s.usd_inr_rate, s.telephony_cost_inr_per_min, s.fixed_monthly_cost_inr) == (0.0, 0.0, 0.0)
    assert s.sms_provider == "console" and s.public_base_url == "http://localhost:8000"


def test_vercel_config_points_at_the_app_in_singapore():
    import json
    import tomllib

    vercel = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    entry = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["vercel"][
        "entrypoint"
    ]
    module, _, attr = entry.partition(":")
    assert vercel["regions"] == ["sin1"]  # same AWS region as the Neon database
    assert (ROOT / (module.replace(".", "/") + ".py")).exists() and attr == "app"
    assert list(vercel["functions"]) == [module.replace(".", "/") + ".py"]
    # Files the app reads at runtime must not be excluded from the bundle.
    excluded = vercel["functions"]["app/main.py"]["excludeFiles"]
    for needed in ("config/", "prompts/", "app/"):
        assert needed not in excluded
