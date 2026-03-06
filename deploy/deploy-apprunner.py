#!/usr/bin/env python3
"""Deploy Ansible Galaxy Publisher to AWS App Runner.

This script automates the deployment of the Galaxy Publisher proxy to AWS App Runner.
It handles:
- Building and pushing container images to ECR
- Managing secrets in AWS Secrets Manager
- Creating/updating App Runner services
- Setting up required IAM roles

Usage:
    # Interactive mode
    ./deploy/deploy-apprunner.py --service-name my-galaxy-proxy

    # With all options
    ./deploy/deploy-apprunner.py \\
        --region us-east-1 \\
        --service-name galaxy-proxy-prod \\
        --config config/servers.yml \\
        --cpu 1024 \\
        --memory 2048
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import pathlib
import re
import subprocess
import sys
import time

try:
    import boto3  # type: ignore
    import yaml
except ImportError:
    print("Error: Required dependencies not installed.")
    print("Please install deployment dependencies:")
    print("  uv sync --group deploy")
    print("Or with pip:")
    print("  pip install boto3")
    sys.exit(1)


class DeploymentError(Exception):
    """Deployment failed."""


def detect_container_cli() -> str:
    """Detect available container CLI (podman or docker).

    Returns:
        Name of available CLI command

    Raises:
        DeploymentError: If neither podman nor docker is available
    """
    for cli in ["podman", "docker"]:
        try:
            subprocess.run(
                [cli, "--version"],
                check=True,
                capture_output=True,
            )
            print(f"✓ Using {cli} for container operations")
            return cli
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue

    raise DeploymentError(
        "Neither podman nor docker found. Please install one:\n"
        "  - Podman: https://podman.io/getting-started/installation\n"
        "  - Docker: https://docs.docker.com/get-docker/"
    )


def get_git_commit_sha() -> str:
    """Get current git commit SHA.

    Returns:
        Git commit SHA (short form)

    Raises:
        DeploymentError: If not in a git repository
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        sha = result.stdout.strip()
        print(f"✓ Building from commit: {sha}")
        return sha
    except subprocess.CalledProcessError:
        raise DeploymentError(
            "Not in a git repository or git not installed.\n"
            "This script requires git to tag container images."
        )


def extract_env_vars_from_config(config_path: pathlib.Path) -> set[str]:
    """Extract environment variable names from servers.yml.

    All token, client_id, and client_secret values in the config reference
    environment variables that need secrets created in AWS Secrets Manager.

    Args:
        config_path: Path to servers.yml configuration file

    Returns:
        Set of environment variable names that need secrets

    Raises:
        DeploymentError: If config file is invalid
    """
    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
    except Exception as e:
        raise DeploymentError(f"Failed to read config file: {e}")

    env_vars = set()

    for server_id, server_config in config.get("servers", {}).items():
        if not isinstance(server_config, dict):
            continue

        # Extract token field (references an env var)
        token = server_config.get("token")
        if token:
            # Strip ${} if present, otherwise use as-is
            env_vars.add(str(token).strip("${}"))

        # Extract oauth_secret fields (reference env vars)
        oauth = server_config.get("oauth_secret", {})
        if isinstance(oauth, dict):
            for field in ["client_id", "client_secret"]:
                value = oauth.get(field)
                if value:
                    # Strip ${} if present, otherwise use as-is
                    env_vars.add(str(value).strip("${}"))

    print(f"✓ Found {len(env_vars)} environment variables in config")
    return env_vars


def check_aws_credentials(region: str) -> tuple[boto3.Session, str]:
    """Verify AWS credentials are configured.

    Args:
        region: AWS region to use

    Returns:
        Tuple of (boto3_session, account_id)

    Raises:
        DeploymentError: If credentials are not configured
    """
    try:
        session = boto3.Session(region_name=region)
        sts = session.client("sts")
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        print(f"✓ AWS credentials configured for account: {account_id}")
        return session, account_id
    except Exception as e:
        raise DeploymentError(
            f"AWS credentials not configured: {e}\n"
            "Please configure AWS credentials:\n"
            "  aws configure\n"
            "Or set environment variables:\n"
            "  export AWS_ACCESS_KEY_ID=...\n"
            "  export AWS_SECRET_ACCESS_KEY=..."
        )


