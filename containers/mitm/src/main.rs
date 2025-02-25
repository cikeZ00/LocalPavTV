use axum::http::Method;
use axum::{
    body::{boxed, Full, StreamBody},
    extract::{Path, Query, State},
    http::{HeaderMap, HeaderValue, Response, StatusCode},
    response::{IntoResponse, Redirect},
    routing::{get, post},
    Json, Router,
};
use flate2::write::GzEncoder;
use flate2::Compression;
use futures::StreamExt;
use reqwest::Client;
use serde::Deserialize;
use serde_json::Value;
use std::{
    collections::HashMap,
    io::Write,
    sync::{Arc, Mutex},
};
use tokio::fs;
use tower_http::cors::CorsLayer;

/// The remote server base URL.
const REMOTE_BASE_URL: &str = "https://tv.vankrupt.net:443";

/// Shared application state.
#[derive(Clone)]
struct AppState {
    global_index: Arc<Mutex<HashMap<String, Vec<u8>>>>,
    client: Client,
    data_dir: String,
}

#[tokio::main]
async fn main() {
    // Build a Reqwest client that accepts invalid certificates.
    let client = Client::builder()
        .danger_accept_invalid_certs(true)
        .build()
        .expect("failed to build client");

    let state = AppState {
        global_index: Arc::new(Mutex::new(HashMap::new())),
        client,
        data_dir: "./data".to_string(),
    };

    // Allowed origins for CORS.
    let cors = CorsLayer::new()
        .allow_origin(
            vec!["http://localhost", "https://tv.vankrupt.net"]
                .into_iter()
                .map(|origin| origin.parse().unwrap())
                .collect::<Vec<_>>()
        )
        .allow_methods(vec![Method::GET, Method::POST])
        .allow_headers(vec![
            axum::http::header::CONTENT_TYPE,
            axum::http::header::AUTHORIZATION,
            axum::http::header::ACCEPT,
        ])
        .allow_credentials(true);

    // Build the Axum router.
    let app = Router::new()
        .route("/", get(root_handler))
        .route("/event/:event_id", get(event_handler))
        .route("/find/any", get(find_any_handler))
        .route("/meta/:replay_id", get(meta_handler))
        .route(
            "/replay/:replay_id/file/:file_name",
            get(replay_file_handler),
        )
        .route("/replay/:replay_id/event", get(replay_event_handler))
        .route(
            "/replay/:replay_id/startDownloading",
            post(start_downloading_handler),
        )
        .route(
            "/replay/:replay_id/viewer/:viewer_id",
            post(replay_viewer_handler),
        )
        .route("/__tv.vankrupt.net/relay", get(relay_handler))
        .with_state(state)
        .layer(cors);

    // Run the server on 127.0.0.1:8081.
    axum::Server::bind(&"127.0.0.1:8081".parse().unwrap())
        .serve(app.into_make_service())
        .await
        .unwrap();
}

/// GET "/" → redirect to the remote TV site.
async fn root_handler() -> impl IntoResponse {
    Redirect::temporary("https://tv.vankrupt.net")
}

/// GET "/event/:event_id"
async fn event_handler(
    Path(event_id): Path<String>,
    State(state): State<AppState>,
) -> Result<impl IntoResponse, (StatusCode, String)> {
    let global_index = state.global_index.lock().unwrap();
    if let Some(event_data) = global_index.get(&event_id) {
        // Compress the event data using gzip.
        let mut encoder = GzEncoder::new(Vec::new(), Compression::default());
        encoder
            .write_all(event_data)
            .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
        let compressed_data = encoder
            .finish()
            .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

        let response = Response::builder()
            .status(StatusCode::OK)
            .header("Content-Encoding", "gzip")
            .header("Content-Type", "application/octet-stream")
            .body(Full::from(compressed_data))
            .unwrap();
        Ok(response)
    } else {
        Err((StatusCode::NOT_FOUND, "Event data not found".to_string()))
    }
}

/// GET "/find/any"
async fn find_any_handler(State(state): State<AppState>) -> impl IntoResponse {
    match get_all_replays(&state).await {
        Ok(json) => (StatusCode::OK, Json(json)).into_response(),
        Err((code, msg)) => (code, msg).into_response(),
    }
}

