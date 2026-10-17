"""Copy what the Cloud Run service needs into scratch/cloud_build (the code and the language list; the Dockerfile
downloads the two local voices), so `gcloud run deploy --source scratch/cloud_build` uploads only that.
Usage: python cloud/stage.py"""
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "scratch" / "cloud_build"

FILES = [
    "cloud/app.py", "cloud/worker.py", "cloud/store.py", "cloud/Dockerfile", "cloud/requirements.txt",
    "helper/tts.py", "helper/supertonic_helper.py", "app/src/languages.json",
]
DIRS: list[str] = []

if OUT.exists():
    shutil.rmtree(OUT)
for f in FILES:
    (OUT / f).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / f, OUT / f)
for d in DIRS:
    shutil.copytree(ROOT / d, OUT / d)
shutil.move(OUT / "cloud" / "Dockerfile", OUT / "Dockerfile")
shutil.move(OUT / "cloud" / "requirements.txt", OUT / "requirements.txt")
size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
print(f"staged {OUT} ({size / 1e3:.0f} kB)")
