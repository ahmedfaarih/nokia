# Deploying to your own server

GitHub Actions (`.github/workflows/deploy.yml`) SSHes into your server on every
push to `main` and does: `git reset --hard origin/main`, reinstall
dependencies, restart the `reconciliation` systemd service. Nothing runs on
GitHub's side except the SSH connection - no self-hosted runner needed.

## One-time server setup

1. **Create a deploy user** (or reuse an existing one) with SSH key auth, and
   clone the repo somewhere on the server, e.g.:
   ```
   sudo mkdir -p /opt/recon_tool && sudo chown deploy:deploy /opt/recon_tool
   su - deploy
   git clone <your-repo-url> /opt/recon_tool
   cd /opt/recon_tool/v2
   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt
   ```
   (`v2/` is ReconRadar - the Streamlit-based reconciliation engine. The
   older `recon_tool/` FastAPI app is still in the repo but no longer
   deployed.)

2. **Install the systemd service.** Edit `deploy/reconciliation.service` if
   your path/user differ from `/opt/recon_tool` / `deploy`, then:
   ```
   sudo cp deploy/reconciliation.service /etc/systemd/system/reconciliation.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now reconciliation
   sudo systemctl status reconciliation   # should be active (running)
   ```
   It binds to `127.0.0.1:8501` (Streamlit's default port) - not exposed
   publicly, nginx proxies to it.

3. **Let the deploy user restart the service without a password.** The GitHub
   Actions script runs `sudo systemctl restart reconciliation`, so add a
   scoped sudoers rule (run `sudo visudo -f /etc/sudoers.d/reconciliation-deploy`):
   ```
   deploy ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart reconciliation, /usr/bin/systemctl status reconciliation
   ```
   (Replace `deploy` with your actual deploy username. This grants exactly
   those two commands, nothing broader.)

4. **Add the nginx location block** from `deploy/nginx-snippet.conf` into your
   existing `server { }` block for the domain you want to serve this from,
   then `sudo nginx -t && sudo systemctl reload nginx`. This matters more
   than it looks: Streamlit talks to the browser over a WebSocket, so the
   snippet's `Upgrade`/`Connection` headers and long `proxy_read_timeout`
   aren't optional extras - without them the page loads but never connects.

## GitHub repo secrets

Add these under **Settings → Secrets and variables → Actions** on the GitHub
repo:

| Secret            | Value                                                        |
|--------------------|---------------------------------------------------------------|
| `DEPLOY_HOST`      | Server IP or hostname                                         |
| `DEPLOY_USER`      | The deploy user (e.g. `deploy`)                                |
| `DEPLOY_SSH_KEY`   | Private key (PEM) for that user - generate a dedicated deploy key, don't reuse your personal one |
| `DEPLOY_PATH`      | Absolute path to the cloned repo root, e.g. `/opt/recon_tool`  |
| `DEPLOY_PORT`      | Optional, only if SSH isn't on port 22                         |

Generate a dedicated keypair for this rather than reusing a personal SSH key:
```
ssh-keygen -t ed25519 -f deploy_key -C "github-actions-deploy" -N ""
```
Add `deploy_key.pub` to the deploy user's `~/.ssh/authorized_keys` on the
server, and the contents of `deploy_key` (private half) as the
`DEPLOY_SSH_KEY` secret. Delete the local copies afterward.

## First deploy

Push to `main` (or run the workflow manually via Actions → Deploy →
"Run workflow"). Check the Actions log, and `sudo journalctl -u reconciliation -f`
on the server if something doesn't come up.

## AI comment drafting (optional)

`v2` can call an LLM (Claude, Gemini, Groq, OpenRouter, Mistral, or a local
Ollama model) to draft reconciliation comments. No server-side setup is
required - each user pastes their own API key into the app's "AI settings"
popover, kept only for their browser session. You can optionally pre-fill a
key for everyone by setting an environment variable before starting the
service (e.g. `Environment=ANTHROPIC_API_KEY=...` in the systemd unit), but
this means that key is shared by anyone who uses the deployed instance.
