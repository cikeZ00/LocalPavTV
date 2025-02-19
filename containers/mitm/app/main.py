import json
import aiofiles
import asyncio
import os
import io
import gzip

import uvicorn
from fastapi import FastAPI
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

# In-memory index
global_index = {}

def update_global_index(replay_id):
    """Updates the global index in memory when a replay is downloaded."""
    replay_path = os.path.join(DATA_DIR, replay_id, "metadata.json")
    
    if os.path.exists(replay_path):
        with open(replay_path, "r") as file:
            replay_data = json.load(file)
            
            for event in replay_data.get("events", {}).get("events", []):
                global_index[event["id"]] = event["data"]["data"]



http_client = AsyncClient(base_url="https://tv.vankrupt.net:443/", verify=False)

async def read_replay_metadata(replay_id):
    replay_path = os.path.join(DATA_DIR, replay_id, "metadata.json")
    if os.path.exists(replay_path):
        async with aiofiles.open(replay_path, "r") as file:
            async for line in file:
                if '"find":' in line:
                    try:
                        replay_data = json.loads(line.strip().rstrip(','))
                        return replay_data["find"]
                    except json.JSONDecodeError:
                        return None
    return None

async def get_all_replays():
    replays = []
    tasks = [read_replay_metadata(replay_id) for replay_id in os.listdir(DATA_DIR)]
    results = await asyncio.gather(*tasks)
    for result in results:
        if result:
            replays.append(result)
    replays.sort(key=lambda x: x["created"], reverse=True)
    return replays

@app.get("/")
def home():
    return RedirectResponse("https://tv.vankrupt.net")

@app.get("/event/{event_id}")
async def get_event_stream(event_id: str):
    event = global_index.get(event_id)

    if not event:
        return Response(content="Event data not found", status_code=404)
    
    byte_data = bytes(event)

    # Gzip-compress the data
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as gz_file:
        gz_file.write(byte_data)

    compressed_data = buffer.getvalue()

    return Response(
        content=compressed_data,
        status_code=200,
        media_type="application/octet-stream",
        headers={"Content-Encoding": "gzip"}
    )


@app.get("/find/any")
async def list_replays():
    replays = await get_all_replays()
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
            global_index.clear()
            update_global_index(replay_id)
            return replay_data["start_downloading"]
    else:
        request = http_client.build_request("POST", f"/replay/{replay_id}/startDownloading?user={user}")
        response = await http_client.send(request, stream=True)
        update_global_index(replay_id)
        return StreamingResponse(response.aiter_raw(), background=BackgroundTask(response.aclose), headers=response.headers)

@app.post("/replay/{replay_id}/viewer/{viewer_id}")
def replay_viewer():
    return Response(content="", status_code=204)

@app.get("/__tv.vankrupt.net/relay")
def relay():
    return {"__tv.vankrupt.net/relay": True}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8081)