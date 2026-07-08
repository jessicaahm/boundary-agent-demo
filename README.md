# Boundary + AI agent troubleshooting demo

A deliberately breakable 3-tier app for demoing how an AI agent (Claude)
troubleshoots production through HashiCorp Boundary — without a VPN and
without ever holding long-lived credentials.

![Boundary AI agent troubleshooting demo](img/boundary-agent.gif)

## Architecture Diagram

Refer to this page: https://jessicaahm.github.io/boundary-agent-demo/

## Stack

    browser --> nginx (:8080) --> Flask app (:5000) --> Postgres (:5432)

- `GET /` returns products from the database
- `GET /health` reports whether the app can reach the DB
- `GET /nginx-health` proves nginx itself is alive

## Installation

### Prerequisites

On the demo VM / target host:

- Docker Engine 24+ with the Docker Compose plugin (`docker compose version`)
- `git` and `curl`
- Ports `8080` (nginx) free on the host; `5000` and `5432` are only used
  inside the compose network

On the operator / agent side:

- [Boundary CLI](https://developer.hashicorp.com/boundary/install) (`boundary version`)
- An SSH client (Boundary shells out to it for `boundary connect ssh`)

### Install the packages

On the demo VM (Ubuntu):

    # git and curl
    sudo apt-get update
    sudo apt-get install -y git curl

    # Docker Engine + Compose plugin (official convenience script)
    curl -fsSL https://get.docker.com | sudo sh

    # run docker without sudo (log out and back in afterwards)
    sudo usermod -aG docker "$USER"

    # verify
    docker --version
    docker compose version

On the operator / agent machine, install the Boundary CLI:

    # macOS
    brew install hashicorp/tap/boundary

    # Ubuntu (HashiCorp apt repo)
    curl -fsSL https://apt.releases.hashicorp.com/gpg | \
      sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] \
      https://apt.releases.hashicorp.com $(lsb_release -cs) main" | \
      sudo tee /etc/apt/sources.list.d/hashicorp.list
    sudo apt-get update && sudo apt-get install -y boundary

    # verify
    boundary version

### Setup

1. Clone the repo onto the target host:

       git clone <this-repo-url> boundary-agent-demo
       cd boundary-agent-demo

2. Make the chaos scripts executable:

       chmod +x chaos/*.sh

3. Build and start the stack:

       docker compose build

   This starts three containers: `demo-nginx` (host port 8080),
   `demo-app` (Flask, built from `app/`), and `demo-db` (Postgres 16,
   seeded from `db/init.sql`). The app waits for the DB healthcheck
   before starting.

4. Verify the install:

       curl http://localhost:8080/nginx-health   # nginx alive
       curl http://localhost:8080/health         # {"status": "ok"}
       curl http://localhost:8080/               # JSON products

To tear everything down (including the seeded database):

    docker compose down -v

## Quick start (on the demo VM / target host)

    docker compose up -d --build
    curl http://localhost:8080/          # should return JSON products
    curl http://localhost:8080/health    # {"status": "ok"}

## Break it (pick one per demo run)

    ./chaos/break-nginx.sh          # 502 — nginx upstream misconfig
    ./chaos/break-db-password.sh    # 500 — stale DB credentials (best for the Vault story)
    ./chaos/break-db-down.sh        # 503 — database down
    ./chaos/restore.sh              # fix everything

## Boundary wiring

1. Register this VM as an SSH target in Boundary (worker in the same VPC,
   dialing outbound to the controller — no inbound rules needed).
2. Optional (HCP/Enterprise): attach a Vault SSH credential library to the
   target and enable credential injection + session recording.
3. The agent's only path to this box is:
    `boundary authenticate password -auth-method-id $AUTH_METHOD `
   `boundary connect ssh -target-id ttcp_XXXX` (or the Boundary MCP/CLI
   wrapper if you let Claude establish the session itself).

## The agent runbook (what you ask Claude to do)

Prompt:

> The storefront at http://localhost:8080 is returning errors. You have an
> SSH session to the host via Boundary. Diagnose the root cause, fix it,
> and verify. Explain each command before running it.

Expected investigation flow (Claude typically follows this shape):

1. Reproduce:      curl -i http://localhost:8080/  and  /health
2. Triage layer:   502 => nginx problem; 500/503 => app or DB problem
3. nginx:          docker logs demo-nginx --tail 50 ; docker exec demo-nginx nginx -T
4. app:            docker logs demo-app --tail 50   (look for psycopg2 errors)
5. db:             docker ps ; docker exec demo-db pg_isready -U appuser
6. Fix:
   - 502:  correct upstream to app:5000, nginx -s reload
   - 500:  credentials stale -> reset password (or, in the Vault demo,
           request fresh dynamic creds and restart the app)
   - 503:  docker start demo-db
7. Verify:         curl /health then / ; confirm products JSON returns
8. Report:         root cause, fix applied, prevention suggestion

## Demo talking points

- The agent never received an SSH key or DB password in its context —
  Boundary injected credentials at the worker.
- Kill switch: terminate the agent's session live from the Boundary admin UI.
- Blast radius: the agent can only reach this one target, in this one scope,
  for the TTL of this one session.

```sh
curl http://localhost:8080/health  
boundary authenticate password -auth-method-id $AUTH_METHOD 
python sre_agent.py
boundary connect ssh -target-id $TARGET_ID

```