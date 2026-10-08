from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from project_sekai.release_update import main

if __name__ == "__main__":
    main()
