import subprocess
import sys
from pathlib import Path

base = Path(__file__).resolve().parent
processes = []
try:
    for i in range(1, 11):
        file = base / f"{i}.music_bot.py"
        print(f"Starting {file.name}...", flush=True)
        processes.append(subprocess.Popen([sys.executable, str(file)], cwd=base))
    for p in processes:
        p.wait()
finally:
    for p in processes:
        if p.poll() is None:
            p.terminate()
