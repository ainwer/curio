# Self-hosting Curio

## Overview

Curio runs as three containers defined in `docker-compose.yml`:

| Service | Role |
| --- | --- |
| `tailscale` | Tailscale sidecar. Joins the host to your tailnet, terminates HTTPS on port 443 and proxies requests to the app on `127.0.0.1:8000`. |
| `app` | FastAPI application: generates questions, runs tutor sessions, writes cards to the vault and pushes them to Git. Shares the sidecar's network namespace, so it publishes no ports. |
| `converter` | Docling service that converts uploaded PDFs into Markdown. Uses the NVIDIA GPU and is reachable only from the app on the internal network. |

Persistent data:

- Vault: a dedicated clone of your vault repository, bind-mounted from `VAULT_PATH`.
- App data: the SQLite database (`app.db`), drafts and converted books, bind-mounted
  from `DATA_PATH`. Converted books are stored under `DATA_PATH/books/<book-id>/` and
  never enter a Git repository.
- Converter model cache: the `converter-cache` Docker volume.
- Tailscale node state: `./tailscale/state` in the repository checkout.

The app is not exposed on the host. It is reachable only from devices in your tailnet,
over HTTPS at `https://<TS_HOSTNAME>.<tailnet>.ts.net`.

## Requirements

- A Linux host with Docker Engine and Docker Compose v2.
- An NVIDIA GPU with the proprietary driver and the
  [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
  Verify GPU access with:

  ```sh
  docker run --rm --gpus all ubuntu nvidia-smi
  ```

- A Tailscale account with MagicDNS and HTTPS certificates enabled
  (admin console → DNS).
- An Obsidian vault kept in a private GitHub repository.
- An Anthropic API key, or credentials for another supported provider (see
  [Configuration](#configuration)).

## Install

1. Clone the Curio repository and enter it:

   ```sh
   sudo git clone https://github.com/<your-github-user>/curio.git /opt/curio
   cd /opt/curio
   ```

2. Create a deploy key for the vault repository:

   ```sh
   sudo mkdir -p /opt/curio-secrets
   sudo ssh-keygen -t ed25519 -N "" -C curio-vault -f /opt/curio-secrets/vault_deploy_key
   ```

   Add the contents of `/opt/curio-secrets/vault_deploy_key.pub` as a deploy key
   **with write access** on the vault repository (GitHub → repository → Settings →
   Deploy keys). Then set ownership and permissions:

   ```sh
   sudo chown 1000:1000 /opt/curio-secrets/vault_deploy_key
   sudo chmod 600 /opt/curio-secrets/vault_deploy_key
   ```

3. Clone the vault into a dedicated directory. Curio rebases before every write and
   refuses to work on a dirty tree, so it needs a clone that nothing else touches. Do
   not point it at the folder Obsidian syncs:

   ```sh
   sudo GIT_SSH_COMMAND="ssh -i /opt/curio-secrets/vault_deploy_key -o IdentitiesOnly=yes" \
     git clone git@github.com:<your-github-user>/<your-vault-repo>.git /opt/curio-vault
   sudo chown -R 1000:1000 /opt/curio-vault
   ```

4. Create the data directory:

   ```sh
   sudo mkdir -p /opt/curio-data/books
   sudo chown -R 1000:1000 /opt/curio-data
   ```

5. Generate a Tailscale auth key: admin console → Settings → Keys. Use a one-off key,
   not an ephemeral one; a tag is optional. The key is only needed for the first start.

6. Create the environment file and fill it in:

   ```sh
   cp .env.example .env
   chmod 600 .env
   ```

   Set at least `TS_AUTHKEY`, `TS_HOSTNAME`, `VAULT_PATH=/opt/curio-vault`,
   `DATA_PATH=/opt/curio-data`,
   `VAULT_DEPLOY_KEY_PATH=/opt/curio-secrets/vault_deploy_key`, and
   `ANTHROPIC_API_KEY`. Adjust `VAULT_QUESTIONS_DIR` and `VAULT_PROFILE_PATH` to match
   the folders in your vault.

7. Start the stack:

   ```sh
   docker compose up -d --build
   docker compose ps
   docker compose logs -f app
   ```

   The first book conversion downloads the Docling models into the `converter-cache`
   volume, which takes a few minutes.

8. From a device in the tailnet, open `https://<TS_HOSTNAME>.<tailnet>.ts.net`.
   On iPhone, use Share → Add to Home Screen to install the web app.

9. Remove `TS_AUTHKEY` from `.env` after the first successful start. The node identity
   is stored in `./tailscale/state` and is reused on later starts.

## Knowledge profile

Curio reads the file at `VAULT_PROFILE_PATH` to ground bridge questions and question
generation. Describe what you already know and what you want to learn. Useful
material:

- fields and topics you know well, including the level of depth;
- systems, tools or projects you work with;
- topics you are currently studying and questions you want answered.

Keep it short and factual. For example:

```markdown
# Knowledge profile

## Strong
- Power electronics: switching converters, magnetics, control loops.
- Embedded C on ARM Cortex-M; basic DSP.

## Working knowledge
- Linux administration, Docker, Git.
- Linear algebra and probability.

## Learning
- Analog filter design, especially active filters.
- Bayesian statistics for experiment design.
```

## Configuration

All settings live in `.env`, which is passed to the containers. Host paths are mounted
into the containers and overridden there with fixed paths (`/vault`, `/data`); set the
host paths in `.env`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TS_AUTHKEY` | empty | One-time Tailscale auth key. Only needed for the first start; afterwards remove it. |
| `TS_HOSTNAME` | `curio` | Tailscale node name, also the host part of the HTTPS URL. |
| `VAULT_PATH` | `/path/to/dedicated/vault-clone` | Host path of the dedicated vault clone. |
| `DATA_PATH` | `/path/to/curio-data` | Host path for the SQLite database and converted books. Must be outside any Git repository. |
| `VAULT_DEPLOY_KEY_PATH` | `/path/to/vault_deploy_key` | Host path of the SSH deploy key with write access to the vault repository. |
| `VAULT_KNOWN_HOSTS_FILE` | empty | Optional SSH known_hosts file for vault pushes. Empty uses the file packaged into the image. |
| `VAULT_QUESTIONS_DIR` | `Learning/Questions` | Directory inside the vault where question files are written. |
| `VAULT_PROFILE_PATH` | `Learning/Knowledge-Profile.md` | Path inside the vault of the knowledge profile. |
| `VAULT_GIT_BRANCH` | `main` | Branch Curio commits and pushes to. |
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `openai_compatible` or `fake`. |
| `LLM_MODEL` | `claude-sonnet-5-5` | Model used for generation and wrap-up. |
| `LLM_TUTOR_MODEL` | empty | Optional model override for the tutor chat. Empty uses `LLM_MODEL`. |
| `LLM_EFFORT_GENERATION` | `medium` | Anthropic only: `low`, `medium`, `high`, `xhigh` or `max`. |
| `LLM_EFFORT_TUTOR` | `low` | Anthropic only: effort for tutor turns. |
| `LLM_FALLBACK` | `default` | Anthropic only: server-side refusal fallback, `default` or `off`. |
| `ANTHROPIC_API_KEY` | empty | Anthropic API key. Required when `LLM_PROVIDER=anthropic`. |
| `LLM_BASE_URL` | empty | `openai_compatible` only: base URL of the API endpoint. |
| `LLM_API_KEY` | empty | `openai_compatible` only: API key for that endpoint. |
| `CONVERTER_URL` | `http://converter:8001` | Internal address of the converter service. Keep the default on Compose. |
| `MAX_UPLOAD_MB` | `200` | Maximum size of an uploaded PDF in megabytes. |
| `LOG_LEVEL` | `INFO` | Application log level. |

To use an OpenAI-compatible provider, set:

```sh
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.example.com/v1
LLM_API_KEY=<your-api-key>
LLM_MODEL=<model-name>
```

Then restart the app with `docker compose up -d`.

## Updating

Pull the new version and rebuild:

```sh
cd /opt/curio
git pull
docker compose up -d --build
```

Your data, vault and Tailscale state are kept in their mounted directories and are not
affected by a rebuild.

## Backups

- The vault is backed up by its Git history. Push regularly, or use your existing
  GitHub backup routine.
- `DATA_PATH` holds the SQLite database with chat history and drafts, plus the
  converted books. Back it up while the app is stopped, or take a consistent copy of
  the database:

  ```sh
  sudo docker compose stop app
  sqlite3 /opt/curio-data/app.db ".backup '/opt/backups/app.db'"
  sudo docker compose start app
  ```

  Copy `/opt/curio-data/books` as well if you want to keep the converted books.
- Keep a copy of `.env` and the vault deploy key in a secure location. Without the
  deploy key, the app cannot push to the vault.

## Troubleshooting

**The inbox shows "vault has uncommitted changes".**
Something modified the dedicated vault clone. Inspect it with:

```sh
git -C /opt/curio-vault status
```

Resolve the changes manually (commit or discard them), then press Retry in the inbox
banner.

**Push rejected or a conflict is reported.**
The remote changed while Curio was writing. Resolve the conflict in the vault
repository:

```sh
git -C /opt/curio-vault status
```

Finish or abort the rebase, then press Retry. Curio does not auto-resolve conflicts; the
rendered file is kept in the database until the write succeeds.

**A conversion stays in "converting".**
Check the converter log:

```sh
docker compose logs converter
```

Verify that the GPU is visible to Docker with `docker run --rm --gpus all ubuntu
nvidia-smi`, and that the NVIDIA Container Toolkit is installed.

**The app is not reachable.**
Check the sidecar:

```sh
docker compose logs tailscale
```

Confirm that MagicDNS and HTTPS certificates are enabled in the Tailscale admin console,
that `TS_HOSTNAME` matches the URL you open, and that the device you use from is in the
tailnet.

## GitHub repository settings for forks

Enable Secret scanning and Push protection (Settings → Code security).
