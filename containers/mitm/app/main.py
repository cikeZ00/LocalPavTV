import json
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse, Response
from httpx import AsyncClient
from starlette.background import BackgroundTask

PORT = os.environ.get("PORT")
DATA_DIR = "./data"

allowed_origins = [
    "http://localhost",
    "https://tv.vankrupt.net"
]

app = FastAPI(
    title="mitm.tv.vankrupt.net"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

http_client = AsyncClient(base_url="https://tv.vankrupt.net:443/", verify=False)

def get_all_replays():
    replays = []
    for replay_id in os.listdir(DATA_DIR):
        replay_path = os.path.join(DATA_DIR, replay_id, "metadata.json")
        if os.path.exists(replay_path):
            with open(replay_path, "r") as file:
                replay_data = json.load(file)
                replays.append(replay_data["find"])
    return replays

@app.get("/")
def home():
    return RedirectResponse("https://tv.vankrupt.net")

@app.get("/find/any")
async def list_replays():
    replays = get_all_replays()
    return {"replays": replays}

@app.get("/meta/{replay_id}")
async def meta(replay_id: str):
    replay_path = os.path.join(DATA_DIR, replay_id, "metadata.json")
    if os.path.exists(replay_path):
        with open(replay_path, "r") as file:
            replay_data = json.load(file)
            return replay_data["meta"]
    else:
        request = http_client.build_request("GET", f"/meta/{replay_id}")
        response = await http_client.send(request, stream=True)
        return StreamingResponse(response.aiter_raw(), background=BackgroundTask(response.aclose), headers=response.headers)

@app.get("/replay/{replay_id}/file/{file_name}")
async def get_replay_file(replay_id: str, file_name: str):
    file_path = os.path.join(DATA_DIR, replay_id, file_name)
    timing_path = os.path.join(DATA_DIR, replay_id, "timing.json")

    headers = {}
    if os.path.exists(timing_path):
        with open(timing_path, "r") as timing_file:
            timing_data = json.load(timing_file)
            if file_name.startswith("stream."):
                index = int(file_name.split(".")[1])
                if index < len(timing_data):
                    headers = {
                        "numchunks": str(timing_data[index].get("numchunks")),
                        "time": str(timing_data[index].get("time")),
                        "state": timing_data[index].get("state"),
                        "mtime1": str(timing_data[index].get("mtime1")),
                        "mtime2": str(timing_data[index].get("mtime2"))
                    }

    if os.path.exists(file_path):
        with open(file_path, "rb") as file:
            return Response(content=file.read(), status_code=200, headers=headers)
    else:
        request = http_client.build_request("GET", f"/replay/{replay_id}/file/{file_name}")
        response = await http_client.send(request, stream=True)
        return StreamingResponse(response.aiter_raw(), background=BackgroundTask(response.aclose), headers={**response.headers, **headers})

@app.get("/replay/{replay_id}/event")
async def get_events(replay_id: str, group: str = "checkpoint"):
    replay_path = os.path.join(DATA_DIR, replay_id, "metadata.json")
    if os.path.exists(replay_path):
        with open(replay_path, "r") as file:
            replay_data = json.load(file)
            if group == "checkpoint":
                return replay_data["events"]
            elif group == "Pavlov":
                return replay_data["events_pavlov"]
            else:
                return {"error": "Invalid group specified"}
    else:
        request = http_client.build_request("GET", f"/replay/{replay_id}/event?group={group}")
        response = await http_client.send(request, stream=True)
        return StreamingResponse(response.aiter_raw(), background=BackgroundTask(response.aclose), headers=response.headers)

@app.post("/replay/{replay_id}/startDownloading")
async def start_downloading(replay_id: str, user: str):
    replay_path = os.path.join(DATA_DIR, replay_id, "metadata.json")
    if os.path.exists(replay_path):
        with open(replay_path, "r") as file:
            replay_data = json.load(file)
            return replay_data["start_downloading"]
    else:
        request = http_client.build_request("POST", f"/replay/{replay_id}/startDownloading?user={user}")
        response = await http_client.send(request, stream=True)
        return StreamingResponse(response.aiter_raw(), background=BackgroundTask(response.aclose), headers=response.headers)

@app.post("/replay/{replay_id}/viewer/{viewer_id}")
def replay_viewer():
    return Response(content="", status_code=204)

@app.get("/__tv.vankrupt.net/relay")
def relay():
    return {"__tv.vankrupt.net/relay": True}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8081)