/// Read (and update) the "find" cache from disk.
async fn get_all_replays(state: &AppState) -> Result<Value, (StatusCode, String)> {
    let cache_path = format!("{}/find_cache.json", state.data_dir);
    let mut find_cache: HashMap<String, Value> = if let Ok(content) = fs::read_to_string(&cache_path).await {
        serde_json::from_str(&content).unwrap_or_default()
    } else {
        HashMap::new()
    };

    // Get the current replay directories.
    let mut entries = fs::read_dir(&state.data_dir)
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    let mut current_ids = Vec::new();
    while let Some(entry) = entries
        .next_entry()
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?
    {
        let ft = entry.file_type().await.map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
        if ft.is_dir() {
            if let Some(name) = entry.file_name().to_str() {
                current_ids.push(name.to_string());
            }
        }
    }

    // Remove stale cache entries.
    find_cache.retain(|k, _| current_ids.contains(k));

    // Add new replays into the cache.
    for replay_id in &current_ids {
        if !find_cache.contains_key(replay_id) {
            let metadata_path = format!("{}/{}/metadata.json", state.data_dir, replay_id);
            if let Ok(content) = fs::read_to_string(&metadata_path).await {
                if let Ok(replay_data) = serde_json::from_str::<Value>(&content) {
                    if let Some(find_value) = replay_data.get("find") {
                        find_cache.insert(replay_id.clone(), find_value.clone());
                    }
                }
            }
        }
    }

    // Write the updated cache back to disk.
    let cache_content = serde_json::to_string(&find_cache)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
    fs::write(&cache_path, cache_content)
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;

    // Gather, sort (by "created" descending), and return the replays.
    let mut replays: Vec<Value> = find_cache.into_values().collect();
    replays.sort_by(|a, b| {
        let a_created = a.get("created").and_then(|v| v.as_i64()).unwrap_or(0);
        let b_created = b.get("created").and_then(|v| v.as_i64()).unwrap_or(0);
        b_created.cmp(&a_created)
    });
    Ok(serde_json::json!({ "replays": replays }))
}

/// GET "/meta/:replay_id"
async fn meta_handler(
    Path(replay_id): Path<String>,
    State(state): State<AppState>,
) -> impl IntoResponse {
    let local_path = format!("{}/{}/metadata.json", state.data_dir, replay_id);
    if fs::metadata(&local_path).await.is_ok() {
        if let Ok(content) = fs::read_to_string(&local_path).await {
            if let Ok(json_val) = serde_json::from_str::<Value>(&content) {
                if let Some(meta) = json_val.get("meta") {
                    return (StatusCode::OK, Json(meta.clone())).into_response();
                }
            }
        }
        (StatusCode::INTERNAL_SERVER_ERROR, "Failed to read local metadata".to_string()).into_response()
    } else {
        // Forward the request to the remote server.
        let url = format!("{}/meta/{}", REMOTE_BASE_URL, replay_id);
        match state.client.get(url).send().await {
            Ok(resp) => {
                let status = resp.status();
                let orig_headers = resp.headers().clone();
                let mut builder = Response::builder().status(status);
                for (k, v) in orig_headers.iter() {
                    builder = builder.header(k, v);
                }
                let stream = resp.bytes_stream().map(|result| {
                    result.map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))
                });
                // Box the body so both match arms return the same type.
                let body = StreamBody::new(stream);
                builder.body(boxed(body)).unwrap()
            }
            Err(e) => (StatusCode::BAD_GATEWAY, e.to_string()).into_response(),
        }
    }
}

