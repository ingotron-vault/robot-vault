//! Durable multipart upload receipts. Only completed S3 parts enter the resume plan.
use super::*;
use aws_sdk_s3::error::{ProvideErrorMetadata, SdkError};
use std::collections::{BTreeMap, BTreeSet};
use std::future::Future;
use tokio::io::AsyncReadExt;

const S3_TIMEOUT: Duration = Duration::from_secs(120);
const SESSION_METADATA: &str = "rdv-resume-session";

// Session lifetime. Each stored part or resume claim moves `expires_at` to
// seven days from now, but never past 14 days after the session (and its S3
// multipart upload) was created. Object storage counts the age of an
// incomplete multipart upload from its creation, not from its last part, so
// the hard limit is what makes a finite storage-side expiry safe. The object store's
// `RUSTFS_API_STALE_UPLOADS_EXPIRY` (360h, CONTRACT.md section 6) must therefore be longer than 14 days plus
// one sweep interval. The sweep below aborts expired sessions' uploads itself;
// the storage-side expiry only removes uploads that no session row records.

#[derive(Clone, Debug)]
struct Session {
    upload_id: Uuid,
    session_id: Uuid,
    mpu_id: String,
    chunk_size: i64,
    generation: i64,
    phase: String,
    storage_path: String,
    expires_at: chrono::DateTime<Utc>,
}

#[derive(Clone, Debug)]
struct Part {
    number: i32,
    offset: i64,
    size: i64,
    etag: String,
    sha256: String,
}

#[derive(Debug, PartialEq, Eq)]
struct Plan {
    bytes_received: i64,
    missing: Vec<i32>,
}

#[allow(clippy::result_large_err)]
fn part_count(file_size: i64, chunk_size: i64) -> Result<i32, Status> {
    if !(5 * 1024 * 1024..=STREAM_CHUNK_CEILING_BYTES as i64).contains(&chunk_size) {
        return Err(Status::invalid_argument(
            "resumable chunk_size_bytes must be between 5 and 8 MiB",
        ));
    }
    if file_size <= 0 {
        return Err(Status::invalid_argument(
            "resumable streaming requires a positive file_size",
        ));
    }
    let count = ((file_size - 1) / chunk_size) + 1;
    if count > MAX_PARTS as i64 {
        return Err(Status::invalid_argument(
            "file exceeds the 10000-part limit at this chunk size",
        ));
    }
    Ok(count as i32)
}

#[allow(clippy::result_large_err)]
fn plan(file_size: i64, chunk_size: i64, parts: &[Part]) -> Result<Plan, Status> {
    let count = part_count(file_size, chunk_size)?;
    let mut accepted = BTreeSet::new();
    let mut bytes_received = 0;
    for part in parts {
        if part.number < 1 || part.number > count {
            return Err(Status::data_loss("invalid durable multipart part number"));
        }
        let offset = i64::from(part.number - 1) * chunk_size;
        if part.offset != offset
            || part.size != (file_size - offset).min(chunk_size)
            || part.etag.is_empty()
            || !accepted.insert(part.number - 1)
        {
            return Err(Status::data_loss("invalid durable multipart receipt"));
        }
        bytes_received += part.size;
    }
    Ok(Plan {
        bytes_received,
        missing: (0..count)
            .filter(|number| !accepted.contains(number))
            .collect(),
    })
}

fn db_error(error: impl std::fmt::Display) -> Status {
    warn!(%error, "durable multipart database operation failed");
    Status::unavailable("durable upload database unavailable")
}

async fn s3_call<T, E: std::fmt::Debug + ProvideErrorMetadata>(
    future: impl Future<Output = Result<T, SdkError<E>>>,
) -> Result<T, Status> {
    tokio::time::timeout(S3_TIMEOUT, future)
        .await
        .map_err(|_| Status::deadline_exceeded("object storage operation timed out"))?
        .map_err(|error| {
            warn!(?error, "durable multipart object storage operation failed");
            if error
                .as_service_error()
                .is_some_and(|error| error.code() == Some("NoSuchUpload"))
            {
                return Status::not_found("multipart storage session no longer exists");
            }
            Status::unavailable("object storage operation failed; retry to resume durable parts")
        })
}

async fn load_session(
    client: &tokio_postgres::Client,
    upload_id: &str,
) -> Result<Option<Session>, Status> {
    let row = client.query_opt(
        "SELECT s.upload_id, s.session_id, s.s3_upload_id, s.chunk_size_bytes, s.attempt_generation, s.state, s.expires_at, u.storage_path FROM upload_multipart_sessions s JOIN uploads u ON u.id=s.upload_id WHERE u.client_upload_id=$1",
        &[&upload_id],
    ).await.map_err(db_error)?;
    Ok(row.map(|r| Session {
        upload_id: r.get(0),
        session_id: r.get(1),
        mpu_id: r.get(2),
        chunk_size: r.get(3),
        generation: r.get(4),
        phase: r.get(5),
        expires_at: r.get(6),
        storage_path: r.get(7),
    }))
}

