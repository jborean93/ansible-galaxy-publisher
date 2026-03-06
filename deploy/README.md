# AWS App Runner Deployment

This directory contains everything needed to deploy Ansible Galaxy Publisher to AWS App Runner.

## Quick Start

```bash
# Install dependencies
uv sync --group deploy

# Deploy
./deploy/deploy-apprunner.py --service-name my-galaxy-proxy
```

The script will interactively prompt for any required information.

## Prerequisites

### Required
- **Python 3.12+** - For running the deployment script
- **Podman or Docker** - For building container images
- **Git** - For tagging images with commit SHAs
- **AWS Account** - With appropriate permissions
- **AWS Credentials** - Configured locally

**Architecture Note**: The script automatically builds for `linux/amd64` (AWS App Runner's architecture).

- Dependencies are exported from `uv.lock` to `requirements.txt` (on host), then installed with `pip` (in container)
- This approach works reliably on ARM64/aarch64 by avoiding Rust binaries (`uv`) under emulation
- The container uses standard Python and pip, avoiding emulation issues

### AWS Permissions Required

The user/role running the deployment script needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "apprunner:CreateService",
        "apprunner:UpdateService",
        "apprunner:DescribeService",
        "apprunner:ListServices",
        "ecr:CreateRepository",
        "ecr:DescribeRepositories",
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
        "secretsmanager:CreateSecret",
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "iam:CreateRole",
        "iam:GetRole",
        "iam:AttachRolePolicy",
        "iam:PutRolePolicy",
        "iam:PassRole"
      ],
      "Resource": "*"
    }
  ]
}
```

## Configuration

### servers.yml Setup

First, create your local configuration from the example:

```bash
cp config/servers.example.yml config/servers.yml
```

Your `config/servers.yml` must reference environment variables for secrets:

```yaml
settings:
  audience: "https://your-proxy.example.com"

oidc_issuers:
  github:
    issuer_url: "https://token.actions.githubusercontent.com"
    jwks_url: "https://token.actions.githubusercontent.com/.well-known/jwks"

servers:
  galaxy:
    base_url: "https://galaxy.ansible.com"
    token: "GALAXY_TOKEN"  # ← Environment variable name

  automation_hub:
    base_url: "https://console.redhat.com/api/automation-hub/"
    oauth_secret:
      client_id: "AH_CLIENT_ID"      # ← Environment variable name
      client_secret: "AH_CLIENT_SECRET"  # ← Environment variable name
      auth_url: "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"

authorization_rules:
  - oidc_issuer: github
    claims:
      repository: "myorg/*"
      ref: "refs/tags/v*"
    servers:
      - galaxy
    allowed_collections:
      - "myorg.collection1"
      - "myorg.collection2"
```

### Environment Variables

All `token`, `client_id`, and `client_secret` values in your config are environment variable names. The deployment script will:
1. Extract all env var names from your config
2. Prompt for secret values (or use existing AWS secrets)
3. Store secrets in AWS Secrets Manager as `galaxy-publisher/{VAR_NAME}`
4. Configure App Runner to inject them as environment variables

You can use either plain names (`GALAXY_TOKEN`) or shell syntax (`${GALAXY_TOKEN}`) - both work.

## Usage

### Interactive Deployment

The simplest way to deploy:

```bash
./deploy/deploy-apprunner.py --service-name my-galaxy-proxy
```

The script will:
1. Detect podman/docker
2. Get current git commit SHA
3. Verify AWS credentials
4. Parse your config file
5. Prompt for secret values
6. Build container image
7. **Test container locally** (ensures it starts and health check passes)
8. Push to ECR (only if test passes)
9. Deploy to App Runner
10. Wait for deployment to complete
11. Display the service URL

### With Arguments

Specify all options upfront:

```bash
./deploy/deploy-apprunner.py \
  --region us-east-1 \
  --service-name galaxy-proxy-prod \
  --config config/servers.yml \
  --cpu 1024 \
  --memory 2048
```

### Update Existing Deployment

To update an existing deployment with a new commit:

```bash
./deploy/deploy-apprunner.py --service-name my-galaxy-proxy
```

The script will:
- Use existing secrets (no prompts)
- Build new container from current commit
- Update App Runner service
- Wait for deployment

To force update secrets:

```bash
./deploy/deploy-apprunner.py \
  --service-name my-galaxy-proxy \
  --update-secrets
