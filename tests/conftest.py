"""Keep tests independent from the operator's installed metsuke settings."""

import os
from pathlib import Path


for name in tuple(os.environ):
    if name.startswith("METSUKE_"):
        os.environ.pop(name)
os.environ["METSUKE_CONFIG"] = str(Path(__file__).with_name(".nonexistent-config.env"))