async fn load_parts(client: &tokio_postgres::Client, id: Uuid) -> Result<Vec<Part>, Status> {
    Ok(client.query(
        "SELECT part_number, offset_bytes, size_bytes, etag, sha256_hash FROM upload_multipart_parts WHERE upload_id=$1 ORDER BY part_number",
        &[&id],
    ).await.map_err(db_error)?.into_iter().map(|r| Part {
        number: r.get(0), offset: r.get(1), size: r.get(2), etag: r.get(3), sha256: r.get(4),
    }).collect())
}

pub(super) async fn resume(
    state: &AppState,
    upload_id: String,
    identity: &RequestIdentity,
) -> Result<Response<pb::ResumeUploadResponse>, Status> {
    let client = state.pg.get().await.map_err(db_error)?;
    let row = client
        .query_opt(
            "SELECT robot_id, status, file_size_bytes FROM uploads WHERE client_upload_id=$1",
            &[&upload_id],
        )
        .await
        .map_err(db_error)?;
    let mut response = pb::ResumeUploadResponse {
        upload_id: upload_id.clone(),
        resume_version: 1,
        status: "unknown".into(),
        ..Default::default()
    };
    let Some(row) = row else {
        return Ok(Response::new(response));
    };
    let robot_id: String = row.get(0);
    enforce_robot_resource_owner(identity.robot(), &robot_id)?;
    response.status = row.get(1);
    let file_size: i64 = row.get(2);
    if response.status == "completed" {
        response.bytes_received = file_size;
        return Ok(Response::new(response));
    }
    if let Some(session) = load_session(&client, &upload_id).await? {
        if session.expires_at > Utc::now() && session.phase != "expiring" {
            let parts = load_parts(&client, session.upload_id).await?;
            let checkpoint = plan(file_size, session.chunk_size, &parts)?;
            response.bytes_received = checkpoint.bytes_received;
            response.missing_chunks = checkpoint.missing;
            response.chunk_size_bytes = session.chunk_size;
            response.resume_session_id = session.session_id.to_string();
            response.expires_at = Some(Timestamp {
                seconds: session.expires_at.timestamp(),
                nanos: session.expires_at.timestamp_subsec_nanos() as i32,
            });
        }
    }
    Ok(Response::new(response))
}

async fn assert_owner(client: &tokio_postgres::Client, session: &Session) -> Result<(), Status> {
    if client.query_opt(
        "SELECT 1 FROM upload_multipart_sessions s JOIN uploads u ON u.id=s.upload_id WHERE s.upload_id=$1 AND s.session_id=$2 AND s.attempt_generation=$3 AND u.status<>'completed'",
        &[&session.upload_id, &session.session_id, &session.generation],
    ).await.map_err(db_error)?.is_none() {
        return Err(Status::aborted("upload attempt was superseded; request a fresh resume plan"));
    }
    Ok(())
}

async fn persist_part(
    lock: &mut UploadAdvisoryLock,
    session: &Session,
    part: &Part,
) -> Result<(), Status> {
    // Use the very connection holding the advisory lock. A lost connection
    // prevents a stale attempt from recording any receipt or terminal state.
    let transaction = lock.client_mut().transaction().await.map_err(db_error)?;
    let owned = transaction.execute(
        "UPDATE upload_multipart_sessions SET updated_at=NOW(), expires_at=LEAST(NOW()+INTERVAL '7 days', created_at+INTERVAL '14 days') WHERE upload_id=$1 AND session_id=$2 AND attempt_generation=$3",
        &[&session.upload_id, &session.session_id, &session.generation],
    ).await.map_err(db_error)?;
    if owned != 1 {
        return Err(Status::aborted("upload attempt was superseded"));
    }
    transaction.execute(
        "INSERT INTO upload_multipart_parts(upload_id,part_number,offset_bytes,size_bytes,etag,sha256_hash) VALUES($1,$2,$3,$4,$5,$6)",
        &[&session.upload_id, &part.number, &part.offset, &part.size, &part.etag, &part.sha256],
    ).await.map_err(db_error)?;
    transaction.execute(
        "UPDATE uploads SET bytes_received=(SELECT COALESCE(SUM(size_bytes),0) FROM upload_multipart_parts WHERE upload_id=$1), status='uploading',error_message=NULL WHERE id=$1 AND status<>'completed'",
        &[&session.upload_id],
    ).await.map_err(db_error)?;
    transaction.commit().await.map_err(db_error)?;
    Ok(())
}

async fn drain_parts(
    inflight: &mut tokio::task::JoinSet<Result<Part, Status>>,
    lock: &mut UploadAdvisoryLock,
    session: &Session,
    keep: usize,
) -> Result<(), Status> {
    let mut first_error = None;
    while inflight.len() > keep {
        match inflight.join_next().await {
            Some(Ok(Ok(part))) => {
                if let Err(error) = persist_part(lock, session, &part).await {
                    first_error.get_or_insert(error);
                }
            }
            Some(Ok(Err(error))) => {
                first_error.get_or_insert(error);
            }
            Some(Err(_)) => {
                first_error.get_or_insert(Status::unavailable("part task interrupted"));
            }
            None => break,
        }
    }
    match first_error {
        Some(error) => Err(error),
        None => Ok(()),
    }
}

