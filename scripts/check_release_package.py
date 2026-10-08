import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from project_sekai.release_package import check_package

if __name__ == "__main__":
    print(json.dumps(check_package(Path(sys.argv[1])), ensure_ascii=False))
