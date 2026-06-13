import time
import itertools
import random
from datetime import datetime
from pypresence import Presence

# ============================
#   CẤU HÌNH - SỬA Ở ĐÂY
# ============================

CLIENT_ID = "1513391573306445954"  # discord.com/developers/applications → New App → Copy ID

# Danh sách status sẽ xoay vòng
# large_image / small_image: tên ảnh đã upload trong Developer Portal → Rich Presence → Art Assets
STATUSES = [
    {
        "details": "🎮 Đang chơi Roblox",
        "state": "Broken Blades / TDS / RoN / ...",
        "large_image": "roblox",       # tên ảnh trong portal
        "large_text": "Roblox",
        "small_image": "online",
        "small_text": "Online",
    },
    {
        "details": "💻 Đang code bot",
        "state": "Python · music.py",
        "large_image": "vscode",
        "large_text": "VS Code",
    },
    {
        "details": "🎵 Nghe nhạc",
        "state": "lofi hip hop",
        "large_image": "music",
        "large_text": "Music",
    },
    {
        "details": "😴 AFK",
        "state": "Không ở đây",
        "large_image": "afk",
        "large_text": "AFK",
    },
]

# Button hiển thị trên profile (tối đa 2)
BUTTONS = [
    {"label": "vmtd discord", "url": "https://discord.gg/link_cua_may"},
    # {"label": "GitHub", "url": "https://github.com/ten_may"},
]

# Thời gian mỗi status (giây) — tối thiểu 15
INTERVAL = 60

# Chế độ: "cycle" (tuần tự) hoặc "random" hoặc "time" (theo giờ)
MODE = "time"

# ============================
#   LOGIC THEO GIỜ (nếu MODE = "time")
# ============================

def get_status_by_time():
    hour = datetime.now().hour
    if 0 <= hour < 6:
        return {"details": "😴 Đang ngủ", "state": "Zzz...", "large_image": "afk"}
    elif 6 <= hour < 12:
        return {"details": "☀️ Buổi sáng", "state": "Đang học / làm việc", "large_image": "online"}
    elif 12 <= hour < 18:
        return {"details": "🎮 Buổi chiều", "state": "Chơi game / lướt net", "large_image": "roblox"}
    else:
        return {"details": "🌙 Buổi tối", "state": "Code hoặc chill", "large_image": "vscode"}

# ============================
#   MAIN
# ============================

def main():
    print(f"[RPC] Kết nối với Discord... (Client ID: {CLIENT_ID})")
    rpc = Presence(CLIENT_ID)
    rpc.connect()
    print("[RPC] Kết nối thành công! Bắt đầu xoay status...\n")

    start_time = int(time.time())

    if MODE == "cycle":
        status_iter = itertools.cycle(STATUSES)
    
    try:
        while True:
            if MODE == "cycle":
                status = next(status_iter)
            elif MODE == "random":
                status = random.choice(STATUSES)
            elif MODE == "time":
                status = get_status_by_time()
            else:
                status = STATUSES[0]

            rpc.update(
                **status,
                start=start_time,  # hiển thị "đã trải qua X phút"
                buttons=BUTTONS if BUTTONS else None,
            )

            print(f"[{datetime.now().strftime('%H:%M:%S')}] Status: {status.get('details')} — {status.get('state', '')}")
            time.sleep(INTERVAL)

    except KeyboardInterrupt:
        print("\n[RPC] Đã dừng.")
        rpc.close()

if __name__ == "__main__":
    main()