async fn create_or_claim(
    state: &AppState,
    lock: &mut UploadAdvisoryLock,
    meta: &pb::UploadMetadata,
    bucket: &str,
    key: &str,
) -> Result<Session, Status> {
    let s3 = state
        .s3_client
        .as_ref()
        .ok_or_else(|| Status::unavailable("s3_not_configured"))?;
    let mut existing = load_session(lock.client_mut(), &meta.upload_id).await?;
    if let Some(session) = &existing {
        if session.expires_at <= Utc::now() || session.phase == "expiring" {
            expire_session(state, lock, session, bucket, key).await?;
            existing = None;
        }
    }
    if let Some(mut session) = existing {
        if meta.resume_session_id != session.session_id.to_string() {
            return Err(Status::aborted(
                "resume session changed; request a fresh resume plan",
            ));
        }
        if meta.chunk_size_bytes != session.chunk_size {
            return Err(Status::aborted(
                "resume chunk size changed; request a fresh resume plan",
            ));
        }
        let row = lock.client_mut().query_one(
            "UPDATE upload_multipart_sessions SET attempt_generation=attempt_generation+1, updated_at=NOW(), expires_at=LEAST(NOW()+INTERVAL '7 days', created_at+INTERVAL '14 days') WHERE upload_id=$1 AND session_id=$2 RETURNING attempt_generation, expires_at",
            &[&session.upload_id, &session.session_id],
        ).await.map_err(db_error)?;
        session.generation = row.get(0);
        session.expires_at = row.get(1);
        return Ok(session);
    }
    if !meta.resume_session_id.is_empty() {
        return Err(Status::aborted(
            "resume session expired or reset; request a fresh resume plan",
        ));
    }
    let session_id = Uuid::new_v4();
    // Each new storage session gets its own key. A delayed S3 request from
    // an expired session cannot delete or overwrite its replacement's bytes.
    let destination = storage_path(
        &state.storage_root,
        &meta.robot_id,
        &format!("{}-{}", meta.upload_id, session_id),
        &meta.filename,
    );
    let new_key = path_to_s3_key(&destination);
    let new_location = format!("s3://{bucket}/{new_key}");
    let key = new_key.as_str();
    let created = s3_call(
        s3.create_multipart_upload()
            .bucket(bucket)
            .key(key)
            .metadata(SESSION_METADATA, session_id.to_string())
            .send(),
    )
    .await?;
    let mpu_id = created
        .upload_id()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| Status::unavailable("object storage returned no multipart id"))?
        .to_string();
    let published: Result<Session, Status> = async {
        let transaction = lock.client_mut().transaction().await.map_err(db_error)?;
        let row = transaction.query_one(
            "INSERT INTO upload_multipart_sessions(upload_id,session_id,s3_upload_id,chunk_size_bytes,attempt_generation) SELECT id,$2,$3,$4,1 FROM uploads WHERE client_upload_id=$1 AND status<>'completed' RETURNING upload_id,expires_at",
            &[&meta.upload_id, &session_id, &mpu_id, &meta.chunk_size_bytes],
        ).await.map_err(db_error)?;
        let id: Uuid = row.get(0);
        let written = transaction.execute("UPDATE uploads SET storage_path=$2,bytes_received=0,status='uploading',error_message=NULL WHERE id=$1 AND status<>'completed'",
            &[&id, &new_location]).await.map_err(db_error)?;
        if written != 1 { return Err(Status::aborted("upload already completed")); }
        transaction.commit().await.map_err(db_error)?;
        Ok(Session { upload_id: id, session_id, mpu_id: mpu_id.clone(),
            chunk_size: meta.chunk_size_bytes, generation: 1, phase: "uploading".into(),
            storage_path: new_location, expires_at: row.get(1) })
    }.await;
    if published.is_err() {
        // No session is published, so this MPU cannot be used by a retry.
        let _ = s3_call(
            s3.abort_multipart_upload()
                .bucket(bucket)
                .key(key)
                .upload_id(&mpu_id)
                .send(),
        )
        .await;
    }
    published
}

async fn mark_failed(lock: &mut UploadAdvisoryLock, session: &Session, error: &Status) {
    let result = lock.client_mut().execute(
        "UPDATE uploads u SET status='failed',error_message=$4 WHERE u.id=$1 AND u.status<>'completed' AND EXISTS(SELECT 1 FROM upload_multipart_sessions s WHERE s.upload_id=u.id AND s.session_id=$2 AND s.attempt_generation=$3)",
        &[&session.upload_id, &session.session_id, &session.generation, &error.message()],
    ).await;
    if let Err(error) = result {
        warn!(%error, "could not record interrupted resumable upload");
    }
}

