# Compose files

Sanitized docker-compose files for every service in the homelab. Each service lives in its own folder (on the server: `/opt/<service>/docker-compose.yml`).

| Folder | Services | Web port | Notes |
|---|---|---|---|
| `media-stack` | aria2, AriaNg, Jellyfin | 6880 (AriaNg), 8096 (Jellyfin) | My first compose file; downloads land in a folder Jellyfin reads. Transcoding disabled |
| `immich` | Immich server, Valkey, Postgres | 2283 | Machine-learning container removed, memory limit set |
| `sftpgo` | SFTPGo | 8082 | Web file manager + SFTP on 2022 |
| `dockhand` | Dockhand | 3030 | Container management UI |
| `beszel` | Beszel hub + agent | 8090 | Monitoring |
| `dashy` | Dashy | 4000 | Dashboard. The link list lives in `/opt/dashy/conf.yml` (not published, it contains my domain) |
| `open-webui` | Open WebUI | 3000 | Frontend for the LLM server on my main PC |

## Usage

```bash
cd media-stack
cp .env.example .env     # fill in real values
nano .env
docker compose up -d
```

Real `.env` files are never committed (see `.gitignore`). Every IP, token, key and password in this repo is a placeholder.
