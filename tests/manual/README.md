# Manual Testing

This directory contains scripts for manual testing of the Galaxy Publisher proxy against real or mock Galaxy servers.

## Quick Start

### Basic Test

```bash
# Token will be prompted if not in environment
./tests/manual/run_test_environment.py myorg.mycollection

# Or provide token via environment variable
export TEST_GALAXY_TOKEN=your-galaxy-token-here
./tests/manual/run_test_environment.py myorg.mycollection

# Or provide token via command line
./tests/manual/run_test_environment.py --token your-token myorg.mycollection
```

## run_test_environment.py

Complete test environment setup script that creates:
- Mock Galaxy server with JWKS endpoint
- Proxy server with auto-generated configuration
- JWT token for authentication
- Ready-to-use ansible-galaxy command

### Usage

```bash
./tests/manual/run_test_environment.py [OPTIONS] ALLOWED_COLLECTIONS...
```

### Arguments

- `ALLOWED_COLLECTIONS` - One or more collection names (required)
  - Format: `namespace.name` (e.g., `myorg.mycollection`)
  - **Note:** Wildcards are not supported; use specific collection names

### Options

- `--audience TEXT` - OIDC audience value (default: `galaxy-test.oidc.publisher`)
- `--claim TEXT` - Value for test_oidc_claim (default: random string)
- `--server URL` - Galaxy server base URL (default: `https://galaxy.ansible.com`)
- `--token TEXT` - Galaxy server API token (default: `TEST_GALAXY_TOKEN` env var, or prompt)
- `--log-level LEVEL` - Logging level: INFO or DEBUG (default: INFO)

### Examples

**Test with default settings (token prompted):**
```bash
./tests/manual/run_test_environment.py myorg.mycollection
# Enter Galaxy server API token: ********
```

**Token from environment variable:**
```bash
export TEST_GALAXY_TOKEN=your-token
./tests/manual/run_test_environment.py myorg.mycollection
```

**Token on command line:**
```bash
./tests/manual/run_test_environment.py --token your-token myorg.mycollection
```

**Custom audience and claim:**
```bash
./tests/manual/run_test_environment.py \
  --audience myapp.example.com \
  --claim myrepo \
  --token your-token \
  myorg.mycollection
```

**Multiple collections with debug logging:**
```bash
./tests/manual/run_test_environment.py \
  --log-level DEBUG \
  --token your-token \
  myorg.collection1 \
  myorg.collection2
```

### How It Works

1. **Validates server and token** - Tests connection to Galaxy server with provided token
2. **Generates JWKS keypair** - Creates RSA keys for JWT signing
3. **Starts mock Galaxy server** - Provides JWKS endpoint and API (dynamic port)
4. **Creates servers.yml** - Configures OIDC issuer, server, and authorization rules
5. **Starts proxy server** - Runs on http://127.0.0.1 (dynamic port)
6. **Displays setup info** - Shows all URLs, files, and the exact ansible-galaxy command to run
7. **Waits for Ctrl+C** - Keeps environment running
8. **Cleans up** - Stops servers and removes temporary files

**Pre-flight Validation:**
Before starting any servers, the script validates your Galaxy server and token by making a test request to `/api/`. If authentication fails, you'll see the exact error response from the server.

**Command Output:**
The script displays a ready-to-use `ansible-galaxy collection publish` command with the correct server URL and JWT token already filled in - just copy and run it.

### Output

The script displays:
- Working directory path
- OIDC configuration details
- Server URLs (mock Galaxy, proxy, target)
- Allowed collection patterns
- Configuration file paths
- Usage examples

### Testing with ansible-galaxy

Once the environment is running, the script displays the exact command to run:

```bash
# Build your collection first
cd my-collection/
ansible-galaxy collection build

# The script will show something like (port is dynamically assigned):
📝 To publish a collection, run:

   ansible-galaxy collection publish \
     --server http://127.0.0.1:54321/api/v1/test_server \
     --token "$(cat /tmp/galaxy-publisher-test-abc123/token.jwt)" \
     <collection-tarball.tar.gz>

# Copy and run that command with your tarball:
ansible-galaxy collection publish \
  --server http://127.0.0.1:54321/api/v1/test_server \
  --token "$(cat /tmp/galaxy-publisher-test-abc123/token.jwt)" \
  myorg-mycollection-1.0.0.tar.gz
```

The token is read from a file using shell command substitution `$(cat ...)`, making the command more readable than showing the full JWT token.

### Stopping

Press `Ctrl+C` to:
- Stop the proxy server
- Stop the mock Galaxy server
- Remove all temporary files
- Clean up processes

### Environment Variables

- `TEST_GALAXY_TOKEN` - Token for Galaxy server (optional)
  - Used as default for `--token` if not provided on command line
  - If neither env var nor `--token` is set, you will be prompted
  - Can be set for convenience to avoid repeated prompting

### Temporary Files

All temporary files are created in a system temp directory and automatically cleaned up on exit:
- `token.jwt` - JWT token for authentication (also displayed in command output)
- `servers.yml` - Proxy configuration
- `test_rsa_key.pem` - RSA keypair for JWT signing

## Tips

- Start with mock Galaxy server to test basic functionality
- Use `--log-level DEBUG` to see detailed request/response logs
- Check collection namespace matches allowed patterns
- JWT token is valid for 1 hour by default
- Mock Galaxy server simulates publish and task status endpoints
- Proxy logs show all requests in real-time

## Troubleshooting

**Authentication failed (HTTP 401):**
- The script validates your token before starting
- If validation fails, you'll see the exact error response from Galaxy
- Common causes:
  - Invalid or expired token
  - Token doesn't have required permissions
  - Wrong server URL
- Solution: Generate a new token from Galaxy server settings

**Connection errors:**
- Cannot connect to server: Check server URL is correct and accessible
- Timeout: Check network connection and firewall settings
- The script tests the connection before starting any servers

**Collection rejected:**
- Check collection namespace matches allowed_collections exactly
- Wildcards are not supported in collection names
- Example: Use `myorg.mycollection` not `myorg.*`

**Server not starting:**
- Check logs with `--log-level DEBUG`
- Ensure uv environment is set up: `uv sync`
- Both servers use OS-assigned ports to avoid conflicts