pub(super) async fn upload(
    state: &AppState,
    mut stream: tonic::Streaming<UploadRequest>,
    meta: pb::UploadMetadata,
    request_identity: RequestIdentity,
    robot_id: String,
) -> Result<Response<UploadResponse>, Status> {
    if meta.resume_version != 1 {
        return Err(Status::invalid_argument("unsupported resume_version"));
    }
    validate_upload_metadata_shape(&meta.upload_id, &meta.filename, meta.file_size)?;
    part_count(meta.file_size, meta.chunk_size_bytes)?;
    let expected = normalize_sha256(&meta.expected_sha256)?;
    let s3 = state
        .s3_client
        .as_ref()
        .ok_or_else(|| Status::unavailable("s3_not_configured"))?;
    let bucket = state
        .s3_bucket
        .as_deref()
        .filter(|bucket| !bucket.is_empty())
        .ok_or_else(|| Status::unavailable("s3_not_configured"))?;
    let _robot_permit = try_acquire_per_robot(state, &robot_id, state.per_robot_limit)?;
    let _global_permit = state
        .global_sem
        .clone()
        .try_acquire_owned()
        .map_err(|_| Status::resource_exhausted("global upload capacity exceeded"))?;
    let destination = storage_path(
        &state.storage_root,
        &robot_id,
        &meta.upload_id,
        &meta.filename,
    );
    let proposed_path = format!("s3://{}/{}", bucket, path_to_s3_key(&destination));
    let identity = ImmutableUploadIdentity {
        client_upload_id: meta.upload_id.clone(),
        robot_id: robot_id.clone(),
        filename: meta.filename.clone(),
        file_size_bytes: meta.file_size,
        expected_sha256: expected.clone(),
    };
    let (mut lock, outcome) = reserve_upload_identity(
        &state.pg,
        &identity,
        &proposed_path,
        &meta.content_type,
        &meta.operator,
        &meta.site,
        &meta.permission_ref,
        &meta.session_tags,
        &meta.task_label,
        true,
    )
    .await?;
    let location = match outcome {
        UploadReservationOutcome::AlreadyCompleted(response) => {
            lock.release().await;
            return Ok(Response::new(response));
        }
        UploadReservationOutcome::Writable { storage_path } => storage_path,
    };
    let (bucket, key) = parse_s3_storage_path(&location)?;
    let session = create_or_claim(state, &mut lock, &meta, &bucket, &key).await?;
    let (bucket, key) = parse_s3_storage_path(&session.storage_path)?;
    let previous_parts = load_parts(lock.client_mut(), session.upload_id).await?;
    let previous = plan(meta.file_size, session.chunk_size, &previous_parts)?;
    lock.client_mut()
        .execute(
            "UPDATE uploads SET bytes_received=$2 WHERE id=$1 AND status<>'completed'",
            &[&session.upload_id, &previous.bytes_received],
        )
        .await
        .map_err(db_error)?;
    let accepted: BTreeMap<i32, Part> = previous_parts
        .into_iter()
        .map(|part| (part.number, part))
        .collect();
    let mut seen = BTreeSet::new();
    let mut last_number = -1;
    let mut final_seen = false;
    let mut inflight = tokio::task::JoinSet::new();
    let mut last_auth_recheck = std::time::Instant::now();
    let transfer_started = std::time::Instant::now();
    let transfer = async {
        let mut idle_deadline = tokio::time::Instant::now() + state.stream_idle_timeout;
        loop {
            let next = tokio::select! {
                next = stream.next() => next,
                completed = inflight.join_next(), if !inflight.is_empty() => {
                    let part = completed.ok_or_else(|| Status::unavailable("part task interrupted"))?
                        .map_err(|_| Status::unavailable("part task interrupted"))??;
                    persist_part(&mut lock, &session, &part).await?;
                    continue;
                }
                _ = tokio::time::sleep_until(idle_deadline) => {
                    return Err(Status::deadline_exceeded("upload stream idle timeout; durable parts retained"));
                }
            };
            idle_deadline = tokio::time::Instant::now() + state.stream_idle_timeout;
            let Some(message) = next else { break; };
            let request = message?;
            let chunk = match request.data {
                Some(pb::upload_request::Data::Chunk(chunk)) => chunk,
                _ => return Err(Status::invalid_argument("only chunks may follow upload metadata")),
            };
            if final_seen { return Err(Status::invalid_argument("chunk follows final transmitted chunk")); }
            let count = part_count(meta.file_size, session.chunk_size)?;
            if chunk.chunk_number < 0 || chunk.chunk_number >= count || chunk.chunk_number <= last_number {
                return Err(Status::invalid_argument("resume chunks must have ascending original chunk numbers"));
            }
            let offset = i64::from(chunk.chunk_number) * session.chunk_size;
            if chunk.offset != offset || chunk.data.len() as i64 != (meta.file_size - offset).min(session.chunk_size) {
                return Err(Status::invalid_argument("resume chunk offset or size does not match pinned part boundaries"));
            }
            if state.include_robot_labels {
                counter!("upload_bytes_total", "method" => "upload_resumable", "robot_id" => robot_id.clone()).increment(chunk.data.len() as u64);
            } else {
                counter!("upload_bytes_total", "method" => "upload_resumable").increment(chunk.data.len() as u64);
            }
            let digest = hex::encode(Sha256::digest(&chunk.data));
            if !chunk.chunk_hash.is_empty() && normalize_sha256(&chunk.chunk_hash)? != digest {
                return Err(Status::invalid_argument("chunk_hash_mismatch"));
            }
            if last_auth_recheck.elapsed() >= state.auth_recheck_interval {
                if let Some(reason) = recheck_stream_identity(state, &request_identity, &robot_id).await {
                    warn!(
                        upload_id = %meta.upload_id, robot_id = %robot_id, %reason,
                        credential_id = ?request_identity.credential_id,
                        "aborting in-flight upload: credential or robot state no longer valid"
                    );
                    counter!("ingest_stream_aborted_total", "reason" => reason).increment(1);
                    return Err(Status::permission_denied("upload authorization was revoked"));
                }
                last_auth_recheck = std::time::Instant::now();
            }
            last_number = chunk.chunk_number;
            final_seen = chunk.is_final;
            let number = chunk.chunk_number + 1;
            seen.insert(number);
            if let Some(part) = accepted.get(&number) {
                // Another attempt may have committed this part after ResumeUpload
                // returned. An exact duplicate costs no S3 write; different bytes
                // cannot silently replace a durable receipt.
                if part.sha256 != digest { return Err(Status::aborted("durable part differs; request a fresh resume plan")); }
                continue;
            }
            drain_parts(&mut inflight, &mut lock, &session, state.max_inflight_parts - 1).await?;
            assert_owner(lock.client_mut(), &session).await?;
            let s3 = s3.clone();
            let bucket = bucket.clone();
            let key = key.clone();
            let mpu_id = session.mpu_id.clone();
            let size = chunk.data.len() as i64;
            inflight.spawn(async move {
                let started = std::time::Instant::now();
                let response = s3_call(s3.upload_part().bucket(bucket).key(key).upload_id(mpu_id)
                    .part_number(number).body(ByteStream::from(chunk.data)).send()).await;
                histogram!("ingest_part_upload_seconds").record(started.elapsed().as_secs_f64());
                let response = response?;
                let etag = response.e_tag().filter(|value| !value.is_empty())
                    .ok_or_else(|| Status::unavailable("object storage returned no part ETag"))?.to_string();
                Ok(Part { number, offset, size, etag, sha256: digest })
            });
        }
        if !seen.is_empty() && !final_seen { return Err(Status::invalid_argument("missing final transmitted chunk marker")); }
        Ok(())
    }.await;
    // Keep successful later parts even when another part or the client fails.
    // Each S3 task has a finite deadline. No part task survives this RPC.
    let drained = drain_parts(&mut inflight, &mut lock, &session, 0).await;
    histogram!("ingest_receive_seconds", "method" => "upload_resumable")
        .record(transfer_started.elapsed().as_secs_f64());
    if let Err(error) = transfer.and(drained) {
        mark_failed(&mut lock, &session, &error).await;
        if error.code() == tonic::Code::NotFound {
            expire_session(state, &mut lock, &session, &bucket, &key).await?;
            return Err(Status::aborted(
                "multipart storage session expired; request a fresh resume plan",
            ));
        }
        return Err(error);
    }
    let parts = load_parts(lock.client_mut(), session.upload_id).await?;
    let checkpoint = plan(meta.file_size, session.chunk_size, &parts)?;
    if !checkpoint.missing.is_empty() {
        let error =
            Status::aborted("upload still has missing durable parts; request a fresh resume plan");
        mark_failed(&mut lock, &session, &error).await;
        return Err(error);
    }
    let write_started = std::time::Instant::now();
    let result = finish(
        state,
        &mut lock,
        &session,
        &meta,
        &robot_id,
        &bucket,
        &key,
        &expected,
        parts,
        &request_identity,
    )
    .await;
    histogram!("ingest_write_seconds", "method" => "upload_resumable")
        .record(write_started.elapsed().as_secs_f64());
    let result = match result {
        Err(error) if error.code() == tonic::Code::NotFound => {
            expire_session(state, &mut lock, &session, &bucket, &key).await?;
            Err(Status::aborted(
                "multipart storage session expired; request a fresh resume plan",
            ))
        }
        other => other,
    };
    if let Err(error) = &result {
        mark_failed(&mut lock, &session, error).await;
    }
    lock.release().await;
    result.map(Response::new)
}

