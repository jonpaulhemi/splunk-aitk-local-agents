"""AWS credentials for a Bedrock model or knowledge base connection.

One interface for every caller: given a connection's region, key pair and
role ARN, return the keyword arguments a boto3 client or ChatBedrockConverse
takes (aws_access_key_id, aws_secret_access_key, aws_session_token).

- With a key pair: the pair, or, when a role ARN is set, temporary
  credentials for that role assumed with the pair, as AI Toolkit does for its
  own Bedrock calls.
- Without a key pair (an instance-role connection): the EC2 instance
  profile's credentials, fetched from IMDSv2 only, assume the role ARN, which
  is then required. Environment variables and ~/.aws profiles are never used.

The role is assumed afresh on every call, on the STS endpoint of the
connection's region.
"""

from aitk_local import log

# AI Toolkit's own Bedrock calls use this session name when assuming the role.
ROLE_SESSION_NAME = "splunk-ai-commander-session-name"


class IncompleteKeys(ValueError):
    """The connection has an access key ID without a secret key, or the reverse."""


class MissingRoleArn(ValueError):
    """An instance-role connection has no role ARN."""


class NoInstanceCredentials(Exception):
    """IMDSv2 gave no credentials: no instance profile, IMDS unreachable, or not on EC2."""


class AssumeRoleFailed(Exception):
    """AssumeRole was refused or failed; `code` is the AWS error code or the exception type."""

    def __init__(self, role_arn, code, cause):
        super().__init__(f"assuming role {role_arn} failed ({code})")
        self.role_arn, self.code, self.cause = role_arn, code, cause


def for_connection(region, key_id, secret, role_arn, config):
    """Credentials to call Bedrock with for one connection; `config` is the botocore Config for STS."""
    if not (key_id or secret):
        if not role_arn:
            raise MissingRoleArn("no role ARN")
        found = _assumed(region, _instance_credentials(), role_arn, config)
        log.get_logger().info("assumed role %s with the instance credentials", role_arn)
        return found
    if not (key_id and secret):
        raise IncompleteKeys("half an access key pair")
    keys = {"aws_access_key_id": key_id, "aws_secret_access_key": secret, "aws_session_token": None}
    if not role_arn:
        return keys
    return _assumed(region, keys, role_arn, config)


def _instance_credentials():
    """The instance profile's credentials from IMDSv2, ignoring every other source."""
    import botocore.session

    session = botocore.session.get_session()
    session.set_config_variable("ec2_metadata_v1_disabled", True)
    provider = session.get_component("credential_provider").get_provider("iam-role")
    try:
        found = provider.load()
    except Exception as e:
        log.get_logger().info("no instance credentials from IMDS (%s)", reason(e))
        found = None
    if found is None:
        raise NoInstanceCredentials("no instance credentials")
    frozen = found.get_frozen_credentials()
    return {"aws_access_key_id": frozen.access_key, "aws_secret_access_key": frozen.secret_key,
            "aws_session_token": frozen.token}


def _assumed(region, source, role_arn, config):
    import boto3

    session = boto3.Session(region_name=region, **source)
    try:
        found = session.client("sts", config=config).assume_role(RoleArn=role_arn,
                                                                  RoleSessionName=ROLE_SESSION_NAME)
    except Exception as e:
        raise AssumeRoleFailed(role_arn, reason(e), e) from None
    return {
        "aws_access_key_id": found["Credentials"]["AccessKeyId"],
        "aws_secret_access_key": found["Credentials"]["SecretAccessKey"],
        "aws_session_token": found["Credentials"]["SessionToken"],
    }


def reason(e):
    """The AWS error code, or the exception type: never the message, which can echo the request."""
    aws = getattr(e, "response", None)
    if isinstance(aws, dict) and aws.get("Error", {}).get("Code"):
        return aws["Error"]["Code"]
    return type(e).__name__