def get_or_create_secret(
    session: boto3.Session,
    secret_name: str,
    update_secrets: bool = False,
) -> str:
    """Get existing secret or create new one.

    Args:
        session: Boto3 session
        secret_name: Name of the secret
        update_secrets: Force update existing secrets

    Returns:
        Secret ARN

    Raises:
        DeploymentError: If secret operations fail
    """
    sm = session.client("secretsmanager")
    full_name = f"galaxy-publisher/{secret_name}"

    # Check if secret exists
    try:
        response = sm.describe_secret(SecretId=full_name)
        secret_arn = response["ARN"]

        if not update_secrets:
            use_existing = input(f"  Use existing secret '{secret_name}'? [Y/n]: ").strip().lower()
            if use_existing in ("", "y", "yes"):
                print(f"  ✓ Using existing secret: {secret_name}")
                return str(secret_arn)

        # Update existing secret
        value = getpass.getpass(f"  Enter new value for {secret_name}: ")
        if not value:
            raise DeploymentError(f"Secret value for {secret_name} cannot be empty")

        sm.put_secret_value(SecretId=full_name, SecretString=value)
        print(f"  ✓ Updated secret: {secret_name}")
        return str(secret_arn)

    except sm.exceptions.ResourceNotFoundException:
        # Create new secret
        value = getpass.getpass(f"  Enter value for {secret_name}: ")
        if not value:
            raise DeploymentError(f"Secret value for {secret_name} cannot be empty")

        response = sm.create_secret(
            Name=full_name,
            Description=f"Secret for Ansible Galaxy Publisher ({secret_name})",
            SecretString=value,
        )
        print(f"  ✓ Created secret: {secret_name}")
        return str(response["ARN"])


def build_container_image(
    container_cli: str,
    git_sha: str,
    ecr_uri: str,
) -> str:
    """Build container image.

    Args:
        container_cli: Container CLI to use (podman or docker)
        git_sha: Git commit SHA for tagging
        ecr_uri: ECR repository URI

    Returns:
        Full image identifier

    Raises:
        DeploymentError: If build fails
    """
    repo_root = pathlib.Path(__file__).parent.parent
    dockerfile = pathlib.Path(__file__).parent / "Dockerfile"

    image_name = f"galaxy-publisher:{git_sha}"
    full_image = f"{ecr_uri}:{git_sha}"

    print("\nBuilding container image...")
    print(f"  Tag: {git_sha}")

    try:
        # Generate frozen requirements.txt from uv.lock
        # This avoids using uv in the container (which crashes under emulation)
        requirements_file = pathlib.Path(__file__).parent / "requirements.txt"
        print("  Generating frozen requirements from uv.lock...")
        subprocess.run(
            ["uv", "export", "--no-dev", "--no-emit-project", "--frozen"],
            cwd=repo_root,
            stdout=open(requirements_file, "w"),
            check=True,
        )
        print(f"  ✓ Generated {requirements_file}")

        # Build image for linux/amd64 (AWS App Runner architecture)
        subprocess.run(
            [
                container_cli,
                "build",
                "--platform",
                "linux/amd64",
                "-f",
                str(dockerfile),
                "-t",
                image_name,
                str(repo_root),
            ],
            check=True,
        )

        # Tag for ECR
        subprocess.run(
            [container_cli, "tag", image_name, full_image],
            check=True,
        )

        print(f"✓ Container image built: {git_sha}")
        return full_image

    except subprocess.CalledProcessError as e:
        raise DeploymentError(f"Container build failed: {e}")

    finally:
        # Clean up generated requirements.txt
        if requirements_file.exists():
            requirements_file.unlink()