```

## Secret Management

### How Secrets Work

1. **Config file references env vars**:
   ```yaml
   servers:
     galaxy:
       token: "GALAXY_TOKEN"
   ```

2. **Script detects env var names**:
   ```
   Found environment variables: GALAXY_TOKEN, AH_CLIENT_ID, AH_CLIENT_SECRET
   ```

3. **Script prompts for values** (masked input):
   ```
   Enter value for GALAXY_TOKEN: ********
   ✓ Created secret: galaxy-publisher/GALAXY_TOKEN
   ```

4. **Secrets stored in AWS Secrets Manager**:
   - Name: `galaxy-publisher/GALAXY_TOKEN`
   - Encrypted at rest
   - Access controlled by IAM

5. **App Runner injects secrets as env vars**:
   - Container sees: `GALAXY_TOKEN=actual-token-value`
   - Config loading works normally

### Managing Secrets

**View secrets**:
```bash
aws secretsmanager list-secrets \
  --query "SecretList[?starts_with(Name, 'galaxy-publisher/')].Name"
```

**Update a secret**:
```bash
aws secretsmanager put-secret-value \
  --secret-id galaxy-publisher/GALAXY_TOKEN \
  --secret-string "new-token-value"
```

Then redeploy:
```bash
./deploy/deploy-apprunner.py --service-name my-galaxy-proxy
```

**Delete a secret**:
```bash
aws secretsmanager delete-secret \
  --secret-id galaxy-publisher/GALAXY_TOKEN \
  --force-delete-without-recovery
```

## Resources Created

The deployment script creates/manages these AWS resources:

| Resource | Name Pattern | Description |
|----------|--------------|-------------|
| ECR Repository | `galaxy-publisher-{service-name}` | Container image storage |
| Secrets Manager | `galaxy-publisher/{VAR_NAME}` | Secret storage |
| IAM Role (ECR Access) | `galaxy-publisher-{service-name}-ecr-access` | Allows App Runner to pull from ECR |
| IAM Role (Instance) | `galaxy-publisher-{service-name}-instance` | Allows App Runner to access Secrets Manager |
| App Runner Service | `{service-name}` | The running application |

## Service Configuration

### CPU and Memory

Available configurations:

| CPU | Memory Options (MB) |
|-----|---------------------|
| 256 (0.25 vCPU) | 512, 1024 |
| 512 (0.5 vCPU) | 1024, 2048 |
| 1024 (1 vCPU) | 2048, 3072, 4096 |
| 2048 (2 vCPU) | 4096, 6144, 8192 |
| 4096 (4 vCPU) | 10240, 12288 |

**Default**: 1024 CPU (1 vCPU) + 2048 MB (2 GB)

**Recommendation**: Start with defaults, monitor CloudWatch metrics, adjust if needed.

### Auto-Scaling

App Runner automatically scales based on:
- Concurrent requests
- CPU utilization
- Memory utilization

**Scaling**:
- Minimum instances: 1
- Maximum instances: 25 (default)
- Scale-up: Automatic when thresholds exceeded
- Scale-down: Automatic after idle period

## HTTPS and Domains

### Default HTTPS

App Runner automatically provides:
- ✅ HTTPS endpoint: `https://abc123.us-east-1.awsapprunner.com`
- ✅ SSL/TLS certificate (AWS-managed)
- ✅ Automatic certificate renewal
- ✅ HTTP → HTTPS redirect

**No configuration needed!**

### Custom Domain (Optional)

To use a custom domain:

1. **Add custom domain in App Runner console**:
   ```bash
   aws apprunner associate-custom-domain \
     --service-arn arn:aws:apprunner:... \
     --domain-name galaxy-proxy.example.com
   ```

2. **Add DNS records** (App Runner will provide):
   - CNAME or A record
   - Certificate validation records

3. **App Runner provisions certificate** (automatic)

## Monitoring and Logs

### CloudWatch Logs

App Runner automatically sends logs to CloudWatch:

**View logs**:
```bash
aws logs tail /aws/apprunner/{service-name}/{service-id}/application --follow
```

**In AWS Console**:
1. Go to CloudWatch → Log groups
2. Find `/aws/apprunner/{service-name}/{service-id}/application`
3. View real-time logs

### CloudWatch Metrics

Available metrics:
- `ActiveInstances` - Number of running instances
- `CPUUtilization` - CPU usage percentage
- `MemoryUtilization` - Memory usage percentage
- `RequestCount` - Total requests
- `Http2xxStatusCount` - Successful requests
- `Http4xxStatusCount` - Client errors
- `Http5xxStatusCount` - Server errors

**View metrics**:
```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/AppRunner \
  --metric-name CPUUtilization \
  --dimensions Name=ServiceName,Value={service-name}
```

