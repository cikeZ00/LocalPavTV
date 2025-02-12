use actix_cors::Cors;
use actix_web::http::header::{HeaderMap, HeaderName, HeaderValue};
use actix_web::http::StatusCode as ActixStatusCode;
use actix_web::{web, App, Error, HttpResponse, HttpServer, Responder};
use futures::StreamExt;
use reqwest::Client;
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::fs;
use std::io::Write;
use std::path::Path;
use std::sync::Mutex;

// --------------------------------------------------
// Shared application state
// --------------------------------------------------
struct AppState {
    data_dir: String,
    base_url: String,
    http_client: Client,
    /// In-memory index mapping event IDs to (binary) event data.
    global_index: Mutex<HashMap<String, Vec<u8>>>,
}

fn update_global_index(state: &AppState, replay_id: &str) -> Result<(), Box<dyn std::error::Error>> {
    let replay_path = format!("{}/{}/metadata.json", state.data_dir, replay_id);
    if Path::new(&replay_path).exists() {
        let file = fs::File::open(&replay_path)?;
        let replay_data: Value = serde_json::from_reader(file)?;
        if let Some(events_array) = replay_data
            .get("events")
            .and_then(|v| v.get("events"))
            .and_then(|v| v.as_array())
        {
            let mut index = state.global_index.lock().unwrap();
            // (Clear the index first if desired)
            for event in events_array {
                if let Some(event_id) = event.get("id").and_then(|v| v.as_str()) {
                    if let Some(event_data_val) = event.get("data").and_then(|v| v.get("data")) {
                        // In the original Python code the event “data” is simply converted to bytes.
                        // Here we try to support either an array of numbers or a string.
                        let data_bytes = if let Some(arr) = event_data_val.as_array() {
                            arr.iter()
                                .filter_map(|x| x.as_u64().map(|n| n as u8))
                                .collect()
                        } else if let Some(s) = event_data_val.as_str() {
                            s.as_bytes().to_vec()
                        } else {
                            Vec::new()
                        };
                        index.insert(event_id.to_string(), data_bytes);
                    }
                }
            }
        }
    }
    Ok(())
}

// --------------------------------------------------
// GET "/find/any" - List replays from the local data directory.
// --------------------------------------------------

async fn list_replays(state: web::Data<AppState>) -> Result<HttpResponse, Error> {
    let mut replays = Vec::new();
    let data_dir = &state.data_dir;
    if let Ok(entries) = fs::read_dir(data_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                let replay_path = path.join("metadata.json");
                if replay_path.exists() {
                    if let Ok(file) = fs::File::open(&replay_path) {
                        if let Ok(replay_data) = serde_json::from_reader::<_, Value>(file) {
                            if let Some(find_val) = replay_data.get("find") {
                                replays.push(find_val.clone());
                            }
                        }
                    }
                }
            }
        }
    }
    // Sort replays descending by the "created" field.
    replays.sort_by(|a, b| {
        let a_created = a.get("created").and_then(|v| v.as_i64()).unwrap_or(0);
        let b_created = b.get("created").and_then(|v| v.as_i64()).unwrap_or(0);
        b_created.cmp(&a_created)
    });
    Ok(HttpResponse::Ok().json(json!({ "replays": replays })))
}


// --------------------------------------------------
// GET "/event/{event_id}" - Look up an event and return it gzipped.
// --------------------------------------------------

async fn get_event_stream(
    state: web::Data<AppState>,
    path: web::Path<String>,
) -> Result<HttpResponse, Error> {
    let event_id = path.into_inner();
    let index = state.global_index.lock().unwrap();
    if let Some(event_data) = index.get(&event_id) {
        // Compress data with gzip
        let mut encoder = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
        encoder.write_all(event_data)?;
        let compressed_data = encoder.finish()?;
        Ok(HttpResponse::Ok()
            .content_type("application/octet-stream")
            .append_header(("Content-Encoding", "gzip"))
            .body(compressed_data))
    } else {
        Ok(HttpResponse::NotFound().body("Event data not found"))
    }
}