def test_container(
    container_cli: str,
    image: str,
    env_vars: set[str],
) -> None:
    """Test that container starts and health check passes.

    Args:
        container_cli: Container CLI to use (podman or docker)
        image: Full image identifier
        env_vars: Set of environment variable names to inject

    Raises:
        DeploymentError: If container test fails
    """
    test_container_name = "galaxy-publisher-deploy-test"

    print("\nTesting container locally...")
    print(f"  Image: {image}")

    try:
        # Stop and remove any existing test container
        subprocess.run(
            [container_cli, "rm", "-f", test_container_name],
            capture_output=True,
        )

        # Build env var arguments with dummy values
        env_args = []
        for env_var in env_vars:
            env_args.extend(["-e", f"{env_var}=test-{env_var.lower()}-value"])

        # Run container in detached mode
        subprocess.run(
            [
                container_cli,
                "run",
                "-d",
                "--name",
                test_container_name,
                "-p",
                "8001:8000",  # Use 8001 to avoid conflicts
            ]
            + env_args
            + [image],
            check=True,
            capture_output=True,
        )

        # Wait for health check (max 60 seconds)
        # Note: May be slower on ARM64 due to amd64 emulation
        print("  Waiting for health check (dependencies downloading on first run)...")
        max_wait = 60
        for i in range(max_wait):
            try:
                result = subprocess.run(
                    ["curl", "-sf", "http://127.0.0.1:8001/health"],
                    capture_output=True,
                    timeout=2,
                )
                if result.returncode == 0:
                    print(f"  ✓ Health check passed (after {i+1}s)")
                    break
            except subprocess.TimeoutExpired:
                pass

            if i == max_wait - 1:
                # Get logs for debugging
                logs_result = subprocess.run(
                    [container_cli, "logs", test_container_name],
                    capture_output=True,
                    text=True,
                )
                raise DeploymentError(
                    f"Container health check failed after {max_wait}s\n"
                    f"Container logs:\n{logs_result.stdout}\n{logs_result.stderr}"
                )

            # Show progress every 10 seconds
            if (i + 1) % 10 == 0:
                print(f"    Still waiting... ({i+1}s)")

            time.sleep(1)

        print("  ✓ Container test passed")

    except subprocess.CalledProcessError as e:
        raise DeploymentError(f"Container test failed: {e}")

    finally:
        # Clean up test container
        subprocess.run(
            [container_cli, "rm", "-f", test_container_name],
            capture_output=True,
        )


def get_or_create_ecr_repo(
    session: boto3.Session,
    repo_name: str,
) -> str:
    """Get existing ECR repository or create new one.

    Args:
        session: Boto3 session
        repo_name: ECR repository name

    Returns:
        ECR repository URI

    Raises:
        DeploymentError: If ECR operations fail
    """
    ecr = session.client("ecr")

    try:
        response = ecr.describe_repositories(repositoryNames=[repo_name])
        repo_uri = response["repositories"][0]["repositoryUri"]
        print(f"✓ Using existing ECR repository: {repo_uri}")
        return str(repo_uri)

    except ecr.exceptions.RepositoryNotFoundException:
        # Create repository
        response = ecr.create_repository(
            repositoryName=repo_name,
            imageScanningConfiguration={"scanOnPush": True},
            imageTagMutability="MUTABLE",
        )
        repo_uri = response["repository"]["repositoryUri"]
        print(f"✓ Created ECR repository: {repo_uri}")
        return str(repo_uri)