### Health Checks

Configured automatically:
- **Endpoint**: `/health`
- **Protocol**: HTTP
- **Interval**: 20 seconds
- **Timeout**: 5 seconds
- **Healthy threshold**: 1 check
- **Unhealthy threshold**: 5 checks

## Costs

### Estimated Monthly Cost

**Low traffic** (1 instance, minimal requests):
- App Runner: ~$25-30/month (1 vCPU, 2GB, mostly idle)
- ECR: ~$1/month (image storage)
- Secrets Manager: ~$1-2/month (2-5 secrets)
- CloudWatch Logs: ~$1/month (minimal logs)

**Total**: ~$30-35/month

**Medium traffic** (2-3 instances average):
- App Runner: ~$75-100/month
- Other services: ~$3/month

**Total**: ~$80-105/month

### Cost Optimization

- **Use default CPU/memory** - Start small, scale as needed
- **Monitor metrics** - Identify if you're over-provisioned
- **Archive old logs** - Set CloudWatch log retention to 7-30 days
- **Delete old images** - Keep only recent ECR images

## Troubleshooting

### Deployment fails with "service already exists"

The script handles updates automatically. If you see this error, it means the service name is taken by another deployment in your account.

**Solution**: Use a different service name or delete the existing service:

```bash
aws apprunner delete-service --service-arn <service-arn>
```

### Container build fails

**Check**:
- Podman/Docker is installed and running
- You're in the git repository root
- `config/servers.yml` exists

**Debug**:
```bash
cd /path/to/repo
podman build --platform linux/amd64 -f deploy/Dockerfile .
```

### Service deployment times out

App Runner deployments typically take 5-10 minutes. If it exceeds 15 minutes:

**Check**:
1. CloudWatch logs for errors
2. Health check endpoint is responding
3. Container is listening on port 8000

**Force new deployment**:
```bash
aws apprunner start-deployment --service-arn <service-arn>
```

### Secrets not found

If the service can't find secrets:

**Check**:
1. Secrets exist: `aws secretsmanager list-secrets`
2. IAM role has `secretsmanager:GetSecretValue` permission
3. Secret names match env var names in config

### Health checks failing

If App Runner marks instance as unhealthy:

**Check CloudWatch logs**:
```bash
aws logs tail /aws/apprunner/{service}/application --follow
```

**Common issues**:
- Config file missing or invalid
- Required secrets not set
- Application startup error

## Cleanup

To delete all resources:

```bash
# Delete App Runner service
aws apprunner delete-service --service-arn <service-arn>

# Delete ECR repository
aws ecr delete-repository --repository-name galaxy-publisher-{service-name} --force

# Delete secrets
aws secretsmanager delete-secret --secret-id galaxy-publisher/GALAXY_TOKEN --force-delete-without-recovery
aws secretsmanager delete-secret --secret-id galaxy-publisher/AH_CLIENT_ID --force-delete-without-recovery
# ... repeat for all secrets

# Delete IAM roles
# ECR access role
aws iam detach-role-policy --role-name galaxy-publisher-{service-name}-ecr-access --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
aws iam delete-role --role-name galaxy-publisher-{service-name}-ecr-access

# Instance role
aws iam delete-role-policy --role-name galaxy-publisher-{service-name}-instance --policy-name SecretsManagerAccess
aws iam delete-role --role-name galaxy-publisher-{service-name}-instance
```

## Advanced

### Multiple Environments

Deploy separate instances for dev/staging/prod:

```bash
# Development
./deploy/deploy-apprunner.py --service-name galaxy-proxy-dev --cpu 512 --memory 1024

# Staging
./deploy/deploy-apprunner.py --service-name galaxy-proxy-staging

# Production
./deploy/deploy-apprunner.py --service-name galaxy-proxy-prod --cpu 2048 --memory 4096
```

Each gets its own:
- Service URL
- Secrets namespace
- ECR repository
- IAM role

### CI/CD Integration

Run from GitHub Actions:

```yaml
name: Deploy to AWS

on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: us-east-1

      - name: Install dependencies
        run: |
          pip install uv
          uv sync --group deploy

      - name: Deploy to App Runner
        run: |
          ./deploy/deploy-apprunner.py \
            --service-name galaxy-proxy-prod \
            --config config/servers-prod.yml
```

**Note**: Secrets must already exist in AWS Secrets Manager. The script won't prompt in CI.

## Support

For issues or questions:
- GitHub Issues: https://github.com/jborean93/ansible-galaxy-publisher/issues
- Documentation: See main README.md
