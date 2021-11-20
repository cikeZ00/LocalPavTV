import json
import os
import uvicorn
import boto3
import httpx
from starlette.background import BackgroundTask
from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from httpx import AsyncClient

PORT = os.environ.get("PORT")
BUCKET_NAME = os.environ.get("BUCKET_NAME")
BUCKET_REGION = os.environ.get("BUCKET_REGION")
SCW_ACCESS_KEY = os.environ.get("SCW_ACCESS_KEY")
SCW_SECRET_KEY = os.environ.get("SCW_SECRET_KEY")
REPLAY_FILES_URL = os.environ.get("REPLAY_FILES_URL")

allowed_origins = [
    "http://localhost",
    "https://tv.pavlovhosting.com"
]

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

session = boto3.Session(region_name=BUCKET_REGION)
resource = session.resource(
    's3',
    endpoint_url=f"https://s3.{BUCKET_REGION}.scw.cloud",
    aws_access_key_id=SCW_ACCESS_KEY,
    aws_secret_access_key=SCW_SECRET_KEY
)

http_client = AsyncClient(base_url="http://tv.pavlov-vr.com:80/")


def get_ip_state(ip_address):
    try:
        s3_object = resource.Bucket(BUCKET_NAME).Object(ip_address + ".json").get()
        s3_body = s3_object["Body"].read()
        json_content = json.loads(s3_body)

        if "mounted_replay" in json_content:
            # Check if mounted replay is valid
            replay_id = json_content["mounted_replay"]["find"]["_id"]
            # Does this key exist in the bucket with replay files?
            response = httpx.head(REPLAY_FILES_URL + replay_id + "/replay.header")
            if response.status_code != 200:
                # Invalidate the mounted replay
                json_content["mounted_replay"] = None

        return json_content
    except ClientError as ex:
        if ex.response["Error"]["Code"] == "NoSuchKey":
            return {}
        else:
            raise ex


@app.get("/")
def home():
    return RedirectResponse("https://tv.pavlovhosting.com")


@app.get("/find/any")
async def list_replays(request: Request):
    # Find the IP state for this IP address
    ip_state = get_ip_state(request.client.host)
    # Does the user have a mounted replay?
    if "mounted_replay" in ip_state:
        # Load the mounted replay only
        return {
            "replays": [ip_state["mounted_replay"]["find"]]
        }
    else:
        # Load all replays
        request = http_client.build_request("GET", "/find/any?dummy=0")
        response = await http_client.send(request, stream=True)
        return StreamingResponse(
            response.aiter_raw(),
            background=BackgroundTask(response.aclose),
            headers=response.headers
        )


@app.get("/meta/{replay_id}")
async def meta(request: Request, replay_id: str):
    # Find the IP state for this IP address
    ip_state = get_ip_state(request.client.host)
    # Does the user have a mounted replay?
    if "mounted_replay" in ip_state:
        # Return the mounted replay metadata
        return ip_state["mounted_replay"]["meta"]
    else:
        # Load metadata
        request = http_client.build_request("GET", "/meta/" + replay_id)
        response = await http_client.send(request, stream=True)
        return StreamingResponse(
            response.aiter_raw(),
            background=BackgroundTask(response.aclose),
            headers=response.headers
        )


@app.get("/replay/{replay_id}/file/{file_name}")
async def get_replay_file(request: Request, replay_id: str, file_name: str):
    # Find the IP state for this IP address
    ip_state = get_ip_state(request.client.host)
    # Does the user have a mounted replay?
    if "mounted_replay" in ip_state:
        # Send to the bucket
        return RedirectResponse(REPLAY_FILES_URL + replay_id + "/" + file_name)
    else:
        # Stream response from server
        request = http_client.build_request("GET", f"/replay/{replay_id}/file/{file_name}")
        response = await http_client.send(request, stream=True)
        return StreamingResponse(
            response.aiter_raw(),
            background=BackgroundTask(response.aclose),
            headers=response.headers
        )


@app.get("/replay/{replay_id}/event")
async def get_events(request: Request, replay_id: str):
    # Find the IP state for this IP address
    ip_state = get_ip_state(request.client.host)
    # Does the user have a mounted replay?
    if "mounted_replay" in ip_state:
        # Send to the bucket
        return ip_state["mounted_replay"]["events"]
    else:
        # Load events
        request = http_client.build_request("GET", f"/replay/{replay_id}/event")
        response = await http_client.send(request, stream=True)
        return StreamingResponse(
            response.aiter_raw(),
            background=BackgroundTask(response.aclose),
            headers=response.headers
        )


@app.post("/replay/{replay_id}/startDownloading")
async def start_downloading(request: Request, replay_id: str, user:str):
    # Find the IP state for this IP address
    ip_state = get_ip_state(request.client.host)
    # Does the user have a mounted replay?
    if "mounted_replay" in ip_state:
        return ip_state["mounted_replay"]["start_downloading"]
    else:
        # Load download response
        request = http_client.build_request(
            "POST",
            f"/replay/{replay_id}/startDownloading?user={user}"
        )
        response = await http_client.send(request, stream=True)
        return StreamingResponse(
            response.aiter_raw(),
            background=BackgroundTask(response.aclose),
            headers=response.headers
        )


@app.get("/__tv.pavlovhosting.com/relay")
def relay():
    return {
        "__tv.pavlovhosting.com/relay": True
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(PORT))