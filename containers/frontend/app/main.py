import os
import json
import requests
import uvicorn
import base64
import boto3
import hurry.filesize
from fastapi import FastAPI, HTTPException, Response, File, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from cryptography.fernet import Fernet

PRIVATE_KEY = os.environ.get("PRIVATE_KEY")
BUCKET_REGION = os.environ.get("BUCKET_REGION")
SCW_ACCESS_KEY = os.environ.get("SCW_ACCESS_KEY")
SCW_SECRET_KEY = os.environ.get("SCW_SECRET_KEY")
IP_STATE_BUCKET_NAME = os.environ.get("IP_STATE_BUCKET_NAME")
REPLAY_FILES_BUCKET_NAME = os.environ.get("REPLAY_FILES_BUCKET_NAME")
FILES_FOR_DOWNLOAD_BUCKET_NAME = os.environ.get("FILES_FOR_DOWNLOAD_BUCKET_NAME")

SERVER = "http://tv.pavlov-vr.com"

app = FastAPI(
    title="tv.pavlovhosting.com",
    description="Download and replay Pavlov TV files\n"
                "1. Add the following to your hosts file: "
                "51.15.238.121 tv.pavlov-vr.com\n"
                "2. Check you can reach this page: \n"
                "http://tv.pavlov-vr.com/__tv.pavlovhosting.com/relay\n"
                "3. Start downloading and uploading replays!",
    version="0.0.1",
    contact={
        "name": "PavlovHosting.com"
    }
)

session = boto3.Session(region_name=BUCKET_REGION)
resource = session.resource(
    's3',
    endpoint_url=f"https://s3.{BUCKET_REGION}.scw.cloud",
    aws_access_key_id=SCW_ACCESS_KEY,
    aws_secret_access_key=SCW_SECRET_KEY
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
    interesting_games = []
    for replay in games["replays"]:
        if len(replay["users"]) > 0 and replay["live"] is False:
            interesting_games.append(replay)
    return interesting_games


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

    return Response(
        content=str.encode("1 tv.pavlovhosting.com\n") + encrypted_content,
        media_type="application/pavlovtv",
        headers={
            "content-disposition":
                "attachment; "
                f"filename=\"{gamemode}-{replayMap}-{replay_id}.pavlovtv\""
        }
    )


@app.post("/upload")
def upload(request: Request, file: bytes = File(...)):
    # Decrypt the file
    fernet = Fernet(PRIVATE_KEY)
    raw_file = file.decode()
    file = str.encode("\n".join(raw_file.split("\n")[1:]))
    decrypted_content = fernet.decrypt(file)

    # With the decrypted bytes, load the JSON file
    json_payload = json.loads(decrypted_content.decode())

    replay_id = json_payload["data"]["find"]["_id"]

    # Upload the replay files
    replay_files_bucket = resource.Bucket(REPLAY_FILES_BUCKET_NAME)
    for key in json_payload["files"]:
        value = base64_to_bytes(json_payload["files"][key])
        replay_files_bucket.put_object(
            Body=value,
            Key=replay_id + "/" + key
        )

    # Attach the data to the IP
    ip_address = request.client.host

    ip_state_bucket = resource.Bucket(IP_STATE_BUCKET_NAME)
    ip_state_bucket.put_object(
        Body=json.dumps({
            "mounted_replay": json_payload["data"]
        }),
        Key=ip_address + ".json"
    )

    return {
        "ok": True
    }


@app.post("/reset")
def reset(request: Request):
    ip_address = request.client.host

    resource.Bucket(IP_STATE_BUCKET_NAME).delete_objects(
        Delete={
            "Objects": [
                {
                    "Key": ip_address + ".json"
                }
            ]
        }
    )

    return {
        "ok": True
    }


@app.get("/whoami")
def whoami(request: Request):
    return {
        "ip": request.client.host
    }


@app.get("/all_recordings")
def all_recordings_html():
    anchor_list = ""
    entries = {}
    for s3_object in resource.Bucket(FILES_FOR_DOWNLOAD_BUCKET_NAME).objects.all():
        if s3_object.key.endswith(".pavlovtv"):
            name = s3_object.key.split("/")[1]
            timestamp = name.split(" ")[0]
            entries[timestamp] = f"<li><a href='https://pavlovtv-files-for-download.s3-website.fr-par.scw.cloud//{s3_object.key}'>{name}</a>" \
                                 f" ({hurry.filesize.size(s3_object.size)}B)</li>"
    for _, v in sorted(entries.items()):
        anchor_list = v + anchor_list
    return HTMLResponse(
        """
        <!doctype html>
        <html>
        <head>
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Open+Sans&display=swap');
            body{
                font-family: 'Open Sans', sans-serif;
            }
        </style>
        <title>All recordings</title>
        </head>
        <body>
        <ol>
        """
        + anchor_list +
        """
        </ol>
        Recordings are retained for 24 hours, so download them before they're gone!
        <br/>
        With ❤️ by <a href="//lucy.sh">Lucy</a>
        </body>
        </html>
        """
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
