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


def test_render_yaml_sets_every_runtime_secret():
    text = (ROOT / "render.yaml").read_text(encoding="utf-8")
    runtime = {n.upper() for n in Settings.model_fields} - {"ANTHROPIC_API_KEY"}
    assert [k for k in sorted(runtime) if f"key: {k}" not in text] == []