// --------------------------------------------------
// GET "/meta/{replay_id}" - Return local metadata or proxy from remote.
// --------------------------------------------------
async fn meta(
    state: web::Data<AppState>,
    path: web::Path<String>,
) -> Result<HttpResponse, Error> {
    let replay_id = path.into_inner();
    let meta_path = format!("{}/{}/metadata.json", state.data_dir, replay_id);
    if Path::new(&meta_path).exists() {
        let file = fs::File::open(&meta_path)?;
        let replay_data: Value = serde_json::from_reader(file)?;
        if let Some(meta) = replay_data.get("meta") {
            return Ok(HttpResponse::Ok().json(meta));
        } else {
            return Ok(HttpResponse::InternalServerError().body("Meta not found in file"));
        }
    } else {
        // Proxy to the remote server.
        let url = format!("{}/meta/{}", state.base_url, replay_id);
        let client = &state.http_client;
        let resp = client
            .get(&url)
            .send()
            .await
            .map_err(actix_web::error::ErrorBadGateway)?;
        // Convert Reqwest status code to Actix status code.
        let status = ActixStatusCode::from_u16(resp.status().as_u16())
            .unwrap_or(ActixStatusCode::INTERNAL_SERVER_ERROR);
        let reqwest_headers = resp.headers().clone();
        // Convert the reqwest stream to an Actix stream.
        let stream = resp.bytes_stream().map(|item| {
            item.map_err(|e| actix_web::error::ErrorBadGateway(e))
        });
        let mut builder = HttpResponse::build(status);
        // Convert each header from reqwest’s type to a (&str, &str) tuple.
        for (key, value) in reqwest_headers.iter() {
            if let Ok(val) = value.to_str() {
                builder.append_header((key.to_string(), val.to_string()));
            }
        }
        Ok(builder.streaming(stream))
    }
}

