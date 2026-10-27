# Double-click (or autostart) launcher: pythonw runs it without a console window.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from earshot.__main__ import main  # noqa: E402

main()
