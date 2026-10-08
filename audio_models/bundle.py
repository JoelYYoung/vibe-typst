"""Finalize an image bundle without embedding a worker credential."""
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
config = root / 'config.json'
data = json.loads(config.read_text())
data.pop('token', None)
(root / 'bundle.json').write_text(json.dumps(data, indent=2))
config.unlink()