#[allow(clippy::too_many_arguments)]
async fn verify_object(
    state: &AppState,
    session: &Session,
    bucket: &str,
    key: &str,
    expected: &str,
    file_size: i64,
    request_identity: &RequestIdentity,
    robot_id: &str,
) -> Result<String, Status> {
    let s3 = state
        .s3_client
        .as_ref()
        .ok_or_else(|| Status::unavailable("s3_not_configured"))?;
    let object = s3_call(s3.get_object().bucket(bucket).key(key).send()).await?;
    if object
        .metadata()
        .and_then(|metadata| metadata.get(SESSION_METADATA))
        != Some(&session.session_id.to_string())
    {
        return Err(Status::data_loss(
            "assembled object does not belong to this upload session",
        ));
    }
    if object.content_length() != Some(file_size) {
        return Err(Status::data_loss("assembled object size mismatch"));
    }
    let etag = object.e_tag().unwrap_or_default().to_string();
    let mut reader = object.body.into_async_read();
    let mut buffer = vec![0u8; 256 * 1024];
    let mut hash = Sha256::new();
    let mut received = 0i64;
    let mut last_auth_recheck = std::time::Instant::now();
    loop {
        if last_auth_recheck.elapsed() >= state.auth_recheck_interval {
            if recheck_stream_identity(state, request_identity, robot_id)
                .await
                .is_some()
            {
                return Err(Status::permission_denied(
                    "upload authorization was revoked",
                ));
            }
            last_auth_recheck = std::time::Instant::now();
        }
        let n = tokio::time::timeout(S3_TIMEOUT, reader.read(&mut buffer))
            .await
            .map_err(|_| Status::deadline_exceeded("assembled object verification timed out"))?
            .map_err(|_| Status::unavailable("assembled object read failed; retry verification"))?;
        if n == 0 {
            break;
        }
        received += n as i64;
        if received > file_size {
            return Err(Status::data_loss(
                "assembled object exceeded declared file size",
            ));
        }
        hash.update(&buffer[..n]);
    }
    if received != file_size || hex::encode(hash.finalize()) != expected {
        return Err(Status::data_loss("assembled object sha256_mismatch"));
    }
    Ok(etag)
}

