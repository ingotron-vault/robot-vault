"""Fixture and fingerprint helper for development/backup-restore-e2e/e2e.py.

Runs inside the dev image on the stack's Compose network (hosts `postgres` and
`objectstore`) and prints one JSON document on stdout.

    fingerprint      every user table (row count and an md5 over the rows' text
                     form), every sequence value, and every object (sha256,
                     size, content type, user metadata)
    db-reset         drop and create the robotvault database (empty: no schema)
    db-seed          create and fill the fixture table bre_fixture
    db-change TAG    insert one bre_fixture row
    plant            the fixture objects: a 12 MiB multipart object with
                     rdv-resume-session metadata, odd keys, an empty object
    damage           after a backup: same-size wrong bytes, metadata-only and
                     content-type-only changes, and one extra object
    mutate TAG       one extra object and one metadata change (so that any
                     restore would change the bucket)
    empty            delete every object
    marker / unmark  put or delete the zero-byte folder marker `partial-marker/`

Environment: OBJECTSTORE_ROOT_USER, OBJECTSTORE_ROOT_PASSWORD, S3_BUCKET.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys

import boto3
import psycopg
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

BUCKET = os.environ.get("S3_BUCKET", "robotics-data")
DSN = "postgres://robotvault:robotvault@postgres:5432/{}"
MULTIPART = "fixtures/recording multipart.mcap"
SMALL = "fixtures/meta.bin"
ODD = "fixtures/odd name +%20 ü/ä.txt"


def s3():
    return boto3.client(
        "s3",
        endpoint_url="http://objectstore:9000",
        aws_access_key_id=os.environ["OBJECTSTORE_ROOT_USER"],
        aws_secret_access_key=os.environ["OBJECTSTORE_ROOT_PASSWORD"],
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def keys(client) -> list[str]:
    out = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        out.extend(item["Key"] for item in page.get("Contents", []))
    return out


def database() -> dict:
    tables, sequences = {}, {}
    with psycopg.connect(DSN.format("robotvault")) as conn:
        for schema, table in conn.execute(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2"
        ).fetchall():
            count, digest = conn.execute(
                f'SELECT count(*), md5(coalesce(string_agg(t::text, E\'\\n\' ORDER BY t::text), \'\')) '
                f'FROM "{schema}"."{table}" t'
            ).fetchone()
            tables[f"{schema}.{table}"] = {"rows": count, "md5": digest}
        for schema, name in conn.execute("SELECT schemaname, sequencename FROM pg_sequences ORDER BY 1, 2").fetchall():
            sequences[f"{schema}.{name}"] = conn.execute(f'SELECT last_value FROM "{schema}"."{name}"').fetchone()[0]
    return {"tables": tables, "sequences": sequences}


def fingerprint() -> dict:
    client = s3()
    objects = {}
    for key in keys(client):
        head = client.head_object(Bucket=BUCKET, Key=key)
        body = client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        objects[key] = {"sha256": hashlib.sha256(body).hexdigest(), "size": len(body),
                        "content_type": head["ContentType"], "metadata": head.get("Metadata", {})}
    return {"db": database(), "objects": objects}


def put(client, key: str, body: bytes, content_type: str, metadata: dict) -> None:
    client.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType=content_type, Metadata=metadata)


def plant() -> dict:
    client = s3()
    rng = random.Random(20261122)
    big = rng.randbytes(12 * 1024 * 1024 + 17)
    with open("/tmp/bre-multipart.bin", "wb") as handle:
        handle.write(big)
    # Three parts and no MD5 ETag, like ingest's resumable uploads.
    client.upload_file("/tmp/bre-multipart.bin", BUCKET, MULTIPART,
                       ExtraArgs={"ContentType": "application/x-mcap",
                                  "Metadata": {"rdv-resume-session": "session-0001"}},
                       Config=TransferConfig(multipart_threshold=5 * 1024 * 1024,
                                             multipart_chunksize=5 * 1024 * 1024))
    head = client.head_object(Bucket=BUCKET, Key=MULTIPART)
    assert head["ETag"].strip('"').endswith("-3"), head["ETag"]
    put(client, SMALL, rng.randbytes(4096), "application/octet-stream",
        {"rdv-resume-session": "session-0002", "x-origin": "plant"})
    put(client, ODD, b"odd key\n", "text/plain", {"x-origin": "plant"})
    put(client, "fixtures/empty.bin", b"", "application/octet-stream", {})
    put(client, "cas/files/ab/abcdef0123", rng.randbytes(1000), "binary/octet-stream", {})
    return {"planted": 5}


def damage() -> dict:
    client = s3()
    old = client.get_object(Bucket=BUCKET, Key=SMALL)
    body = old["Body"].read()
    put(client, SMALL, bytes(b ^ 0xFF for b in body), old["ContentType"], old["Metadata"])  # same size
    big = client.get_object(Bucket=BUCKET, Key=MULTIPART)
    put(client, MULTIPART, big["Body"].read(), big["ContentType"], {"rdv-resume-session": "session-CHANGED"})
    odd = client.get_object(Bucket=BUCKET, Key=ODD)
    put(client, ODD, odd["Body"].read(), "application/json", odd["Metadata"])
    put(client, "stray/after-backup.txt", b"stray\n", "text/plain", {})
    return {"damaged": 4}


def mutate(tag: str) -> dict:
    client = s3()
    put(client, f"stray/{tag}.txt", tag.encode(), "text/plain", {})
    # A failed (red) restore before may have deleted it; any state works, it only has to change.
    body = client.get_object(Bucket=BUCKET, Key=SMALL)["Body"].read() if SMALL in keys(client) else b"x"
    put(client, SMALL, body, "application/octet-stream", {"rdv-resume-session": tag})
    return db_change(tag)


def empty() -> dict:
    client = s3()
    found = keys(client)
    for start in range(0, len(found), 1000):
        client.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": k} for k in found[start:start + 1000]]})
    return {"deleted": len(found)}


def db_reset() -> dict:
    with psycopg.connect(DSN.format("postgres"), autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS robotvault WITH (FORCE)")
        conn.execute("CREATE DATABASE robotvault OWNER robotvault")
    return {"reset": True}


def db_seed() -> dict:
    with psycopg.connect(DSN.format("robotvault")) as conn:
        conn.execute("CREATE TABLE bre_fixture (id serial PRIMARY KEY, note text NOT NULL, payload bytea)")
        for i in range(50):
            conn.execute("INSERT INTO bre_fixture (note, payload) VALUES (%s, %s)", (f"row {i}", bytes([i]) * i))
    return {"seeded": 50}


def db_change(tag: str) -> dict:
    with psycopg.connect(DSN.format("robotvault")) as conn:
        conn.execute("INSERT INTO bre_fixture (note) VALUES (%s)", (f"after backup {tag}",))
    return {"changed": tag}


def main(argv: list[str]) -> int:
    commands = {"fingerprint": fingerprint, "plant": plant, "damage": damage, "empty": empty,
                "db-reset": db_reset, "db-seed": db_seed,
                "marker": lambda: put(s3(), "partial-marker/", b"", "application/x-directory", {}) or {},
                "unmark": lambda: s3().delete_object(Bucket=BUCKET, Key="partial-marker/") and {}}
    if len(argv) == 1 and argv[0] in commands:
        print(json.dumps(commands[argv[0]](), sort_keys=True))
        return 0
    if len(argv) == 2 and argv[0] in ("mutate", "db-change"):
        print(json.dumps((mutate if argv[0] == "mutate" else db_change)(argv[1]), sort_keys=True))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
