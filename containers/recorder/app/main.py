import os
import json
import requests
import base64
import boto3
import time
import uvicorn

from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from cryptography.fernet import Fernet
from requests import HTTPError

PRIVATE_KEY = os.environ.get("PRIVATE_KEY")
BUCKET_REGION = os.environ.get("BUCKET_REGION")
SCW_ACCESS_KEY = os.environ.get("SCALEWAY_ACCESS_KEY")
SCW_SECRET_KEY = os.environ.get("SCALEWAY_SECRET_KEY")
FILES_FOR_DOWNLOAD_BUCKET_NAME = os.environ.get("FILES_FOR_DOWNLOAD_BUCKET_NAME")

SERVER = "http://tv.pavlov-vr.com"

app = FastAPI(
    version="0.0.2"
)


session = boto3.Session(region_name=BUCKET_REGION)
resource = session.resource(
    's3',
    endpoint_url=f"https://s3.{BUCKET_REGION}.scw.cloud",
    aws_access_key_id=SCW_ACCESS_KEY,
    aws_secret_access_key=SCW_SECRET_KEY
)


def bytes_to_base64(bytes_str):
    return base64.b64encode(bytes_str).decode("ascii")


def base64_to_bytes(base64_str):
    return base64.b64decode(base64_str)


def does_key_exist(bucket, key):
    try:
        bucket.Object(key).get()
        return True
    except ClientError as ex:
        if ex.response["Error"]["Code"] == "NoSuchKey":
            return False
        else:
            raise ex


@app.post("/")
def cron():
    # Get all the current recordings from pavlov TV
    all_recordings = requests.get(SERVER + "/find/any?dummy=0")
    all_recordings.raise_for_status()
    # Find any that aren't being downloaded and have players
    for recording in all_recordings.json()["replays"]:
        if len(recording["users"]) > 0:
            replay_id = recording["_id"]
            if not does_key_exist(
                resource.Bucket(FILES_FOR_DOWNLOAD_BUCKET_NAME),
                f"{replay_id}/in_progress.txt"
            ):
                # We'll handle this one
                return download_replay(replay_id)
    return {
        "ok": True,
        "message": "Nothing to do"
    }


@app.get("/download/{replay_id}")
def download_replay(replay_id: str):
    print("Downloading " + replay_id)
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

    # Mark that we're going to download this recording
    resource.Bucket(FILES_FOR_DOWNLOAD_BUCKET_NAME).put_object(
        Key=f"{replay_id}/in_progress.txt",
        Body=str(time.time())
    )

    replay_data["find"] = findAllResponse

    startDownload = requests.post(
        f"{SERVER}/replay/{replay_id}/startDownloading?user"
    )
    startDownload.raise_for_status()
    startDownload_json = startDownload.json()

    current_state = startDownload_json["state"]

    startDownload_json["state"] = "Recorded"
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

    if current_state != "Recorded":
        chunk_number = startDownload_json["numChunks"]
        confirmed_chunks = startDownload_json["numChunks"] - 1
        # Need to buffer for more chunks
        # We will wait up to 2 minutes for another chunk to become available
        time_since_last_good_chunk = time.time()
        final_time = 0
        while time_since_last_good_chunk + 120 > time.time():
            response = requests.get(
                f"{SERVER}/replay/{replay_id}/file/stream." + str(chunk_number)
            )
            try:
                response.raise_for_status()
                replay_files["stream." + str(chunk_number)] = \
                    bytes_to_base64(response.content)
                headers_json = json.dumps({
                    "MTime1": response.headers["MTime1"],
                    "MTime2": response.headers["MTime2"],
                    "NumChunks": response.headers["NumChunks"],
                    "State": response.headers["State"],
                    "Time": response.headers["Time"],
                    "Transfer-Encoding": response.headers["Transfer-Encoding"]
                })
                final_time = response.headers["Time"]
                replay_files["stream." + str(chunk_number) + ".headers"] = \
                    bytes_to_base64(headers_json.encode("utf-8"))
                confirmed_chunks = chunk_number
                chunk_number = chunk_number + 1
                time_since_last_good_chunk = time.time()

                if response.headers["State"] == "Recorded":
                    print("End of stream confirmed by server.")
                    break
            except HTTPError:
                # Wait 10 seconds before retrying
                time.sleep(10)
                print("Waiting for chunk " + str(chunk_number) + " " +
                      str((time_since_last_good_chunk + 120) - time.time()) +
                      " seconds remain...")
        # Need to now correct the number of chunks in each recording.
        # The following need to be re-written
        # live in find
        # numChunks in meta
        # live in meta
        # numChunks in startDownloading
        # Time, State and NumChunks in stream headers

        # Total chunks = confirmed_chunks
        # State = "Recorded"
        # Time = final_time

        # Correct the find record
        replay_data["find"]["live"] = False

        # Correct the meta record
        replay_data["meta"]["numChunks"] = confirmed_chunks + 1
        replay_data["meta"]["live"] = False

        # Correct the startDownloading record
        replay_data["start_downloading"]["time"] = final_time
        replay_data["start_downloading"]["state"] = "Recorded"
        replay_data["start_downloading"]["numChunks"] = confirmed_chunks + 1

        # Correct the stream headers
        for i in range(0, confirmed_chunks + 1):
            print(f"Rewriting stream chunk {i} of {confirmed_chunks}...")
            # Get the current headers out
            headers_json = json.loads(
                base64_to_bytes(
                    replay_files["stream." + str(i) + ".headers"]
                )
            )
            # Headers MUST be strings or the server will crash when serving them
            headers_json["Time"] = str(final_time)
            headers_json["State"] = "Recorded"
            headers_json["NumChunks"] = str(confirmed_chunks)

            replay_files["stream." + str(i) + ".headers"] = \
                bytes_to_base64(json.dumps(headers_json).encode("utf-8"))
        # This is the end of file correction to turn live streams into
        # recordings

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
        Key=f"{replay_id}/{gamemode}-{replayMap}-{replay_id}.pavlovtv",
        Body=file_content
    )

    return {
        "file": f"{gamemode}-{replayMap}-{replay_id}.pavlovtv"
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
