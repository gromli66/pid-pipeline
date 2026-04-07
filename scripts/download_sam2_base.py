"""Download SAM2 Hiera Small base weights to models/sam2/.

Run once before first docker-compose up:
    python scripts/download_sam2_base.py

Avoids HuggingFace download at container startup
(TRANSFORMERS_OFFLINE=1 in Dockerfile blocks runtime downloads).
"""

import urllib.request
from pathlib import Path


URL = "https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt"
DEST = Path("models/sam2/sam2_hiera_small.pt")


def main():
    DEST.parent.mkdir(parents=True, exist_ok=True)
    if DEST.exists():
        size_mb = DEST.stat().st_size / 1e6
        print(f"Already exists: {DEST} ({size_mb:.1f} MB)")
        return
    print(f"Downloading {URL} ...")
    urllib.request.urlretrieve(URL, DEST)
    size_mb = DEST.stat().st_size / 1e6
    print(f"Saved: {DEST} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
