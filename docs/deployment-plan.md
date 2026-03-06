# AWS App Runner Deployment Plan

## Overview

Create a deployment script that deploys the Ansible Galaxy Publisher proxy to AWS App Runner with minimal dependencies and maximum ease of use.

## Requirements

1. **Deploy current commit** - Deploy the exact code in the current working directory
2. **Minimal dependencies** - Avoid heavy tooling (just AWS CLI + Python/Bash)
3. **Interactive prompts** - Prompt for required information if not provided
4. **Argument support** - Allow all values to be passed via CLI arguments
5. **Secrets management** - Securely handle Galaxy tokens and OAuth credentials
6. **Configuration management** - Deploy with the user's servers.yml config

## AWS App Runner Basics

### Deployment Options

**Option 1: Source-based (Recommended)**
- ✅ App Runner builds the container automatically
- ✅ No Docker required locally
- ✅ Simpler workflow
- ❌ Requires GitHub connection (or ECR for private repos)

**Option 2: Container-based**
- ✅ Full control over container
- ✅ Works without GitHub
- ❌ Requires Docker locally
- ❌ Requires ECR setup
- ❌ More complex workflow

**Recommendation**: Start with container-based for maximum flexibility, since users may not want to connect GitHub.

### What App Runner Provides

- Automatic HTTPS endpoint
- Auto-scaling (CPU/memory/requests)
- Load balancing
- Health checks
- Rolling deployments
- IAM integration
- VPC connectivity (optional)

## Architecture

```
┌─────────────────────────────────────────────────────┐
│ Developer Machine                                    │
│                                                      │
│  1. Run: ./deploy/deploy-apprunner.py               │
│  2. Script collects:                                 │
│     - AWS credentials/region                        │
│     - Service name                                   │
│     - Configuration file path                        │
│     - Secrets (Galaxy tokens, OAuth)                │
│                                                      │
│  3. Script builds Docker image                       │
│  4. Script pushes to ECR                             │
│  5. Script creates/updates App Runner service        │
└─────────────────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────┐
│ AWS                                                  │
│                                                      │
│  ┌──────────────┐      ┌─────────────────┐         │
│  │     ECR      │─────▶│  App Runner     │         │
│  │ (Container)  │      │   Service       │         │
│  └──────────────┘      └─────────────────┘         │
│                               │                      │
│                               ▼                      │
│                        ┌─────────────────┐          │
│                        │ Secrets Manager │          │
│                        │  - Galaxy tokens │         │
│                        │  - OAuth creds   │         │
│                        └─────────────────┘          │
└─────────────────────────────────────────────────────┘
```

## Deployment Script Design

### Script Location
`deploy/deploy-apprunner.py` - Python script using boto3

### Dependencies
- **Required**: Python 3.12+, boto3, Docker
- **Optional**: AWS CLI (for profile management)

### Command Structure

```bash
# Interactive mode (prompts for everything)
./deploy/deploy-apprunner.py

# With arguments (no prompts)
./deploy/deploy-apprunner.py \
  --region us-east-1 \
  --service-name galaxy-publisher-prod \
  --config config/servers.yml \
  --galaxy-token-secret arn:aws:secretsmanager:... \
  --cpu 1024 \
  --memory 2048

# Update existing deployment
./deploy/deploy-apprunner.py --service-name galaxy-publisher-prod --update
```

### Required Information

| Parameter | Description | Source |
|-----------|-------------|--------|
| `--region` | AWS region | Prompt / Arg / AWS_REGION env |
| `--service-name` | App Runner service name | Prompt / Arg |
| `--config` | Path to servers.yml | Prompt / Arg (default: config/servers.yml) |
| `--audience` | OIDC audience from config | Extracted from config file |

### Secrets Handling

**Approach**: Use AWS Secrets Manager for sensitive values

1. **Galaxy Tokens** - Environment variables reference secrets:
   ```yaml
   servers:
     galaxy:
       token: "GALAXY_TOKEN"  # References env var
   ```

2. **Script creates/references secrets**:
   ```bash
   # Script prompts: "Galaxy token for 'galaxy' server: "
   # User enters token (masked)
   # Script creates: aws secretsmanager create-secret --name galaxy-publisher/GALAXY_TOKEN
   ```

3. **App Runner configuration**:
   ```json
   {
     "Secrets": {
       "GALAXY_TOKEN": "arn:aws:secretsmanager:us-east-1:123456789:secret:galaxy-publisher/GALAXY_TOKEN"
     }
   }
   ```

### Configuration File Handling

**Challenge**: servers.yml contains environment variable references, not actual secrets.

