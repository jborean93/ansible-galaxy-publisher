# Ansible Galaxy Publisher

[![CI](https://github.com/jborean93/ansible-galaxy-publisher/actions/workflows/ci.yml/badge.svg)](https://github.com/jborean93/ansible-galaxy-publisher/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/jborean93/ansible-galaxy-publisher/graph/badge.svg?token=ohl8Kwp7rP)](https://codecov.io/gh/jborean93/ansible-galaxy-publisher)

**Transparent authenticated proxy for Ansible Galaxy/Automation Hub APIs**

## Overview

This project implements a secure proxy server that validates GitHub OIDC tokens and forwards authenticated requests to Ansible Galaxy or Automation Hub. It solves the problem of distributing sensitive Galaxy API tokens by keeping them server-side while allowing GitHub Actions workflows to publish collections using OIDC tokens.

## Key Features

- **OIDC Authentication**: Validates GitHub OIDC tokens with JWKS signature verification
- **Authorization Rules**: Flexible claim-based authorization with wildcard support
- **Collection Validation**: Ensures users can only publish authorized collections
- **Multi-Server Support**: Proxy to multiple Galaxy/Automation Hub servers
- **Token & OAuth**: Supports both token-based and OAuth authentication for backend servers
- **In-Memory Caching**: JWKS and OAuth tokens are cached to reduce latency
- **Version Agnostic**: Catch-all GET proxy works with any Galaxy-NG version's URL format

## Architecture

```
Client (ansible-galaxy CLI)
        ↓
    [OIDC Token]
        ↓
Proxy Server (FastAPI)
  1. Validate JWT signature (JWKS)
  2. Match authorization rules
  3. Validate collection namespace (POST publish only)
  4. Get server token (token or OAuth)
  5. Proxy request to Galaxy
        ↓
Galaxy/Automation Hub
```

**Request Handling:**
- **GET requests**: All GET requests are proxied after authentication/authorization
- **POST requests**: Only collection publish endpoint is handled, with namespace validation

## Installation

```bash
# Install with uv
uv sync

# Or with pip
pip install -e .
```

## Configuration

Create a `config/servers.yml` file:

```yaml
settings:
  audience: "https://galaxy-publisher.example.com"

oidc_issuers:
  github:
    issuer_url: "https://token.actions.githubusercontent.com"
    jwks_url: "https://token.actions.githubusercontent.com/.well-known/jwks"

servers:
  galaxy_ng:
    base_url: "https://galaxy.ansible.com"
    token: "GALAXY_NG_TOKEN"

  automation_hub:
    base_url: "https://console.redhat.com/api/automation-hub/"
    oauth_secret:
      auth_url: "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
      client_id: "AH_CLIENT_ID"
      client_secret: "AH_CLIENT_SECRET"

authorization_rules:
  - oidc_issuer: github
    claims:
      repository: "myorg/*"
      ref: "refs/tags/v*"
    servers:
      - galaxy_ng
    allowed_collections:
      - "myorg.collection1"
      - "myorg.collection2"
```

**Configuration Notes**:
- `settings.audience`: Must match the audience used in OIDC token exchange
- `oidc_issuers`: Configure trusted OIDC providers (GitHub, GitLab, etc.)
- `authorization_rules.claims`: Use wildcards (`*`) for flexible matching
- `allowed_collections`: **Exact collection names only** (e.g., `myorg.collection1`). Wildcards are NOT supported.

## Usage

### Start the Server

```bash
export CONFIG_PATH=config/servers.yml
export GALAXY_NG_TOKEN=your-galaxy-token

uv run python -m uvicorn galaxy_publisher.main:app --host 0.0.0.0 --port 8000
```

### Use with ansible-galaxy CLI

In your GitHub Actions workflow:

```yaml
name: Publish Collection

on:
  push:
    tags:
      - 'v*'

jobs:
  publish:
    runs-on: ubuntu-latest

    # Required: Enable OIDC token access
    permissions:
      id-token: write
      contents: read

    steps:
      - uses: actions/checkout@v4

      - name: Build collection
        run: ansible-galaxy collection build

      - name: Get OIDC token
        id: oidc
        run: |
          # Exchange GitHub's OIDC token with correct audience
          OIDC_TOKEN=$(curl -sS -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
            "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=https://galaxy-publisher.example.com" | jq -r '.value')
          echo "::add-mask::$OIDC_TOKEN"
          echo "token=$OIDC_TOKEN" >> $GITHUB_OUTPUT

      - name: Publish to Galaxy
        run: |
          ansible-galaxy collection publish *.tar.gz \
            --server http://your-proxy:8000/api/v1/galaxy_ng/ \
            --token ${{ steps.oidc.outputs.token }}
```

**Important**:
- Set `permissions.id-token: write` to enable OIDC token generation
- Exchange the token with the correct `audience` matching your proxy configuration
- The `ACTIONS_ID_TOKEN_REQUEST_TOKEN` is not used directly by ansible-galaxy

### How OIDC Token Exchange Works

1. **GitHub Actions** generates an OIDC token request token (`ACTIONS_ID_TOKEN_REQUEST_TOKEN`)
2. **Workflow** exchanges this for a JWT with your custom audience:
   ```bash
   curl -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
     "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=https://galaxy-publisher.example.com"
   ```
3. **Response** contains a JWT with claims like `repository`, `ref`, `workflow`, etc.
4. **Proxy** validates the JWT signature using GitHub's JWKS and checks authorization rules
5. **Proxy** forwards the request to Galaxy with the server's API token

**Why the audience must match**:
- The proxy's `settings.audience` must match the audience in the token exchange
- This prevents tokens issued for other services from being used with your proxy

## API Endpoints

- `GET /health` - Health check (no auth required)
- `GET /api/v1/{server_id}/{path:path}` - **Proxy all GET requests** (catch-all route)
  - Proxies any GET request to the Galaxy server after authentication/authorization
  - Examples: `/api/`, `/api/v3/imports/collections/{task_id}/`, `/api/v3/tasks/{task_id}/`
  - Works with any Galaxy-NG version's URL format
- `POST /api/v1/{server_id}/api/v3/artifacts/collections/` - Publish collection
  - Validates collection namespace against allowed collections before proxying
  - **Automatically rewrites task URLs** in the response to include the proxy prefix
  - This fixes `urljoin` behavior when Galaxy returns task URLs starting with `/`
  - Example: Galaxy returns `{"task": "/api/v3/imports/collections/abc/"}`, proxy returns `{"task": "/api/v1/{server_id}/api/v3/imports/collections/abc/"}`

It should be noted that while publishing is restricted to only the collections allowed for the authorization claim, the `GET` requests will be proxied regardless of the URL path requested. This means a client with a valid token will be able to perform other operations like listing collections. This is considered an acceptable risk as the client still needs to hold a pre-authorized JWTS token and publishing is the critical component that should be requested and not read operations.

## Development

### Run Tests

```bash
# All tests
uv run pytest

# With coverage
uv run pytest --cov=galaxy_publisher --cov-report=html

# Specific test file
uv run pytest tests/test_auth.py -v
```

### Linting and Type Checking

```bash
# Format code
uv run ruff format .

# Check formatting (CI)
uv run ruff format --check .

# Lint code
uv run ruff check .

# Type check
uv run mypy .
```

### Continuous Integration

The project uses GitHub Actions for CI, which runs on every push and pull request:

- **Python versions**: 3.12, 3.13
- **Checks**:
  - Ruff format check (`ruff format --check .`)
  - Ruff linting (`ruff check .`)
  - Mypy type checking (`mypy .`)
  - Pytest with coverage (`pytest --cov=galaxy_publisher`)
- **Coverage**: Uploaded to Codecov (Python 3.12 only)

To run the same checks locally:

```bash
# Run all CI checks
uv run ruff format --check . && \
uv run ruff check . && \
uv run mypy . && \
uv run pytest --cov=galaxy_publisher --cov-report=term
```

## License

MIT License

## Contributing

Contributions welcome! Please:
1. Run tests: `uv run pytest`
2. Format code: `uv run ruff format .`
3. Type check: `uv run mypy .`