async fn owns_assembled_object(
    s3: &s3::Client,
    session: &Session,
    bucket: &str,
    key: &str,
) -> Result<bool, Status> {
    let response =
        tokio::time::timeout(S3_TIMEOUT, s3.head_object().bucket(bucket).key(key).send())
            .await
            .map_err(|_| Status::deadline_exceeded("assembled object lookup timed out"))?;
    match response {
        Ok(object) => {
            if object
                .metadata()
                .and_then(|metadata| metadata.get(SESSION_METADATA))
                == Some(&session.session_id.to_string())
            {
                Ok(true)
            } else {
                Err(Status::data_loss(
                    "object at reserved path belongs to another session",
                ))
            }
        }
        Err(error)
            if error.as_service_error().is_some_and(|error| {
                matches!(error.code(), Some("NotFound" | "NoSuchKey" | "404"))
            }) =>
        {
            Ok(false)
        }
        Err(_) => Err(Status::unavailable(
            "assembled object lookup failed; retry verification",
        )),
    }
}

#[allow(clippy::too_many_arguments)]
async fn finish(
    state: &AppState,
    lock: &mut UploadAdvisoryLock,
    session: &Session,
    meta: &pb::UploadMetadata,
    robot_id: &str,
    bucket: &str,
    key: &str,
    expected: &str,
    parts: Vec<Part>,
    request_identity: &RequestIdentity,
) -> Result<UploadResponse, Status> {
    let s3 = state
        .s3_client
        .as_ref()
        .ok_or_else(|| Status::unavailable("s3_not_configured"))?;
    assert_owner(lock.client_mut(), session).await?;
    // Once finalizing is durable, a retry first looks for the assembled object.
    // This covers a crash/lost reply between CompleteMultipartUpload and SQL.
    let phase_changed = lock.client_mut().execute(
        "UPDATE upload_multipart_sessions SET state='finalizing',updated_at=NOW() WHERE upload_id=$1 AND session_id=$2 AND attempt_generation=$3",
        &[&session.upload_id, &session.session_id, &session.generation],
    ).await.map_err(db_error)?;
    if phase_changed != 1 {
        return Err(Status::aborted("upload attempt was superseded"));
    }
    let existing = if session.phase == "finalizing" {
        owns_assembled_object(s3, session, bucket, key).await?
    } else {
        false
    };
    if !existing {
        let complete = s3::types::CompletedMultipartUpload::builder()
            .set_parts(Some(
                parts
                    .into_iter()
                    .map(|part| {
                        s3::types::CompletedPart::builder()
                            .part_number(part.number)
                            .e_tag(part.etag)
                            .build()
                    })
                    .collect(),
            ))
            .build();
        let completed = s3_call(
            s3.complete_multipart_upload()
                .bucket(bucket)
                .key(key)
                .upload_id(&session.mpu_id)
                .if_none_match("*")
                .multipart_upload(complete)
                .send(),
        )
        .await;
        if let Err(error) = completed {
            if error.code() != tonic::Code::NotFound
                || !owns_assembled_object(s3, session, bucket, key).await?
            {
                return Err(error);
            }
        }
    }
    if let Err(error) = verify_object(
        state,
        session,
        bucket,
        key,
        expected,
        meta.file_size,
        request_identity,
        robot_id,
    )
    .await
    {
        if error.code() == tonic::Code::DataLoss {
            expire_session(state, lock, session, bucket, key).await?;
            lock.client_mut().execute("UPDATE uploads SET status='failed',error_message=$2,bytes_received=0 WHERE id=$1 AND status<>'completed'",
                &[&session.upload_id, &error.message()]).await.map_err(db_error)?;
        }
        return Err(error);
    }
    if recheck_stream_identity(state, request_identity, robot_id)
        .await
        .is_some()
    {
        return Err(Status::permission_denied(
            "upload authorization was revoked",
        ));
    }
    let transaction = lock.client_mut().transaction().await.map_err(db_error)?;
    let owned = transaction.execute("DELETE FROM upload_multipart_sessions WHERE upload_id=$1 AND session_id=$2 AND attempt_generation=$3",
        &[&session.upload_id, &session.session_id, &session.generation]).await.map_err(db_error)?;
    if owned != 1 {
        return Err(Status::aborted("upload attempt was superseded"));
    }
    let location = format!("s3://{bucket}/{key}");
    let operator = empty_to_null(&meta.operator);
    let site = empty_to_null(&meta.site);
    let permission_ref = empty_to_null(&meta.permission_ref);
    let session_tags = empty_to_null(&meta.session_tags);
    let task_label = empty_to_null(&meta.task_label);
    let written = transaction
        .execute(
            COMPLETED_UPLOAD_UPSERT_SQL,
            &[
                &meta.upload_id,
                &robot_id,
                &meta.filename,
                &location,
                &meta.file_size,
                &expected,
                &meta.content_type,
                &operator,
                &site,
                &permission_ref,
                &session_tags,
                &task_label,
            ],
        )
        .await
        .map_err(db_error)?;
    if written != 1 {
        return Err(Status::aborted("completed upload identity conflict"));
    }
    transaction.commit().await.map_err(db_error)?;
    if state.include_robot_labels {
        counter!("uploads_completed", "method" => "upload_resumable", "robot_id" => robot_id.to_string()).increment(1);
    } else {
        counter!("uploads_completed", "method" => "upload_resumable").increment(1);
    }
    Ok(UploadResponse {
        upload_id: meta.upload_id.clone(),
        status: "completed".into(),
        bytes_received: meta.file_size,
        final_hash: expected.to_string(),
        completed_at: now_ts(),
        storage_path: location,
        error_message: String::new(),
    })
}