**Solution**:
1. Script reads servers.yml
2. Extracts all environment variable names (e.g., `GALAXY_TOKEN`, `AH_CLIENT_ID`)
3. Prompts for actual values
4. Creates/updates secrets in AWS Secrets Manager
5. Configures App Runner to inject secrets as environment variables

## Deployment Steps (Detailed)

### Step 1: Pre-flight Checks
- ✅ Verify AWS credentials are configured
- ✅ Verify Docker is running
- ✅ Verify config file exists and is valid
- ✅ Verify required AWS permissions

### Step 2: Parse Configuration
```python
config = load_servers_yml(config_path)
required_secrets = extract_secret_names(config)
# Example: ['GALAXY_TOKEN', 'AH_CLIENT_ID', 'AH_CLIENT_SECRET']
```

### Step 3: Collect Secrets
```python
secrets = {}
for secret_name in required_secrets:
    # Check if secret already exists in AWS
    existing = get_secret_if_exists(secret_name)

    if existing and not args.update_secrets:
        print(f"✓ Using existing secret: {secret_name}")
        secrets[secret_name] = existing  # ARN
    else:
        # Prompt for value (masked input)
        value = prompt_secret(f"Enter {secret_name}: ")
        secrets[secret_name] = create_or_update_secret(secret_name, value)
```

### Step 4: Build Container Image
```bash
# Create Dockerfile if it doesn't exist
# Build: docker build -t galaxy-publisher:latest .
# Tag: docker tag galaxy-publisher:latest {ecr_uri}:latest
```

### Step 5: Push to ECR
```python
# Create ECR repository if it doesn't exist
ecr_repo = create_ecr_repo_if_needed(service_name)

# Get ECR login credentials
ecr_login()

# Push image
docker_push(ecr_repo)
```

### Step 6: Create/Update App Runner Service
```python
apprunner.create_service(
    ServiceName=service_name,
    SourceConfiguration={
        'ImageRepository': {
            'ImageIdentifier': f'{ecr_repo}:latest',
            'ImageRepositoryType': 'ECR',
            'ImageConfiguration': {
                'Port': '8000',
                'RuntimeEnvironmentVariables': {
                    'CONFIG_PATH': '/app/config/servers.yml'
                },
                'RuntimeEnvironmentSecrets': secrets  # Secret ARNs
            }
        },
        'AutoDeploymentsEnabled': False  # Manual deploys only
    },
    InstanceConfiguration={
        'Cpu': '1 vCPU',
        'Memory': '2 GB',
        'InstanceRoleArn': instance_role_arn
    },
    HealthCheckConfiguration={
        'Path': '/health',
        'Protocol': 'HTTP'
    }
)
```

### Step 7: Wait for Deployment
```python
waiter = apprunner.get_waiter('service_running')
waiter.wait(ServiceArn=service_arn)
print(f"✓ Service deployed: {service_url}")
```

## IAM Permissions Required

### For Developer (running the script)
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "apprunner:CreateService",
        "apprunner:UpdateService",
        "apprunner:DeleteService",
        "apprunner:DescribeService",
        "ecr:CreateRepository",
        "ecr:GetAuthorizationToken",
        "ecr:PutImage",
        "ecr:BatchCheckLayerAvailability",
        "secretsmanager:CreateSecret",
        "secretsmanager:UpdateSecret",
        "secretsmanager:DescribeSecret",
        "iam:CreateRole",
        "iam:AttachRolePolicy",
        "iam:PassRole"
      ],
      "Resource": "*"
    }
  ]
}
```

### For App Runner Instance Role
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": "arn:aws:secretsmanager:*:*:secret:galaxy-publisher/*"
    }
  ]
}
```

## Dockerfile

**Location**: `Dockerfile` (root of repo)

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Copy application code
COPY pyproject.toml ./
COPY src/ ./src/

# Install uv and dependencies
RUN pip install uv && uv sync --no-dev

# Copy configuration template
# (actual secrets injected via env vars)
COPY config/servers.yml /app/config/servers.yml

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "galaxy_publisher.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## Configuration Strategy

### servers.yml in Container
The servers.yml file in the container should have environment variable placeholders:

```yaml
servers:
  galaxy:
    base_url: "https://galaxy.ansible.com"
    token: "${GALAXY_TOKEN}"  # Will be resolved from env var
```

**Problem**: Current config loading doesn't support `${VAR}` syntax.

**Solutions**:
1. **Keep current approach** - Use env var names directly (e.g., `token: GALAXY_TOKEN`)
2. **Add env var expansion** - Update config loader to expand `${VAR}` syntax
3. **Generate config at runtime** - Script generates servers.yml from template + secrets