def push_to_ecr(
    session: boto3.Session,
    container_cli: str,
    image: str,
) -> None:
    """Push container image to ECR.

    Args:
        session: Boto3 session
        container_cli: Container CLI to use
        image: Full image identifier

    Raises:
        DeploymentError: If push fails
    """
    ecr = session.client("ecr")

    print("\nPushing image to ECR...")

    try:
        # Get ECR login credentials
        auth_response = ecr.get_authorization_token()
        auth_data = auth_response["authorizationData"][0]

        registry = auth_data["proxyEndpoint"]
        auth_token = str(auth_data["authorizationToken"])

        # Decode base64 token (format is "AWS:password")
        decoded = base64.b64decode(auth_token).decode()
        username, password = decoded.split(":", 1)

        # Login to ECR
        subprocess.run(
            [container_cli, "login", "-u", username, "-p", password, registry],
            check=True,
            capture_output=True,
        )

        # Push image
        subprocess.run(
            [container_cli, "push", image],
            check=True,
        )

        print("✓ Image pushed to ECR")

    except Exception as e:
        raise DeploymentError(f"Failed to push image to ECR: {e}")


def get_or_create_iam_role(
    session: boto3.Session,
    role_name: str,
    account_id: str,
) -> str:
    """Get existing IAM role or create new one for App Runner instance.

    Args:
        session: Boto3 session
        role_name: IAM role name
        account_id: AWS account ID

    Returns:
        Role ARN

    Raises:
        DeploymentError: If IAM operations fail
    """
    iam = session.client("iam")

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "tasks.apprunner.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    try:
        response = iam.get_role(RoleName=role_name)
        role_arn = response["Role"]["Arn"]
        print(f"✓ Using existing IAM role: {role_name}")
        return str(role_arn)

    except iam.exceptions.NoSuchEntityException:
        # Create role
        response = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="IAM role for Ansible Galaxy Publisher App Runner service",
        )
        role_arn = response["Role"]["Arn"]

        # Attach policy for Secrets Manager access
        policy_document = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["secretsmanager:GetSecretValue"],
                    "Resource": f"arn:aws:secretsmanager:*:{account_id}:secret:galaxy-publisher/*",
                }
            ],
        }

        iam.put_role_policy(
            RoleName=role_name,
            PolicyName="SecretsManagerAccess",
            PolicyDocument=json.dumps(policy_document),
        )

        # Wait for role to propagate
        print(f"✓ Created IAM role: {role_name}")
        print("  Waiting for IAM role to propagate...")
        time.sleep(10)

        return str(role_arn)


def get_or_create_ecr_access_role(
    session: boto3.Session,
    role_name: str,
) -> str:
    """Get existing IAM role or create new one for App Runner ECR access.

    Args:
        session: Boto3 session
        role_name: IAM role name

    Returns:
        Role ARN

    Raises:
        DeploymentError: If IAM operations fail
    """
    iam = session.client("iam")

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "build.apprunner.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    try:
        response = iam.get_role(RoleName=role_name)
        role_arn = response["Role"]["Arn"]
        print(f"✓ Using existing ECR access role: {role_name}")
        return str(role_arn)

    except iam.exceptions.NoSuchEntityException:
        # Create role
        response = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="IAM role for App Runner to access ECR",
        )
        role_arn = response["Role"]["Arn"]

        # Attach managed policy for ECR access
        iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess",
        )

        # Wait for role to propagate
        print(f"✓ Created ECR access role: {role_name}")
        print("  Waiting for IAM role to propagate...")
        time.sleep(10)

        return str(role_arn)