// --------------------------------------------------
// GET "/replay/{replay_id}/file/{file_name}" - Serve a replay file.
// --------------------------------------------------
async fn get_replay_file(
    state: web::Data<AppState>,
    path: web::Path<(String, String)>,
) -> Result<HttpResponse, Error> {
    let (replay_id, file_name) = path.into_inner();
    let file_path = format!("{}/{}/{}", state.data_dir, replay_id, file_name);
    let timing_path = format!("{}/{}/timing.json", state.data_dir, replay_id);

    let mut extra_headers = HeaderMap::new();
    if Path::new(&timing_path).exists() {
        if let Ok(file) = fs::File::open(&timing_path) {
            if let Ok(timing_data) = serde_json::from_reader::<_, Value>(file) {
                if file_name.starts_with("stream.") {
                    let parts: Vec<&str> = file_name.split('.').collect();
                    if parts.len() >= 2 {
                        if let Ok(index) = parts[1].parse::<usize>() {
                            if let Some(array) = timing_data.as_array() {
                                if index < array.len() {
                                    if let Some(chunk) = array.get(index) {
                                        if let Some(numchunks) = chunk.get("numchunks") {
                                            extra_headers.insert(
                                                HeaderName::from_static("numchunks"),
                                                HeaderValue::from_str(&numchunks.to_string()).unwrap(),
                                            );
                                        }
                                        if let Some(time) = chunk.get("time") {
                                            extra_headers.insert(
                                                HeaderName::from_static("time"),
                                                HeaderValue::from_str(&time.to_string()).unwrap(),
                                            );
                                        }
                                        if let Some(state_val) = chunk.get("state").and_then(|v| v.as_str()) {
                                            extra_headers.insert(
                                                HeaderName::from_static("state"),
                                                HeaderValue::from_str(state_val).unwrap(),
                                            );
                                        }
                                        if let Some(mtime1) = chunk.get("mtime1") {
                                            extra_headers.insert(
                                                HeaderName::from_static("mtime1"),
                                                HeaderValue::from_str(&mtime1.to_string()).unwrap(),
                                            );
                                        }
                                        if let Some(mtime2) = chunk.get("mtime2") {
                                            extra_headers.insert(
                                                HeaderName::from_static("mtime2"),
                                                HeaderValue::from_str(&mtime2.to_string()).unwrap(),
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

    if Path::new(&file_path).exists() {
        let data = fs::read(&file_path)?;
        let mut response = HttpResponse::Ok().body(data);
        // Insert any extra headers.
        for (k, v) in extra_headers.iter() {
            if let Ok(val) = v.to_str() {
                response.headers_mut().insert(k.clone(), HeaderValue::from_str(val).unwrap());
            }
        }
        Ok(response)
    } else {
        // Proxy to the remote server.
        let url = format!("{}/replay/{}/file/{}", state.base_url, replay_id, file_name);
        let client = &state.http_client;
        let resp = client
            .get(&url)
            .send()
            .await
            .map_err(actix_web::error::ErrorBadGateway)?;
        let status = ActixStatusCode::from_u16(resp.status().as_u16())
            .unwrap_or(ActixStatusCode::INTERNAL_SERVER_ERROR);
        let mut headers = resp.headers().clone();
        // Merge in the extra headers.
        let stream = resp.bytes_stream().map(|item| {
            item.map_err(|e| actix_web::error::ErrorBadGateway(e))
        });
        let mut builder = HttpResponse::build(status);
        // Convert reqwest headers to Actix-friendly (&str, &str) tuples.
        for (key, value) in headers.iter() {
            if let Ok(val) = value.to_str() {
                builder.append_header((key.to_string(), val.to_string()));
            }
        }
        Ok(builder.streaming(stream))
    }
}

// --------------------------------------------------
// GET "/replay/{replay_id}/event" - Return events from metadata (or proxy).
// --------------------------------------------------
#[derive(Deserialize)]
struct EventQuery {
    group: Option<String>,
}

async fn get_events(
    state: web::Data<AppState>,
    path: web::Path<String>,
    query: web::Query<EventQuery>,
) -> Result<HttpResponse, Error> {
    let replay_id = path.into_inner();
    let group = query.group.clone().unwrap_or_else(|| "checkpoint".to_string());
    let meta_path = format!("{}/{}/metadata.json", state.data_dir, replay_id);
    if Path::new(&meta_path).exists() {
        let file = fs::File::open(&meta_path)?;
        let replay_data: Value = serde_json::from_reader(file)?;
        if group == "checkpoint" {
            if let Some(events) = replay_data.get("events") {
                return Ok(HttpResponse::Ok().json(events));
            }
        } else if group == "Pavlov" {
            if let Some(events) = replay_data.get("events_pavlov") {
                return Ok(HttpResponse::Ok().json(events));
            }
        }
        return Ok(HttpResponse::BadRequest().json(json!({"error": "Invalid group specified"})));
    } else {
        // Proxy to remote.
        let url = format!("{}/replay/{}/event", state.base_url, replay_id);
        let client = &state.http_client;
        let req = client.get(&url).query(&[("group", &group)]);
        let resp = req.send().await.map_err(actix_web::error::ErrorBadGateway)?;
        let status = ActixStatusCode::from_u16(resp.status().as_u16())
            .unwrap_or(ActixStatusCode::INTERNAL_SERVER_ERROR);
        let reqwest_headers = resp.headers().clone();
        let stream = resp.bytes_stream().map(|item| {
            item.map_err(|e| actix_web::error::ErrorBadGateway(e))
        });
        let mut builder = HttpResponse::build(status);
        for (key, value) in reqwest_headers.iter() {
            if let Ok(val) = value.to_str() {
                builder.append_header((key.to_string(), val.to_string()));
            }
        }
        Ok(builder.streaming(stream))
    }
}

// --------------------------------------------------
// POST "/replay/{replay_id}/startDownloading" - Start downloading a replay.
// --------------------------------------------------
#[derive(Deserialize)]
struct UserQuery {
    user: String,
}

async fn start_downloading(
    state: web::Data<AppState>,
    path: web::Path<String>,
    query: web::Query<UserQuery>,
) -> Result<HttpResponse, Error> {
    let replay_id = path.into_inner();
    let meta_path = format!("{}/{}/metadata.json", state.data_dir, replay_id);
    if Path::new(&meta_path).exists() {
        let file = fs::File::open(&meta_path)?;
        let replay_data: Value = serde_json::from_reader(file)?;
        // Clear and update the global index.
        {
            let mut index = state.global_index.lock().unwrap();
            index.clear();
        }
        update_global_index(&state, &replay_id)
            .map_err(actix_web::error::ErrorInternalServerError)?;
        if let Some(start_downloading) = replay_data.get("start_downloading") {
            Ok(HttpResponse::Ok().json(start_downloading))
        } else {
            Ok(HttpResponse::InternalServerError().body("start_downloading not found"))
        }
    } else {
        // Proxy to remote.
        let url = format!("{}/replay/{}/startDownloading", state.base_url, replay_id);
        let client = &state.http_client;
        let resp = client
            .post(&url)
            .query(&[("user", &query.user)])
            .send()
            .await
            .map_err(actix_web::error::ErrorBadGateway)?;
        // Update the index regardless.
        update_global_index(&state, &replay_id)
            .map_err(actix_web::error::ErrorInternalServerError)?;
        let status = ActixStatusCode::from_u16(resp.status().as_u16())
            .unwrap_or(ActixStatusCode::INTERNAL_SERVER_ERROR);
        let reqwest_headers = resp.headers().clone();
        let stream = resp.bytes_stream().map(|item| {
            item.map_err(|e| actix_web::error::ErrorBadGateway(e))
        });
        let mut builder = HttpResponse::build(status);
        for (key, value) in reqwest_headers.iter() {
            if let Ok(val) = value.to_str() {
                builder.append_header((key.to_string(), val.to_string()));
            }
        }
        Ok(builder.streaming(stream))
    }
}

// --------------------------------------------------
// POST "/replay/{replay_id}/viewer/{viewer_id}" - Return 204 No Content.
// --------------------------------------------------

async fn replay_viewer() -> impl Responder {
    HttpResponse::NoContent().finish()
}

// --------------------------------------------------

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    let port = 8080;
    let data_dir = "./data".to_string();
    let base_url = "https://tv.vankrupt.net:443".to_string();

    let http_client = Client::builder()
        .danger_accept_invalid_certs(true)
        .build()
        .expect("Failed to build HTTP client");

    let state = web::Data::new(AppState {
        data_dir,
        base_url,
        http_client,
        global_index: Mutex::new(HashMap::new()),
    });

    HttpServer::new(move || {
        let cors = Cors::default()
            .allowed_origin("http://localhost")
            .allowed_origin("https://tv.vankrupt.net")
            .allow_any_method()
            .allow_any_header();
        App::new()
            .wrap(cors)
            .app_data(state.clone())
            .route("/find/any", web::get().to(list_replays))
            .route("/meta/{replay_id}", web::get().to(meta))
            .route("/event/{event_id}", web::get().to(get_event_stream))
            .route("/replay/{replay_id}/file/{file_name}", web::get().to(get_replay_file))
            .route("/replay/{replay_id}/event", web::get().to(get_events))
            .route("/replay/{replay_id}/startDownloading", web::post().to(start_downloading))
            .route(
                "/replay/{replay_id}/viewer/{viewer_id}",
                web::post().to(replay_viewer),
            )
    })
        .bind(("0.0.0.0", port))?
        .run()
        .await
}