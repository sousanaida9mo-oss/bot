# server.py
import os, subprocess, atexit, signal
from fastapi import FastAPI
import uvicorn

app = FastAPI()

# Запускаем твой bot.py как отдельный процесс
bot_proc = subprocess.Popen(["python", "-u", "bot.py"])

@atexit.register
def _cleanup():
    try:
        bot_proc.terminate()
    except Exception:
        pass

@app.get("/")
def root():
    return {"ok": True, "service": "telegram-bot"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))  # Koyeb проставит PORT автоматически
    uvicorn.run(app, host="0.0.0.0", port=port)