def create_or_update_apprunner_service(
    session: boto3.Session,
    service_name: str,
    image: str,
    secrets: dict[str, str],
    ecr_access_role_arn: str,
    instance_role_arn: str,
    cpu: int,
    memory: int,
) -> str:
    """Create new App Runner service or update existing one.

    Args:
        session: Boto3 session
        service_name: Service name
        image: Container image identifier
        secrets: Dict of env var names to secret ARNs
        instance_role_arn: IAM role ARN
        cpu: CPU units (256, 512, 1024, 2048, 4096)
        memory: Memory in MB (512, 1024, 2048, 3072, 4096, 6144, 8192, 10240, 12288)

    Returns:
        Service URL

    Raises:
        DeploymentError: If deployment fails
    """
    apprunner = session.client("apprunner")

    # Convert secrets dict to App Runner format
    runtime_secrets = {name: arn for name, arn in secrets.items()}

    service_config = {
        "ServiceName": service_name,
        "SourceConfiguration": {
            "AuthenticationConfiguration": {
                "AccessRoleArn": ecr_access_role_arn,
            },
            "ImageRepository": {
                "ImageIdentifier": image,
                "ImageRepositoryType": "ECR",
                "ImageConfiguration": {
                    "Port": "8000",
                    "RuntimeEnvironmentVariables": {
                        "CONFIG_PATH": "/app/config/servers.yml",
                    },
                    "RuntimeEnvironmentSecrets": runtime_secrets,
                },
            },
            "AutoDeploymentsEnabled": False,
        },
        "InstanceConfiguration": {
            "Cpu": str(cpu),
            "Memory": str(memory),
            "InstanceRoleArn": instance_role_arn,
        },
        "HealthCheckConfiguration": {
            "Protocol": "HTTP",
            "Path": "/health",
            "Interval": 20,
            "Timeout": 5,
            "HealthyThreshold": 1,
            "UnhealthyThreshold": 5,
        },
    }

    try:
        # Check if service exists
        list_response = apprunner.list_services()
        existing_service = None

        for service_summary in list_response.get("ServiceSummaryList", []):
            if service_summary["ServiceName"] == service_name:
                existing_service = service_summary["ServiceArn"]
                break

        if existing_service:
            print(f"\nUpdating existing App Runner service: {service_name}")

            # Update service
            update_config = {
                "ServiceArn": existing_service,
                "SourceConfiguration": service_config["SourceConfiguration"],
                "InstanceConfiguration": service_config["InstanceConfiguration"],
                "HealthCheckConfiguration": service_config["HealthCheckConfiguration"],
            }

            response = apprunner.update_service(**update_config)
            service_arn = response["Service"]["ServiceArn"]

        else:
            print(f"\nCreating new App Runner service: {service_name}")
            response = apprunner.create_service(**service_config)
            service_arn = response["Service"]["ServiceArn"]

        # Wait for service to be running
        print("  Waiting for deployment (this may take 5-10 minutes)...")

        while True:
            response = apprunner.describe_service(ServiceArn=service_arn)
            status = response["Service"]["Status"]

            if status == "RUNNING":
                service_url = response["Service"]["ServiceUrl"]
                print("\n✓ Service deployed successfully!")
                print(f"  URL: https://{service_url}")
                return f"https://{service_url}"

            elif status in ("OPERATION_IN_PROGRESS", "CREATE_FAILED", "UPDATE_FAILED"):
                if status.endswith("_FAILED"):
                    raise DeploymentError(f"Service deployment failed with status: {status}")

                # Still in progress
                time.sleep(15)

            else:
                raise DeploymentError(f"Unexpected service status: {status}")

    except Exception as e:
        raise DeploymentError(f"Failed to deploy App Runner service: {e}")


def validate_service_name(name: str) -> None:
    """Validate App Runner service name.

    Args:
        name: Service name to validate

    Raises:
        DeploymentError: If name is invalid
    """
    if not re.match(r"^[a-z][a-z0-9-]*$", name):
        raise DeploymentError(
            f"Invalid service name: {name}\n"
            "Service name must:\n"
            "  - Start with a lowercase letter\n"
            "  - Contain only lowercase letters, numbers, and hyphens\n"
            "  - Be between 4 and 40 characters"
        )

    if len(name) < 4 or len(name) > 40:
        raise DeploymentError(
            f"Invalid service name length: {len(name)}\n"
            "Service name must be between 4 and 40 characters"
        )