async fn expire_session(
    state: &AppState,
    lock: &mut UploadAdvisoryLock,
    session: &Session,
    bucket: &str,
    key: &str,
) -> Result<(), Status> {
    let s3 = state
        .s3_client
        .as_ref()
        .ok_or_else(|| Status::unavailable("s3_not_configured"))?;
    assert_owner(lock.client_mut(), session).await?;
    let previous_phase: String = lock.client_mut().query_one(
        "SELECT state FROM upload_multipart_sessions WHERE upload_id=$1 AND session_id=$2 AND attempt_generation=$3",
        &[&session.upload_id, &session.session_id, &session.generation],
    ).await.map_err(db_error)?.get(0);
    lock.client_mut().execute(
        "UPDATE upload_multipart_sessions SET state='expiring' WHERE upload_id=$1 AND session_id=$2 AND attempt_generation=$3",
        &[&session.upload_id, &session.session_id, &session.generation],
    ).await.map_err(db_error)?;
    // Abort is idempotent for an MPU which has already been completed.
    let aborted = tokio::time::timeout(
        S3_TIMEOUT,
        s3.abort_multipart_upload()
            .bucket(bucket)
            .key(key)
            .upload_id(&session.mpu_id)
            .send(),
    )
    .await
    .map_err(|_| Status::deadline_exceeded("multipart expiry abort timed out"))?;
    if let Err(error) = aborted {
        if error
            .as_service_error()
            .is_none_or(|error| error.code() != Some("NoSuchUpload"))
        {
            return Err(Status::unavailable("multipart expiry abort failed"));
        }
    }
    // A completed-but-unverified object belongs to this session only if its
    // metadata token matches. Never delete a different or verified object.
    if previous_phase != "uploading" {
        match tokio::time::timeout(S3_TIMEOUT, s3.head_object().bucket(bucket).key(key).send())
            .await
        {
            Ok(Ok(object)) => {
                if object
                    .metadata()
                    .and_then(|metadata| metadata.get(SESSION_METADATA))
                    == Some(&session.session_id.to_string())
                {
                    assert_owner(lock.client_mut(), session).await?;
                    let etag = object
                        .e_tag()
                        .filter(|etag| !etag.is_empty())
                        .ok_or_else(|| Status::unavailable("expired object has no ETag"))?;
                    s3_call(
                        s3.delete_object()
                            .bucket(bucket)
                            .key(key)
                            .if_match(etag)
                            .send(),
                    )
                    .await?;
                }
            }
            Ok(Err(error))
                if error.as_service_error().is_some_and(|error| {
                    matches!(error.code(), Some("NotFound" | "NoSuchKey" | "404"))
                }) => {}
            _ => {
                return Err(Status::unavailable(
                    "cannot confirm expired session object cleanup",
                ))
            }
        }
    }
    let transaction = lock.client_mut().transaction().await.map_err(db_error)?;
    let deleted = transaction.execute("DELETE FROM upload_multipart_sessions WHERE upload_id=$1 AND session_id=$2 AND attempt_generation=$3",
        &[&session.upload_id, &session.session_id, &session.generation]).await.map_err(db_error)?;
    if deleted != 1 {
        return Err(Status::aborted("upload session was superseded"));
    }
    transaction.execute("UPDATE uploads SET status='failed',error_message='partial upload session reset',bytes_received=0 WHERE id=$1 AND status<>'completed'",
        &[&session.upload_id]).await.map_err(db_error)?;
    transaction.commit().await.map_err(db_error)?;
    Ok(())
}

