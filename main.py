"""进程入口，供 `uvicorn main:app` / Docker 使用。实际实现见 app/main.py（controller/service/dao 三层结构）"""
from app.main import app

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