/// GET "/replay/:replay_id/file/:file_name"
async fn replay_file_handler(
    Path((replay_id, file_name)): Path<(String, String)>,
    State(state): State<AppState>,
) -> impl IntoResponse {
    let file_path = format!("{}/{}/{}", state.data_dir, replay_id, file_name);
    let timing_path = format!("{}/{}/timing.json", state.data_dir, replay_id);
    let mut extra_headers = HeaderMap::new();

    // If a timing file exists, attempt to add extra headers.
    if fs::metadata(&timing_path).await.is_ok() {
        if let Ok(timing_content) = fs::read_to_string(&timing_path).await {
            if let Ok(timing_data) = serde_json::from_str::<Value>(&timing_content) {
                if file_name.starts_with("stream.") {
                    let parts: Vec<&str> = file_name.split('.').collect();
                    if parts.len() >= 2 {
                        if let Ok(index) = parts[1].parse::<usize>() {
                            if let Some(timing_array) = timing_data.as_array() {
                                if index < timing_array.len() {
                                    if let Some(obj) = timing_array.get(index) {
                                        if let Some(numchunks) = obj.get("numchunks") {
                                            extra_headers.insert(
                                                "numchunks",
                                                HeaderValue::from_str(&numchunks.to_string())
                                                    .unwrap_or_else(|_| HeaderValue::from_static("")),
                                            );
                                        }
                                        if let Some(time) = obj.get("time") {
                                            extra_headers.insert(
                                                "time",
                                                HeaderValue::from_str(&time.to_string())
                                                    .unwrap_or_else(|_| HeaderValue::from_static("")),
                                            );
                                        }
                                        if let Some(state_val) = obj.get("state") {
                                            let s = if let Some(s) = state_val.as_str() {
                                                s.to_string()
                                            } else {
                                                state_val.to_string()
                                            };
                                            extra_headers.insert(
                                                "state",
                                                HeaderValue::from_str(&s)
                                                    .unwrap_or_else(|_| HeaderValue::from_static("")),
                                            );
                                        }
                                        if let Some(mtime1) = obj.get("mtime1") {
                                            extra_headers.insert(
                                                "mtime1",
                                                HeaderValue::from_str(&mtime1.to_string())
                                                    .unwrap_or_else(|_| HeaderValue::from_static("")),
                                            );
                                        }
                                        if let Some(mtime2) = obj.get("mtime2") {
                                            extra_headers.insert(
                                                "mtime2",
                                                HeaderValue::from_str(&mtime2.to_string())
                                                    .unwrap_or_else(|_| HeaderValue::from_static("")),
                                            );
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    if fs::metadata(&file_path).await.is_ok() {
        match fs::read(&file_path).await {
            Ok(data) => {
                let mut builder = Response::builder().status(StatusCode::OK);
                for (k, v) in extra_headers.iter() {
                    builder = builder.header(k, v);
                }
                // Box the body so it matches the error branch's type.
                builder.body(boxed(Full::from(data))).unwrap()
            }
            Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
        }
    } else {
        // Forward the request to the remote server.
        let url = format!("{}/replay/{}/file/{}", REMOTE_BASE_URL, replay_id, file_name);
        match state.client.get(url).send().await {
            Ok(resp) => {
                let status = resp.status();
                let mut headers = resp.headers().clone();
                // Merge in our extra headers.
                for (k, v) in extra_headers.iter() {
                    headers.insert(k, v.clone());
                }
                let mut builder = Response::builder().status(status);
                for (k, v) in headers.iter() {
                    builder = builder.header(k, v);
                }
                let stream = resp.bytes_stream().map(|result| {
                    result.map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))
                });
                builder.body(boxed(StreamBody::new(stream))).unwrap()
            }
            Err(e) => (StatusCode::BAD_GATEWAY, e.to_string()).into_response(),
        }
    }
}

#[derive(Deserialize)]
struct EventQuery {
    group: Option<String>,
}

/// GET "/replay/:replay_id/event"
async fn replay_event_handler(
    Path(replay_id): Path<String>,
    Query(params): Query<EventQuery>,
    State(state): State<AppState>,
) -> impl IntoResponse {
    let group = params.group.unwrap_or_else(|| "checkpoint".to_string());
    let metadata_path = format!("{}/{}/metadata.json", state.data_dir, replay_id);
    if fs::metadata(&metadata_path).await.is_ok() {
        if let Ok(content) = fs::read_to_string(&metadata_path).await {
            if let Ok(json_val) = serde_json::from_str::<Value>(&content) {
                if group == "checkpoint" {
                    if let Some(events) = json_val.get("events") {
                        return (StatusCode::OK, Json(events.clone())).into_response();
                    }
                } else if group == "Pavlov" {
                    if let Some(events) = json_val.get("events_pavlov") {
                        return (StatusCode::OK, Json(events.clone())).into_response();
                    }
                } else {
                    return (
                        StatusCode::BAD_REQUEST,
                        Json(serde_json::json!({"error": "Invalid group specified"})),
                    )
                        .into_response();
                }
            }
        }
        (StatusCode::INTERNAL_SERVER_ERROR, "Failed to read local metadata".to_string()).into_response()
    } else {
        // Forward the request.
        let url = format!("{}/replay/{}/event?group={}", REMOTE_BASE_URL, replay_id, group);
        match state.client.get(url).send().await {
            Ok(resp) => {
                let status = resp.status();
                let orig_headers = resp.headers().clone();
                let mut builder = Response::builder().status(status);
                for (k, v) in orig_headers.iter() {
                    builder = builder.header(k, v);
                }
                let stream = resp.bytes_stream().map(|result| {
                    result.map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))
                });
                builder.body(boxed(StreamBody::new(stream))).unwrap()
            }
            Err(e) => (StatusCode::BAD_GATEWAY, e.to_string()).into_response(),
        }
    }
}

#[derive(Deserialize)]
struct StartDownloadingQuery {
    user: String,
}

/// POST "/replay/:replay_id/startDownloading"
async fn start_downloading_handler(
    Path(replay_id): Path<String>,
    Query(params): Query<StartDownloadingQuery>,
    State(state): State<AppState>,
) -> impl IntoResponse {
    let metadata_path = format!("{}/{}/metadata.json", state.data_dir, replay_id);
    if fs::metadata(&metadata_path).await.is_ok() {
        if let Ok(content) = fs::read_to_string(&metadata_path).await {
            if let Ok(json_val) = serde_json::from_str::<Value>(&content) {
                {
                    let mut global_index = state.global_index.lock().unwrap();
                    global_index.clear();
                }
                let _ = update_global_index(&state, &replay_id).await;
                if let Some(resp_val) = json_val.get("start_downloading") {
                    return (StatusCode::OK, Json(resp_val.clone())).into_response();
                }
            }
        }
        (StatusCode::INTERNAL_SERVER_ERROR, "Failed to process local metadata".to_string()).into_response()
    } else {
        // Forward the request.
        let url = format!("{}/replay/{}/startDownloading?user={}", REMOTE_BASE_URL, replay_id, params.user);
        match state.client.post(url).send().await {
            Ok(resp) => {
                let _ = update_global_index(&state, &replay_id).await;
                let status = resp.status();
                let orig_headers = resp.headers().clone();
                let mut builder = Response::builder().status(status);
                for (k, v) in orig_headers.iter() {
                    builder = builder.header(k, v);
                }
                let stream = resp.bytes_stream().map(|result| {
                    result.map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e))
                });
                builder.body(boxed(StreamBody::new(stream))).unwrap()
            }
            Err(e) => (StatusCode::BAD_GATEWAY, e.to_string()).into_response(),
        }
    }
}

/// Helper: update the global index with events from the metadata.
async fn update_global_index(state: &AppState, replay_id: &str) -> Result<(), (StatusCode, String)> {
    let metadata_path = format!("{}/{}/metadata.json", state.data_dir, replay_id);
    if fs::metadata(&metadata_path).await.is_ok() {
        if let Ok(content) = fs::read_to_string(&metadata_path).await {
            if let Ok(json_val) = serde_json::from_str::<Value>(&content) {
                if let Some(events) = json_val.pointer("/events/events").and_then(|v| v.as_array()) {
                    let mut global_index = state.global_index.lock().unwrap();
                    for event in events {
                        if let (Some(id), Some(data)) = (event.get("id"), event.pointer("/data/data")) {
                            if let Some(id_str) = id.as_str() {
                                // Assume the event data is stored as a string.
                                let data_bytes = data.as_str().map(|s| s.as_bytes().to_vec()).unwrap_or_default();
                                global_index.insert(id_str.to_string(), data_bytes);
                            }
                        }
                    }
                }
            }
        }
    }
    Ok(())
}

/// POST "/replay/:replay_id/viewer/:viewer_id" → return 204 No Content.
async fn replay_viewer_handler(
    Path((_replay_id, _viewer_id)): Path<(String, String)>,
) -> impl IntoResponse {
    StatusCode::NO_CONTENT
}

/// GET "/__tv.vankrupt.net/relay" → simple JSON response.
async fn relay_handler() -> impl IntoResponse {
    (StatusCode::OK, Json(serde_json::json!({"__tv.vankrupt.net/relay": true})))
}