def main() -> None:
    """Main deployment workflow."""
    parser = argparse.ArgumentParser(
        description="Deploy Ansible Galaxy Publisher to AWS App Runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive deployment
  ./deploy/deploy-apprunner.py --service-name my-galaxy-proxy

  # Specify all options
  ./deploy/deploy-apprunner.py \\
      --region us-east-1 \\
      --service-name galaxy-proxy-prod \\
      --config config/servers.yml \\
      --cpu 1024 \\
      --memory 2048

  # Update existing deployment with new secrets
  ./deploy/deploy-apprunner.py \\
      --service-name galaxy-proxy-prod \\
      --update-secrets
        """,
    )

    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", "us-east-1"),
        help="AWS region (default: us-east-1 or AWS_REGION env var)",
    )

    parser.add_argument(
        "--service-name",
        required=True,
        help="App Runner service name (4-40 chars, lowercase, hyphens allowed)",
    )

    parser.add_argument(
        "--config",
        default="config/servers.yml",
        help="Path to servers.yml configuration file (default: config/servers.yml)",
    )

    parser.add_argument(
        "--cpu",
        type=int,
        default=1024,
        choices=[256, 512, 1024, 2048, 4096],
        help="CPU units (default: 1024 = 1 vCPU)",
    )

    parser.add_argument(
        "--memory",
        type=int,
        default=2048,
        choices=[512, 1024, 2048, 3072, 4096, 6144, 8192, 10240, 12288],
        help="Memory in MB (default: 2048 = 2GB)",
    )

    parser.add_argument(
        "--update-secrets",
        action="store_true",
        help="Force update all secrets (don't prompt to use existing)",
    )

    args = parser.parse_args()

    print("=" * 70)
    print("Ansible Galaxy Publisher - AWS App Runner Deployment")
    print("=" * 70)

    # Validate inputs
    validate_service_name(args.service_name)

    config_path = pathlib.Path(args.config)
    if not config_path.exists():
        raise DeploymentError(f"Configuration file not found: {config_path}")

    # Detect container CLI
    container_cli = detect_container_cli()

    # Get git commit SHA
    git_sha = get_git_commit_sha()

    # Check AWS credentials
    session, account_id = check_aws_credentials(args.region)

    # Extract environment variables from config
    print(f"\nAnalyzing configuration: {config_path}")
    env_vars = extract_env_vars_from_config(config_path)

    if not env_vars:
        print("  No environment variables found in config")
    else:
        print(f"  Environment variables: {', '.join(sorted(env_vars))}")

    # Get or create secrets
    print("\nConfiguring secrets in AWS Secrets Manager...")
    secrets = {}
    for env_var in sorted(env_vars):
        secret_arn = get_or_create_secret(session, env_var, args.update_secrets)
        secrets[env_var] = secret_arn

    # Get or create ECR repository
    print("\nSetting up ECR repository...")
    ecr_uri = get_or_create_ecr_repo(session, f"galaxy-publisher-{args.service_name}")

    # Build container image
    image = build_container_image(container_cli, git_sha, ecr_uri)

    # Test container locally before pushing
    test_container(container_cli, image, env_vars)

    # Push to ECR
    push_to_ecr(session, container_cli, image)

    # Get or create IAM roles
    print("\nSetting up IAM roles...")
    ecr_access_role_name = f"galaxy-publisher-{args.service_name}-ecr-access"
    ecr_access_role_arn = get_or_create_ecr_access_role(session, ecr_access_role_name)

    instance_role_name = f"galaxy-publisher-{args.service_name}-instance"
    instance_role_arn = get_or_create_iam_role(session, instance_role_name, account_id)

    # Deploy to App Runner
    service_url = create_or_update_apprunner_service(
        session,
        args.service_name,
        image,
        secrets,
        ecr_access_role_arn,
        instance_role_arn,
        args.cpu,
        args.memory,
    )

    print("\n" + "=" * 70)
    print("Deployment Complete!")
    print("=" * 70)
    print(f"\nService URL: {service_url}")
    print(f"Service Name: {args.service_name}")
    print(f"Region: {args.region}")
    print(f"Image: {git_sha}")
    print("\nYour Galaxy Publisher proxy is now running on AWS App Runner.")
    print("All traffic is automatically served over HTTPS.")
    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
