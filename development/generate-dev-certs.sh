#!/usr/bin/env bash
set -euo pipefail

# Generates a local CA, server, and client certs for dev/testing.
# Outputs into ./certs

cd "$(dirname "$0")/.."

mkdir -p certs

# 1) Create a local CA
openssl genrsa -out certs/ca.key 4096
openssl req -x509 -new -nodes -key certs/ca.key -sha256 -days 3650 \
  -subj "/C=US/ST=CA/O=RobotVault/OU=Vault-Dev-CA/CN=robot-vault-dev-ca" \
  -out certs/ca.crt

# 2) Server cert for host 'ingest' with SAN DNS:ingest (plus localhost/127.0.0.1
#    so a host-side client can verify it too when the data plane is published
#    via docker-compose.debug.yml — hostname verification uses the SAN list)
openssl genrsa -out certs/ingest.key 2048
cat > certs/ingest.cnf <<EOF
[req]
distinguished_name = dn
req_extensions = v3_req
prompt = no

[dn]
C=US
ST=CA
O=RobotVault
OU=Vault-Dev
CN=ingest

[v3_req]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = ingest
DNS.2 = localhost
DNS.3 = robot-vault-ingest
IP.1 = 127.0.0.1
EOF
openssl req -new -key certs/ingest.key -out certs/ingest.csr -config certs/ingest.cnf
openssl x509 -req -in certs/ingest.csr -CA certs/ca.crt -CAkey certs/ca.key -CAcreateserial \
  -out certs/ingest.crt -days 825 -sha256 -extensions v3_req -extfile certs/ingest.cnf

# 3) Client cert for robot
openssl genrsa -out certs/robot.key 2048
cat > certs/robot.cnf <<EOF
[req]
distinguished_name = dn
req_extensions = v3_req
prompt = no

[dn]
C=US
ST=CA
O=RobotVault
OU=Vault-Dev
CN=robot

[v3_req]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
EOF
openssl req -new -key certs/robot.key -out certs/robot.csr -config certs/robot.cnf
openssl x509 -req -in certs/robot.csr -CA certs/ca.crt -CAkey certs/ca.key -CAcreateserial \
  -out certs/robot.crt -days 825 -sha256 -extensions v3_req -extfile certs/robot.cnf

echo "Dev certs generated under ./certs"

