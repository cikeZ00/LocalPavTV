import os
import json
import requests
import base64
import boto3
from fastapi import FastAPI, HTTPException
from cryptography.fernet import Fernet

PRIVATE_KEY = os.environ.get("PRIVATE_KEY")
BUCKET_REGION = os.environ.get("BUCKET_REGION")
SCW_ACCESS_KEY = os.environ.get("SCW_ACCESS_KEY")
SCW_SECRET_KEY = os.environ.get("SCW_SECRET_KEY")
FILES_FOR_DOWNLOAD_BUCKET_NAME = os.environ.get("FILES_FOR_DOWNLOAD_BUCKET_NAME")

SERVER = "http://tv.pavlov-vr.com"

app = FastAPI()


session = boto3.Session(region_name=BUCKET_REGION)
resource = session.resource(
    's3',
    endpoint_url=f"https://s3.{BUCKET_REGION}.scw.cloud",
    aws_access_key_id=SCW_ACCESS_KEY,
    aws_secret_access_key=SCW_SECRET_KEY
)


def bytes_to_base64(bytes_str):
    return base64.b64encode(bytes_str).decode("ascii")


@app.get("/download/{replay_id}")
def download_replay(replay_id: str):
    # To download a replay we need to collect
    # the /meta page
    # the /event page
    # the /startDownload page
    # the steam files (replay.header and stream.1-2-3)
    if not replay_id.isalnum():
        raise HTTPException(status_code=404)

    replay_data = {}
    replay_files = {}

    findAll = requests.get(
        f"{SERVER}/find/any?dummy=0"
    )
    findAll.raise_for_status()
    findAll_json = findAll.json()
    findAllResponse = None
    for playback in findAll_json["replays"]:
        if playback["_id"] == replay_id:
            findAllResponse = playback

    if findAllResponse is None:
        raise HTTPException(
            status_code=400,
            detail="A recording must still be available to download it."
        )

    replay_data["find"] = findAllResponse

    startDownload = requests.post(
        f"{SERVER}/replay/{replay_id}/startDownloading?user"
    )
    startDownload.raise_for_status()
    startDownload_json = startDownload.json()
    if startDownload_json["state"] != "Recorded":
        raise HTTPException(
            status_code=400,
            detail="A recording must be finished "
                   "(not live) before it can be downloaded."
        )
    replay_data["start_downloading"] = startDownload_json

    meta = requests.get(f"{SERVER}/meta/{replay_id}")
    meta.raise_for_status()
    replay_data["meta"] = meta.json()

    events = requests.get(f"{SERVER}/replay/{replay_id}/event")
    events.raise_for_status()
    replay_data["events"] = events.json()

    # Now just download the stream files
    header_response = requests.get(
        f"{SERVER}/replay/{replay_id}/file/replay.header"
    )
    header_response.raise_for_status()
    replay_files["replay.header"] = bytes_to_base64(header_response.content)
    replay_files["replay.header.headers"] = json.dumps({})

    for i in range(0, startDownload_json["numChunks"]):
        file_response = requests.get(
            f"{SERVER}/replay/{replay_id}/file/stream." + str(i)
        )
        file_response.raise_for_status()
        replay_files["stream." + str(i)] = \
            bytes_to_base64(file_response.content)
        headers_json = json.dumps({
            "MTime1": file_response.headers["MTime1"],
            "MTime2": file_response.headers["MTime2"],
            "NumChunks": file_response.headers["NumChunks"],
            "State": file_response.headers["State"],
            "Time": file_response.headers["Time"],
            "Transfer-Encoding": file_response.headers["Transfer-Encoding"]
        })
        replay_files["stream." + str(i) + ".headers"] = bytes_to_base64(
            headers_json.encode("utf-8")
        )

    full_content = {
        "data": replay_data,
        "files": replay_files
    }

    serialized_content = json.dumps(full_content)

    # Encrypt file
    fernet = Fernet(PRIVATE_KEY)
    encrypted_content = fernet.encrypt(str.encode(serialized_content))

    # Serve file
    gamemode = meta.json()["gameMode"]
    replayMap = findAllResponse["friendlyName"].strip()

    file_content = str.encode("1 tv.pavlovhosting.com\n") + encrypted_content

    resource.Bucket(FILES_FOR_DOWNLOAD_BUCKET_NAME).put_object(
        f"{gamemode}-{replayMap}-{replay_id}.pavlovtv",
        file_content
    )

    return {
        "file": f"{gamemode}-{replayMap}-{replay_id}.pavlovtv"
    }
