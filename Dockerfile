FROM docker:29-cli AS dockercli
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home cloudagent \
    && mkdir /data && chown 10001:10001 /data
USER 10001:10001
ENV CLOUD_DATABASE_URL=sqlite:////data/cloud-agent.db
EXPOSE 8000
CMD ["cloud-agent"]