async fn sweep_once(state: &AppState) -> Result<(), Status> {
    let client = state.pg.get().await.map_err(db_error)?;
    let rows = client.query("SELECT u.client_upload_id FROM upload_multipart_sessions s JOIN uploads u ON u.id=s.upload_id WHERE (s.expires_at<NOW() OR s.state='expiring') AND u.status<>'completed' ORDER BY s.expires_at LIMIT 32", &[]).await.map_err(db_error)?;
    drop(client);
    for row in rows {
        let upload_id: String = row.get(0);
        let cleanup: Result<(), Status> = async {
            let client = state.pg.get().await.map_err(db_error)?;
            let acquired: bool = client.query_one(UPLOAD_ADVISORY_LOCK_SQL, &[&upload_id]).await.map_err(db_error)?.get(0);
            if !acquired { return Ok(()); }
            let mut lock = UploadAdvisoryLock::new(client, &upload_id);
            let result = async {
                if let Some(session) = load_session(lock.client_mut(), &upload_id).await? {
                    if session.expires_at <= Utc::now() || session.phase == "expiring" {
                        let (bucket, key) = parse_s3_storage_path(&session.storage_path)?;
                        expire_session(state, &mut lock, &session, &bucket, &key).await?;
                        lock.client_mut().execute("UPDATE uploads SET status='failed',error_message='partial upload session expired',bytes_received=0 WHERE id=$1 AND status<>'completed'", &[&session.upload_id]).await.map_err(db_error)?;
                    }
                }
                Ok(())
            }.await;
            lock.release().await;
            result
        }.await;
        if let Err(error) = cleanup {
            warn!(%upload_id, %error, "could not expire multipart session; continuing sweep");
        }
    }
    Ok(())
}

pub(super) async fn sweep_loop(state: AppState) {
    let mut timer = tokio::time::interval(Duration::from_secs(15 * 60));
    loop {
        timer.tick().await;
        if let Err(error) = sweep_once(&state).await {
            warn!(%error, "durable upload expiry sweep failed");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn part(number: i32, size: i64) -> Part {
        Part {
            number,
            offset: i64::from(number - 1) * 5 * 1024 * 1024,
            size,
            etag: format!("etag-{number}"),
            sha256: "a".repeat(64),
        }
    }
    #[test]
    fn missing_plan_preserves_parts_after_a_hole() {
        let chunk = 5 * 1024 * 1024;
        assert_eq!(
            plan(
                3 * chunk + 7,
                chunk,
                &[part(1, chunk), part(3, chunk), part(4, 7)]
            )
            .unwrap(),
            Plan {
                bytes_received: 2 * chunk + 7,
                missing: vec![1]
            }
        );
    }
    #[test]
    fn complete_plan_needs_no_chunks() {
        let chunk = 5 * 1024 * 1024;
        assert_eq!(
            plan(chunk + 7, chunk, &[part(1, chunk), part(2, 7)])
                .unwrap()
                .missing,
            Vec::<i32>::new()
        );
    }
    #[test]
    fn malformed_receipts_never_become_progress() {
        let chunk = 5 * 1024 * 1024;
        assert!(plan(chunk + 7, chunk, &[part(1, 3)]).is_err());
        assert!(plan(chunk + 7, chunk, &[part(1, chunk), part(1, chunk)]).is_err());
        assert!(plan(chunk + 7, chunk, &[part(3, 7)]).is_err());
        assert!(part_count(chunk * 10001, chunk).is_err());
        assert!(part_count(100, 4 * 1024 * 1024).is_err());
    }
}
