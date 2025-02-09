import os
import json
import requests
import uvicorn
import base64
from fastapi import FastAPI, HTTPException, Response, File, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from cryptography.fernet import Fernet

PRIVATE_KEY = os.environ.get("PRIVATE_KEY")
SERVER = "https://tv.vankrupt.net"
DATA_DIR = "data"

HEADERS = {
    "Host": "tv.vankrupt.net",
    "Accept": "*/*",
    "User-Agent": "Pavlov/++UE5+Release-5.1-CL-23901901 Windows/10.0.22631.1.256.64bit"
}

os.makedirs(DATA_DIR, exist_ok=True)

app = FastAPI(
    title="LocalPavTV",
    description="Download and replay Pavlov TV files",
    version="0.0.1",
)

@app.get("/")
def serve_homepage():
    return RedirectResponse("/docs")

def bytes_to_base64(bytes_str):
    return base64.b64encode(bytes_str).decode("ascii")

def base64_to_bytes(base64_str):
    return base64.b64decode(base64_str)

@app.get("/list")
def list_interesting_games():
    games_list = requests.get(SERVER + "/find/any?dummy=0")
    games_list.raise_for_status()
    games = games_list.json()
    return [replay for replay in games["replays"] if replay["users"] and not replay["live"]]

@app.get("/download/{replay_id}")
def download_replay(replay_id: str):
    if not replay_id.isalnum():
        raise HTTPException(status_code=404)

    replay_data = {}
    replay_files = {}
    
    findAll = requests.get(f"{SERVER}/find/any?dummy=0", verify=False, headers=HEADERS)
    findAll.raise_for_status()
    findAll_json = findAll.json()
    findAllResponse = next((playback for playback in findAll_json["replays"] if playback["_id"] == replay_id), None)

    if not findAllResponse:
        raise HTTPException(status_code=400, detail="Recording not available.")

    replay_data["find"] = findAllResponse

    startDownload = requests.post(
        f"{SERVER}/replay/{replay_id}/startDownloading?user", 
        verify=False, 
        headers=HEADERS
    )
    startDownload.raise_for_status()
    startDownload_json = startDownload.json()
    
    if startDownload_json["state"] != "Recorded":
        raise HTTPException(status_code=400, detail="Recording must be finished before download.")
    
    replay_data["start_downloading"] = startDownload_json

    meta = requests.get(f"{SERVER}/meta/{replay_id}", verify=False, headers=HEADERS)
    meta.raise_for_status()
    replay_data["meta"] = meta.json()
    
    events = requests.get(f"{SERVER}/replay/{replay_id}/event", verify=False, headers=HEADERS)
    events.raise_for_status()
    replay_data["events"] = events.json()
    
    replay_dir = os.path.join(DATA_DIR, replay_id)
    os.makedirs(replay_dir, exist_ok=True)
    
    with open(os.path.join(replay_dir, "replay.header"), "wb") as f:
        f.write(requests.get(f"{SERVER}/replay/{replay_id}/file/replay.header", headers=HEADERS).content)
    
    for i in range(startDownload_json["numChunks"]):
        with open(os.path.join(replay_dir, f"stream.{i}"), "wb") as f:
            f.write(requests.get(f"{SERVER}/replay/{replay_id}/file/stream.{i}", headers=HEADERS).content)
    
    with open(os.path.join(replay_dir, "metadata.json"), "w") as f:
        json.dump(replay_data, f)

    return {"message": "Download completed", "path": replay_dir}

@app.post("/upload")
def upload(request: Request, file: bytes = File(...)):
    fernet = Fernet(PRIVATE_KEY)
    decrypted_content = fernet.decrypt(file)
    json_payload = json.loads(decrypted_content.decode())
    
    replay_id = json_payload["data"]["find"]["_id"]
    replay_dir = os.path.join(DATA_DIR, replay_id)
    os.makedirs(replay_dir, exist_ok=True)

    for key, value in json_payload["files"].items():
        with open(os.path.join(replay_dir, key), "wb") as f:
            f.write(base64_to_bytes(value))
    
    with open(os.path.join(replay_dir, "metadata.json"), "w") as f:
        json.dump(json_payload["data"], f)
    
    return {"ok": True}

@app.post("/reset")
def reset(request: Request):
    ip_address = request.client.host
    ip_file = os.path.join(DATA_DIR, f"{ip_address}.json")
    if os.path.exists(ip_file):
        os.remove(ip_file)
    return {"ok": True}

@app.get("/whoami")
def whoami(request: Request):
    return {"ip": request.client.host}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