**Recommendation**: Keep current approach (#1) - it already works.

## Script Workflow

```
┌─────────────────────────────────────────────────────┐
│ Start: ./deploy/deploy-apprunner.py                 │
└───────────────┬─────────────────────────────────────┘
                │
                ▼
         ┌──────────────┐
         │ Parse args   │
         └──────┬───────┘
                │
                ▼
    ┌──────────────────────┐
    │ Load servers.yml     │
    │ Extract secret names │
    └──────┬───────────────┘
           │
           ▼
    ┌─────────────────────┐
    │ Prompt for missing: │
    │ - AWS region        │
    │ - Service name      │
    │ - Secret values     │
    └──────┬──────────────┘
           │
           ▼
    ┌─────────────────────┐
    │ Create/update       │
    │ AWS Secrets         │
    └──────┬──────────────┘
           │
           ▼
    ┌─────────────────────┐
    │ Build Docker image  │
    └──────┬──────────────┘
           │
           ▼
    ┌─────────────────────┐
    │ Push to ECR         │
    └──────┬──────────────┘
           │
           ▼
    ┌─────────────────────┐
    │ Create IAM roles    │
    └──────┬──────────────┘
           │
           ▼
    ┌─────────────────────┐
    │ Create/update       │
    │ App Runner service  │
    └──────┬──────────────┘
           │
           ▼
    ┌─────────────────────┐
    │ Wait for deployment │
    └──────┬──────────────┘
           │
           ▼
    ┌─────────────────────┐
    │ Display service URL │
    └─────────────────────┘
```

## Alternative: Simpler Approach (Phase 1)

If the full solution is too complex, start simpler:

### Option A: CloudFormation Template
- Create a CFN template that sets up everything
- Script just runs `aws cloudformation deploy`
- Still need to handle secrets separately

### Option B: Manual Dockerfile + Instructions
- Provide Dockerfile
- Document manual steps
- Let users deploy however they want

### Option C: Docker Compose + AWS ECS
- Use docker-compose.yml
- Deploy to ECS instead of App Runner
- More control, more complexity

## Recommendations

### Phase 1: MVP (Minimal Viable Product)
1. Create Dockerfile
2. Create simple Python script that:
   - Builds image
   - Pushes to ECR
   - Creates App Runner service
   - Prompts for secrets interactively
3. Document manual setup steps
4. Test with one Galaxy server

### Phase 2: Enhanced
1. Add argument parsing
2. Add secret detection from config
3. Add update functionality
4. Add validation/pre-flight checks
5. Support multiple environments (dev/prod)

### Phase 3: Production-Ready
1. Add CloudFormation template option
2. Add Terraform module option
3. Add CI/CD integration
4. Add monitoring/alerting setup
5. Add backup/disaster recovery

## Dependencies Decision

**Minimal (Phase 1)**:
```
- Python 3.12+ (already required)
- boto3 (AWS SDK)
- PyYAML (already dependency)
- Docker (external)
```

**No additional Python dependencies** beyond what's already in the project.

## Open Questions

1. **VPC**: Should the service run in a VPC or public internet?
   - **Recommendation**: Public (simpler), add VPC support later if needed

2. **Auto-deploy**: Should we enable auto-deploy from ECR?
   - **Recommendation**: No, manual deploys only for control

3. **Multiple environments**: Support dev/staging/prod?
   - **Recommendation**: Use service name prefix (e.g., `dev-galaxy-publisher`)

4. **Custom domain**: Support custom domains?
   - **Recommendation**: Document manual setup, don't automate yet

5. **Logging**: CloudWatch Logs integration?
   - **Recommendation**: Yes, App Runner does this automatically

6. **Monitoring**: CloudWatch alarms?
   - **Recommendation**: Phase 2, not MVP

## Next Steps

1. **Decide on approach** - Container vs Source, MVP vs Full
2. **Create Dockerfile** - Containerize the application
3. **Create deployment script** - Python script for deployment
4. **Test deployment** - Deploy to test AWS account
5. **Document** - README section on deployment

## Estimated Complexity

- **Dockerfile**: 1 hour
- **Basic deployment script**: 4-6 hours
- **Secret management**: 2-3 hours
- **Testing**: 2-4 hours
- **Documentation**: 1-2 hours

**Total**: ~10-16 hours for MVP

## Cost Estimation (AWS)

- **App Runner**: ~$25-50/month (1 vCPU, 2GB, low traffic)
- **ECR**: ~$1/month (storage)
- **Secrets Manager**: ~$0.40/secret/month
- **Data transfer**: Minimal for API proxy

**Total**: ~$30-60/month for low-traffic deployment
