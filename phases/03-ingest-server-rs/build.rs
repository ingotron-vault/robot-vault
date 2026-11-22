fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Use local ./proto (Dockerfile copies root proto/ into /app/proto). For
    // native builds outside Docker, that dir doesn't exist, so fall back to
    // the repo-root proto/ directory instead.
    let manifest_dir = std::env::var("CARGO_MANIFEST_DIR").unwrap();
    let local_proto_dir = std::path::Path::new(&manifest_dir).join("proto");
    let repo_root_proto_dir = std::path::Path::new(&manifest_dir).join("../../proto");
    let proto_dir = if local_proto_dir.join("upload.proto").exists() {
        local_proto_dir
    } else {
        repo_root_proto_dir
    };
    let upload_proto = proto_dir.join("upload.proto");
    let enrollment_proto = proto_dir.join("enrollment.proto");

    let out_dir = std::env::var("OUT_DIR").unwrap();
    let descriptor_path = std::path::Path::new(&out_dir).join("upload_descriptor.bin");
    tonic_build::configure()
        // This oneof is generated from the public wire contract. Boxing only
        // its metadata arm would change the generated Rust API for every
        // client while leaving the protobuf bytes identical, so keep the
        // generated representation and scope the lint exception to that enum.
        .type_attribute(
            "robotics.upload.v1.UploadRequest.data",
            "#[allow(clippy::large_enum_variant)]",
        )
        .file_descriptor_set_path(&descriptor_path)
        .compile(
            &[
                upload_proto.to_string_lossy().to_string(),
                enrollment_proto.to_string_lossy().to_string(),
            ],
            &[proto_dir.to_string_lossy().to_string()],
        )?;
    println!("cargo:rerun-if-changed={}", upload_proto.display());
    println!("cargo:rerun-if-changed={}", enrollment_proto.display());
    Ok(())
}
