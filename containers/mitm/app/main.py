import json
import os
import io
import gzip

import uvicorn
from fastapi import FastAPI, Query
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

def get_all_replays():
    cache_file = os.path.join(DATA_DIR, "find_cache.json")
    
    # Load the existing cache or start with an empty dict
    if os.path.exists(cache_file):
        with open(cache_file, "r") as cache:
            find_cache = json.load(cache)
    else:
        find_cache = {}
    
    # Get current replay directories (ensure we only consider directories)
    current_ids = [
        replay_id for replay_id in os.listdir(DATA_DIR)
        if os.path.isdir(os.path.join(DATA_DIR, replay_id))
    ]
    
    # Remove cache entries for non-existent replays
    for replay_id in list(find_cache.keys()):
        if replay_id not in current_ids:
            del find_cache[replay_id]
    
    # Add or update new replays into the cache
    for replay_id in current_ids:
        # Only add if not in cache (or you could re-read to update if desired)
        if replay_id not in find_cache:
            replay_path = os.path.join(DATA_DIR, replay_id, "metadata.json")
            if os.path.exists(replay_path):
                with open(replay_path, "r") as file:
                    replay_data = json.load(file)
                    find_cache[replay_id] = replay_data["find"]
    
    # Dump the updated cache file back to disk
    with open(cache_file, "w") as cache:
        json.dump(find_cache, cache)
    
    # Gather the "find" dicts, sort and return
    replays = list(find_cache.values())
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


@app.get("/find/")
async def list_replays(
    game: str = Query("all"),
    offset: int = Query(0),
    shack: bool = Query(False),
    live: bool = Query(False)
):
    replays = get_all_replays()
    
    # Optionally filter by game if not "all"
    if game != "all":
        replays = [r for r in replays if r.get("game") == game]
    
    # Apply offset (pagination)
    replays = replays[offset:]
    
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