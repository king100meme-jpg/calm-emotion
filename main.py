import subprocess
import sys

# تشغيل جميع بوتات الأغاني من 1 إلى 10 معاً في نفس الوقت
bot_files = [f"{i}.music_bot.py" for i in range(1, 11)]

processes = []
for file in bot_files:
    print(f"Starting {file}...")
    p = subprocess.Popen([sys.executable, file])
    processes.append(p)

for p in processes:
    p.wait()