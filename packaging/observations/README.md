# Private receiver examples

The receiver accepts private observations only when you start it explicitly.
These templates do not activate services or deploy containers.

Provide AUT_OBSERVE_TOKEN through the runtime environment. Use at least 16 non-whitespace characters.
For systemd, create /etc/aut-observations.env with mode 0600 and an AUT_OBSERVE_TOKEN assignment.
Create the dedicated aut user before installing the service. StateDirectory supplies private persistent storage.

Build the container from the repository root with this Dockerfile.
Mount a private persistent volume at /data, owned by uid 10001 with mode 0700.
Provide AUT_OBSERVE_TOKEN at runtime. Do not bake it into the image.
Bind the published port to 127.0.0.1 only.

The container listens on all container interfaces to support port forwarding.
Any nonloopback exposure requires an authenticated TLS reverse proxy and a private network boundary.
This minimal internal receiver is not a production internet service.
Health checks expose no private data. Private reads and ingestion both require bearer authentication.
A 409 receipt means conflicting evidence exists; other records in that batch may already be durable.
Retry exact records to recover lost acknowledgments. Do not treat a 409 as complete acceptance.
