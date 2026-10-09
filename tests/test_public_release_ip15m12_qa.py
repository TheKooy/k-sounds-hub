"""Final public 0.4.7 checkpoint contract (no local hardware needed)."""
from __future__ import annotations
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_checkpoint_version_and_meter_defaults():
    glass = (ROOT / 'src/ksound_hub/glass_app.py').read_text(encoding='utf-8')
    version = (ROOT / 'src/ksound_hub/_version.py').read_text(encoding='utf-8')
    tree = ast.parse(glass)
    build = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == 'APP_BUILD' for t in node.targets)
             and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
    assert build == ['IP15m12']
    assert '__version__ = "0.4.7"' in version
    assert 'KSH_METER_INTERVAL_MS", "25"' in glass
    assert "KSH_METER_LED_WIDTH_PX', '14'" in glass
    assert "KSH_METER_EVEN_GRID', '0'" in glass
    assert 'SEGMENTS = 48' in glass
